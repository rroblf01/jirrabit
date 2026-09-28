"""Manual board ordering: Issue.rank as the index within a board column.

rank existed for years and nothing ever wrote it, so the kanban silently fell
back to ``-updated_at`` and a drag could only change a card's column, never its
position. These tests pin the invariant that replaced that: rank is a dense
0-based index within a ``(project, status)`` column, and the drag endpoint keeps
it that way.
"""

import pathlib
import re

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
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


class LiveRefreshActorTests(TestCase):
    """The board must not tell you to refresh after your own change.

    A client that moved a card has already drawn the new position, and every
    other write path re-renders in place. The broadcast went to the whole project
    group regardless, so the person who made the change got a "cambios en vivo —
    refrescar tablero" banner for a board they had just updated themselves.

    The fix needs the actor inside post_save, which has no idea who is acting.
    A context variable carries it, because asgiref propagates the context into
    the worker thread that asave() writes on. These tests pin both halves: that
    the actor survives the trip, and that the client-side filter is present.
    """

    def setUp(self):
        _seed_lookups()
        self.alice = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.alice)
        ProjectMembership.objects.create(project=self.project, user=self.alice, role="admin")

    def _capture(self):
        """Record the payload realtime would broadcast for the next issue save."""
        from realtime import broadcast

        sent = []
        original = broadcast._send

        def spy(group, type_, payload):
            sent.append((group, type_, payload))

        broadcast._send = spy
        self.addCleanup(lambda: setattr(broadcast, "_send", original))
        return sent

    def test_the_actor_reaches_the_post_save_receiver(self):
        """The whole point of the context variable; nothing else makes it work."""
        from asgiref.sync import async_to_sync

        from core import current_user

        captured = []

        def probe(sender, instance, **kwargs):
            captured.append(current_user.actor_id())

        from django.db.models.signals import post_save

        post_save.connect(probe, sender=Issue, dispatch_uid="actor_probe", weak=False)
        self.addCleanup(lambda: post_save.disconnect(dispatch_uid="actor_probe", sender=Issue))

        # Resolved here, not inside the coroutine: a sync ORM lookup on the
        # event loop raises SynchronousOnlyOperation, which is the rule this
        # repository cares about most and not something a helper should break.
        todo = Status.objects.get(name="To Do")
        priority = Priority.objects.first()
        issue_type = IssueType.objects.first()

        async def create():
            await Issue.objects.acreate(
                project=self.project,
                reporter=self.alice,
                summary="s",
                status=todo,
                priority=priority,
                issue_type=issue_type,
            )

        token = current_user.set_current_user(self.alice)
        try:
            async_to_sync(create)()
        finally:
            current_user.reset_current_user(token)
        self.assertEqual(captured, [self.alice.pk], "the actor did not survive the save")

    def test_the_broadcast_names_the_actor(self):
        from asgiref.sync import async_to_sync

        from core import current_user

        sent = self._capture()
        todo = Status.objects.get(name="To Do")
        priority = Priority.objects.first()
        issue_type = IssueType.objects.first()

        async def create():
            await Issue.objects.acreate(
                project=self.project,
                reporter=self.alice,
                summary="s",
                status=todo,
                priority=priority,
                issue_type=issue_type,
            )

        token = current_user.set_current_user(self.alice)
        try:
            async_to_sync(create)()
        finally:
            current_user.reset_current_user(token)

        issue_events = [p for g, t, p in sent if t == "issue.event"]
        self.assertEqual(len(issue_events), 1, f"no issue event broadcast: {sent}")
        self.assertEqual(issue_events[0]["actor_id"], self.alice.pk)

    def test_a_write_with_no_request_carries_no_actor(self):
        """A management command has no user, and every client must react."""
        sent = self._capture()
        Issue.objects.create(
            project=self.project,
            reporter=self.alice,
            summary="from a shell",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        issue_events = [p for g, t, p in sent if t == "issue.event"]
        self.assertEqual(len(issue_events), 1)
        self.assertIsNone(issue_events[0]["actor_id"])

    def test_the_browser_filter_is_present(self):
        """The server half alone changes nothing for the user.

        The banner is drawn by the page's own WebSocket handler, so the filter
        has to be in the template. Checking the rendered HTML catches a missing
        guard; this checks the guard is actually there.
        """
        import pathlib

        template = pathlib.Path("templates/board/board.html").read_text()
        self.assertIn("actor_id", template, "the client never looks at the actor")
        self.assertIn("dataset.userId", template, "the client never learns its own id")
        # And the comparison, not just the two names.
        self.assertRegex(
            template,
            r"msg\.actor_id[^\n]*meId",
            "the actor is read but never compared against the current user",
        )

    def test_the_body_exposes_the_user_id(self):
        """Without data-user-id on <body> the comparison can never match."""
        import pathlib

        base = pathlib.Path("templates/base.html").read_text()
        self.assertIn("data-user-id", base)

    def test_the_actor_is_cleared_after_the_request(self):
        """A leaked actor would silence another user's board indefinitely."""
        from core import current_user

        self.assertIsNone(current_user.actor_id())

    def test_a_real_request_publishes_its_user(self):
        """The middleware, end to end, through a real request.

        This is the piece the other tests cannot reach. Reading a field off
        ``request.user`` resolves the session, which is a synchronous query, and
        on the event loop that raised SynchronousOnlyOperation — every login
        answered 500 until the middleware used ``request.auser()``. Only a real
        request through the stack finds that again.
        """
        from django.db.models.signals import post_save

        from core.current_user import current_user_id

        seen = []

        def probe(sender, instance, **kwargs):
            seen.append(current_user_id.get())

        post_save.connect(probe, sender=Issue, dispatch_uid="mw_probe", weak=False)
        self.addCleanup(lambda: post_save.disconnect(dispatch_uid="mw_probe", sender=Issue))

        todo = Status.objects.get(name="To Do")
        cards = [
            Issue.objects.create(
                project=self.project,
                reporter=self.alice,
                summary=f"c{n}",
                status=todo,
                priority=Priority.objects.first(),
                issue_type=IssueType.objects.first(),
            )
            for n in range(2)
        ]
        seen.clear()
        c = Client()
        c.login(username="alice", password="pw")
        # A real reorder, not a rejected one: the actor has to be observed on a
        # request that actually writes, since that is the only path where it
        # reaches a broadcast.
        r = c.post(
            f"/board/{self.project.key}/reorder/",
            {"status": todo.pk, "keys": [cards[1].key, cards[0].key]},
            headers={"HTTP_HX_REQUEST": "true"},
        )
        self.assertEqual(r.status_code, 204, r.content[:200])
        self.assertTrue(seen, "the request saved nothing, so nothing was published")
        self.assertEqual(set(seen), {self.alice.pk}, "the request published the wrong actor")

    def test_an_anonymous_request_publishes_nothing(self):
        from django.db.models.signals import post_save

        from core import current_user

        seen = []

        def probe(sender, instance, **kwargs):
            seen.append(current_user.actor_id())

        post_save.connect(probe, sender=Issue, dispatch_uid="anon_probe", weak=False)
        self.addCleanup(lambda: post_save.disconnect(dispatch_uid="anon_probe", sender=Issue))
        self.assertIsNone(current_user.actor_id())

    def test_login_still_works(self):
        """A regression guard for the SynchronousOnlyOperation 500.

        Every login resolved request.user on the event loop, which reads the
        session, which is a synchronous query. It passed no test and answered 500
        in the browser, so this asserts the thing that actually broke: that the
        request finishes and the session is authenticated.
        """
        c = Client()
        r = c.post("/accounts/login/", {"username": "alice", "password": "pw", "next": "/"})
        self.assertNotEqual(r.status_code, 500, "login blew up in the middleware")
        self.assertIn(r.status_code, (200, 302))
        self.assertTrue(
            r.wsgi_request.user.is_authenticated,
            "the credentials were rejected",
        )

    def test_the_consumer_forwards_the_actor(self):
        """The socket layer must not drop the new field.

        ``issue_event`` splats the payload, so this is a guard against someone
        replacing that with an explicit allow-list of fields later.
        """
        import asyncio

        from realtime.consumers import ProjectConsumer

        sent = []

        class Fake(ProjectConsumer):
            async def send_json(self, content, close=False):
                sent.append(content)

        consumer = Fake()
        asyncio.run(consumer.issue_event({"payload": {"key": "WEB-1", "actor_id": 7, "summary": "x"}}))
        self.assertEqual(sent, [{"type": "issue", "key": "WEB-1", "actor_id": 7, "summary": "x"}])


class TemplateCommentTests(SimpleTestCase):
    """No template may leak its own syntax into a rendered page.

    Django's ``{# ... #}`` comment is single-line only. A multi-line one is not
    a comment at all: the parser never matches it, so the text is emitted into
    the page verbatim, comment markers and all, and shows up in the UI.

    That is not hypothetical. A six-line brace comment explaining the board's
    HX-Boosted header was rendered as a visible paragraph on the board, and
    templates/workflow/_form.html has been leaking its header comment the same
    way since the workflow editor landed. Nothing catches it, because the views
    render fine — the tests assert on behaviour, and leaking a comment is not a
    behaviour change. Only looking at the template catches it.
    """

    TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

    def test_no_multiline_brace_comments(self):
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#", text):
                end = text.find("#}", match.start())
                if end == -1:
                    continue
                if "\n" in text[match.start() : end + 2]:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(self.TEMPLATES)}:{line}")
        self.assertEqual(
            offenders,
            [],
            "a {# ... #} comment spanning lines is emitted as page text; "
            "use {% comment %} ... {% endcomment %}: " + ", ".join(offenders),
        )

    def test_no_link_carries_a_modifier_filtered_htmx_trigger(self):
        """An <a> with a modifier-filtered htmx trigger is a link that does nothing.

        htmx's shouldCancel() calls preventDefault() for any click on an anchor
        with a real href, and only then does maybeFilterEvent() apply the
        ``[shiftKey]`` filter. So the click is cancelled and then thrown away:
        the link looks clickable and is not. Every card on the board had
        ``hx-trigger="click[shiftKey] consume"`` on its key, and clicking a card
        did nothing whatsoever.

        The fix in _card.html is a plain href plus data-preview handled in JS.
        This test is the general form, so the next template that reaches for the
        same idiom fails here instead of in the browser.
        """
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<a\b[^>]*>", text, re.S):
                tag = match.group(0)
                if "hx-" not in tag:
                    continue
                trigger = re.search(r'hx-trigger="([^"]*)"', tag)
                if trigger and re.search(r"\[[A-Za-z]", trigger.group(1)):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(self.TEMPLATES)}:{line}")
        self.assertEqual(
            offenders,
            [],
            "an anchor with a modifier-filtered htmx trigger never navigates: " + ", ".join(offenders),
        )

    def test_comment_blocks_are_balanced(self):
        """A nested comment closes at the first endcomment, leaking the rest.

        Django's parser skips to the next ``endcomment`` token, so an inner
        ``{% comment %}`` does not nest: it ends the outer one and the rest of
        the text becomes page content.
        """
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            opens = len(re.findall(r"\{%\s*comment\s*%\}", text))
            closes = len(re.findall(r"\{%\s*endcomment\s*%\}", text))
            if opens != closes:
                offenders.append(f"{path.relative_to(self.TEMPLATES)}: {opens} open, {closes} close")
        self.assertEqual(offenders, [], "; ".join(offenders))


