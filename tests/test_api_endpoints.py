"""Tests for the API endpoints added for the MCP server.

The emphasis is on the visibility gate. A search endpoint and a link endpoint
are both places where a caller could reach data they should not see, so each test
here checks the negative case as well as the positive one.
"""

import json

from django.test import Client, TestCase

from issues.models import IssueLink, Status
from projects.models import SavedFilter
from tests.test_smoke import _make_issue, _make_project, _make_user, _seed_lookups


class APISearchTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")

        # Owned by somebody else, with no membership for alice. Using
        # _make_project(self.user) here would make her a member and quietly
        # defeat the visibility assertions below.
        self.stranger = _make_user("mallory")
        self.hidden = _make_project(self.stranger, key="HID")

        self.mine = _make_issue(self.project, self.user, summary="Login button broken")
        self.theirs = _make_issue(self.hidden, self.stranger, summary="Server restart loop")
        self.done = _make_issue(self.project, self.user, summary="Signup finished")
        self.done.status = Status.objects.get(name="Done")
        self.done.save()

        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _search(self, jql):
        r = self.c.get(f"/api/v1/search?jql={jql}")
        return r

    def test_search_by_key(self):
        r = self._search(f"key = {self.mine.key}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i["key"] for i in r.json()["items"]], [self.mine.key])

    def test_search_by_status_category(self):
        r = self._search("statusCategory = Done")
        self.assertEqual(r.status_code, 200)
        keys = [i["key"] for i in r.json()["items"]]
        self.assertIn(self.done.key, keys)
        self.assertNotIn(self.mine.key, keys)

    def test_search_assignee_is_empty(self):
        r = self._search("assignee is EMPTY")
        self.assertEqual(r.status_code, 200)
        keys = [i["key"] for i in r.json()["items"]]
        self.assertIn(self.mine.key, keys)

    def test_search_invalid_jql_returns_400_with_a_reason(self):
        r = self._search("statuss = Done")
        self.assertEqual(r.status_code, 400)
        self.assertIn("statuss", r.json()["detail"])

    def test_search_is_scoped_to_visible_projects(self):
        """A caller must not retrieve another project's issues by key."""
        r = self.c.get(f"/api/v1/search?jql=key = {self.theirs.key}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_search_does_not_leak_issue_counts_from_hidden_projects(self):
        """A 200 with count 0 is right; a 403 would confirm the key exists."""
        r = self._search(f"key = {self.theirs.key}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_search_free_text_still_works(self):
        r = self._search("Signup")
        self.assertEqual(r.status_code, 200)
        keys = [i["key"] for i in r.json()["items"]]
        self.assertIn(self.done.key, keys)


class APIIssueLinkTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.a = _make_issue(self.project, self.user, summary="A")
        self.b = _make_issue(self.project, self.user, summary="B")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_list_link_types(self):
        r = self.c.get("/api/v1/link-types/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("blocks", r.json())
        self.assertIn("relates_to", r.json())

    def test_create_link_with_model_key(self):
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps({
                "link_type": "blocks", "inward_issue_key": self.b.key, "outward_issue_key": self.a.key,
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(IssueLink.objects.filter(source=self.a, target=self.b, type="blocks").exists())

    def test_create_link_with_jira_spelling(self):
        """Agents write Jira's display names, not the model's keys."""
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps({
                "link_type": "Relates", "inward_issue_key": self.b.key, "outward_issue_key": self.a.key,
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(IssueLink.objects.filter(type="relates_to").exists())

    def test_create_link_rejects_unknown_type(self):
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps({
                "link_type": "nonsense", "inward_issue_key": self.b.key, "outward_issue_key": self.a.key,
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_create_link_rejects_duplicate(self):
        payload = {
            "link_type": "blocks", "inward_issue_key": self.b.key, "outward_issue_key": self.a.key,
        }
        self.assertEqual(
            self.c.post(f"/api/v1/issues/{self.a.key}/links/", data=json.dumps(payload),
                        content_type="application/json").status_code, 200)
        self.assertEqual(
            self.c.post(f"/api/v1/issues/{self.a.key}/links/", data=json.dumps(payload),
                        content_type="application/json").status_code, 400)

    def test_create_link_cannot_target_an_invisible_issue(self):
        """The other end goes through the same visibility gate."""
        stranger = _make_user("mallory")
        hidden = _make_project(stranger, key="HID")
        secret = _make_issue(hidden, stranger, summary="secret")
        c = Client()
        c.login(username="mallory", password="pw")
        r = c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps({
                "link_type": "blocks", "inward_issue_key": secret.key, "outward_issue_key": self.a.key,
            }),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)
        self.assertFalse(IssueLink.objects.exists())

    def test_list_links_includes_both_directions(self):
        IssueLink.objects.create(source=self.a, target=self.b, type="blocks", created_by=self.user)
        IssueLink.objects.create(source=self.b, target=self.a, type="relates_to", created_by=self.user)
        r = self.c.get(f"/api/v1/issues/{self.a.key}/links/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 2)


class APIWatcherTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_watch_and_unwatch(self):
        self.assertEqual(self.c.post(f"/api/v1/issues/{self.issue.key}/watchers/").status_code, 200)
        self.issue.refresh_from_db()
        self.assertTrue(self.issue.watchers.filter(pk=self.user.pk).exists())

        self.assertEqual(self.c.delete(f"/api/v1/issues/{self.issue.key}/watchers/").status_code, 200)
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.watchers.filter(pk=self.user.pk).exists())

    def test_watch_is_idempotent(self):
        self.c.post(f"/api/v1/issues/{self.issue.key}/watchers/")
        r = self.c.post(f"/api/v1/issues/{self.issue.key}/watchers/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)

    def test_unwatch_when_not_watching_does_not_error(self):
        r = self.c.delete(f"/api/v1/issues/{self.issue.key}/watchers/")
        self.assertEqual(r.status_code, 200)

    def test_list_watchers(self):
        self.c.post(f"/api/v1/issues/{self.issue.key}/watchers/")
        r = self.c.get(f"/api/v1/issues/{self.issue.key}/watchers/")
        self.assertEqual(r.json(), ["alice"])


class APISavedFilterTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.bob = _make_user("bob")
        # Owned by bob, so alice cannot see it and a shared filter scoped to HID
        # is genuinely unreachable for her.
        self.hidden = _make_project(self.bob, key="HID")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_lists_own_and_shared(self):
        mine = SavedFilter.objects.create(owner=self.user, name="mine", query="project = WEB")
        shared = SavedFilter.objects.create(owner=self.bob, name="shared", query="project = WEB", scope="shared")
        someone_elses = SavedFilter.objects.create(owner=self.bob, name="private", query="project = WEB")
        names = {f["name"] for f in self.c.get("/api/v1/filters/").json()}
        self.assertIn(mine.name, names)
        self.assertIn(shared.name, names)
        self.assertNotIn(someone_elses.name, names)

    def test_hides_shared_filter_pointing_at_an_invisible_project(self):
        """A shared filter is only useful if its results are reachable.

        Returning a shared filter scoped to a project the caller cannot see
        invites a client to run it and get an empty result with no explanation.
        """
        hidden = SavedFilter.objects.create(
            owner=self.bob, name="hidden-project", query="project = HID", scope="shared"
        )
        names = {f["name"] for f in self.c.get("/api/v1/filters/").json()}
        self.assertNotIn(hidden.name, names)

    def test_keeps_shared_filter_with_no_project_clause(self):
        shared = SavedFilter.objects.create(
            owner=self.bob, name="text-only", query="urgent", scope="shared"
        )
        names = {f["name"] for f in self.c.get("/api/v1/filters/").json()}
        self.assertIn(shared.name, names)

    def test_hides_shared_filter_mixing_visible_and_hidden_projects(self):
        """A filter naming any project the caller cannot see is hidden.

        Note that "project = WEB AND project = HID" is also unsatisfiable — no
        issue belongs to two projects — so hiding it loses nothing usable. The
        rule is stated this way because it is the simple one: a filter is offered
        only when every project it references is visible.
        """
        mixed = SavedFilter.objects.create(
            owner=self.bob, name="mixed", query="project = WEB AND project = HID", scope="shared"
        )
        names = {f["name"] for f in self.c.get("/api/v1/filters/").json()}
        self.assertNotIn(mixed.name, names)

    def test_keeps_shared_filter_whose_only_project_is_visible(self):
        """Extra clauses that are not project references do not hide a filter."""
        scoped = SavedFilter.objects.create(
            owner=self.bob, name="scoped", query="project = WEB AND statusCategory != Done", scope="shared"
        )
        names = {f["name"] for f in self.c.get("/api/v1/filters/").json()}
        self.assertIn(scoped.name, names)
