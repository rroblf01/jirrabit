"""Tests for the API endpoints added for the MCP server.

The emphasis is on the visibility gate. A search endpoint and a link endpoint
are both places where a caller could reach data they should not see, so each test
here checks the negative case as well as the positive one.
"""

import json
import os
from unittest import mock

from django.test import Client, TestCase

from issues.models import Comment, IssueLink, Status, WorkLog
from projects.models import ProjectMembership, SavedFilter, Sprint
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

    def test_links_report_keys_not_ids(self):
        """A key is the only handle a client has on an issue.

        Returning bare ids made the response unusable: there is no way to turn
        31 back into WEB-19, so a caller could create a link and not act on it.
        """
        self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps({
                "link_type": "blocks", "inward_issue_key": self.b.key,
                "outward_issue_key": self.a.key,
            }),
            content_type="application/json",
        )
        row = self.c.get(f"/api/v1/issues/{self.a.key}/links/").json()[0]
        self.assertEqual(row["source"], self.a.key)
        self.assertEqual(row["target"], self.b.key)
        # The numeric ids stay available, under their own names.
        self.assertEqual(row["sourceId"], self.a.pk)
        self.assertEqual(row["targetId"], self.b.pk)
        self.assertEqual(row["type"], "blocks")


class APISprintTests(TestCase):
    """Sprint writes. The API could read and patch sprints but not create one,
    so an agent could not plan a sprint at all."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_create_sprint(self):
        r = self.c.post(
            "/api/v1/projects/WEB/sprints/",
            data=json.dumps({"name": "Sprint 1", "goal": "ship it", "start_date": "2026-10-01"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["name"], "Sprint 1")
        self.assertEqual(body["goal"], "ship it")
        self.assertEqual(body["start_date"], "2026-10-01")
        # A new sprint belongs to the project it was created in.
        self.assertEqual(Sprint.objects.get(pk=body["id"]).project_id, self.project.pk)

    def test_create_sprint_requires_a_name(self):
        r = self.c.post(
            "/api/v1/projects/WEB/sprints/",
            data=json.dumps({"goal": "sin nombre"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("name", r.json()["detail"])

    def test_create_sprint_needs_admin(self):
        member = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=member, role="member")
        c2 = Client()
        c2.login(username="bob", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/sprints/",
            data=json.dumps({"name": "noAllowed"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    def test_create_sprint_in_a_project_the_caller_cannot_see(self):
        stranger = _make_user("carol")
        other = _make_project(stranger, key="OPS")
        r = self.c.post(
            "/api/v1/projects/OPS/sprints/",
            data=json.dumps({"name": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Sprint.objects.filter(name="nope").exists())
        self.assertEqual(other.sprints.count(), 0)

    def test_created_sprint_is_listed_and_patchable(self):
        created = self.c.post(
            "/api/v1/projects/WEB/sprints/",
            data=json.dumps({"name": "Sprint 2"}),
            content_type="application/json",
        ).json()
        listing = self.c.get("/api/v1/projects/WEB/sprints/").json()
        self.assertIn(created["id"], [s["id"] for s in listing["items"]])
        patched = self.c.patch(
            f"/api/v1/sprints/{created['id']}/",
            data=json.dumps({"goal": "now with a goal"}),
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["goal"], "now with a goal")


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


class SeedDemoAPIKeyTests(TestCase):
    """The published demo token, and the guards around it."""

    TOKEN = "jirrabit-demo-token-0123456789"

    def _demo(self, **extras):
        from tests.test_smoke import _make_user

        defaults = {"is_superuser": True, "is_staff": True}
        defaults.update(extras)
        return _make_user("alice_pm", **defaults)

    def _run(self, token=None, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch.dict(os.environ, {} if token is None else {"JIRRABIT_DEMO_API_KEY": token}):
            return call_command("seed_demo_api_key", stdout=out, stderr=out, **kwargs)

    def test_refuses_without_the_env_var(self):
        """It must never invent a token: a predictable one is a published one."""
        from django.core.management.base import CommandError

        self._demo()
        with self.assertRaises(CommandError):
            self._run(token=None)

    def test_refuses_a_short_token(self):
        from django.core.management.base import CommandError

        self._demo()
        with self.assertRaises(CommandError):
            self._run(token="tooshort")

    def test_mints_a_working_key(self):

        from accounts.models import APIKey

        user = self._demo()
        self._run(token=self.TOKEN)
        key = APIKey.objects.get(token_hash=APIKey.hash_token(self.TOKEN))
        self.assertEqual(key.owner, user)

        c = Client()
        r = c.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + self.TOKEN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "alice_pm")

    def test_is_idempotent(self):
        from accounts.models import APIKey

        self._demo()
        self._run(token=self.TOKEN)
        self._run(token=self.TOKEN)
        self.assertEqual(APIKey.objects.filter(token_hash=APIKey.hash_token(self.TOKEN)).count(), 1)

    def test_regrants_a_revoked_key(self):
        """seed_demo may run on every boot, so a revoked demo token has to come back."""
        from django.utils import timezone

        from accounts.models import APIKey

        self._demo()
        self._run(token=self.TOKEN)
        key = APIKey.objects.get(token_hash=APIKey.hash_token(self.TOKEN))
        APIKey.objects.filter(pk=key.pk).update(revoked_at=timezone.now())

        self._run(token=self.TOKEN)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

        c = Client()
        r = c.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + self.TOKEN)
        self.assertEqual(r.status_code, 200)

    def test_refuses_a_non_superuser_without_force(self):
        from django.core.management.base import CommandError

        self._demo(is_superuser=False)
        with self.assertRaises(CommandError):
            self._run(token=self.TOKEN)
        with mock.patch.dict(os.environ, {"JIRRABIT_DEMO_API_KEY": self.TOKEN}):
            from django.core.management import call_command

            call_command("seed_demo_api_key", force=True, verbosity=0)

    def test_refuses_an_unknown_user(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._run(token=self.TOKEN)


class APIPaginationTests(TestCase):
    """Every list endpoint, so no row converter can rot unnoticed.

    Each endpoint hands ``paginate`` a different converter, and a converter that
    is subtly wrong fails at response-validation time, not at import time. These
    exist because ``/projects/`` was briefly broken exactly that way with no test
    touching it.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.other_issue = _make_issue(self.project, self.user)
        Sprint.objects.create(project=self.project, name="S1", status="planned")
        _make_user("bob")

        # One row in every endpoint, so "returns a list" is not vacuously true.
        Comment.objects.create(issue=self.issue, author=self.user, body="hola")
        WorkLog.objects.create(issue=self.issue, author=self.user, minutes=30)
        self.issue.watchers.add(self.user)
        IssueLink.objects.create(
            source=self.issue, target=self.other_issue, type="blocks", created_by=self.user
        )
        SavedFilter.objects.create(owner=self.user, name="mine", query="project = WEB")

        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _rows(self, path):
        """Fetch a list endpoint and return its rows.

        Paginated endpoints return a ``{"items": [...]}`` envelope; the ones typed
        ``list[...]`` in the API return a bare array. Both have to build their
        rows correctly, so the shape is normalised here.
        """
        r = self.c.get(path)
        self.assertEqual(r.status_code, 200, f"{path} returned {r.status_code}: {r.content[:300]}")
        payload = r.json()
        if isinstance(payload, dict):
            return payload["items"]
        return payload

    def test_every_list_endpoint_builds_its_rows(self):
        key = self.issue.key
        cases = {
            "/api/v1/projects/": lambda rows: rows[0]["key"] == "WEB",
            # Ordered by -updated_at, so membership is the assertion, not position.
            "/api/v1/projects/WEB/issues/": lambda rows: key in {r["key"] for r in rows},
            "/api/v1/projects/WEB/sprints/": lambda rows: rows[0]["name"] == "S1",
            f"/api/v1/issues/{key}/links/": lambda rows: rows[0]["type"] == "blocks",
            "/api/v1/statuses/": lambda rows: "To Do" in {r["name"] for r in rows},
            "/api/v1/priorities/": lambda rows: all("weight" in r for r in rows),
            "/api/v1/issue-types/": lambda rows: all("category" in r for r in rows),
            "/api/v1/users/search/?query=alice": lambda rows: rows[0]["username"] == "alice",
            f"/api/v1/issues/{key}/comments/": lambda rows: rows[0]["body"] == "hola",
            f"/api/v1/issues/{key}/worklogs/": lambda rows: rows[0]["minutes"] == 30,
            f"/api/v1/issues/{key}/watchers/": lambda rows: rows == ["alice"],
            "/api/v1/link-types/": lambda rows: "blocks" in rows,
            "/api/v1/filters/": lambda rows: rows[0]["name"] == "mine",
            "/api/v1/search?jql=project = WEB": lambda rows: rows[0]["project"] == "WEB",
        }
        for path, check in cases.items():
            with self.subTest(path=path):
                rows = self._rows(path)
                self.assertIsInstance(rows, list, f"{path} did not return a list")
                self.assertTrue(rows, f"{path} returned no rows")
                self.assertTrue(check(rows), f"{path} returned unexpected rows: {rows}")

    def test_paginated_envelope_shape(self):
        payload = self.c.get("/api/v1/projects/").json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["size"], 50)
        self.assertEqual(payload["pages"], 1)
        self.assertIsNone(payload["next"])

    def test_user_search_excludes_inactive_and_leaks_no_privileges(self):
        inactive = _make_user("gone")
        inactive.is_active = False
        inactive.save()
        self.assertEqual(self._rows("/api/v1/users/search/?query=gone"), [])
        row = self._rows("/api/v1/users/search/?query=alice")[0]
        self.assertNotIn("is_superuser", row)
        self.assertNotIn("is_staff", row)
