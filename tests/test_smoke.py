"""High-value smoke tests.

Cover: auth, project + issue CRUD via UI, inline edit, comment lifecycle,
workflow validation, permissions, REST API, JQL search. Each test owns
its setup; no shared fixtures.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from issues.models import Issue, IssueType, Priority, Status
from projects.models import Project, ProjectMembership

User = get_user_model()


def _seed_lookups():
    Status.objects.get_or_create(name="To Do", defaults={"category": "todo", "order": 10})
    Status.objects.get_or_create(name="In Progress", defaults={"category": "in_progress", "order": 20})
    Status.objects.get_or_create(name="Done", defaults={"category": "done", "order": 50})
    Priority.objects.get_or_create(name="High", defaults={"weight": 40, "color": "#f97316"})
    Priority.objects.get_or_create(name="Medium", defaults={"weight": 30, "color": "#1e6fff"})
    IssueType.objects.get_or_create(name="Task", defaults={"category": "task", "icon": "✓", "color": "#1e6fff"})


def _make_user(username="alice", **extras):
    u = User.objects.create_user(username=username, password="pw", email=f"{username}@x.com", **extras)
    return u


def _make_project(lead, key="WEB"):
    p = Project.objects.create(key=key, name=f"{key} project", lead=lead)
    ProjectMembership.objects.create(project=p, user=lead, role="admin")
    return p


def _make_issue(project, reporter, summary="Test"):
    return Issue.objects.create(
        project=project, reporter=reporter, summary=summary,
        status=Status.objects.first(),
        priority=Priority.objects.first(),
        issue_type=IssueType.objects.first(),
    )


class AuthFlowTests(TestCase):
    def test_login_redirects_to_home(self):
        _make_user("alice")
        c = Client()
        ok = c.login(username="alice", password="pw")
        self.assertTrue(ok)
        r = c.get(reverse("core:home"))
        self.assertEqual(r.status_code, 200)


class IssueWorkflowTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_change_status_open_workflow(self):
        target = Status.objects.get(name="In Progress")
        r = self.c.post(
            reverse("issues:change_status", args=[self.issue.key]),
            data={"status": target.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, target)

    def test_workflow_blocks_disallowed_transition(self):
        todo = Status.objects.get(name="To Do")
        done = Status.objects.get(name="Done")
        in_prog = Status.objects.get(name="In Progress")
        # Only To Do -> In Progress allowed
        todo.allowed_next.set([in_prog])
        r = self.c.post(
            reverse("issues:change_status", args=[self.issue.key]),
            data={"status": done.pk},
        )
        self.assertEqual(r.status_code, 400)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, todo)


class InlineEditTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_inline_edit_summary(self):
        r = self.c.post(
            reverse("issues:inline_edit", args=[self.issue.key, "summary"]),
            data={"summary": "Updated summary"},
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, "Updated summary")

    def test_inline_edit_priority(self):
        new_prio = Priority.objects.get(name="Medium")
        r = self.c.post(
            reverse("issues:inline_edit", args=[self.issue.key, "priority"]),
            data={"value": new_prio.pk},
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.priority, new_prio)


class PermissionTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.lead = _make_user("alice")
        self.outsider = _make_user("bob")
        self.project = _make_project(self.lead)
        self.issue = _make_issue(self.project, self.lead)

    def test_outsider_cannot_edit_issue(self):
        c = Client()
        c.login(username="bob", password="pw")
        r = c.post(
            reverse("issues:inline_edit", args=[self.issue.key, "summary"]),
            data={"summary": "Hijacked"},
        )
        self.assertEqual(r.status_code, 403)
        self.issue.refresh_from_db()
        self.assertNotEqual(self.issue.summary, "Hijacked")

    def test_member_can_edit(self):
        ProjectMembership.objects.create(project=self.project, user=self.outsider, role="member")
        c = Client()
        c.login(username="bob", password="pw")
        r = c.post(
            reverse("issues:inline_edit", args=[self.issue.key, "summary"]),
            data={"summary": "Member edit"},
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, "Member edit")


class CommentTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_add_edit_delete_comment(self):
        from issues.models import Comment
        # add
        r = self.c.post(
            reverse("issues:add_comment", args=[self.issue.key]),
            data={"body": "Primer comment"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(r.status_code, 200)
        c = Comment.objects.get(issue=self.issue)
        # edit
        r = self.c.post(
            reverse("issues:comment_edit", args=[c.pk]),
            data={"body": "Editado"},
        )
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.body, "Editado")
        self.assertTrue(c.edited)
        # delete (soft): row remains but deleted_at is set, undo URL exposed
        r = self.c.post(reverse("issues:comment_delete", args=[c.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.has_header("X-Undo-URL"))
        c.refresh_from_db()
        self.assertIsNotNone(c.deleted_at)


class APITests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user, summary="API test")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_api_list_issues(self):
        r = self.c.get(f"/api/v1/projects/{self.project.key}/issues/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        # The API now returns a ``Page`` envelope.
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["key"], self.issue.key)

    def test_api_patch_issue(self):
        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data='{"summary": "via API"}',
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, "via API")


class WorkflowEditorTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.admin = User.objects.create_superuser(
            username="root", password="pw", email="root@x.com",
        )
        self.regular = _make_user("alice")
        self.c_admin = Client()
        self.c_admin.login(username="root", password="pw")
        self.c_user = Client()
        self.c_user.login(username="alice", password="pw")

    def test_non_superuser_denied(self):
        r = self.c_user.get(reverse("workflow:overview"))
        self.assertEqual(r.status_code, 403)

    def test_overview_renders(self):
        r = self.c_admin.get(reverse("workflow:overview"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Workflow editor")

    def test_status_list_renders(self):
        r = self.c_admin.get(reverse("workflow:status_list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "To Do")

    def test_status_create(self):
        r = self.c_admin.post(
            reverse("workflow:status_create"),
            data={"name": "Review", "category": "in_progress", "order": "25"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Status.objects.filter(name="Review").exists())

    def test_status_delete_in_use_blocked(self):
        s = Status.objects.get(name="To Do")
        Issue.objects.create(
            project=_make_project(_make_user("bob", is_superuser=False)),
            reporter=self.admin, summary="x", status=s,
            priority=Priority.objects.first(), issue_type=IssueType.objects.first(),
        )
        r = self.c_admin.post(reverse("workflow:status_delete", args=[s.pk]))
        self.assertEqual(r.status_code, 302)
        # Still exists because PROTECT FK fired.
        self.assertTrue(Status.objects.filter(pk=s.pk).exists())

    def test_status_transitions_save(self):
        a = Status.objects.get(name="To Do")
        b = Status.objects.get(name="In Progress")
        r = self.c_admin.post(
            reverse("workflow:status_transitions", args=[a.pk]),
            data={"allowed_next": [str(b.pk)]},
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn(b, list(a.allowed_next.all()))

    def test_priority_create(self):
        r = self.c_admin.post(
            reverse("workflow:priority_create"),
            data={"name": "Critical", "weight": "50", "color": "#dc2626"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Priority.objects.filter(name="Critical").exists())

    def test_status_reorder(self):
        s1 = Status.objects.get(name="To Do")
        s2 = Status.objects.get(name="In Progress")
        s3 = Status.objects.get(name="Done")
        r = self.c_admin.post(
            reverse("workflow:status_reorder"),
            data={"order": [str(s3.pk), str(s1.pk), str(s2.pk)]},
        )
        self.assertEqual(r.status_code, 302)
        s1.refresh_from_db(); s2.refresh_from_db(); s3.refresh_from_db()
        self.assertEqual(s3.order, 0)
        self.assertEqual(s1.order, 1)
        self.assertEqual(s2.order, 2)


class ProductivityTests(TestCase):
    """Smoke tests for the Tier-1/2/3 productivity features."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_pin_toggle(self):
        from issues.models import Pin
        r = self.c.post(reverse("issues:pin_toggle", args=["issue", self.issue.pk]))
        self.assertEqual(r.status_code, 204)
        self.assertTrue(Pin.objects.filter(user=self.user, issue=self.issue).exists())
        # Toggling again removes it.
        r = self.c.post(reverse("issues:pin_toggle", args=["issue", self.issue.pk]))
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Pin.objects.filter(user=self.user, issue=self.issue).exists())

    def test_advance_status_with_open_workflow(self):
        # No allowed_next on default seed -> falls through to order-based pick.
        r = self.c.post(reverse("issues:advance_status", args=[self.issue.key]))
        self.assertEqual(r.status_code, 302)
        self.issue.refresh_from_db()
        self.assertNotEqual(self.issue.status.name, "To Do")

    def test_clone_creates_new_issue(self):
        before = Issue.objects.filter(project=self.project).count()
        r = self.c.post(reverse("issues:clone", args=[self.issue.key]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Issue.objects.filter(project=self.project).count(), before + 1)
        clone = Issue.objects.exclude(pk=self.issue.pk).get(project=self.project)
        self.assertTrue(clone.summary.startswith("[clon]"))

    def test_snooze_and_unsnooze(self):
        from issues.models import NotificationSnooze
        r = self.c.post(reverse("issues:snooze", args=[self.issue.key]), data={"hours": "4"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(NotificationSnooze.objects.filter(user=self.user, issue=self.issue).exists())
        r = self.c.post(reverse("issues:unsnooze", args=[self.issue.key]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(NotificationSnooze.objects.filter(user=self.user, issue=self.issue).exists())

    def test_timer_start_stop_logs_work(self):
        from issues.models import Timer, WorkLog
        r = self.c.post(reverse("issues:timer_start", args=[self.issue.key]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Timer.objects.filter(user=self.user, issue=self.issue).exists())
        r = self.c.post(reverse("issues:timer_stop", args=[self.issue.key]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Timer.objects.exists())
        self.assertTrue(WorkLog.objects.filter(issue=self.issue).exists())

    def test_auto_watch_on_comment(self):
        # Make a second user; comment as them; they should now be a watcher.
        bob = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=bob, role="member")
        c2 = Client(); c2.login(username="bob", password="pw")
        r = c2.post(
            reverse("issues:add_comment", args=[self.issue.key]),
            data={"body": "hola"}, HTTP_HX_REQUEST="true",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(bob, self.issue.watchers.all())

    def test_quick_switch_returns_json(self):
        r = self.c.get("/search/quickswitch/?q=Test")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertIn("items", payload)
        keys = [i["label"] for i in payload["items"]]
        self.assertTrue(any(self.issue.key in k for k in keys))

    def test_csv_export(self):
        r = self.c.get(reverse("issues:export_csv", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn(self.issue.key, r.content.decode("utf-8"))

    def test_csv_import_preview_and_apply(self):
        before = Issue.objects.filter(project=self.project).count()
        csv = "summary,type,priority\nNueva 1,Task,High\nNueva 2,Task,Medium\n"
        r = self.c.post(
            reverse("issues:import_csv", args=[self.project.key]),
            data={"csv": csv, "action": "preview"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Nueva 1")
        r = self.c.post(
            reverse("issues:import_csv", args=[self.project.key]),
            data={"csv": csv, "action": "import"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Issue.objects.filter(project=self.project).count(), before + 2)

    def test_markdown_preview_endpoint(self):
        r = self.c.post("/md/preview/", data={"body": "**bold**"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<strong>", r.content)

    def test_reports_view_renders(self):
        r = self.c.get(reverse("projects:reports", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)

    def test_roadmap_view_renders(self):
        r = self.c.get(reverse("projects:roadmap", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)

    def test_dependencies_view_renders(self):
        r = self.c.get(reverse("projects:dependencies", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)


class AdvancedFeatureTests(TestCase):
    """Smoke tests for the 20-feature rollout: subtasks, reactions, dates,
    teams, branches, dashboard, wiki, etc."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_subtask_create_and_toggle(self):
        from issues.models import Issue, IssueType
        IssueType.objects.get_or_create(name="Subtask", defaults={"category": "subtask"})
        r = self.c.post(
            reverse("issues:subtask_create", args=[self.issue.key]),
            data={"summary": "Sub one"},
        )
        self.assertEqual(r.status_code, 200)
        sub = Issue.objects.get(parent=self.issue)
        self.assertEqual(sub.summary, "Sub one")
        r = self.c.post(reverse("issues:subtask_toggle", args=[sub.key]))
        self.assertEqual(r.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status.category, "done")

    def test_reaction_toggle(self):
        from issues.models import Comment, Reaction
        c = Comment.objects.create(issue=self.issue, author=self.user, body="hi")
        r = self.c.post(reverse("issues:react", args=[c.pk]), data={"emoji": "+1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Reaction.objects.filter(comment=c, user=self.user, emoji="+1").exists())
        r = self.c.post(reverse("issues:react", args=[c.pk]), data={"emoji": "+1"})
        self.assertFalse(Reaction.objects.filter(comment=c, user=self.user, emoji="+1").exists())

    def test_smart_due_date_parser(self):
        from datetime import date, timedelta
        from core.dates import parse_due_date
        today = date(2026, 5, 18)  # Monday
        self.assertEqual(parse_due_date("tomorrow", today), today + timedelta(days=1))
        self.assertEqual(parse_due_date("mañana", today), today + timedelta(days=1))
        self.assertEqual(parse_due_date("3d", today), today + timedelta(days=3))
        self.assertEqual(parse_due_date("in 5 days", today), today + timedelta(days=5))
        self.assertEqual(parse_due_date("2026-06-01", today), date(2026, 6, 1))
        # Friday after this Monday.
        self.assertEqual(parse_due_date("friday", today), today + timedelta(days=4))
        self.assertEqual(parse_due_date("next monday", today), today + timedelta(days=7))
        self.assertIsNone(parse_due_date("garbage"))

    def test_tshirt_sizing(self):
        from issues.inline import TSHIRT_TO_SP
        r = self.c.post(
            reverse("issues:inline_edit", args=[self.issue.key, "story_points"]),
            data={"value": "L"},
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.story_points, TSHIRT_TO_SP["L"])

    def test_team_mention_expansion(self):
        from accounts.models import Notification, Team
        from issues.models import Comment
        bob = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=bob, role="member")
        team = Team.objects.create(slug="qa", name="QA")
        team.members.add(bob)
        # Posting a comment that mentions the team should create a Notification for bob.
        self.c.post(
            reverse("issues:add_comment", args=[self.issue.key]),
            data={"body": "ping @team:qa please"},
            HTTP_HX_REQUEST="true",
        )
        self.assertTrue(Notification.objects.filter(recipient=bob, kind="mention").exists())

    def test_branch_link_create_delete(self):
        from issues.models import BranchLink
        r = self.c.post(
            reverse("issues:branch_create", args=[self.issue.key]),
            data={"branch": "feature/login", "repo_url": "https://example.com/repo"},
        )
        self.assertEqual(r.status_code, 200)
        link = BranchLink.objects.get(issue=self.issue, branch="feature/login")
        r = self.c.post(reverse("issues:branch_delete", args=[self.issue.key, link.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(BranchLink.objects.filter(pk=link.pk).exists())

    def test_auto_link_issue_keys_in_markdown(self):
        from core.markdown import render_markdown
        html = render_markdown(f"see {self.issue.key} for details")
        self.assertIn(f'href="/issues/{self.issue.key}/"', html)
        # ``team:foo`` should not capture as a user mention.
        html = render_markdown("hello @team:qa")
        self.assertIn("team:qa", html)
        self.assertIn('class="mention team"', html)

    def test_csv_export_with_columns(self):
        r = self.c.get(
            reverse("issues:export_csv", args=[self.project.key]) + "?cols=key,summary",
        )
        self.assertEqual(r.status_code, 200)
        body = r.content.decode("utf-8")
        self.assertIn(self.issue.key, body)
        # Header should only have selected columns.
        header = body.splitlines()[0]
        self.assertEqual(header, "key,summary")

    def test_comment_edit_history(self):
        from issues.models import Comment, CommentEdit
        c = Comment.objects.create(issue=self.issue, author=self.user, body="v1")
        r = self.c.post(reverse("issues:comment_edit", args=[c.pk]), data={"body": "v2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(CommentEdit.objects.filter(comment=c).count(), 1)

    def test_board_column_quick_create(self):
        from issues.models import Issue, Status
        before = Issue.objects.filter(project=self.project).count()
        status = Status.objects.first()
        r = self.c.post(
            reverse("board:column_create", args=[self.project.key]),
            data={"summary": "Quick one", "status_id": status.pk},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Issue.objects.filter(project=self.project).count(), before + 1)

    def test_saved_board_view(self):
        from board.models import SavedBoardView
        r = self.c.post(
            reverse("board:view_save", args=[self.project.key]),
            data={"name": "Mías P1", "assignee": "me", "priority": "1"},
        )
        self.assertEqual(r.status_code, 204)
        v = SavedBoardView.objects.get(user=self.user, project=self.project, name="Mías P1")
        self.assertEqual(v.filters["assignee"], "me")

    def test_project_wiki_create_and_read(self):
        from projects.models import ProjectWiki
        r = self.c.post(
            reverse("projects:wiki", args=[self.project.key]),
            data={"body": "# Welcome\n\nproject readme"},
        )
        self.assertEqual(r.status_code, 302)
        w = ProjectWiki.objects.get(project=self.project)
        self.assertIn("Welcome", w.body)
        r = self.c.get(reverse("projects:wiki", args=[self.project.key]))
        self.assertContains(r, "Welcome")

    def test_auto_archive_command(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        from issues.models import Issue, Status
        done = Status.objects.get(name="Done")
        old = _make_issue(self.project, self.user, summary="old done")
        Issue.objects.filter(pk=old.pk).update(
            status=done, resolved_at=timezone.now() - timedelta(days=60),
        )
        call_command("auto_archive", "--days", "30")
        old.refresh_from_db()
        self.assertTrue(old.archived)

    def test_dashboard_config_persists(self):
        from accounts.models import DashboardWidget
        r = self.c.post(
            reverse("core:dashboard_config"),
            data={
                "order": ["assigned", "watching", "pinned"],
                "enabled_assigned": "1", "enabled_watching": "1",
            },
        )
        self.assertEqual(r.status_code, 302)
        w = DashboardWidget.objects.get(user=self.user, kind="assigned")
        self.assertEqual(w.order, 0)
        self.assertTrue(w.enabled)
        # pinned has no enabled checkbox → stored as False.
        p = DashboardWidget.objects.get(user=self.user, kind="pinned")
        self.assertFalse(p.enabled)

    def test_heatmap_renders(self):
        r = self.c.get(reverse("projects:heatmap", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)

    def test_sla_view_renders(self):
        r = self.c.get(reverse("projects:sla", args=[self.project.key]))
        self.assertEqual(r.status_code, 200)
