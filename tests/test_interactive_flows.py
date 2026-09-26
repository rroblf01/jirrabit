"""Flows that were only covered by an ad-hoc script against a live server.

These exercised the real HTTP surface — session cookie, CSRF, HX-Request — which
is how the browser drives the app, and none of them was in the suite. That left
inline editing, the advance button, the attachment-free comment lifecycle, work
logging, the watcher toggle and JQL search reachable only by hand.

They live here as ordinary TestCase tests against the sync test client, which is
what a request handler sees anyway; the difference from the script is only that
the assertions now run in CI.
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from issues.models import (
    Comment,
    HistoryEntry,
    Issue,
    IssueType,
    Priority,
    Status,
    WorkLog,
)
from projects.models import Project, ProjectMembership

User = get_user_model()


def _seed_lookups():
    Status.objects.get_or_create(name="To Do", defaults={"category": "todo", "order": 10})
    Status.objects.get_or_create(
        name="In Progress", defaults={"category": "in_progress", "order": 20}
    )
    Status.objects.get_or_create(name="Done", defaults={"category": "done", "order": 50})
    Priority.objects.get_or_create(name="High", defaults={"weight": 40})
    IssueType.objects.get_or_create(name="Task", defaults={"category": "task"})


class InteractiveFlowTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(
            username="alice", password="pw", email="a@x.com"
        )
        self.other = User.objects.create_user(
            username="bob", password="pw", email="b@x.com"
        )
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(
            project=self.project, user=self.user, role="admin"
        )
        ProjectMembership.objects.create(
            project=self.project, user=self.other, role="member"
        )
        self.issue = Issue.objects.create(
            project=self.project, reporter=self.user, summary="Original",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(), issue_type=IssueType.objects.first(),
        )
        self.c = Client()
        self.c.login(username="alice", password="pw")
        # The test client takes WSGI environ keys, not header names.
        self.htmx = {"HTTP_HX_REQUEST": "true"}

    # --- inline editing ---------------------------------------------------

    def test_inline_edit_uses_the_field_specific_parameter(self):
        """The summary handler reads POST['summary'], not POST['value'].

        Sending 'value' is what an agent does on its first attempt. It must not
        silently clear the field, and it must not be the only thing standing
        between a typo and data loss.
        """
        r = self.c.post(
            f"/issues/{self.issue.key}/inline/summary/",
            {"value": "should be ignored"},
            **self.htmx,
        )
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, "Original", "an unknown param must not edit")

        r = self.c.post(
            f"/issues/{self.issue.key}/inline/summary/",
            {"summary": "Edited inline"},
            **self.htmx,
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, "Edited inline")
        self.assertTrue(
            HistoryEntry.objects.filter(issue=self.issue, field="summary").exists()
        )

    def test_inline_edit_of_a_relation_checks_project_membership(self):
        r = self.c.post(
            f"/issues/{self.issue.key}/inline/assignee/",
            {"value": str(self.other.pk)},
            **self.htmx,
        )
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.assignee_id, self.other.pk)

    def test_inline_edit_rejects_an_assignee_outside_the_project(self):
        stranger = User.objects.create_user(
            username="eve", password="pw", email="e@x.com"
        )
        r = self.c.post(
            f"/issues/{self.issue.key}/inline/assignee/",
            {"value": str(stranger.pk)},
            **self.htmx,
        )
        self.assertEqual(r.status_code, 403)
        self.issue.refresh_from_db()
        self.assertIsNone(self.issue.assignee_id)

    def test_inline_edit_of_an_unknown_field_is_404(self):
        r = self.c.post(
            f"/issues/{self.issue.key}/inline/nonsense/", {"x": "1"}, **self.htmx
        )
        self.assertEqual(r.status_code, 404)

    # --- the advance button ----------------------------------------------

    def test_advance_moves_to_the_next_status(self):
        r = self.c.post(f"/issues/{self.issue.key}/advance/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status.name, "In Progress")
        self.assertTrue(
            HistoryEntry.objects.filter(
                issue=self.issue, field="status", new_value="In Progress"
            ).exists()
        )

    def test_advance_honours_a_restricted_workflow(self):
        todo = Status.objects.get(name="To Do")
        done = Status.objects.get(name="Done")
        todo.allowed_next.set([done])
        todo.save()
        r = self.c.post(f"/issues/{self.issue.key}/advance/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status.name, "Done")

    def test_advance_on_the_last_status_reports_no_next(self):
        self.issue.status = Status.objects.get(name="Done")
        self.issue.save()
        r = self.c.post(f"/issues/{self.issue.key}/advance/", **self.htmx)
        self.assertEqual(r.status_code, 400)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status.name, "Done")

    # --- the watcher toggle ----------------------------------------------

    def test_watch_toggle_is_idempotent_per_direction(self):
        r = self.c.post(f"/issues/{self.issue.key}/watch/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(self.issue.watchers.values_list("username", flat=True)), ["alice"])
        r = self.c.post(f"/issues/{self.issue.key}/watch/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.issue.watchers.count(), 0)

    def test_watch_requires_a_login(self):
        self.c.logout()
        r = self.c.post(f"/issues/{self.issue.key}/watch/", **self.htmx)
        self.assertIn(r.status_code, (302, 403))

    # --- work logging ------------------------------------------------------

    def test_log_work_records_the_row_and_the_total(self):
        r = self.c.post(f"/issues/{self.issue.key}/log-work/", {"minutes": "90"}, **self.htmx)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertTrue(WorkLog.objects.filter(issue=self.issue, minutes=90).exists())
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 90)

    def test_log_work_accumulates(self):
        for minutes in ("30", "45"):
            self.c.post(f"/issues/{self.issue.key}/log-work/", {"minutes": minutes}, **self.htmx)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 75)

    def test_log_work_rejects_zero_and_garbage(self):
        for bad in ("0", "-5", "abc", ""):
            r = self.c.post(f"/issues/{self.issue.key}/log-work/", {"minutes": bad}, **self.htmx)
            self.assertEqual(r.status_code, 400, f"{bad!r} should be rejected")
        self.assertEqual(WorkLog.objects.filter(issue=self.issue).count(), 0)

    # --- comment lifecycle -------------------------------------------------

    def test_comment_lifecycle(self):
        r = self.c.post(
            f"/issues/{self.issue.key}/comment/", {"body": "first"}, **self.htmx
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        comment = Comment.objects.get(issue=self.issue)

        r = self.c.post(
            f"/issues/comment/{comment.pk}/edit/", {"body": "second"}, **self.htmx
        )
        self.assertEqual(r.status_code, 200)
        comment.refresh_from_db()
        self.assertEqual(comment.body, "second")

        r = self.c.post(f"/issues/comment/{comment.pk}/delete/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        comment.refresh_from_db()
        self.assertIsNotNone(comment.deleted_at, "delete is a soft delete")

        r = self.c.post(f"/issues/comment/{comment.pk}/restore/", **self.htmx)
        self.assertEqual(r.status_code, 200)
        comment.refresh_from_db()
        self.assertIsNone(comment.deleted_at)

    def test_a_non_author_cannot_edit_someone_elses_comment(self):
        comment = Comment.objects.create(
            issue=self.issue, author=self.user, body="mine"
        )
        self.c.login(username="bob", password="pw")
        r = self.c.post(
            f"/issues/comment/{comment.pk}/edit/", {"body": "hijacked"}, **self.htmx
        )
        self.assertEqual(r.status_code, 403)
        comment.refresh_from_db()
        self.assertEqual(comment.body, "mine")

    def test_internal_comments_are_forced_public_for_non_staff(self):
        self.c.login(username="bob", password="pw")
        self.c.post(
            f"/issues/{self.issue.key}/comment/",
            {"body": "secret", "is_internal": "on"},
            **self.htmx,
        )
        comment = Comment.objects.latest("pk")
        self.assertFalse(
            comment.is_internal,
            "a non-staff user must not be able to mark a comment internal",
        )

    # --- search ------------------------------------------------------------

    def test_jql_search_from_the_web(self):
        Issue.objects.create(
            project=self.project, reporter=self.user, summary="Segundo",
            status=Status.objects.get(name="In Progress"),
            priority=Priority.objects.first(), issue_type=IssueType.objects.first(),
        )
        r = self.c.get("/search/", {"q": "project = WEB"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.issue.key, r.content.decode())

    def test_malformed_jql_renders_the_error_not_a_500(self):
        r = self.c.get("/search/", {"q": "project ===== WEB"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.issue.key, r.content.decode())

    def test_jql_with_no_matches_is_empty_not_an_error(self):
        r = self.c.get("/search/", {"q": 'project = WEB AND summary = "nada"'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.issue.key, r.content.decode())

    def test_search_does_not_leak_another_project(self):
        """Project.objects.filter_visible is the only gate; search must use it."""
        hidden = Project.objects.create(key="OPS", name="Ops", lead=self.other)
        secret = Issue.objects.create(
            project=hidden, reporter=self.other, summary="clasificado",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(), issue_type=IssueType.objects.first(),
        )
        r = self.c.get("/search/", {"q": "project = OPS"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(secret.key, r.content.decode())
