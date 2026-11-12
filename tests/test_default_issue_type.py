"""An issue created without a type must not become an epic.

The default used to be ``IssueType.objects.first()``, and the seed creates Epic
first, so every untyped issue from the API, the board's quick add and CSV import
was an issue of type Epic. That is not an epic in jirrabit (``projects.Epic``
is), so it showed up in no epic list. Each test seeds Epic *before* Task to
reproduce the seeded order.
"""

from django.test import Client, TestCase

from issues.models import Issue, IssueType, Status
from tests.test_smoke import _make_project, _make_user, _seed_lookups


def _seed_epic_first():
    IssueType.objects.create(name="Epic", category="epic")
    _seed_lookups()
    IssueType.objects.create(name="Subtask", category="subtask")


class PickDefaultTests(TestCase):
    def test_prefers_task_over_an_earlier_epic(self):
        _seed_epic_first()
        self.assertEqual(IssueType.pick_default(IssueType.objects.all()).category, "task")

    def test_falls_back_past_subtask_and_epic(self):
        types = [
            IssueType(pk=1, name="Epic", category="epic"),
            IssueType(pk=2, name="Subtask", category="subtask"),
            IssueType(pk=3, name="Bug", category="bug"),
        ]
        self.assertEqual(IssueType.pick_default(types).name, "Bug")

    def test_an_instance_with_only_epics_still_gets_a_type(self):
        self.assertEqual(IssueType.pick_default([IssueType(pk=1, name="Epic", category="epic")]).name, "Epic")
        self.assertIsNone(IssueType.pick_default([]))


class UntypedCreateTests(TestCase):
    def setUp(self):
        _seed_epic_first()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_api_create_without_type_is_a_task(self):
        r = self.c.post(
            "/api/v1/projects/WEB/issues/", {"summary": "No type given"}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Issue.objects.get(summary="No type given").issue_type.category, "task")

    def test_board_quick_add_is_a_task(self):
        status = Status.objects.get(name="To Do")
        r = self.c.post("/board/WEB/column-create/", {"summary": "Sticky note", "status_id": status.pk})
        self.assertLess(r.status_code, 400, r.content)
        self.assertEqual(Issue.objects.get(summary="Sticky note").issue_type.category, "task")

    def test_api_csv_import_without_type_column_is_a_task(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            {"csv": "summary\nImported row\n", "dry_run": False},
            content_type="application/json",
        )
        self.assertLess(r.status_code, 400, r.content)
        self.assertEqual(Issue.objects.get(summary="Imported row").issue_type.category, "task")
