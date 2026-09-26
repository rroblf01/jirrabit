"""Regression tests for the two bulk-update paths that were skipping post_save.

Both used ``QuerySet.aupdate()``, which is silent about signals, so every
receiver wired in core/apps.py — notifications, audit, webhooks and the
realtime broadcast — was skipped. A bulk edit looked like it worked and left no
trace, which is the worst failure mode: no error, no history, no update for
anyone else watching the board.

These tests assert the side effects, not the field value, because the field was
always set correctly. That is why the bug survived.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from issues.models import AuditEntry, HistoryEntry, Issue, IssueType, Priority, Status
from projects.models import Project, ProjectMembership, Sprint

User = get_user_model()


def _seed_lookups():
    Status.objects.get_or_create(name="To Do", defaults={"category": "todo", "order": 10})
    Status.objects.get_or_create(name="Done", defaults={"category": "done", "order": 50})
    Priority.objects.get_or_create(name="High", defaults={"weight": 40})
    IssueType.objects.get_or_create(name="Task", defaults={"category": "task"})


def _make_user(username="alice", **extras):
    return User.objects.create_user(
        username=username, password="pw", email=f"{username}@x.com", **extras
    )


def _make_project(lead, key="WEB"):
    p = Project.objects.create(key=key, name=f"{key} project", lead=lead)
    ProjectMembership.objects.create(project=p, user=lead, role="admin")
    return p


def _make_issue(project, reporter, summary="Test"):
    return Issue.objects.create(
        project=project,
        reporter=reporter,
        summary=summary,
        status=Status.objects.first(),
        priority=Priority.objects.first(),
        issue_type=IssueType.objects.first(),
    )


class BoardBulkUpdateTests(TestCase):
    """``board.BulkUpdateView`` — the kanban's multi-select actions."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.issues = [_make_issue(self.project, self.user, f"i{n}") for n in range(3)]
        self.c = Client()
        self.c.login(username="alice", password="pw")
        self.url = reverse("board:bulk_update", args=[self.project.key])

    def _post(self, action, value=""):
        return self.c.post(
            self.url, {"keys": [i.key for i in self.issues], "action": action, "value": value}
        )

    def test_bulk_status_consults_the_workflow(self):
        """A restricted workflow must reject the whole bulk operation.

        Previously this wrote status_id directly, so any status was reachable
        from the board regardless of the transitions the admin configured.
        """
        todo = Status.objects.get(name="To Do")
        done = Status.objects.get(name="Done")
        blocked = Status.objects.create(name="Blocked", category="todo", order=15)
        # An empty allowed_next means an *open* workflow where anything goes,
        # so the restriction has to name a status that is not the target.
        todo.allowed_next.set([blocked])
        todo.save()

        r = self._post("status", str(done.pk))
        self.assertEqual(r.status_code, 400)
        for i in self.issues:
            i.refresh_from_db()
            self.assertEqual(i.status_id, todo.pk, "an illegal transition went through")

    def test_bulk_status_writes_history_and_audit(self):
        done = Status.objects.get(name="Done")
        r = self._post("status", str(done.pk))
        self.assertEqual(r.status_code, 204)
        for i in self.issues:
            i.refresh_from_db()
            self.assertEqual(i.status_id, done.pk)
            # resolved_at is maintained by the chokepoint, not by a raw write.
            self.assertIsNotNone(i.resolved_at)
            self.assertTrue(
                HistoryEntry.objects.filter(
                    issue=i, field="status", new_value=str(done)
                ).exists(),
                f"{i.key} has no status HistoryEntry",
            )
        self.assertEqual(
            AuditEntry.objects.filter(
                target_type="issue", verb="updated"
            ).count(),
            len(self.issues),
            "bulk status change wrote no audit rows",
        )

    def test_bulk_assignee_writes_audit_rows(self):
        other = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=other, role="member")
        r = self._post("assignee", str(other.pk))
        self.assertEqual(r.status_code, 204)
        for i in self.issues:
            i.refresh_from_db()
            self.assertEqual(i.assignee_id, other.pk)
        self.assertEqual(
            AuditEntry.objects.filter(target_type="issue", verb="updated").count(),
            len(self.issues),
        )

    def test_bulk_rejects_an_assignee_outside_the_project(self):
        stranger = _make_user("carol")
        r = self._post("assignee", str(stranger.pk))
        self.assertEqual(r.status_code, 400)
        for i in self.issues:
            i.refresh_from_db()
            self.assertIsNone(i.assignee_id)


class SprintCloseTests(TestCase):
    """``projects.models.Sprint.aclose`` — carrying issues to another sprint."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user)
        self.sprint = Sprint.objects.create(project=self.project, name="S1", status="active")
        self.target = Sprint.objects.create(project=self.project, name="S2", status="planned")
        self.done_status = Status.objects.get(name="Done")
        self.open_issues = [
            _make_issue(self.project, self.user, f"open{n}") for n in range(2)
        ]
        for i in self.open_issues:
            i.sprint = self.sprint
            i.save()
        self.closed = _make_issue(self.project, self.user, "already done")
        self.closed.sprint = self.sprint
        self.closed.status = self.done_status
        self.closed.save()
        # setUp's own saves each wrote an audit row. Clear them so the assertions
        # measure what aclose did rather than what the fixture did.
        AuditEntry.objects.all().delete()

    def test_aclose_carries_issues_and_audits_them(self):
        # Sprint.aclose is async, and TestCase gives no event loop, so the
        # coroutine is driven directly rather than through a view.
        from asgiref.sync import async_to_sync

        async_to_sync(self.sprint.aclose)(carry_to=self.target)

        for i in self.open_issues:
            i.refresh_from_db()
            self.assertEqual(i.sprint_id, self.target.pk)
        self.assertEqual(
            AuditEntry.objects.filter(
                target_type="issue", verb="updated", target_id__in=[i.pk for i in self.open_issues]
            ).count(),
            2,
            "carrying issues wrote no audit rows — aupdate skipped post_save",
        )
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.status, "closed")

    def test_aclose_leaves_done_issues_where_they_are(self):
        from asgiref.sync import async_to_sync

        async_to_sync(self.sprint.aclose)(carry_to=self.target)

        self.closed.refresh_from_db()
        self.assertEqual(self.closed.sprint_id, self.sprint.pk)
