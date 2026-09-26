"""Tests for the in-process background worker and the cron commands.

core/worker.py had no coverage at all, which is unusual for a module whose
failure modes are quiet: a task that never runs, a task that runs on a loop that
is about to close, or a task that leaks a Postgres connection. All three produce
no traceback.

The commands are here for the same reason. They are sync by necessity — Django
6's BaseCommand has neither iscoroutinefunction nor async_to_sync — so they are
the one place a sync ORM call is correct, and nothing was checking that they
still work.
"""

import asyncio
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Notification
from core import worker
from issues.models import AuditEntry, Issue, IssueType, Priority, Status
from projects.models import Project, ProjectMembership, Sprint

User = get_user_model()


def _seed_lookups():
    Status.objects.get_or_create(name="To Do", defaults={"category": "todo", "order": 10})
    Status.objects.get_or_create(name="Done", defaults={"category": "done", "order": 50})
    Priority.objects.get_or_create(name="High", defaults={"weight": 40})
    IssueType.objects.get_or_create(name="Task", defaults={"category": "task"})


class WorkerTests(TestCase):
    """core.worker.enqueue, driven through a real event loop."""

    def setUp(self):
        _seed_lookups()
        # The worker keeps module-level state, so a test that leaves a task
        # pending would leak into the next one. This has to happen in setUp
        # rather than cleanup: the module is imported once, and a previous
        # test's closed event loop would leave _main_loop pointing at a loop
        # that can no longer accept a hand-off.
        self._reset_worker()
        self.addCleanup(self._reset_worker)

    @staticmethod
    def _reset_worker():
        worker._queue = None
        worker._worker_task = None
        worker._main_loop = None

    def test_enqueue_runs_the_coroutine(self):
        ran = []

        async def task(value):
            ran.append(value)

        async def scenario():
            worker.enqueue(task, "done")
            await worker._queue.join()

        asyncio.run(scenario())
        self.assertEqual(ran, ["done"])

    def test_a_failing_task_does_not_kill_the_worker(self):
        """One bad task must not take the loop down with it.

        The loop catches Exception around every task, so a webhook that 500s
        cannot stop the next email from being sent. This is the behaviour that
        makes the queue safe to fire and forget, and nothing asserted it.
        """
        ran = []

        async def boom():
            raise RuntimeError("delivery failed")

        async def fine():
            ran.append("ok")

        async def scenario():
            worker.enqueue(boom)
            worker.enqueue(fine)
            await worker._queue.join()

        with self.assertLogs("jirrabit.worker", level="ERROR"):
            asyncio.run(scenario())
        self.assertEqual(ran, ["ok"])

    def test_enqueue_from_a_thread_with_no_loop_hands_off(self):
        """A signal firing on a worker thread must not open its own loop.

        This is the path that used to drain Postgres' connection slots, and the
        reason the module has a thread-safe hand-off at all.
        """
        import threading

        got = []
        started = threading.Event()

        async def task():
            got.append(threading.current_thread().name)

        async def scenario():
            # Warm the worker on this loop, so there is a main loop to hand to.
            worker.enqueue(lambda: asyncio.sleep(0))
            await worker._queue.join()
            started.set()
            # A plain thread with no running loop.
            t = threading.Thread(target=lambda: worker.enqueue(task))
            t.start()
            t.join()
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertEqual(len(got), 1)

    def test_two_enqueues_in_a_row_both_run(self):
        """Regression: the second enqueue used to discard the first.

        _ensure_started decided whether to rebuild the queue by reading
        queue._loop, which is a private attribute that stays None until the
        queue's first get(). Two enqueues in the same synchronous block — the
        most natural way to use a fire-and-forget queue — therefore saw
        None is not loop, rebuilt the queue, and dropped whatever was already
        in it. Silent, and it needed the loop to be starved between the two
        calls, which is exactly what happens in the first moments of a request.
        """
        ran = []

        async def one():
            ran.append("one")

        async def two():
            ran.append("two")

        async def scenario():
            worker.enqueue(one)
            worker.enqueue(two)
            await worker._queue.join()

        asyncio.run(scenario())
        self.assertEqual(ran, ["one", "two"])

    def test_enqueue_with_no_loop_at_all_still_runs_the_task(self):
        """The management-command path: no loop anywhere, so spawn one."""
        got = []

        async def task():
            got.append("ran")

        worker.enqueue(task)
        # The bootstrap path is a daemon thread, so wait for it rather than
        # assuming it finished.
        import time

        for _ in range(100):
            if got:
                break
            time.sleep(0.02)
        self.assertEqual(got, ["ran"])


class PurgeOldDataTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.issue = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="old",
            status=Status.objects.get(name="Done"),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )

    def _age(self, model, pk, days):
        from datetime import timedelta

        from django.utils import timezone

        model.objects.filter(pk=pk).update(created_at=timezone.now() - timedelta(days=days))

    def test_dry_run_counts_but_deletes_nothing(self):
        self._age(AuditEntry, AuditEntry.objects.first().pk, 400)
        out = StringIO()
        call_command("purge_old_data", "--days", "30", "--dry-run", stdout=out)
        self.assertIn("dry", out.getvalue().lower())
        self.assertGreater(AuditEntry.objects.count(), 0)

    def test_deletes_old_audit_entries_and_notifications(self):
        # Every row, not just the first: creating the issue writes more than one
        # audit entry, and ageing one of them proves nothing about the command.
        for row in AuditEntry.objects.all():
            self._age(AuditEntry, row.pk, 400)
        # read=True: purge_old_data only takes notifications the user has seen.
        self._age(
            Notification,
            Notification.objects.create(
                recipient=self.user,
                kind="status",
                text="old",
                read=True,
            ).pk,
            400,
        )
        self.assertGreater(AuditEntry.objects.count(), 0)
        call_command("purge_old_data", "--days", "30", stdout=StringIO())
        self.assertEqual(AuditEntry.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_keeps_unread_notifications(self):
        """Purge only takes read notifications.

        An unread notification is a task the user has not seen yet; deleting it
        because it is old is data loss, not housekeeping.
        """
        unread = Notification.objects.create(
            recipient=self.user,
            kind="status",
            text="unread but old",
            read=False,
        )
        self._age(Notification, unread.pk, 400)
        call_command("purge_old_data", "--days", "30", stdout=StringIO())
        self.assertTrue(Notification.objects.filter(pk=unread.pk).exists())

    def test_keeps_recent_rows(self):
        call_command("purge_old_data", "--days", "3650", stdout=StringIO())
        self.assertGreater(AuditEntry.objects.count(), 0)


class AutoArchiveTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.done = Status.objects.get(name="Done")
        self.issue = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="shipped",
            status=self.done,
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
            resolved_at=self._ago(90),
        )
        self.fresh = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="just done",
            status=self.done,
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
            resolved_at=self._ago(2),
        )
        self.open_issue = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="still open",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )

    @staticmethod
    def _ago(days):
        from datetime import timedelta

        from django.utils import timezone

        return timezone.now() - timedelta(days=days)

    def test_archives_only_old_resolved_issues(self):
        call_command("auto_archive", "--days", "30", stdout=StringIO())
        self.issue.refresh_from_db()
        self.fresh.refresh_from_db()
        self.open_issue.refresh_from_db()
        self.assertTrue(self.issue.archived)
        self.assertFalse(self.fresh.archived, "a 2-day-old issue must not be archived")
        self.assertFalse(self.open_issue.archived, "an open issue must not be archived")

    def test_dry_run_changes_nothing(self):
        call_command("auto_archive", "--days", "30", "--dry-run", stdout=StringIO())
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.archived)


class SeedJirrabitTests(TestCase):
    def test_is_idempotent(self):
        """seed_jirrabit runs on every fresh deploy, so running it twice must
        not duplicate the statuses an issue creation depends on."""
        for _ in range(2):
            call_command("seed_jirrabit", stdout=StringIO())
        names = list(Status.objects.values_list("name", flat=True))
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("To Do", names)
        self.assertTrue(Priority.objects.exists())
        self.assertTrue(IssueType.objects.exists())


class SprintPlanningTests(TestCase):
    """Sprint.aclose, the other half of aclose's behaviour, and the only place
    a status change is bundled with a move."""

    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.sprint = Sprint.objects.create(project=self.project, name="S1", status="active")

    def _issue(self, status_name="To Do"):
        return Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="x",
            status=Status.objects.get(name=status_name),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
            sprint=self.sprint,
        )

    def test_aclose_without_carry_to_returns_issues_to_the_backlog(self):
        from asgiref.sync import async_to_sync

        unfinished = self._issue()
        finished = self._issue("Done")
        async_to_sync(self.sprint.aclose)()
        unfinished.refresh_from_db()
        finished.refresh_from_db()
        self.assertIsNone(unfinished.sprint_id, "unfinished work goes back to the backlog")
        self.assertEqual(finished.sprint_id, self.sprint.pk, "a done issue stays where it is")

    def test_aclose_reaches_the_assignee_of_a_carried_issue(self):
        """The point of asave over aupdate: the receiver has to actually fire.

        core.notifications._on_issue writes an in-app notification for the
        assignee, so that is the observable effect. Before the fix this was an
        aupdate(sprint_id=...) and nobody heard about the move at all.
        """
        from asgiref.sync import async_to_sync

        assignee = User.objects.create_user(username="bob", password="pw", email="b@x.com")
        ProjectMembership.objects.create(project=self.project, user=assignee, role="member")
        issue = self._issue()
        issue.assignee = assignee
        issue.save()
        before = Notification.objects.filter(recipient=assignee).count()
        target = Sprint.objects.create(project=self.project, name="S2", status="planned")
        async_to_sync(self.sprint.aclose)(carry_to=target)
        issue.refresh_from_db()
        self.assertEqual(issue.sprint_id, target.pk)
        self.assertGreater(
            Notification.objects.filter(recipient=assignee).count(),
            before,
            "moving an issue out of a sprint must reach the person it is assigned to",
        )
