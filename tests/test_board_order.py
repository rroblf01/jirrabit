"""Manual board ordering: Issue.rank as the index within a board column.

rank existed for years and nothing ever wrote it, so the kanban silently fell
back to ``-updated_at`` and a drag could only change a card's column, never its
position. These tests pin the invariant that replaced that: rank is a dense
0-based index within a ``(project, status)`` column, and the drag endpoint keeps
it that way.
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from issues.models import HistoryEntry, Issue, IssueType, Priority, Status
from projects.models import Project, ProjectMembership

User = get_user_model()


def _seed_lookups():
    Status.objects.get_or_create(name="To Do", defaults={"category": "todo", "order": 10})
    Status.objects.get_or_create(name="In Progress", defaults={"category": "in_progress", "order": 20})
    Status.objects.get_or_create(name="Done", defaults={"category": "done", "order": 50})
    Priority.objects.get_or_create(name="High", defaults={"weight": 40})
    IssueType.objects.get_or_create(name="Task", defaults={"category": "task"})


class BoardPartialTests(TestCase):
    """What the board URL returns, per request flavour.

    The live-refresh banner re-fetches /board/<key>/ and swaps the response into
    .kanban with outerHTML. BoardView used to render board.html for that request
    too, and board.html extends base.html — so the whole page, topbar and
    sidebar included, was injected inside the kanban container.
    """

    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.issue = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="On the board",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_plain_navigation_gets_the_page(self):
        body = self.c.get("/board/WEB/").content.decode()
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn('class="kanban"', body)

    def test_htmx_gets_only_the_board(self):
        r = self.c.get("/board/WEB/", headers={"HX-Request": "true"})
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="kanban"', body)
        self.assertNotIn(
            "<!DOCTYPE html>",
            body,
            "an htmx caller is swapped into .kanban, so a full page would be pasted inside the board",
        )
        # The chrome base.html brings with it.
        for chrome in (
            '<header class="topbar"',
            'class="sidebar"',
            '<script src="https://unpkg.com/htmx.org',
        ):
            self.assertNotIn(chrome, body, f"{chrome} should not be in the board partial")

    def test_boosted_navigation_still_gets_the_page(self):
        """base.html selects main.main on a body-level swap.

        A boosted request therefore needs the whole page, or there is nothing
        for the selector to find.
        """
        r = self.c.get("/board/WEB/", headers={"HX-Request": "true", "HX-Boosted": "true"})
        self.assertIn("<!DOCTYPE html>", r.content.decode())

    def test_the_partial_carries_what_the_drag_needs(self):
        """Reordering reads these off the column, and they must survive the swap."""
        body = self.c.get("/board/WEB/", headers={"HX-Request": "true"}).content.decode()
        self.assertIn(f'data-status-id="{self.issue.status_id}"', body)
        self.assertIn('data-project-key="WEB"', body)
        self.assertIn('class="card-issue', body)

    def test_the_text_filter_still_gets_the_page(self):
        """It swaps into <body>, so it declares that it is boosted.

        The filter input in board.html does hx-get="" hx-target="body". Without
        its HX-Boosted header it would ask for the board partial and inject it
        into <body>, leaving a bare kanban with no page around it. This test
        fails if someone removes the header from the template.
        """
        r = self.c.get(
            "/board/WEB/",
            {"text": "On"},
            headers={"HX-Request": "true", "HX-Boosted": "true"},
        )
        self.assertIn("<!DOCTYPE html>", r.content.decode())

    def test_the_text_filter_header_is_actually_in_the_template(self):
        """A view-only test passes while the page is still broken.

        Checking the rendered response cannot catch this: the view cannot tell
        who is asking, so it answers correctly for both callers and the bug only
        exists in what the template sends. Hence reading the template.
        """
        import pathlib
        import re

        template = pathlib.Path("templates/board/board.html").read_text()
        # The whole <input ...> tag, since the attributes wrap across lines.
        tags = re.findall(r"<input[^>]*hx-get=\"\"[^>]*>", template, re.S)
        self.assertEqual(len(tags), 1, f'expected one hx-get="" input, found {len(tags)}')
        self.assertIn(
            'hx-target="body"',
            tags[0],
            "this test is about the caller that swaps into <body>",
        )
        self.assertIn(
            "HX-Boosted",
            tags[0],
            "the text filter targets <body>, so it must ask for the full page, "
            "not the board partial the live-refresh banner gets",
        )

    def test_the_page_and_the_partial_render_the_same_board(self):
        """They share one file, so they cannot drift — this is the guard for that."""
        page = self.c.get("/board/WEB/").content.decode()
        partial = self.c.get("/board/WEB/", headers={"HX-Request": "true"}).content.decode()
        for key in (self.issue.key, self.project.key):
            self.assertIn(key, page)
            self.assertIn(key, partial)


class BoardRankTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.todo = Status.objects.get(name="To Do")
        self.progress = Status.objects.get(name="In Progress")
        self.c = Client()
        self.c.login(username="alice", password="pw")
        self.url = reverse("board:reorder", args=[self.project.key])

    def _issues(self, n, status=None):
        return [
            Issue.objects.create(
                project=self.project,
                reporter=self.user,
                summary=f"i{n}",
                status=status or self.todo,
                priority=Priority.objects.first(),
                issue_type=IssueType.objects.first(),
            )
            for n in range(n)
        ]

    def _order(self, status=None):
        """The column as the board renders it: rank, then -updated_at."""
        return list(
            Issue.objects.filter(project=self.project, status=status or self.todo)
            .order_by("rank", "-updated_at")
            .values_list("key", flat=True)
        )

    def _reorder(self, status, keys):
        # A list value, not repeated assignment: overwriting the key would send
        # only the last one and quietly test the partial-order path instead.
        return self.c.post(self.url, {"status": status.pk, "keys": list(keys)})

    # --- the invariant ----------------------------------------------------

    def test_new_issues_land_at_the_end_of_their_column(self):
        made = self._issues(3)
        self.assertEqual([i.rank for i in made], [0, 1, 2])
        self.assertEqual(self._order(), [i.key for i in made])

    def test_new_issue_ranks_per_column_not_per_project(self):
        a, b, c = self._issues(3)
        other = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="in progress",
            status=self.progress,
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        # A second card in another column must still start at 0, not continue
        # the first column's numbering.
        self.assertEqual(other.rank, 0)
        self.assertEqual(self._order(self.progress), [other.key])
        self.assertEqual(self._order(self.todo), [a.key, b.key, c.key])

    # --- the drag endpoint ------------------------------------------------

    def test_reorder_persists_the_sent_order(self):
        made = self._issues(4)
        wanted = [made[3].key, made[0].key, made[2].key, made[1].key]
        r = self._reorder(self.todo, wanted)
        self.assertEqual(r.status_code, 204, r.content[:200])
        self.assertEqual(self._order(), wanted)
        ranks = dict(Issue.objects.filter(key__in=wanted).values_list("key", "rank"))
        self.assertEqual(ranks[wanted[0]], 0)
        self.assertEqual(ranks[wanted[3]], 3)

    def test_reorder_is_idempotent(self):
        made = self._issues(3)
        wanted = [made[2].key, made[0].key, made[1].key]
        self._reorder(self.todo, wanted)
        ranks_once = dict(Issue.objects.values_list("key", "rank"))
        self._reorder(self.todo, wanted)
        self.assertEqual(dict(Issue.objects.values_list("key", "rank")), ranks_once)

    def test_partial_order_puts_named_cards_first_and_keeps_the_rest(self):
        """A stale tab sending a partial list must not scramble the column."""
        made = self._issues(4)
        self._reorder(self.todo, [made[3].key, made[0].key])
        order = self._order()
        self.assertEqual(order[:2], [made[3].key, made[0].key])
        self.assertEqual(sorted(order[2:]), sorted([made[1].key, made[2].key]))

    def test_reorder_rejects_keys_from_another_project(self):
        other_user = User.objects.create_user(username="bob", password="pw", email="b@x.com")
        other_project = Project.objects.create(key="OPS", name="Ops", lead=other_user)
        ProjectMembership.objects.create(project=other_project, user=other_user, role="admin")
        mine = self._issues(2)
        theirs = Issue.objects.create(
            project=other_project,
            reporter=other_user,
            summary="theirs",
            status=self.todo,
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        r = self._reorder(self.todo, [theirs.key, mine[0].key])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._order(), [mine[0].key, mine[1].key])

    def test_reorder_needs_a_status_and_keys(self):
        made = self._issues(1)
        self.assertEqual(self.c.post(self.url, {"keys": made[0].key}).status_code, 400)
        self.assertEqual(self.c.post(self.url, {"status": self.todo.pk}).status_code, 400)

    def test_reorder_requires_edit_permission(self):
        made = self._issues(2)
        viewer = User.objects.create_user(username="vic", password="pw", email="v@x.com")
        ProjectMembership.objects.create(project=self.project, user=viewer, role="viewer")
        self.c.logout()
        self.c.login(username="vic", password="pw")
        r = self._reorder(self.todo, [made[1].key, made[0].key])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._order(), [made[0].key, made[1].key])

    # --- moving between columns -------------------------------------------

    def test_reorder_across_columns_runs_the_workflow(self):
        made = self._issues(2)
        r = self._reorder(self.progress, [made[0].key, made[1].key])
        self.assertEqual(r.status_code, 204)
        moved = Issue.objects.get(pk=made[0].pk)
        self.assertEqual(moved.status_id, self.progress.pk)
        self.assertEqual(moved.rank, 0)
        self.assertTrue(
            HistoryEntry.objects.filter(issue=moved, field="status", new_value=str(self.progress)).exists(),
            "a cross-column reorder must still write the status HistoryEntry",
        )

    def test_illegal_transition_across_columns_is_rejected(self):
        self.todo.allowed_next.set([self.progress])
        self.todo.save()
        done = Status.objects.get(name="Done")
        made = self._issues(1)
        r = self._reorder(done, [made[0].key])
        self.assertEqual(r.status_code, 400)
        made[0].refresh_from_db()
        self.assertEqual(made[0].status_id, self.todo.pk)

    def test_old_column_is_renumbered_when_a_card_leaves_it(self):
        made = self._issues(3)
        # Move the middle card out; the two left must close the gap rather than
        # holding ranks 0 and 2.
        self._reorder(self.progress, [made[1].key])
        remaining = dict(Issue.objects.filter(pk__in=[made[0].pk, made[2].pk]).values_list("key", "rank"))
        self.assertEqual(sorted(remaining.values()), [0, 1])

    def test_move_card_view_also_assigns_a_rank(self):
        """A card moved by the status-only endpoint must not keep a foreign rank."""
        made = self._issues(3)
        r = self.c.post(reverse("board:move_card", args=[made[0].key]), {"status": self.progress.pk})
        self.assertEqual(r.status_code, 200)
        moved = Issue.objects.get(pk=made[0].pk)
        self.assertEqual(moved.status_id, self.progress.pk)
        # Alone in the destination column, so index 0 there.
        self.assertEqual(moved.rank, 0)
        # And the column it left closes its gap rather than holding 1 and 2.
        self.assertEqual(self._order(), [made[1].key, made[2].key])

    def test_move_card_view_appends_when_the_column_is_not_empty(self):
        self._issues(2)
        occupant = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="already there",
            status=self.progress,
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        made = self._issues(1, status=self.progress)
        self.assertEqual(occupant.rank, 0)
        self.assertEqual(made[0].rank, 1)
        # Move a card in; it lands at the end rather than on top.
        from_todo = self._issues(1)
        r = self.c.post(
            reverse("board:move_card", args=[from_todo[0].key]),
            {"status": self.progress.pk},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self._order(self.progress),
            [occupant.key, made[0].key, from_todo[0].key],
        )
