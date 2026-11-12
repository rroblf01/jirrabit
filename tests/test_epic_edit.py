"""The epic page renders its summary as Markdown, and the summary can be edited.

The summary used to go through ``linebreaksbr``, so an epic filed through the MCP
-- where agents write Markdown, as they do for issue descriptions -- showed raw
asterisks and hashes. And there was no way to fix it from the web: epics could be
created and viewed, never edited.
"""

from django.test import Client, TestCase

from projects.models import Epic, ProjectMembership
from tests.test_smoke import _make_project, _make_user, _seed_lookups


class EpicSummaryTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.lead = _make_user("alice")
        self.project = _make_project(self.lead, key="WEB")
        self.epic = Epic.objects.create(
            project=self.project,
            name="Checkout",
            summary="**Objetivo**\n\n- uno\n- dos\n\n[malo](javascript:alert(1))",
            created_by=self.lead,
        )
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _detail(self, client=None):
        return (client or self.c).get(f"/projects/WEB/epics/{self.epic.pk}/")

    def test_summary_is_rendered_as_markdown(self):
        html = self._detail().content.decode()
        self.assertIn("<strong>Objetivo</strong>", html)
        self.assertIn("<li>uno</li>", html)
        self.assertNotIn("**Objetivo**", html)

    def test_markdown_drops_javascript_links(self):
        self.assertNotIn("javascript:", self._detail().content.decode())

    def test_editor_sees_the_edit_link(self):
        self.assertIn(f"/projects/WEB/epics/{self.epic.pk}/edit/", self._detail().content.decode())

    def test_member_can_edit_the_summary(self):
        url = f"/projects/WEB/epics/{self.epic.pk}/edit/"
        page = self.c.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-md-preview="1"', page.content.decode())

        r = self.c.post(url, {"name": "Checkout v2", "summary": "## Nuevo", "color": "#22c55e"})
        self.assertRedirects(r, f"/projects/WEB/epics/{self.epic.pk}/", fetch_redirect_response=False)
        self.epic.refresh_from_db()
        self.assertEqual(
            (self.epic.name, self.epic.summary, self.epic.color), ("Checkout v2", "## Nuevo", "#22c55e")
        )

    def test_invalid_edit_rerenders_the_form(self):
        r = self.c.post(
            f"/projects/WEB/epics/{self.epic.pk}/edit/", {"name": "", "summary": "x", "color": "#000000"}
        )
        self.assertEqual(r.status_code, 200)
        self.epic.refresh_from_db()
        self.assertEqual(self.epic.name, "Checkout")

    def test_viewer_can_read_but_not_edit(self):
        viewer = _make_user("victor")
        ProjectMembership.objects.create(project=self.project, user=viewer, role="viewer")
        c = Client()
        c.login(username="victor", password="pw")

        page = self._detail(c)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(f"/epics/{self.epic.pk}/edit/", page.content.decode())

        url = f"/projects/WEB/epics/{self.epic.pk}/edit/"
        self.assertEqual(c.get(url).status_code, 403)
        self.assertEqual(c.post(url, {"name": "hacked", "summary": "", "color": "#000000"}).status_code, 403)
        self.epic.refresh_from_db()
        self.assertEqual(self.epic.name, "Checkout")

    def test_epic_of_another_project_is_not_found(self):
        _make_project(self.lead, key="OPS")
        self.assertEqual(self.c.get(f"/projects/OPS/epics/{self.epic.pk}/edit/").status_code, 404)


class EpicListSummaryTests(TestCase):
    """The project page lists epics with a one-line summary. It truncated the
    raw source, so a Markdown summary showed its ``##`` and ``**``."""

    def setUp(self):
        _seed_lookups()
        self.lead = _make_user("alice")
        self.project = _make_project(self.lead, key="WEB")
        Epic.objects.create(
            project=self.project,
            name="Oven",
            summary="## Contexto\n\nPartimos de un proyecto Django **en blanco** llamado `oven` & <b>x</b>",
            created_by=self.lead,
        )
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_row_shows_markdown_as_plain_text(self):
        html = self.c.get("/projects/WEB/").content.decode()
        self.assertIn("Contexto Partimos de un proyecto Django en blanco llamado oven", html)
        self.assertNotIn("## Contexto", html)
        self.assertNotIn("**en blanco**", html)

    def test_row_text_is_still_escaped(self):
        html = self.c.get("/projects/WEB/").content.decode()
        self.assertNotIn("<b>x</b>", html)

    def test_row_from_the_htmx_create_is_plain_text_too(self):
        r = self.c.post(
            "/projects/WEB/epics/new/",
            {"name": "Nueva", "summary": "**hola** _mundo_", "color": "#1e6fff"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("hola mundo", html)
        self.assertNotIn("**", html)
