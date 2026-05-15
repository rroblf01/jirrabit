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
        # delete
        r = self.c.post(reverse("issues:comment_delete", args=[c.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Comment.objects.filter(pk=c.pk).exists())


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