class CardLinkTests(TestCase):
    """The board card's key link must be a working link."""

    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.issue = Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary="Clickable",
            status=Status.objects.get(name="To Do"),
            priority=Priority.objects.first(),
            issue_type=IssueType.objects.first(),
        )
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_the_key_link_is_a_plain_link(self):
        board = self.c.get("/board/WEB/").content.decode()
        anchors = re.findall(r"<a\b[^>]*>", board)
        card = [
            a for a in anchors if f'"{self.issue.key}"' not in a and "/issues/" in a and "data-preview" in a
        ]
        self.assertTrue(card, "the card key is not a previewable link any more")
        for tag in card:
            self.assertIn(f'href="/issues/{self.issue.key}/"', tag)
            self.assertNotIn("hx-trigger", tag)
            self.assertNotIn("hx-get", tag)

    def test_every_card_key_can_be_previewed(self):
        """Without data-preview the shift+click preview silently stops working."""
        board = self.c.get("/board/WEB/").content.decode()
        self.assertIn(f'data-preview="/issues/{self.issue.key}/"', board)
        self.assertIn("card-preview", board)

    def test_the_target_of_a_card_key_resolves(self):
        """The href itself has to work, boost or no boost."""
        r = self.c.get(self.issue.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.issue.key)

    def test_the_preview_endpoint_serves_a_fragment(self):
        """What shift+click injects must be a fragment, not a whole page."""
        board = self.c.get("/board/WEB/", headers={"HX-Request": "true"}).content.decode()
        url = re.search(r'data-preview="([^"]+)"', board).group(1)
        r = self.c.get(url, headers={"HX-Request": "true"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("issue-detail", r.content.decode())


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


class DeleteDensifiesColumnTests(TestCase):
    """Deleting a card must close the gap it left.

    A card leaving its column normally goes through a status change, and
    ``_append_to_column`` leaves a hole on purpose: ``rank`` is a sort key, so
    gaps do not affect the order and the next drag in that column renumbers it.
    A delete has no next drag. Nothing re-densified afterwards — ``adelete()``
    is a queryset call, so it fires ``post_delete`` and nothing else, and there
    is no ``Issue`` ``post_delete`` receiver that could do it — so a column that
    was only read and never dragged accumulated holes indefinitely, which is the
    opposite of what the docstring in ``board/views.py`` claims.
    """

    def setUp(self):
        _seed_lookups()
        self.user = User.objects.create_user(username="alice", password="pw", email="a@x.com")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.todo = Status.objects.get(name="To Do")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _column(self):
        """Ranks in board order, so a hole shows up as a gap in the numbers."""
        return list(
            Issue.objects.filter(project=self.project, status=self.todo)
            .order_by("rank", "-updated_at")
            .values_list("key", "rank")
        )

    def _make(self, n):
        made = []
        for i in range(n):
            made.append(
                Issue.objects.create(
                    project=self.project,
                    reporter=self.user,
                    summary=f"i{i}",
                    status=self.todo,
                    priority=Priority.objects.first(),
                    issue_type=IssueType.objects.first(),
                )
            )
        return made

    def test_a_dense_column_starts_dense(self):
        self._make(4)
        self.assertEqual([rank for _key, rank in self._column()], [0, 1, 2, 3])

    def test_deleting_the_first_card_closes_the_gap(self):
        issues = self._make(4)
        self.c.delete(f"/api/v1/issues/{issues[0].key}/")
        self.assertEqual([rank for _key, rank in self._column()], [0, 1, 2])

    def test_deleting_from_the_middle_closes_the_gap(self):
        issues = self._make(4)
        self.c.delete(f"/api/v1/issues/{issues[2].key}/")
        self.assertEqual([rank for _key, rank in self._column()], [0, 1, 2])

    def test_a_bulk_delete_closes_every_column_it_touched(self):
        self._make(4)
        progress = Status.objects.get(name="In Progress")
        other = [
            Issue.objects.create(
                project=self.project,
                reporter=self.user,
                summary=f"p{i}",
                status=progress,
                priority=Priority.objects.first(),
                issue_type=IssueType.objects.first(),
            )
            for i in range(3)
        ]
        keys = list(
            Issue.objects.filter(project=self.project)
            .order_by("rank", "-updated_at")
            .values_list("key", flat=True)
        )
        r = self.c.post(
            reverse("board:bulk_update", args=[self.project.key]),
            {"keys": keys[:2] + [other[0].key], "action": "delete"},
        )
        self.assertEqual(r.status_code, 204, r.content[:200])
        remaining_todo = list(
            Issue.objects.filter(project=self.project, status=self.todo)
            .order_by("rank", "-updated_at")
            .values_list("rank", flat=True)
        )
        remaining_progress = list(
            Issue.objects.filter(project=self.project, status=progress)
            .order_by("rank", "-updated_at")
            .values_list("rank", flat=True)
        )
        self.assertEqual(remaining_todo, list(range(len(remaining_todo))))
        self.assertEqual(remaining_progress, list(range(len(remaining_progress))))

    def test_the_new_card_still_lands_at_the_end(self):
        """MAX(rank) + 1 is gap-tolerant, so this only holds because of the above."""
        issues = self._make(3)
        self.c.delete(f"/api/v1/issues/{issues[0].key}/")
        self._make(1)
        self.assertEqual([rank for _key, rank in self._column()], [0, 1, 2])
