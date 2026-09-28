"""Tests for the API endpoints added for the MCP server.

The emphasis is on the visibility gate. A search endpoint and a link endpoint
are both places where a caller could reach data they should not see, so each test
here checks the negative case as well as the positive one.
"""

import base64
import json
import os
from unittest import mock

from django.test import Client, TestCase

from accounts.models import APIKey, Notification, Team, User
from issues.models import (
    AuditEntry,
    Comment,
    Issue,
    IssueLink,
    IssueType,
    Priority,
    Status,
    WorkLog,
)
from projects.models import Project, ProjectMembership, SavedFilter, Sprint
from tests.test_smoke import _make_issue, _make_project, _make_user, _seed_lookups


def _make_issue_in(project, reporter, status, summary="Test"):
    """An issue created *in* ``status``.

    Deliberately not ``_make_issue(...)`` followed by ``issue.status = ...``: that
    is two saves, and ``Issue.save()`` only appends a rank on insert, so the card
    would land in the new column carrying an index from the old one. Half the board
    tests would then be asserting on stale data.
    """
    return Issue.objects.create(
        project=project,
        reporter=reporter,
        summary=summary,
        status=status,
        priority=Priority.objects.first(),
        issue_type=IssueType.objects.first(),
    )


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
            data=json.dumps(
                {
                    "link_type": "blocks",
                    "inward_issue_key": self.b.key,
                    "outward_issue_key": self.a.key,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(IssueLink.objects.filter(source=self.a, target=self.b, type="blocks").exists())

    def test_create_link_with_jira_spelling(self):
        """Agents write Jira's display names, not the model's keys."""
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps(
                {
                    "link_type": "Relates",
                    "inward_issue_key": self.b.key,
                    "outward_issue_key": self.a.key,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(IssueLink.objects.filter(type="relates_to").exists())

    def test_create_link_rejects_unknown_type(self):
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps(
                {
                    "link_type": "nonsense",
                    "inward_issue_key": self.b.key,
                    "outward_issue_key": self.a.key,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_create_link_rejects_duplicate(self):
        payload = {
            "link_type": "blocks",
            "inward_issue_key": self.b.key,
            "outward_issue_key": self.a.key,
        }
        self.assertEqual(
            self.c.post(
                f"/api/v1/issues/{self.a.key}/links/",
                data=json.dumps(payload),
                content_type="application/json",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.c.post(
                f"/api/v1/issues/{self.a.key}/links/",
                data=json.dumps(payload),
                content_type="application/json",
            ).status_code,
            400,
        )

    def test_create_link_cannot_target_an_invisible_issue(self):
        """The other end goes through the same visibility gate."""
        stranger = _make_user("mallory")
        hidden = _make_project(stranger, key="HID")
        secret = _make_issue(hidden, stranger, summary="secret")
        c = Client()
        c.login(username="mallory", password="pw")
        r = c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps(
                {
                    "link_type": "blocks",
                    "inward_issue_key": secret.key,
                    "outward_issue_key": self.a.key,
                }
            ),
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
            data=json.dumps(
                {
                    "link_type": "blocks",
                    "inward_issue_key": self.b.key,
                    "outward_issue_key": self.a.key,
                }
            ),
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


class ProjectDeleteTests(TestCase):
    """Deleting a project with issues in it used to return 500.

    The cascade fires post_delete for every issue and comment, and the audit
    receiver tried to insert a row pointing at the project row that had just
    been deleted. The resulting IntegrityError was caught in Python, which does
    not help: Postgres has already failed the transaction, so the commit fails
    too and the request 500s.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="GONE")
        for n in range(3):
            _make_issue(self.project, self.user, summary=f"i{n}")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_delete_project_with_issues(self):
        r = self.c.delete("/api/v1/projects/GONE/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertFalse(Project.objects.filter(key="GONE").exists())
        self.assertEqual(Issue.objects.filter(project__key="GONE").count(), 0)

    def test_delete_project_leaves_its_audit_rows_alone(self):
        """The other projects' audit history must survive."""
        keep = _make_project(self.user, key="KEEP")
        _make_issue(keep, self.user)
        before = AuditEntry.objects.filter(project=keep).count()
        self.c.delete("/api/v1/projects/GONE/")
        keep.refresh_from_db()
        self.assertTrue(Project.objects.filter(key="KEEP").exists())
        self.assertEqual(AuditEntry.objects.filter(project=keep).count(), before)

    def test_delete_project_needs_admin(self):
        member = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=member, role="member")
        c2 = Client()
        c2.login(username="bob", password="pw")
        r = c2.delete("/api/v1/projects/GONE/")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Project.objects.filter(key="GONE").exists())


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
        shared = SavedFilter.objects.create(
            owner=self.bob, name="shared", query="project = WEB", scope="shared"
        )
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
        shared = SavedFilter.objects.create(owner=self.bob, name="text-only", query="urgent", scope="shared")
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

    def test_uses_the_published_token_without_the_env_var(self):
        """No env var still mints the published demo token.

        It used to raise instead. The guard was dropped because ``seed_demo``
        already publishes a stronger credential — ``alice_pm`` / ``demopass``,
        a superuser account reachable through the web login — so requiring an
        environment variable for the API key alone protected nothing, and in
        practice meant the published token did not work until somebody found
        the variable.
        """
        from accounts.models import APIKey
        from projects.management.commands.seed_demo_api_key import DEFAULT_DEMO_TOKEN

        self._demo()
        self._run(token=None)

        self.assertTrue(
            APIKey.objects.filter(token_hash=APIKey.hash_token(DEFAULT_DEMO_TOKEN)).exists(),
            "the published token should be minted by default",
        )
        c = Client()
        r = c.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + DEFAULT_DEMO_TOKEN)
        self.assertEqual(r.status_code, 200, r.content)

    def test_the_env_var_overrides_the_published_token(self):
        """An operator who wants a different token gets one, and only one."""
        from accounts.models import APIKey
        from projects.management.commands.seed_demo_api_key import DEFAULT_DEMO_TOKEN

        self._demo()
        self._run(token=self.TOKEN)

        self.assertTrue(APIKey.objects.filter(token_hash=APIKey.hash_token(self.TOKEN)).exists())
        self.assertFalse(
            APIKey.objects.filter(token_hash=APIKey.hash_token(DEFAULT_DEMO_TOKEN)).exists(),
            "the override must replace the default, not add to it",
        )

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

    def _seed_demo(self, env=None):
        """Run seed_demo with a controlled environment."""
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False):
            call_command("seed_demo", stdout=out, stderr=out, verbosity=0)
        return out.getvalue()

    def test_seed_demo_re_mints_the_token(self):
        """A daily seed_demo must leave the published token working.

        seed_demo wipes the database, so a key minted earlier stops existing.
        That is invisible from the outside — the instance is up and the token
        just stops authenticating — and it is why the token has to be re-minted
        as part of the seed rather than as a separate step somebody has to
        remember to run afterwards.
        """
        from accounts.models import APIKey

        # Mint it once, the way an operator would before this behaviour existed.
        self._demo()
        self._run(token=self.TOKEN)
        self.assertEqual(APIKey.objects.filter(owner__username="alice_pm").count(), 1)

        # A daily reseed, then check the token still authenticates for real.
        self._seed_demo(env={"JIRRABIT_DEMO_API_KEY": self.TOKEN})

        self.assertTrue(
            APIKey.objects.filter(owner__username="alice_pm").exists(),
            "seed_demo wiped the key and did not put it back",
        )
        response = self.client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + self.TOKEN)
        self.assertEqual(response.status_code, 200, response.content)

    def test_seed_demo_always_ends_with_a_usable_token(self):
        """The whole point: a bare seed_demo leaves the demo reachable.

        No environment variable, no second command, nothing to remember. This is
        what a daily cron actually runs, so it is the case worth pinning.
        """
        from accounts.models import APIKey
        from projects.management.commands.seed_demo_api_key import DEFAULT_DEMO_TOKEN

        self._seed_demo(env={})

        self.assertTrue(
            APIKey.objects.filter(token_hash=APIKey.hash_token(DEFAULT_DEMO_TOKEN)).exists(),
            "a bare seed_demo must leave the published token working",
        )
        c = Client()
        r = c.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + DEFAULT_DEMO_TOKEN)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["username"], "alice_pm")


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


class APIRoleGateTests(TestCase):
    """Every issue-level write needs a role, not just visibility.

    ``_visible_issue`` answers "can you see this", and it goes through
    ``Project.objects.filter_visible``, which admits a membership of *any* role.
    Every write used to stop there, so a ``viewer`` could edit an issue, comment,
    log time, link issues and — the one that made this a hole worth a test —
    permanently delete an issue. The web UI was never affected; its views call
    ``core.permissions.aassert_can_edit``.
    """

    def setUp(self):
        _seed_lookups()
        self.owner = _make_user("alice")
        self.project = _make_project(self.owner, key="WEB")
        self.issue = _make_issue(self.project, self.owner)
        self.viewer = _make_user("vic")
        ProjectMembership.objects.create(project=self.project, user=self.viewer, role="viewer")
        self.c = Client()
        self.c.login(username="vic", password="pw")

    def _assert_forbidden(self, method, path, body=None):
        fn = getattr(self.c, method)
        kwargs = {}
        if body is not None:
            kwargs = {"data": json.dumps(body), "content_type": "application/json"}
        r = fn(path, **kwargs)
        self.assertEqual(r.status_code, 403, f"{method} {path} returned {r.status_code}")
        return r

    def test_a_viewer_can_still_read_the_issue(self):
        """The gate must not turn a read into a 403 as a side effect."""
        r = self.c.get(f"/api/v1/issues/{self.issue.key}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertTrue(r.json()["key"])

    def test_a_viewer_cannot_delete_an_issue(self):
        self._assert_forbidden("delete", f"/api/v1/issues/{self.issue.key}/")
        self.assertTrue(Issue.objects.filter(pk=self.issue.pk).exists())

    def test_a_viewer_cannot_edit_an_issue(self):
        self._assert_forbidden("patch", f"/api/v1/issues/{self.issue.key}/", {"summary": "secuestrada"})
        self.issue.refresh_from_db()
        self.assertNotEqual(self.issue.summary, "secuestrada")

    def test_a_viewer_cannot_comment(self):
        self._assert_forbidden("post", f"/api/v1/issues/{self.issue.key}/comments/", {"body": "hola"})
        self.assertEqual(Comment.objects.filter(issue=self.issue).count(), 0)

    def test_a_viewer_cannot_log_time(self):
        self._assert_forbidden("post", f"/api/v1/issues/{self.issue.key}/worklogs/", {"minutes": 30})
        self.assertEqual(WorkLog.objects.filter(issue=self.issue).count(), 0)

    def test_a_viewer_cannot_create_a_link(self):
        other = _make_issue(self.project, self.owner)
        self._assert_forbidden(
            "post",
            f"/api/v1/issues/{self.issue.key}/links/",
            {"link_type": "relates_to", "outward_issue_key": self.issue.key, "inward_issue_key": other.key},
        )
        self.assertEqual(IssueLink.objects.count(), 0)

    def test_a_member_can_do_all_of_it(self):
        """The gate has to be a role check, not a blanket refusal."""
        member = _make_user("mel")
        ProjectMembership.objects.create(project=self.project, user=member, role="member")
        c = Client()
        c.login(username="mel", password="pw")
        r = c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data=json.dumps({"summary": "mia"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        r = c.post(
            f"/api/v1/issues/{self.issue.key}/comments/",
            data=json.dumps({"body": "hola"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_the_lead_does_not_need_a_membership_row(self):
        lead_project = _make_project(self.owner, key="OPS")
        lead_issue = _make_issue(lead_project, self.owner)
        self.assertEqual(ProjectMembership.objects.filter(project=lead_project).count(), 1)
        r = self.c.delete(f"/api/v1/issues/{lead_issue.key}/")
        # 404 not 403: the viewer cannot see OPS at all, so its existence is not
        # disclosed. That is the rule _visible_project exists to keep.
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Issue.objects.filter(pk=lead_issue.pk).exists())


class APIArchivedIssueTests(TestCase):
    """Archiving is only an alternative to deleting if it actually hides the issue.

    ``Issue.archived`` carried the help text "hidden from default views" while
    being honoured in two views out of seven: not in the list endpoint, not in
    search, and not in JQL. So archiving removed nothing and offered no safety at
    all — strictly worse than a hard delete, which at least is honest about what
    it did.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.live = _make_issue(self.project, self.user, summary=" viva")
        self.shelved = _make_issue(self.project, self.user, summary=" archivada")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _keys(self, path):
        r = self.c.get(path)
        self.assertEqual(r.status_code, 200, r.content[:200])
        return [row["key"] for row in r.json()["items"]]

    def test_archived_is_reported_on_the_issue(self):
        row = self.c.get(f"/api/v1/issues/{self.live.key}/").json()
        self.assertIn("archived", row)
        self.assertFalse(row["archived"])

    def test_archiving_hides_it_from_the_project_list(self):
        r = self.c.patch(
            f"/api/v1/issues/{self.shelved.key}/",
            data=json.dumps({"archived": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertTrue(r.json()["archived"])
        self.assertEqual(self._keys("/api/v1/projects/WEB/issues/"), [self.live.key])

    def test_archived_true_reveals_it(self):
        self.c.patch(
            f"/api/v1/issues/{self.shelved.key}/",
            data=json.dumps({"archived": True}),
            content_type="application/json",
        )
        self.assertIn(self.shelved.key, self._keys("/api/v1/projects/WEB/issues/?archived=true"))

    def test_it_is_hidden_from_search(self):
        self.c.patch(
            f"/api/v1/issues/{self.shelved.key}/",
            data=json.dumps({"archived": True}),
            content_type="application/json",
        )
        r = self.c.get("/api/v1/search?jql=project%20%3D%20WEB")
        self.assertEqual(r.status_code, 200, r.content[:200])
        keys = [row["key"] for row in r.json()["items"]]
        self.assertIn(self.live.key, keys)
        self.assertNotIn(self.shelved.key, keys)

    def test_a_query_that_names_archived_is_left_alone(self):
        """Otherwise `archived = true` could only ever return nothing."""
        self.c.patch(
            f"/api/v1/issues/{self.shelved.key}/",
            data=json.dumps({"archived": True}),
            content_type="application/json",
        )
        r = self.c.get("/api/v1/search?jql=" + "archived%20%3D%20true")
        self.assertEqual(r.status_code, 200, r.content[:200])
        keys = [row["key"] for row in r.json()["items"]]
        self.assertIn(self.shelved.key, keys)
        self.assertNotIn(self.live.key, keys)

    def test_unarchiving_brings_it_back(self):
        """Nothing wrote archived=False anywhere before, so the field was one-way."""
        for value in (True, False):
            r = self.c.patch(
                f"/api/v1/issues/{self.shelved.key}/",
                data=json.dumps({"archived": value}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIn(self.shelved.key, self._keys("/api/v1/projects/WEB/issues/"))

    def test_archived_false_is_not_read_as_true(self):
        """The boolean has to survive the parser, or the query means its opposite.

        Django coerces a BooleanField with bool(), and bool("false") is True, so
        a naive `Q(archived="false")` returns the archived issues.
        """
        self.c.patch(
            f"/api/v1/issues/{self.shelved.key}/",
            data=json.dumps({"archived": True}),
            content_type="application/json",
        )
        r = self.c.get("/api/v1/search?jql=" + "archived%20%3D%20false")
        self.assertEqual(r.status_code, 200, r.content[:200])
        keys = [row["key"] for row in r.json()["items"]]
        self.assertNotIn(self.shelved.key, keys)

    def test_a_nonsense_boolean_is_an_error_not_a_result(self):
        r = self.c.get("/api/v1/search?jql=" + "archived%20%3D%20maybe")
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("booleano", r.json()["detail"])


class APICommentSoftDeleteTests(TestCase):
    """A soft-deleted comment is still a row, so the listing has to filter it.

    ``deleted_at`` made it invisible in the web UI and the API returned it
    anyway, so the same comment was present in one and gone in the other.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.comment = Comment.objects.create(issue=self.issue, author=self.user, body="para borrar")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _bodies(self, query=""):
        r = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/{query}")
        self.assertEqual(r.status_code, 200, r.content[:200])
        return [row["body"] for row in r.json()["items"]]

    def test_a_soft_deleted_comment_leaves_the_default_listing(self):
        from django.utils import timezone

        self.comment.deleted_at = timezone.now()
        self.comment.save(update_fields=["deleted_at"])
        self.assertEqual(self._bodies(), [])

    def test_include_deleted_shows_it_and_says_when(self):
        from django.utils import timezone

        self.comment.deleted_at = timezone.now()
        self.comment.save(update_fields=["deleted_at"])
        rows = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/?include_deleted=true").json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "para borrar")
        self.assertIsNotNone(rows[0]["deleted_at"])


class APICommentLifecycleTests(TestCase):
    """Edit, soft-delete and restore a comment.

    The web UI has had all three for a long time and the API had none, so an
    agent could add a comment and never take one back. Soft delete is the point:
    ``deleted_at`` keeps the body, which is what makes restore exact rather than
    a reconstruction.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.comment = Comment.objects.create(issue=self.issue, author=self.user, body="primero")
        self.c = Client()
        self.c.login(username="alice", password="pw")
        self.url = f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/"

    def test_edit_replaces_the_body_and_marks_it_edited(self):
        r = self.c.patch(self.url, data=json.dumps({"body": "segundo"}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["body"], "segundo")
        self.assertTrue(r.json()["edited"])
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "segundo")

    def test_edit_snapshots_the_previous_body(self):
        self.c.patch(self.url, data=json.dumps({"body": "segundo"}), content_type="application/json")
        r = self.c.get(f"{self.url}history/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = r.json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old_body"], "primero")
        self.assertEqual(rows[0]["edited_by"], "alice")

    def test_an_unchanged_edit_writes_no_history(self):
        r = self.c.patch(self.url, data=json.dumps({"body": "primero"}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(self.c.get(f"{self.url}history/").json()["count"], 0)

    def test_soft_delete_keeps_the_body_and_is_reversible(self):
        r = self.c.delete(self.url)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIsNotNone(r.json()["deleted_at"])
        self.assertEqual(r.json()["body"], "primero")
        self.assertEqual(Comment.objects.filter(pk=self.comment.pk).count(), 1)
        # Out of the default listing, which is the point of the soft delete.
        listing = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/").json()
        self.assertEqual(listing["count"], 0)
        r = self.c.post(f"{self.url}restore/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIsNone(r.json()["deleted_at"])
        listing = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/").json()
        self.assertEqual(listing["count"], 1)

    def test_deleting_twice_is_not_an_error(self):
        """A lost response makes a client retry, and a 404 would read as failure."""
        self.assertEqual(self.c.delete(self.url).status_code, 200)
        self.assertEqual(self.c.delete(self.url).status_code, 200)
        self.assertEqual(self.c.post(f"{self.url}restore/").status_code, 200)
        self.assertEqual(self.c.post(f"{self.url}restore/").status_code, 200)

    def test_a_deleted_comment_cannot_be_edited_until_restored(self):
        self.c.delete(self.url)
        r = self.c.patch(self.url, data=json.dumps({"body": "zombie"}), content_type="application/json")
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.c.post(f"{self.url}restore/")
        self.assertEqual(
            self.c.patch(
                self.url, data=json.dumps({"body": "zombie"}), content_type="application/json"
            ).status_code,
            200,
        )

    def test_an_empty_body_is_rejected(self):
        r = self.c.patch(self.url, data=json.dumps({"body": "   "}), content_type="application/json")
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_only_the_author_or_a_superuser_may_edit(self):
        """Author-or-superuser, not the project role: editing is an authorship claim."""
        other = _make_user("bob")
        ProjectMembership.objects.create(project=self.project, user=other, role="admin")
        c2 = Client()
        c2.login(username="bob", password="pw")
        r = c2.patch(self.url, data=json.dumps({"body": "suplantado"}), content_type="application/json")
        self.assertEqual(r.status_code, 403, r.content[:200])
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "primero")

    def test_a_comment_on_someone_elses_issue_is_a_404(self):
        """Scoped through the issue on purpose, so visibility is decided there."""
        hidden = _make_project(_make_user("stranger"), key="HID")
        other_issue = _make_issue(hidden, self.project.lead)
        c2 = Client()
        c2.login(username="stranger", password="pw")
        r = c2.get(f"/api/v1/issues/{other_issue.key}/comments/{self.comment.pk}/history/")
        self.assertEqual(r.status_code, 404, r.content[:200])


class APIWorkLogDeleteTests(TestCase):
    """Deleting a worklog has to roll its minutes back off the issue."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _log(self, minutes):
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/worklogs/",
            data=json.dumps({"minutes": minutes}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r.json()["id"]

    def test_deleting_a_worklog_subtracts_its_minutes(self):
        first = self._log(60)
        self._log(30)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 90)
        r = self.c.delete(f"/api/v1/issues/{self.issue.key}/worklogs/{first}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["minutes"], 60)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 30)
        self.assertEqual(WorkLog.objects.filter(issue=self.issue).count(), 1)

    def test_the_total_never_goes_negative(self):
        first = self._log(60)
        self.c.delete(f"/api/v1/issues/{self.issue.key}/worklogs/{first}/")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 0)

    def test_a_worklog_from_another_issue_is_a_404(self):
        log_id = self._log(60)
        other = _make_issue(self.project, self.user)
        r = self.c.delete(f"/api/v1/issues/{other.key}/worklogs/{log_id}/")
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertEqual(WorkLog.objects.filter(pk=log_id).count(), 1)

    def test_a_viewer_cannot_delete_one(self):
        log_id = self._log(60)
        viewer = _make_user("vic")
        ProjectMembership.objects.create(project=self.project, user=viewer, role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        r = c2.delete(f"/api/v1/issues/{self.issue.key}/worklogs/{log_id}/")
        self.assertEqual(r.status_code, 403, r.content[:200])
        self.assertEqual(WorkLog.objects.filter(pk=log_id).count(), 1)


class APILinkDeleteTests(TestCase):
    """A link could be created and read but never removed, so a mistake was permanent."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.a = _make_issue(self.project, self.user)
        self.b = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _link(self):
        r = self.c.post(
            f"/api/v1/issues/{self.a.key}/links/",
            data=json.dumps(
                {
                    "link_type": "blocks",
                    "outward_issue_key": self.a.key,
                    "inward_issue_key": self.b.key,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r.json()

    def test_deleting_a_link_removes_it(self):
        link = self._link()
        # The API creates one row, not a pair. Storing both directions is the web
        # UI's convention, so there is nothing to leave behind here.
        self.assertEqual(IssueLink.objects.count(), 1)
        r = self.c.delete(f"/api/v1/issues/{self.a.key}/links/{link['id']}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(IssueLink.objects.count(), 0)
        self.assertEqual(self.c.get(f"/api/v1/issues/{self.b.key}/links/").json(), [])

    def test_deleting_one_half_of_a_pair_removes_the_inverse(self):
        """A half-pair left behind would show the same relationship twice and
        make the remaining half undeletable, since the delete is scoped to
        links whose source is this issue."""
        link = self._link()
        inverse = IssueLink.objects.create(source=self.b, target=self.a, type=IssueLink.INVERSE[link["type"]])
        self.assertEqual(IssueLink.objects.count(), 2)
        r = self.c.delete(f"/api/v1/issues/{self.a.key}/links/{link['id']}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(IssueLink.objects.count(), 0)
        self.assertFalse(IssueLink.objects.filter(pk=inverse.pk).exists())

    def test_a_link_on_another_issue_is_a_404(self):
        link = self._link()
        other = _make_issue(self.project, self.user)
        r = self.c.delete(f"/api/v1/issues/{other.key}/links/{link['id']}/")
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertEqual(IssueLink.objects.count(), 1)

    def test_a_viewer_cannot_delete_one(self):
        link = self._link()
        viewer = _make_user("vic")
        ProjectMembership.objects.create(project=self.project, user=viewer, role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        r = c2.delete(f"/api/v1/issues/{self.a.key}/links/{link['id']}/")
        self.assertEqual(r.status_code, 403, r.content[:200])
        self.assertEqual(IssueLink.objects.count(), 1)


class APISprintLifecycleTests(TestCase):
    """Start and close a sprint over HTTP.

    ``Sprint.aclose`` moves every unfinished issue, so it changes rows the
    request never named. It was reachable only from a session-authenticated form
    POST, and it was not atomic: the sprint was saved and then the issues walked
    with an await between each, so a failure partway left a closed sprint with
    some of its issues moved and no record of which.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.sprint = Sprint.objects.create(project=self.project, name="S1")
        self.target = Sprint.objects.create(project=self.project, name="S2")
        self.done_status = Status.objects.create(name="Hecho", category="done", order=90)
        self.todo = Status.objects.get(name="To Do")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _issue(self, status=None):
        issue = _make_issue(self.project, self.user)
        issue.status = status or self.todo
        issue.sprint = self.sprint
        issue.save()
        return issue

    def test_start_moves_future_to_active(self):
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/start/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["status"], "active")

    def test_starting_twice_is_not_an_error(self):
        self.c.post(f"/api/v1/sprints/{self.sprint.pk}/start/", data="{}", content_type="application/json")
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/start/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_close_carries_unfinished_issues_to_the_target(self):
        carried = self._issue()
        finished = self._issue(status=self.done_status)
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/close/",
            data=json.dumps({"carry_to": self.target.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["status"], "closed")
        self.assertEqual(body["moved_count"], 1)
        self.assertEqual(body["moved_to"], self.target.pk)
        carried.refresh_from_db()
        finished.refresh_from_db()
        self.assertEqual(carried.sprint_id, self.target.pk)
        # Done work stays on the closed sprint, which is what aclose always did.
        self.assertEqual(finished.sprint_id, self.sprint.pk)

    def test_close_without_a_target_sends_them_to_the_backlog(self):
        issue = self._issue()
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/close/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIsNone(r.json()["moved_to"])
        issue.refresh_from_db()
        self.assertIsNone(issue.sprint_id)

    def test_closing_twice_is_not_an_error(self):
        self.c.post(f"/api/v1/sprints/{self.sprint.pk}/close/", data="{}", content_type="application/json")
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/close/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_a_target_in_another_project_is_refused(self):
        self._issue()
        elsewhere = Sprint.objects.create(
            project=_make_project(_make_user("stranger"), key="HID"), name="otro"
        )
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/close/",
            data=json.dumps({"carry_to": elsewhere.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("otro proyecto", r.json()["detail"])
        # And the refusal has to have left the sprint alone.
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.status, "future")

    def test_a_closed_target_is_refused(self):
        self._issue()
        self.target.status = "closed"
        self.target.save()
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/close/",
            data=json.dumps({"carry_to": self.target.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_a_closed_sprint_cannot_be_started(self):
        self.c.post(f"/api/v1/sprints/{self.sprint.pk}/close/", data="{}", content_type="application/json")
        r = self.c.post(
            f"/api/v1/sprints/{self.sprint.pk}/start/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_a_member_cannot_close_a_sprint(self):
        member = _make_user("mel")
        ProjectMembership.objects.create(project=self.project, user=member, role="member")
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.post(f"/api/v1/sprints/{self.sprint.pk}/close/", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 403, r.content[:200])


class APIEpicTests(TestCase):
    """Epics were filterable in JQL and unreachable in every other way.

    A search would return issues tagged with an epic the caller could not learn
    the name of and had no way to change, which is the largest hole in the write
    surface: every planning question an agent asks starts with "which epic".
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_create_and_read_back(self):
        r = self.c.post(
            "/api/v1/projects/WEB/epics/",
            data=json.dumps({"name": "Checkout", "summary": "carrito nuevo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["name"], "Checkout")
        listed = self.c.get("/api/v1/projects/WEB/epics/").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["summary"], "carrito nuevo")

    def test_an_empty_name_is_rejected(self):
        r = self.c.post(
            "/api/v1/projects/WEB/epics/",
            data=json.dumps({"name": "  "}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_attach_an_issue_and_read_the_epic_back(self):
        epic = self.c.post(
            "/api/v1/projects/WEB/epics/",
            data=json.dumps({"name": "Checkout"}),
            content_type="application/json",
        ).json()
        issue = _make_issue(self.project, self.user)
        r = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"epic_id": epic["id"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        # The whole point: the issue says which epic it is in.
        self.assertEqual(r.json()["epic"], "Checkout")
        self.assertEqual(r.json()["epic_id"], epic["id"])

    def test_an_epic_is_creatable_on_the_issue(self):
        epic = self.c.post(
            "/api/v1/projects/WEB/epics/",
            data=json.dumps({"name": "Checkout"}),
            content_type="application/json",
        ).json()
        r = self.c.post(
            "/api/v1/projects/WEB/issues/",
            data=json.dumps({"summary": "con epic", "epic_id": epic["id"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["epic"], "Checkout")

    def test_an_epic_from_another_project_is_refused(self):
        elsewhere = _make_project(_make_user("stranger"), key="HID")
        from projects.models import Epic

        other_epic = Epic.objects.create(project=elsewhere, name="ajeno")
        r = self.c.patch(
            f"/api/v1/issues/{_make_issue(self.project, self.user).key}/",
            data=json.dumps({"epic_id": other_epic.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_deleting_an_epic_keeps_its_issues(self):
        """Issue.epic is SET_NULL, so the issues survive and become unassigned."""
        from projects.models import Epic

        epic = Epic.objects.create(project=self.project, name="Checkout")
        issue = _make_issue(self.project, self.user)
        issue.epic = epic
        issue.save()
        r = self.c.delete(f"/api/v1/projects/WEB/epics/{epic.pk}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["issues_left_unassigned"], 1)
        issue.refresh_from_db()
        self.assertIsNone(issue.epic_id)

    def test_a_member_cannot_create_an_epic(self):
        """Epics are planning, so they are project administration."""
        from projects.models import ProjectMembership

        ProjectMembership.objects.create(project=self.project, user=_make_user("mel"), role="member")
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/epics/",
            data=json.dumps({"name": "suyo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_every_endpoint_that_serialises_an_issue_joins_the_epic(self):
        """afrom_issue reads i.epic, and an unjoined relation there is a
        synchronous query on the event loop.

        IssueOut gained `epic` in one place and three querysets have to follow, or
        they answer 500. The list endpoint was missed, and only flowtest caught
        it — a request that never came near a test.
        """
        from projects.models import Epic

        epic = Epic.objects.create(project=self.project, name="Checkout")
        issue = _make_issue(self.project, self.user)
        issue.epic = epic
        issue.save()
        for path in (
            f"/api/v1/issues/{issue.key}/",
            "/api/v1/projects/WEB/issues/",
            "/api/v1/projects/WEB/issues/?archived=true",
            "/api/v1/search?jql=project%20%3D%20WEB",
            "/api/v1/projects/WEB/epics/",
        ):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}: {r.content[:200]}")
        listed = self.c.get("/api/v1/projects/WEB/issues/").json()["items"][0]
        self.assertEqual(listed["epic"], "Checkout")
        searched = self.c.get("/api/v1/search?jql=project%20%3D%20WEB").json()["items"][0]
        self.assertEqual(searched["epic"], "Checkout")

    def test_an_epic_in_an_invisible_project_is_a_404(self):
        r = self.c.get("/api/v1/projects/HID/epics/1/")
        self.assertEqual(r.status_code, 404, r.content[:200])


class APILabelTests(TestCase):
    """Labels were readable and unwritable.

    IssueOut listed them, JQL filtered on them, and neither IssueIn nor
    IssuePatch had a field for them. A client could find every issue tagged
    `urgent` and could not tag anything.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.user.is_superuser = True
        self.user.save()
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_setting_a_label_creates_it_on_demand(self):
        """A label is a word, not a record, and no agent would look up an id."""
        issue = _make_issue(self.project, self.user)
        r = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"labels": ["urgent", "backend"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(sorted(r.json()["labels"]), ["backend", "urgent"])
        self.assertEqual(self.c.get("/api/v1/labels/").json()["count"], 2)

    def test_patch_replaces_the_whole_set(self):
        """Reading it as "add these" would make a label unremovable."""
        issue = _make_issue(self.project, self.user)
        self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"labels": ["a", "b"]}),
            content_type="application/json",
        )
        r = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"labels": ["b"]}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["labels"], ["b"])

    def test_creating_on_the_issue_too(self):
        r = self.c.post(
            "/api/v1/projects/WEB/issues/",
            data=json.dumps({"summary": "con etiqueta", "labels": ["nuevo"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["labels"], ["nuevo"])

    def test_create_label_is_idempotent(self):
        """Setting a label creates it, so creating one explicitly must not clash."""
        first = self.c.post(
            "/api/v1/labels/", data=json.dumps({"name": "dup"}), content_type="application/json"
        )
        second = self.c.post(
            "/api/v1/labels/", data=json.dumps({"name": "dup"}), content_type="application/json"
        )
        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(second.status_code, 200, second.content[:200])
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.c.get("/api/v1/labels/").json()["count"], 1)

    def test_deleting_a_label_in_use_is_refused(self):
        """An M2M delete would detach it silently, which is the opposite of the ask."""
        issue = _make_issue(self.project, self.user)
        self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"labels": ["ocupada"]}),
            content_type="application/json",
        )
        label_id = self.c.get("/api/v1/labels/").json()["items"][0]["id"]
        r = self.c.delete(f"/api/v1/labels/{label_id}/")
        self.assertEqual(r.status_code, 409, r.content[:200])
        self.assertIn("en uso", r.json()["detail"])

    def test_deleting_an_unused_label_works(self):
        self.c.post("/api/v1/labels/", data=json.dumps({"name": "suelta"}), content_type="application/json")
        label_id = self.c.get("/api/v1/labels/").json()["items"][0]["id"]
        self.assertEqual(self.c.delete(f"/api/v1/labels/{label_id}/").status_code, 200)

    def test_deleting_needs_a_superuser(self):
        self.c.post("/api/v1/labels/", data=json.dumps({"name": "suelta"}), content_type="application/json")
        label_id = self.c.get("/api/v1/labels/").json()["items"][0]["id"]
        self.user.is_superuser = False
        self.user.save()
        r = self.c.delete(f"/api/v1/labels/{label_id}/")
        self.assertEqual(r.status_code, 403, r.content[:200])


class APIIssueRelationsTests(TestCase):
    """Subtasks and estimates: readable, and now writable."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.parent = _make_issue(self.project, self.user, summary="padre")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_a_child_is_created_against_a_parent_key(self):
        r = self.c.post(
            "/api/v1/projects/WEB/issues/",
            data=json.dumps({"summary": "hija", "parent": self.parent.key}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["parent"], self.parent.key)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.subtasks.count(), 1)

    def test_a_parent_can_be_set_afterwards(self):
        child = _make_issue(self.project, self.user)
        r = self.c.patch(
            f"/api/v1/issues/{child.key}/",
            data=json.dumps({"parent": self.parent.key}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["parent"], self.parent.key)

    def test_an_issue_cannot_be_its_own_parent(self):
        """A self-FK so nothing at the database objects, and the chain never resolves."""
        r = self.c.patch(
            f"/api/v1/issues/{self.parent.key}/",
            data=json.dumps({"parent": self.parent.key}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_a_parent_in_another_project_is_refused(self):
        elsewhere = _make_project(_make_user("stranger"), key="HID")
        other = _make_issue(elsewhere, elsewhere.lead)
        r = self.c.post(
            "/api/v1/projects/WEB/issues/",
            data=json.dumps({"summary": "hija", "parent": other.key}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_estimates_round_trip(self):
        r = self.c.patch(
            f"/api/v1/issues/{self.parent.key}/",
            data=json.dumps({"estimate_minutes": 480, "time_remaining_minutes": 240}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["estimate_minutes"], 480)
        self.assertEqual(r.json()["time_remaining_minutes"], 240)

    def test_an_estimate_can_be_set_on_creation(self):
        r = self.c.post(
            "/api/v1/projects/WEB/issues/",
            data=json.dumps({"summary": "estimada", "estimate_minutes": 120}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["estimate_minutes"], 120)


class APIMemberTests(TestCase):
    """Who is in the project, and with what role.

    ``assignee_id`` has always been validated against membership, but the list
    was only reachable through the web UI — so the rule was one a client could
    not satisfy. Together with the missing user directory, that is why assigning
    was a guess.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_the_lead_is_listed_as_admin(self):
        """They are a PROTECT foreign key, not a membership row, and are
        resolved to admin by aget_role without one."""
        rows = self.c.get("/api/v1/projects/WEB/members/").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "alice")
        self.assertEqual(rows[0]["role"], "admin")
        self.assertTrue(rows[0]["is_lead"])

    def test_add_change_and_remove(self):
        _make_user("mel")
        r = self.c.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "mel", "role": "member"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["role"], "member")
        rows = self.c.get("/api/v1/projects/WEB/members/").json()
        self.assertEqual(len(rows), 2)

        mel = next(m for m in rows if m["username"] == "mel")
        r = self.c.patch(
            f"/api/v1/projects/WEB/members/{mel['id']}/",
            data=json.dumps({"role": "viewer"}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["role"], "viewer")
        self.assertEqual(self.c.delete(f"/api/v1/projects/WEB/members/{mel['id']}/").status_code, 200)
        self.assertEqual(len(self.c.get("/api/v1/projects/WEB/members/").json()), 1)

    def test_re_adding_changes_the_role(self):
        _make_user("mel")
        self.c.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "mel", "role": "member"}),
            content_type="application/json",
        )
        r = self.c.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "mel", "role": "viewer"}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["role"], "viewer")
        self.assertEqual(len(self.c.get("/api/v1/projects/WEB/members/").json()), 2)

    def test_an_unknown_username_is_a_404(self):
        r = self.c.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "nadie"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_an_invalid_role_is_refused(self):
        _make_user("mel")
        r = self.c.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "mel", "role": "dictador"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_the_lead_cannot_be_removed_or_degraded(self):
        """lead is on_delete=PROTECT, so removing them is a 500 waiting to happen."""
        r = self.c.delete(f"/api/v1/projects/WEB/members/{self.user.pk}/")
        self.assertEqual(r.status_code, 409, r.content[:200])
        r = self.c.patch(
            f"/api/v1/projects/WEB/members/{self.user.pk}/",
            data=json.dumps({"role": "viewer"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])

    def test_a_member_cannot_manage_membership(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("mel"), role="member")
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/members/",
            data=json.dumps({"username": "alice"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_membership_of_an_invisible_project_is_a_404(self):
        r = self.c.get("/api/v1/projects/HID/members/")
        self.assertEqual(r.status_code, 404, r.content[:200])


class APIProjectCreateTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_the_creator_becomes_the_lead(self):
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "ops", "name": "Operaciones"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()["key"], "OPS", "the key is upper-cased, as every other one is")
        project = Project.objects.get(key="OPS")
        self.assertEqual(project.lead_id, self.user.pk)

    def test_members_are_added_at_creation(self):
        _make_user("mel")
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "OPS", "name": "Ops", "members": [{"username": "mel"}]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(ProjectMembership.objects.filter(project__key="OPS").count(), 1)

    def test_a_duplicate_key_is_a_409(self):
        _make_project(self.user, key="WEB")
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "WEB", "name": "otro"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])

    def test_a_bad_member_leaves_no_project_behind(self):
        """Otherwise the key is taken by a project nobody can see and cannot be reused."""
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "OPS", "name": "Ops", "members": [{"username": "nadie"}]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertFalse(Project.objects.filter(key="OPS").exists())

    def test_naming_another_lead_needs_a_superuser(self):
        _make_user("mel")
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "OPS", "name": "Ops", "lead": "mel"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_a_superuser_can_name_the_lead(self):
        _make_user("mel")
        self.user.is_superuser = True
        self.user.save()
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "OPS", "name": "Ops", "lead": "mel"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(Project.objects.get(key="OPS").lead.username, "mel")


class APISavedFilterWriteTests(TestCase):
    """The UI could save and delete a search; the API could only read them."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.other = _make_user("bob")
        # The listing drops a saved filter whose query names a project the caller
        # cannot see, so the query below needs a project alice belongs to.
        _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_create_list_delete(self):
        r = self.c.post(
            "/api/v1/filters/",
            data=json.dumps({"name": "mis bugs", "query": "project = WEB AND type = Bug"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["name"], "mis bugs")
        listed = self.c.get("/api/v1/filters/").json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.c.delete(f"/api/v1/filters/{listed[0]['id']}/").status_code, 200)
        self.assertEqual(self.c.get("/api/v1/filters/").json(), [])

    def test_an_empty_name_or_query_is_refused(self):
        for body in ({"name": "", "query": "x"}, {"name": "x", "query": "  "}):
            r = self.c.post("/api/v1/filters/", data=json.dumps(body), content_type="application/json")
            self.assertEqual(r.status_code, 400, r.content[:200])

    def test_another_users_filter_is_a_404(self):
        """Scoped by owner in the query, so ids are not enumerable."""
        theirs = SavedFilter.objects.create(owner=self.other, name="suyo", query="x")
        self.assertEqual(self.c.delete(f"/api/v1/filters/{theirs.pk}/").status_code, 404)
        self.assertTrue(SavedFilter.objects.filter(pk=theirs.pk).exists())

    def test_deleting_twice_is_a_404(self):
        r = self.c.post(
            "/api/v1/filters/",
            data=json.dumps({"name": "x", "query": "y"}),
            content_type="application/json",
        )
        self.assertEqual(self.c.delete(f"/api/v1/filters/{r.json()['id']}/").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/v1/filters/{r.json()['id']}/").status_code, 404)


class APITransitionGraphTests(TestCase):
    """The workflow, so an agent does not have to discover it by being refused."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")
        self.todo = Status.objects.get(name="To Do")
        self.progress = Status.objects.get(name="In Progress")

    def test_an_empty_allowed_next_is_an_open_workflow(self):
        """Status.can_transition_to treats it as "anything goes", so the answer
        is every status, not an empty list that reads as nowhere to go."""
        rows = self.c.get(f"/api/v1/statuses/{self.todo.pk}/transitions/").json()
        self.assertTrue(rows)
        self.assertTrue(all(r["open"] for r in rows))
        self.assertIn("In Progress", [r["name"] for r in rows])

    def test_a_restricted_workflow_lists_only_what_it_allows(self):
        self.todo.allowed_next.set([self.progress])
        rows = self.c.get(f"/api/v1/statuses/{self.todo.pk}/transitions/").json()
        self.assertEqual([r["name"] for r in rows], ["In Progress"])
        self.assertFalse(rows[0]["open"])
        self.assertEqual(rows[0]["next"], ["In Progress"])

    def test_the_listed_transitions_are_actually_allowed(self):
        """The endpoint and the check that rejects a PATCH must agree."""
        self.todo.allowed_next.set([self.progress])
        Status.objects.create(name="Hecho", category="done", order=90)
        listed = {r["name"] for r in self.c.get(f"/api/v1/statuses/{self.todo.pk}/transitions/").json()}
        issue = _make_issue(self.project, self.user)
        for name, expected in (("In Progress", 200), ("Hecho", 400)):
            r = self.c.patch(
                f"/api/v1/issues/{issue.key}/",
                data=json.dumps({"statusName": name})
                if False
                else json.dumps({"status_id": Status.objects.get(name=name).pk}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, expected, f"{name}: {r.content[:200]}")
            self.assertEqual(name in listed, expected == 200)
            issue.refresh_from_db()
            issue.status = self.todo
            issue.save()

    def test_an_unknown_status_is_a_404(self):
        self.assertEqual(self.c.get("/api/v1/statuses/999999/transitions/").status_code, 404)


class APIReadingThePastTests(TestCase):
    """An agent could change an issue and not answer "what changed, who, and when".

    Two logs exist and they are not interchangeable, so each test pins the one it
    is about: HistoryEntry is field-level and written by two code paths, and
    AuditEntry is signal-driven and complete for the who and the when.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _transition(self, name):
        status = Status.objects.get(name=name)
        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data=json.dumps({"status_id": status.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r

    def test_the_changelog_records_a_status_change(self):
        # A status change rather than a summary edit, because HistoryEntry is
        # written by two code paths and the status chokepoint is one of them.
        # The next test is about everything else.
        self._transition("In Progress")
        rows = self.c.get(f"/api/v1/issues/{self.issue.key}/changelog/").json()["items"]
        self.assertTrue(rows)
        self.assertIn("status", [row["field"] for row in rows])
        first = rows[0]
        self.assertEqual(first["actor"], "alice")
        self.assertEqual(first["old_value"], "To Do")
        self.assertEqual(first["new_value"], "In Progress")

    def test_the_changelog_is_incomplete_and_that_is_pinned_here(self):
        """HistoryEntry is written by only two code paths, so a summary edit
        leaves no row at all.

        Pinned deliberately rather than tidied away: the endpoint's docstring tells
        callers the log is incomplete, and this test is what stops that claim
        quietly going stale if a third write path is ever added.
        """
        self.c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data=json.dumps({"summary": "primero"}),
            content_type="application/json",
        )
        self.assertEqual(self.c.get(f"/api/v1/issues/{self.issue.key}/changelog/").json()["count"], 0)

    def test_the_changelog_narrows_to_one_field(self):
        self._transition("In Progress")
        self.c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data=json.dumps({"summary": "primero"}),
            content_type="application/json",
        )
        rows = self.c.get(f"/api/v1/issues/{self.issue.key}/changelog/?field=status").json()["items"]
        self.assertTrue(rows)
        self.assertEqual({r["field"] for r in rows}, {"status"})

    def test_the_changelog_of_an_invisible_issue_is_a_404(self):
        hidden = _make_project(_make_user("stranger"), key="HID")
        other = _make_issue(hidden, hidden.lead)
        r = self.c.get(f"/api/v1/issues/{other.key}/changelog/")
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_the_activity_feed_names_the_actor(self):
        rows = self.c.get("/api/v1/projects/WEB/activity/").json()["items"]
        self.assertTrue(rows)
        self.assertIn(rows[0]["actor"], ("alice", ""))
        self.assertIn(rows[0]["verb"], ("created", "updated"))

    def test_the_activity_feed_can_narrow_to_a_verb(self):
        self.c.patch(
            f"/api/v1/issues/{self.issue.key}/",
            data=json.dumps({"summary": "x"}),
            content_type="application/json",
        )
        updated = self.c.get("/api/v1/projects/WEB/activity/?verb=updated").json()["items"]
        self.assertTrue(updated)
        self.assertTrue(all(r["verb"] == "updated" for r in updated))

    def test_the_activity_feed_of_an_invisible_project_is_a_404(self):
        self.assertEqual(self.c.get("/api/v1/projects/HID/activity/").status_code, 404)


class APIAttachmentTests(TestCase):
    """Attachments were fully implemented in the UI and absent from the API.

    Stored as base64 in a TextField, which is why the body is a data: URL: the
    same shape the template builds for a download link, so a client that can
    render an img can render the response without decoding anything.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")
        self.payload = base64.b64encode(b"hello attachment").decode()

    def _upload(self, **overrides):
        body = {"filename": "nota.txt", "data": self.payload, "content_type": "text/plain"}
        body.update(overrides)
        return self.c.post(
            f"/api/v1/issues/{self.issue.key}/attachments/",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_upload_list_and_fetch(self):
        r = self._upload()
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["filename"], "nota.txt")
        # Size is the decoded length, not the base64 length.
        self.assertEqual(r.json()["size"], len(b"hello attachment"))

        listed = self.c.get(f"/api/v1/issues/{self.issue.key}/attachments/").json()
        self.assertEqual(listed["count"], 1)
        # The listing must not carry the bytes, or a page of attachments is 6.7 MB
        # of base64 to answer "is there a screenshot on this".
        self.assertNotIn("dataUrl", listed["items"][0])

        fetched = self.c.get(f"/api/v1/attachments/{r.json()['id']}/")
        self.assertEqual(fetched.status_code, 200, fetched.content[:200])
        self.assertTrue(fetched.json()["dataUrl"].startswith("data:text/plain;base64,"))
        self.assertEqual(fetched.json()["issueIdOrKey"], self.issue.key)

    def test_bad_base64_is_a_400_not_a_500(self):
        r = self._upload(data="not base64 at all!!")
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("base64", r.json()["detail"])

    def test_the_declared_cap_is_actually_reachable(self):
        """5 MB of raw bytes is 6.7 MB of base64, over Django's 2.5 MB default.

        So the cap the attachment code advertises was unreachable: a file over
        2.5 MB was refused by Django before any handler ran, with a bare 400 that
        named neither the limit nor the reason — in the web UI as much as here.
        This test uploads a file over the old default to keep DATA_UPLOAD_MAX_MEMORY_SIZE
        honest about the cap above.
        """
        from django.conf import settings

        self.assertGreater(
            settings.DATA_UPLOAD_MAX_MEMORY_SIZE,
            5 * 1024 * 1024 * 4 // 3,
            "the request limit is below the encoded size of the attachment cap, so the cap is a lie",
        )
        # And the other direction: the request limit must not sit far above the
        # cap, or the point of the cap is to check bytes in code rather than to
        # keep a large body out of memory.
        self.assertLess(
            settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 16 * 1024 * 1024, "requests are not bounded near the cap"
        )

    def test_a_filename_is_required(self):
        self.assertEqual(self._upload(filename="  ").status_code, 400)

    def test_an_oversized_attachment_is_refused(self):
        big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
        r = self._upload(data=big)
        # 400 and not 413: django-ninja only maps a fixed set of codes onto its
        # {"detail": ...} envelope, and 413 came back as Django's HTML 400 page.
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("máximo", r.json()["detail"])

    def test_a_viewer_cannot_upload(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("vic"), role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        body = json.dumps({"filename": "x.txt", "data": self.payload})
        r = c2.post(
            f"/api/v1/issues/{self.issue.key}/attachments/",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_an_attachment_on_an_invisible_issue_is_a_404(self):
        """The bytes are in the response, so the scope check is not optional."""
        hidden = _make_project(_make_user("stranger"), key="HID")
        other = _make_issue(hidden, hidden.lead)
        from issues.models import Attachment

        theirs = Attachment.objects.create(
            issue=other,
            filename="secreto.txt",
            size=3,
            data=self.payload,
            uploaded_by=hidden.lead,
        )
        r = self.c.get(f"/api/v1/attachments/{theirs.pk}/")
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertNotIn(self.payload, r.content.decode())

    def test_delete_removes_the_bytes(self):
        from issues.models import Attachment

        attachment_id = self._upload().json()["id"]
        self.assertEqual(self.c.delete(f"/api/v1/attachments/{attachment_id}/").status_code, 200)
        self.assertEqual(Attachment.objects.filter(pk=attachment_id).count(), 0)
        self.assertEqual(self.c.get(f"/api/v1/attachments/{attachment_id}/").status_code, 404)


class APINotificationTests(TestCase):
    """The caller's own notifications, and nobody else's."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.other = _make_user("bob")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _notify(self, recipient, text="hola"):
        return Notification.objects.create(recipient=recipient, actor=self.user, kind="mention", text=text)

    def test_listing_is_scoped_to_the_caller(self):
        self._notify(self.user, "mía")
        self._notify(self.other, "suyo")
        listed = self.c.get("/api/v1/notifications/").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["text"], "mía")

    def test_unread_only(self):
        mine = self._notify(self.user)
        self._notify(self.user, "leída").read = True
        from django.db.models import Q

        Notification.objects.filter(Q(pk=mine.pk)).update(read=False)
        self._notify(self.user, "otra").read = True
        unread = self.c.get("/api/v1/notifications/?unread_only=true").json()
        self.assertGreaterEqual(unread["count"], 1)
        self.assertTrue(all(n["read"] is False for n in unread["items"]))

    def test_marking_read_with_no_ids_marks_all_of_the_callers(self):
        self._notify(self.user, "a")
        self._notify(self.user, "b")
        theirs = self._notify(self.other, "suyo")
        r = self.c.post("/api/v1/notifications/read/", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["marked_read"], 2)
        theirs.refresh_from_db()
        self.assertFalse(theirs.read, "another user's notification was marked read")

    def test_marking_some_ids_ignores_the_others(self):
        mine = self._notify(self.user, "mía")
        theirs = self._notify(self.other, "suyo")
        r = self.c.post(
            "/api/v1/notifications/read/",
            data=json.dumps({"ids": [mine.pk, theirs.pk]}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["marked_read"], 1)
        theirs.refresh_from_db()
        self.assertFalse(theirs.read)

    def test_marking_read_twice_is_a_no_op(self):
        self._notify(self.user)
        self.c.post("/api/v1/notifications/read/", data="{}", content_type="application/json")
        r = self.c.post("/api/v1/notifications/read/", data="{}", content_type="application/json")
        self.assertEqual(r.json()["marked_read"], 0)


class APIKeyManagementTests(TestCase):
    """An agent that had been handed a key could not mint another nor revoke one."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.other = _make_user("bob")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_mint_list_and_revoke(self):
        r = self.c.post(
            "/api/v1/api-keys/",
            data=json.dumps({"name": "para el MCP"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertTrue(body["token"])
        self.assertTrue(body["prefix"])
        # The plaintext is shown once, and the endpoint has to say so or a client
        # that drops it will look for it later.
        self.assertIn("only time", body["warning"])

        listed = self.c.get("/api/v1/api-keys/").json()
        self.assertEqual(listed["count"], 1)
        # The secret must never come back on the list.
        self.assertNotIn(body["token"], json.dumps(listed))
        self.assertTrue(listed["items"][0]["active"])

        revoked = self.c.delete(f"/api/v1/api-keys/{body['id']}/")
        self.assertEqual(revoked.status_code, 200)
        listed = self.c.get("/api/v1/api-keys/").json()
        # Still listed, as inactive: a key that vanished would be indistinguishable
        # from somebody having deleted the account's keys.
        self.assertEqual(listed["count"], 1)
        self.assertFalse(listed["items"][0]["active"])
        self.assertTrue(listed["items"][0]["revoked_at"])

    def test_the_minted_key_actually_authenticates(self):
        token = self.c.post(
            "/api/v1/api-keys/",
            data=json.dumps({"name": "x"}),
            content_type="application/json",
        ).json()["token"]
        bearer = Client()
        r = bearer.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["username"], "alice")

    def test_a_revoked_key_stops_authenticating(self):
        created = self.c.post(
            "/api/v1/api-keys/",
            data=json.dumps({"name": "x"}),
            content_type="application/json",
        ).json()
        self.c.delete(f"/api/v1/api-keys/{created['id']}/")
        bearer = Client()
        r = bearer.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + created["token"])
        self.assertEqual(r.status_code, 401, r.content[:200])

    def test_another_users_key_is_a_404(self):
        theirs, _plaintext = APIKey.create_for(owner=self.other, name="suyo")
        r = self.c.delete(f"/api/v1/api-keys/{theirs.pk}/")
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertEqual(APIKey.objects.filter(pk=theirs.pk).count(), 1)

    def test_a_name_is_required(self):
        r = self.c.post(
            "/api/v1/api-keys/",
            data=json.dumps({"name": " "}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])


class APITeamTests(TestCase):
    """A @team:slug mention resolves to a team, and nothing could read one."""

    def setUp(self):
        _seed_lookups()
        self.admin = _make_user("root")
        self.admin.is_superuser = True
        self.admin.save()
        self.plain = _make_user("mel")
        self.c = Client()
        self.c.login(username="root", password="pw")

    def test_create_with_members_and_read_back(self):
        r = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "oncall", "name": "Oncall", "members": ["mel"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["members"], ["mel"])
        listed = self.c.get("/api/v1/teams/").json()
        self.assertEqual(listed["count"], 1)

    def test_an_unknown_member_fails_the_whole_team(self):
        """A team with three of its four members would mention somebody silently."""
        r = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "oncall", "name": "Oncall", "members": ["mel", "nadie"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])
        self.assertEqual(Team.objects.count(), 0)

    def test_patch_replaces_the_members(self):
        _make_user("bob")
        self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "oncall", "name": "Oncall"}),
            content_type="application/json",
        )
        r = self.c.patch(
            f"/api/v1/teams/{Team.objects.get().pk}/",
            data=json.dumps({"slug": "oncall", "name": "Oncall", "members": ["mel", "bob"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["members"], ["bob", "mel"])

    def test_anyone_may_list_but_only_a_superuser_may_write(self):
        self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "oncall", "name": "Oncall"}),
            content_type="application/json",
        )
        c2 = Client()
        c2.login(username="mel", password="pw")
        # A team is a broadcast list; membership in it is not a secret.
        self.assertEqual(c2.get("/api/v1/teams/").status_code, 200)
        r = c2.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "nuevo", "name": "Nuevo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_delete(self):
        self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "oncall", "name": "Oncall"}),
            content_type="application/json",
        )
        self.assertEqual(self.c.delete(f"/api/v1/teams/{Team.objects.get().pk}/").status_code, 200)
        self.assertEqual(Team.objects.count(), 0)


class APIUserAdminTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.root = _make_user("root")
        self.root.is_superuser = True
        self.root.save()
        self.plain = _make_user("mel")
        self.c = Client()
        self.c.login(username="root", password="pw")

    def test_the_user_directory_leaks_no_privilege_to_a_normal_caller(self):
        """The counterpart to /users/search/, which is open and says nothing about
        privilege. This one includes it, so it is superuser-only."""
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.get("/api/v1/admin/users/")
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_create_and_deactivate(self):
        r = self.c.post(
            "/api/v1/admin/users/",
            data=json.dumps({"username": "nuevo", "email": "n@x.com", "display_name": "Nuevo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertFalse(r.json()["usable_password"])
        user = User.objects.get(username="nuevo")
        self.assertTrue(user.is_active)

        # Named delete, and it is a soft one: reporter and assignee are PROTECT,
        # so a real delete would fail on any instance where the person filed
        # anything.
        r = self.c.delete(f"/api/v1/admin/users/{user.pk}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertFalse(r.json()["deleted"])
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertTrue(User.objects.filter(username="nuevo").exists())

    def test_a_deactivated_user_cannot_authenticate(self):
        r = self.c.post(
            "/api/v1/admin/users/",
            data=json.dumps({"username": "nuevo"}),
            content_type="application/json",
        )
        user = User.objects.get(pk=r.json()["id"])
        user.set_password("pw")
        user.save()
        c2 = Client()
        self.assertEqual(c2.login(username="nuevo", password="pw"), True)
        self.c.delete(f"/api/v1/admin/users/{user.pk}/")
        c3 = Client()
        self.assertFalse(c3.login(username="nuevo", password="pw"))

    def test_a_duplicate_username_is_a_409(self):
        r = self.c.post(
            "/api/v1/admin/users/",
            data=json.dumps({"username": "mel"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])

    def test_you_cannot_deactivate_yourself(self):
        r = self.c.delete(f"/api/v1/admin/users/{self.root.pk}/")
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.root.refresh_from_db()
        self.assertTrue(self.root.is_active)

    def test_patch_privileges(self):
        r = self.c.patch(
            f"/api/v1/admin/users/{self.plain.pk}/",
            data=json.dumps({"is_superuser": True}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.plain.refresh_from_db()
        self.assertTrue(self.plain.is_superuser)

    def test_a_normal_caller_cannot_reach_any_of_it(self):
        c2 = Client()
        c2.login(username="mel", password="pw")
        self.assertEqual(c2.delete(f"/api/v1/admin/users/{self.root.pk}/").status_code, 403)
        r = c2.post(
            "/api/v1/admin/users/",
            data=json.dumps({"username": "x"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])


class APIWorkflowVocabularyTests(TestCase):
    """Status, priority, issue type and label, all four writable.

    They were readable and writable only through a superuser-only web editor. Two
    properties matter and a naive implementation gets both wrong:

    - They are on_delete=PROTECT, so deleting one in use must be refused with a
      count rather than orphaning every issue that carried it.
    - Status.allowed_next being empty means the workflow is OPEN, not closed, so
      a status must never lose its transitions by accident.
    """

    def setUp(self):
        _seed_lookups()
        self.root = _make_user("root")
        self.root.is_superuser = True
        self.root.save()
        self.plain = _make_user("mel")
        self.c = Client()
        self.c.login(username="root", password="pw")
        self.c2 = Client()
        self.c2.login(username="mel", password="pw")

    def test_status_create_and_restrict_the_workflow(self):
        r = self.c.post(
            "/api/v1/statuses/",
            data=json.dumps({"name": "Blocked", "category": "in_progress"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        blocked = r.json()
        todo = Status.objects.get(name="To Do")
        progress = Status.objects.get(name="In Progress")

        r = self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"allowed_next": [progress.pk]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(sorted(todo.allowed_next.values_list("name", flat=True)), ["In Progress"])
        # The restricted workflow must actually refuse, and the new status must be
        # unreachable: that is the whole point of the list.
        issue = _make_issue(self.project(), self.root)
        refused = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"status_id": blocked["id"]}),
            content_type="application/json",
        )
        self.assertEqual(refused.status_code, 400, refused.content[:200])

    def project(self):
        from projects.models import Project

        return Project.objects.create(key="WF", name="Workflow", lead=self.root)

    def test_an_empty_allowed_next_opens_the_workflow_rather_than_closing_it(self):
        """A status that loses its rules becomes permissive, so this is worth
        pinning: the opposite of what a reader would guess."""
        todo = Status.objects.get(name="To Do")
        progress = Status.objects.get(name="In Progress")
        todo.allowed_next.set([progress])
        self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"allowed_next": []}),
            content_type="application/json",
        )
        self.assertEqual(todo.allowed_next.count(), 0)
        rows = self.c.get(f"/api/v1/statuses/{todo.pk}/transitions/").json()
        self.assertTrue(rows, "an open workflow should list every status, not none")

    def test_patch_replaces_the_transitions_rather_than_adding(self):
        todo = Status.objects.get(name="To Do")
        progress = Status.objects.get(name="In Progress")
        done = Status.objects.get(name="Done")
        self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"allowed_next": [progress.pk, done.pk]}),
            content_type="application/json",
        )
        self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"allowed_next": [done.pk]}),
            content_type="application/json",
        )
        self.assertEqual(list(todo.allowed_next.values_list("name", flat=True)), ["Done"])

    def test_a_nonexistent_transition_target_is_refused(self):
        todo = Status.objects.get(name="To Do")
        r = self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"allowed_next": [999999]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_omitting_allowed_next_leaves_the_workflow_alone(self):
        todo = Status.objects.get(name="To Do")
        progress = Status.objects.get(name="In Progress")
        todo.allowed_next.set([progress])
        self.c.patch(
            f"/api/v1/statuses/{todo.pk}/",
            data=json.dumps({"name": "Renamed"}),
            content_type="application/json",
        )
        self.assertEqual(todo.allowed_next.count(), 1, "a rename must not open or close the workflow")

    def test_deleting_a_status_in_use_is_refused_with_a_count(self):
        todo = Status.objects.get(name="To Do")
        _make_issue(self.project(), self.root)
        in_use = Issue.objects.filter(status=todo).count()
        self.assertGreaterEqual(in_use, 1)
        r = self.c.delete(f"/api/v1/statuses/{todo.pk}/")
        self.assertEqual(r.status_code, 409, r.content[:200])
        self.assertIn(str(in_use), r.json()["detail"])
        self.assertTrue(Status.objects.filter(pk=todo.pk).exists())

    def test_deleting_an_unused_status_works(self):
        r = self.c.post(
            "/api/v1/statuses/",
            data=json.dumps({"name": "Temporal", "category": "todo"}),
            content_type="application/json",
        )
        self.assertEqual(self.c.delete(f"/api/v1/statuses/{r.json()['id']}/").status_code, 200)

    def test_priorities_and_types_follow_the_same_rules(self):
        priority = self.c.post(
            "/api/v1/priorities/",
            data=json.dumps({"name": "Urgente", "weight": 99}),
            content_type="application/json",
        )
        self.assertEqual(priority.status_code, 200, priority.content[:200])
        issue = _make_issue(self.project(), self.root)
        self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"priority_id": priority.json()["id"]}),
            content_type="application/json",
        )
        r = self.c.delete(f"/api/v1/priorities/{priority.json()['id']}/")
        self.assertEqual(r.status_code, 409, r.content[:200])

        itype = self.c.post(
            "/api/v1/issue-types/",
            data=json.dumps({"name": "Chore"}),
            content_type="application/json",
        )
        self.assertEqual(itype.status_code, 200, itype.content[:200])
        self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"issue_type_id": itype.json()["id"]}),
            content_type="application/json",
        )
        r = self.c.delete(f"/api/v1/issue-types/{itype.json()['id']}/")
        self.assertEqual(r.status_code, 409, r.content[:200])

    def test_changing_an_issue_type_by_patch_actually_applies(self):
        """It used to be accepted and dropped.

        issue_type_id was missing from _PATCHABLE_FIELDS, so a PATCH naming it was
        validated by the schema, answered 200, and changed nothing — while
        creating an issue with the same field worked, because that path does not go
        through the list. The asymmetry is what hid it.
        """
        issue = _make_issue(self.project(), self.root)
        created = self.c.post(
            "/api/v1/issue-types/",
            data=json.dumps({"name": "Chore"}),
            content_type="application/json",
        )
        r = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"issue_type_id": created.json()["id"]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["type"], "Chore")
        issue.refresh_from_db()
        self.assertEqual(issue.issue_type.name, "Chore")

    def test_a_bogus_issue_type_is_a_400(self):
        issue = _make_issue(self.project(), self.root)
        r = self.c.patch(
            f"/api/v1/issues/{issue.key}/",
            data=json.dumps({"issue_type_id": 999999}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_a_duplicate_name_is_a_409(self):
        for path, body in (
            ("/api/v1/statuses/", {"name": "To Do"}),
            ("/api/v1/priorities/", {"name": "High"}),
            ("/api/v1/issue-types/", {"name": "Task"}),
        ):
            r = self.c.post(path, data=json.dumps(body), content_type="application/json")
            self.assertEqual(r.status_code, 409, f"{path} -> {r.content[:200]}")

    def test_an_invalid_category_is_refused(self):
        r = self.c.post(
            "/api/v1/statuses/",
            data=json.dumps({"name": "X", "category": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_a_normal_user_cannot_touch_any_of_it(self):
        for path in ("/api/v1/statuses/", "/api/v1/priorities/", "/api/v1/issue-types/"):
            r = self.c2.post(path, data=json.dumps({"name": "suyo"}), content_type="application/json")
            self.assertEqual(r.status_code, 403, f"{path} -> {r.content[:200]}")
        todo = Status.objects.get(name="To Do")
        self.assertEqual(
            self.c2.patch(
                f"/api/v1/statuses/{todo.pk}/",
                data=json.dumps({"name": "suyo"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(self.c2.delete(f"/api/v1/statuses/{todo.pk}/").status_code, 403)

    def test_an_over_long_name_is_refused_and_not_a_500(self):
        """A DataError from Postgres is a 500 with a traceback, or an opaque 500
        with DEBUG off, for what is a typo.

        The limits are per-column and not uniform — Priority.name is 20 while
        Status.name is 40 — so this walks several and pins the status code as
        well as the fact that a limit is declared at all.
        """
        for path, body in (
            ("/api/v1/priorities/", {"name": "x" * 21}),  # name is varchar(20)
            ("/api/v1/statuses/", {"name": "x" * 41}),  # name is varchar(40)
            ("/api/v1/issue-types/", {"name": "x" * 41}),
            ("/api/v1/labels/", {"name": "x" * 41}),
            ("/api/v1/projects/WEB/epics/", {"name": "x" * 201}),
            ("/api/v1/projects/WEB/custom-fields/", {"name": "x" * 81}),
            ("/api/v1/projects/WEB/webhooks/", {"name": "x" * 81}),
            ("/api/v1/teams/", {"name": "x" * 121, "slug": "ok"}),
            ("/api/v1/filters/", {"name": "x" * 121, "query": "y"}),
            ("/api/v1/api-keys/", {"name": "x" * 81}),
        ):
            r = self.c.post(path, data=json.dumps(body), content_type="application/json")
            self.assertIn(
                r.status_code,
                (400, 422),
                f"{path} answered {r.status_code} for an over-long value: {r.content[:200]}",
            )
            self.assertNotIn(b"Traceback", r.content, f"{path} leaked a traceback")

    def test_a_name_at_the_limit_is_accepted(self):
        """The limit has to be the column's, not an approximation of it."""
        r = self.c.post(
            "/api/v1/priorities/",
            data=json.dumps({"name": "x" * 20}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_renaming_a_label_and_its_collision(self):
        from issues.models import Label

        Label.objects.get_or_create(name="vieja")
        label = Label.objects.get(name="vieja")
        Label.objects.get_or_create(name="ocupada")
        r = self.c.patch(
            f"/api/v1/labels/{label.pk}/", data=json.dumps({"name": "nueva"}), content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["name"], "nueva")
        r = self.c.patch(
            f"/api/v1/labels/{label.pk}/",
            data=json.dumps({"name": "ocupada"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])


class APICustomFieldTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _define(self, **body):
        body.setdefault("name", "Severidad")
        r = self.c.post(
            "/api/v1/projects/WEB/custom-fields/",
            data=json.dumps(body),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        return r.json()

    def test_define_list_and_set_values(self):
        field = self._define(type="select", options="baja, media, alta")
        self.assertEqual(field["options"], ["baja", "media", "alta"])
        listed = self.c.get("/api/v1/projects/WEB/custom-fields/").json()
        self.assertEqual(listed["count"], 1)

        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {"severidad": "alta"}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["customFields"], {"severidad": "alta"})
        read = self.c.get(f"/api/v1/issues/{self.issue.key}/custom-fields/").json()
        self.assertEqual(read["customFields"]["severidad"], "alta")

    def test_a_null_value_removes_the_key(self):
        self._define()
        self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {"severidad": "alta"}}),
            content_type="application/json",
        )
        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {"severidad": None}}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["customFields"], {})

    def test_an_unknown_slug_is_refused(self):
        """A typo would otherwise leave a value nothing will ever read."""
        self._define()
        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {"severida": "alta"}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("severida", r.json()["detail"])

    def test_a_duplicate_slug_is_a_409(self):
        self._define()
        r = self.c.post(
            "/api/v1/projects/WEB/custom-fields/",
            data=json.dumps({"name": "Severidad"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])

    def test_an_invalid_type_is_refused(self):
        r = self.c.post(
            "/api/v1/projects/WEB/custom-fields/",
            data=json.dumps({"name": "X", "type": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_deleting_reports_the_orphaned_values(self):
        field = self._define()
        self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {field["slug"]: "alta"}}),
            content_type="application/json",
        )
        r = self.c.delete(f"/api/v1/projects/WEB/custom-fields/{field['id']}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        # The values stay in the JSON as keys nothing reads; saying how many is
        # what lets the caller decide whether that matters.
        self.assertEqual(r.json()["issues_that_still_carry_its_values"], 1)

    def test_a_viewer_cannot_define_fields(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("vic"), role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/custom-fields/",
            data=json.dumps({"name": "suyo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_a_field_from_another_project_is_not_writable(self):
        from projects.models import CustomFieldDef

        elsewhere = _make_project(_make_user("stranger"), key="HID")
        theirs = CustomFieldDef.objects.create(project=elsewhere, name="Ajena", slug="ajena")
        r = self.c.patch(
            f"/api/v1/issues/{self.issue.key}/custom-fields/",
            data=json.dumps({"customFields": {"ajena": "x"}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertEqual(self.c.delete(f"/api/v1/projects/WEB/custom-fields/{theirs.pk}/").status_code, 404)


class APIWebhookTests(TestCase):
    """Configuration with no effect, and that is the point.

    jirrabit dispatches to in-process stub actions that mostly log; there is no
    outbound HTTP. The definition is still real, survives, and is what an operator
    needs to see.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_create_list_patch_delete(self):
        r = self.c.post(
            "/api/v1/projects/WEB/webhooks/",
            data=json.dumps({"name": "Slack", "event": "issue.updated", "state_filter": "Done, Closed"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["project"], "WEB")
        self.assertEqual(body["state_filter"], ["Done", "Closed"])

        listed = self.c.get("/api/v1/projects/WEB/webhooks/").json()
        self.assertEqual(listed["count"], 1)

        r = self.c.patch(
            f"/api/v1/projects/WEB/webhooks/{body['id']}/",
            data=json.dumps({"name": "Slack", "active": False}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["active"], False)
        self.assertEqual(self.c.delete(f"/api/v1/projects/WEB/webhooks/{body['id']}/").status_code, 200)
        self.assertEqual(self.c.get("/api/v1/projects/WEB/webhooks/").json()["count"], 0)

    def test_another_projects_hook_is_a_404(self):
        from projects.models import Webhook

        elsewhere = _make_project(_make_user("stranger"), key="HID")
        theirs = Webhook.objects.create(project=elsewhere, name="Suyo")
        self.assertEqual(self.c.delete(f"/api/v1/projects/WEB/webhooks/{theirs.pk}/").status_code, 404)
        r = self.c.patch(
            f"/api/v1/projects/WEB/webhooks/{theirs.pk}/",
            data=json.dumps({"name": "mio"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_a_member_cannot_configure_webhooks(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("mel"), role="member")
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/webhooks/",
            data=json.dumps({"name": "suyo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])


class APIWikiTests(TestCase):
    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_an_unwritten_page_is_empty_rather_than_404(self):
        """One page per project, so unwritten is a normal state for most of them."""
        r = self.c.get("/api/v1/projects/WEB/wiki/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["body"], "")

    def test_write_then_read(self):
        r = self.c.put(
            "/api/v1/projects/WEB/wiki/",
            data=json.dumps({"body": "# Onboarding"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["body"], "# Onboarding")
        self.assertEqual(r.json()["updated_by"], "alice")
        self.assertEqual(self.c.get("/api/v1/projects/WEB/wiki/").json()["body"], "# Onboarding")

    def test_put_replaces_the_whole_page(self):
        self.c.put(
            "/api/v1/projects/WEB/wiki/",
            data=json.dumps({"body": "primera"}),
            content_type="application/json",
        )
        r = self.c.put(
            "/api/v1/projects/WEB/wiki/",
            data=json.dumps({"body": "segunda"}),
            content_type="application/json",
        )
        self.assertEqual(r.json()["body"], "segunda")

    def test_a_member_cannot_write(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("mel"), role="member")
        c2 = Client()
        c2.login(username="mel", password="pw")
        r = c2.put(
            "/api/v1/projects/WEB/wiki/",
            data=json.dumps({"body": "suyo"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])
        # Reading is fine: every member can see the project.
        self.assertEqual(c2.get("/api/v1/projects/WEB/wiki/").status_code, 200)

    def test_an_invisible_projects_wiki_is_a_404(self):
        self.assertEqual(self.c.get("/api/v1/projects/HID/wiki/").status_code, 404)


class APIBoardPlacementTests(TestCase):
    """An agent could change a card's status but never put it anywhere.

    These are the writes the board's drag performs, exposed over HTTP and routed
    through the same helpers the drag uses — so a move from the API renumbers the
    column it leaves and notifies watchers exactly as a drag does. Assigning a rank
    directly would break the dense 0..n-1 invariant the board depends on.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.todo = Status.objects.get(name="To Do")
        self.progress = Status.objects.get(name="In Progress")
        self.issue = _make_issue_in(self.project, self.user, self.todo)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _ranks(self, status=None):
        return list(
            Issue.objects.filter(project=self.project, status=status or self.todo)
            .order_by("rank", "-updated_at")
            .values_list("rank", flat=True)
        )

    def test_move_appends_and_closes_the_gap_behind(self):
        self.issue.save()
        other = _make_issue_in(self.project, self.user, self.todo)
        other.save()
        self.assertEqual(self._ranks(), [0, 1])
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/move/",
            data=json.dumps({"status_id": self.progress.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["status"], "In Progress")
        # The column it left is dense again, and the destination has it at the end.
        self.assertEqual(self._ranks(), [0])
        self.assertEqual(self._ranks(self.progress), [0])

    def test_move_to_a_position_renumbers_the_whole_column(self):
        first = _make_issue_in(self.project, self.user, self.todo)
        second = _make_issue_in(self.project, self.user, self.todo)
        first.save()
        second.save()
        keys = list(
            Issue.objects.filter(project=self.project, status=self.todo)
            .order_by("rank")
            .values_list("key", flat=True)
        )
        # Put the last card at the front.
        last = keys[-1]
        r = self.c.post(
            f"/api/v1/issues/{last}/move/",
            data=json.dumps({"rank": 0}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        order = list(
            Issue.objects.filter(project=self.project, status=self.todo)
            .order_by("rank")
            .values_list("key", flat=True)
        )
        self.assertEqual(order[0], last)
        self.assertEqual(self._ranks(), [0, 1, 2], "the column must stay dense")

    def test_a_rank_beyond_the_end_is_clamped_not_an_error(self):
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/move/",
            data=json.dumps({"rank": 99}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_an_illegal_transition_is_refused(self):
        done = Status.objects.get(name="Done")
        # A non-empty allowed_next that omits Done. Clear() would mean the
        # opposite — an open workflow, where every transition is allowed.
        self.todo.allowed_next.set([self.progress])
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/move/",
            data=json.dumps({"status_id": done.pk}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status_id, self.todo.pk, "the refused move must not apply")

    def test_a_viewer_cannot_move(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("vic"), role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        r = c2.post(
            f"/api/v1/issues/{self.issue.key}/move/",
            data=json.dumps({"rank": 0}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_reorder_takes_the_whole_column(self):
        issues = [_make_issue_in(self.project, self.user, self.todo) for _ in range(3)]
        # The whole column, setUp's issue included.
        keys = [self.issue.key] + [i.key for i in issues]
        r = self.c.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": self.todo.pk, "keys": list(reversed(keys))}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(
            list(Issue.objects.filter(status=self.todo).order_by("rank").values_list("key", flat=True)),
            list(reversed(keys)),
        )
        self.assertEqual(self._ranks(), [0, 1, 2, 3])

    def test_a_partial_reorder_means_these_to_the_top(self):
        """A stale tab sends fewer keys than the column has, and refusing that
        would make the board unusable the moment someone else moves a card."""
        issues = [_make_issue_in(self.project, self.user, self.todo) for _ in range(2)]
        r = self.c.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": self.todo.pk, "keys": [issues[1].key]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(
            list(Issue.objects.filter(status=self.todo).order_by("rank").values_list("key", flat=True)),
            [issues[1].key, self.issue.key, issues[0].key],
        )
        self.assertEqual(self._ranks(), [0, 1, 2])

    def test_reorder_rejects_a_key_from_another_project(self):
        elsewhere = _make_project(_make_user("stranger"), key="HID")
        foreign = _make_issue(elsewhere, elsewhere.lead)
        r = self.c.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": self.todo.pk, "keys": [foreign.key]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn(foreign.key, r.json()["detail"])

    def test_reorder_deduplicates_repeated_keys(self):
        r = self.c.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": self.todo.pk, "keys": [self.issue.key, self.issue.key]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["moved"], 1)
        self.assertEqual(self._ranks(), [0])

    def test_reorder_of_an_unknown_status_is_refused(self):
        r = self.c.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": 9999, "keys": [self.issue.key]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_a_viewer_cannot_reorder(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("vic"), role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        r = c2.post(
            "/api/v1/projects/WEB/board/reorder/",
            data=json.dumps({"status_id": self.todo.pk, "keys": [self.issue.key]}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_bulk_update_changes_several_and_reports_the_rest(self):
        issues = [_make_issue_in(self.project, self.user, self.todo) for _ in range(3)]
        for i in issues:
            i.save()
        r = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps(
                {"keys": [i.key for i in issues], "action": "status", "value": str(self.progress.pk)}
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(len(r.json()["changed"]), 3)
        self.assertEqual(Issue.objects.filter(status=self.progress).count(), 3)

    def test_bulk_status_reports_the_ones_it_could_not_do(self):
        """One illegal transition must not half-apply the rest, and the caller has
        to be told which ones did not happen."""
        done = Status.objects.get(name="Done")
        # From To Do, Done is allowed. From In Progress it is not. The transition
        # table is per status, not per issue, so the two rows have to start in
        # different columns for one call to have two different answers.
        self.todo.allowed_next.set([done])
        self.progress.allowed_next.set([self.todo])
        blocked = _make_issue_in(self.project, self.user, self.progress, summary="Refused")
        r = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps(
                {"keys": [self.issue.key, blocked.key], "action": "status", "value": str(done.pk)}
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["skipped"], [blocked.key])
        self.assertIn("note", body)

    def test_bulk_refuses_delete(self):
        """The one action that cannot be undone belongs behind the MCP's
        two-step confirmation, not in a call that takes a list."""
        r = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps({"keys": [self.issue.key], "action": "delete"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("deleteJiraIssue", r.json()["detail"])
        self.assertTrue(Issue.objects.filter(pk=self.issue.pk).exists())

    def test_bulk_labels_add_and_remove(self):
        added = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps({"keys": [self.issue.key], "action": "label_add", "value": "nueva"}),
            content_type="application/json",
        )
        self.assertEqual(added.status_code, 200, added.content[:200])
        self.issue.refresh_from_db()
        self.assertIn("nueva", [held.name for held in self.issue.labels.all()])
        removed = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps({"keys": [self.issue.key], "action": "label_remove", "value": "nueva"}),
            content_type="application/json",
        )
        self.assertEqual(removed.status_code, 200, removed.content[:200])
        self.issue.refresh_from_db()
        self.assertNotIn("nueva", [held.name for held in self.issue.labels.all()])

    def test_bulk_rejects_a_key_from_another_project(self):
        elsewhere = _make_project(_make_user("stranger"), key="HID")
        other = _make_issue(elsewhere, elsewhere.lead)
        r = self.c.post(
            "/api/v1/projects/WEB/board/bulk-update/",
            data=json.dumps({"keys": [other.key], "action": "status", "value": str(self.progress.pk)}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        other.refresh_from_db()
        self.assertNotEqual(other.status_id, self.progress.pk)


class APIUserSurfaceTests(TestCase):
    """Pins, timers, snoozes, reactions and branch links: each existed in the UI
    and had no HTTP path at all."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.issue = _make_issue(self.project, self.user)
        self.comment = Comment.objects.create(issue=self.issue, author=self.user, body="reacciona")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_pins_are_idempotent_and_need_exactly_one_target(self):
        r = self.c.post(
            "/api/v1/pins/", data=json.dumps({"issue": self.issue.key}), content_type="application/json"
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["issue"], self.issue.key)
        again = self.c.post(
            "/api/v1/pins/", data=json.dumps({"issue": self.issue.key}), content_type="application/json"
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.c.get("/api/v1/pins/").json()["count"], 1)
        for body in ({}, {"issue": self.issue.key, "project": "WEB"}):
            r = self.c.post("/api/v1/pins/", data=json.dumps(body), content_type="application/json")
            self.assertEqual(r.status_code, 400, f"{body} -> {r.content[:200]}")

    def test_unpinning_somebody_elses_pin_is_a_404(self):
        from issues.models import Pin

        theirs = Pin.objects.create(user=_make_user("bob"), issue=self.issue)
        self.assertEqual(self.c.delete(f"/api/v1/pins/{theirs.pk}/").status_code, 404)
        self.assertEqual(Pin.objects.filter(pk=theirs.pk).count(), 1)

    def test_a_timer_logs_its_minutes_when_stopped(self):
        from django.utils import timezone

        from issues.models import Timer

        started = self.c.post(
            f"/api/v1/issues/{self.issue.key}/timer/start/", data="{}", content_type="application/json"
        )
        self.assertEqual(started.status_code, 200, started.content[:200])
        self.assertEqual(self.c.get(f"/api/v1/issues/{self.issue.key}/timer/").status_code, 200)
        # A second timer on the same user is refused, wherever it would be: the
        # timer is the user's own and moving it would lose the minutes so far.
        second = self.c.post(
            f"/api/v1/issues/{self.issue.key}/timer/start/", data="{}", content_type="application/json"
        )
        self.assertEqual(second.status_code, 409, second.content[:200])

        # Backdate it so the minutes are deterministic.
        timer = Timer.objects.get(user=self.user, issue=self.issue)
        Timer.objects.filter(pk=timer.pk).update(started_at=timezone.now() - timezone.timedelta(minutes=95))
        stopped = self.c.post(
            f"/api/v1/issues/{self.issue.key}/timer/stop/", data="{}", content_type="application/json"
        )
        self.assertEqual(stopped.status_code, 200, stopped.content[:200])
        self.assertEqual(stopped.json()["minutes"], 95)
        self.assertEqual(Timer.objects.filter(user=self.user).count(), 0)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.time_spent_minutes, 95)

    def test_stopping_a_timer_that_is_not_running_is_a_404(self):
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/timer/stop/", data="{}", content_type="application/json"
        )
        self.assertEqual(r.status_code, 404, r.content[:200])

    def test_snooze_and_unsnooze(self):
        until = "2027-01-01T00:00:00+00:00"
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/snooze/",
            data=json.dumps({"until": until}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/snooze/",
            data=json.dumps({"until": "2028-01-01T00:00:00+00:00"}),
            content_type="application/json",
        )
        self.assertIn("2028", r.json()["until"], "snoozing twice should extend, not duplicate")
        self.assertEqual(self.c.delete(f"/api/v1/issues/{self.issue.key}/snooze/").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/v1/issues/{self.issue.key}/snooze/").status_code, 404)

    def test_a_snooze_in_the_past_is_refused(self):
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/snooze/",
            data=json.dumps({"until": "2000-01-01T00:00:00+00:00"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_reactions_are_counted_and_idempotent(self):
        base = f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/reactions/"
        self.assertEqual(
            self.c.post(
                base, data=json.dumps({"emoji": "tada"}), content_type="application/json"
            ).status_code,
            200,
        )
        self.c.post(base, data=json.dumps({"emoji": "tada"}), content_type="application/json")
        listed = self.c.get(base).json()
        self.assertEqual(listed["counts"], {"tada": 1}, "reacting twice is not a second reaction")
        self.assertEqual(listed["mine"], ["tada"])
        self.assertEqual(self.c.delete(base + "tada/").json()["counts"], {})

    def test_an_unknown_emoji_is_refused(self):
        base = f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/reactions/"
        r = self.c.post(base, data=json.dumps({"emoji": "shrug"}), content_type="application/json")
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_branch_links(self):
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/branches/",
            data=json.dumps(
                {"branch": "feat/login", "repo_url": "https://git.example/x", "commit_sha": "abc123"}
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        again = self.c.post(
            f"/api/v1/issues/{self.issue.key}/branches/",
            data=json.dumps(
                {"branch": "feat/login", "repo_url": "https://git.example/x", "commit_sha": "abc123"}
            ),
            content_type="application/json",
        )
        self.assertEqual(again.json()["id"], r.json()["id"], "idempotent on issue+branch+sha")
        listed = self.c.get(f"/api/v1/issues/{self.issue.key}/branches/").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(
            self.c.delete(f"/api/v1/issues/{self.issue.key}/branches/{r.json()['id']}/").status_code, 200
        )

    def test_an_unparseable_repo_url_is_a_400_not_a_500(self):
        """A URLField validates on save, so without the check it is a 500."""
        r = self.c.post(
            f"/api/v1/issues/{self.issue.key}/branches/",
            data=json.dumps({"branch": "feat/x", "repo_url": "not a url"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertNotIn(b"Traceback", r.content)

    def test_issue_templates(self):
        type_id = IssueType.objects.first().pk
        r = self.c.post(
            "/api/v1/projects/WEB/issue-templates/",
            data=json.dumps(
                {"name": "Bug", "issue_type_id": type_id, "summary": "Algo falla", "labels": ["plantilla"]}
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["labels"], ["plantilla"])
        listed = self.c.get("/api/v1/projects/WEB/issue-templates/").json()
        self.assertEqual(listed["count"], 1)
        again = self.c.post(
            "/api/v1/projects/WEB/issue-templates/",
            data=json.dumps({"name": "Bug", "issue_type_id": type_id}),
            content_type="application/json",
        )
        self.assertEqual(again.status_code, 409, again.content[:200])
        self.assertEqual(
            self.c.delete(f"/api/v1/projects/WEB/issue-templates/{r.json()['id']}/").status_code, 200
        )


class APICsvTests(TestCase):
    """CSV import and export.

    Both existed only as HTML pages, which is the one shape an agent cannot use.
    The import is the interesting half: a bulk create is exactly the path that
    skips the four post_save receivers, so a test asserts the created issues are
    real rows a board would show, not silent inserts.
    """

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        _make_issue(self.project, self.user, summary="Existente")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_export_returns_rows_as_data_not_a_csv_inside_json(self):
        r = self.c.get("/api/v1/projects/WEB/csv-export/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["rows"][0]["summary"], "Existente")
        self.assertIn("key", body["columns"])
        self.assertTrue(body["filename"].endswith(".csv"))

    def test_export_honours_a_column_subset_and_ignores_bogus_ones(self):
        r = self.c.get("/api/v1/projects/WEB/csv-export/?cols=key,summary,inventada")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["columns"], ["key", "summary"])

    def test_export_filters_by_text_and_excludes_archived(self):
        Issue.objects.filter(project=self.project).update(archived=True)
        body = self.c.get("/api/v1/projects/WEB/csv-export/").json()
        self.assertEqual(body["count"], 0, "archived issues are off the board by default")
        body = self.c.get("/api/v1/projects/WEB/csv-export/?archived=true").json()
        self.assertEqual(body["count"], 1)
        body = self.c.get("/api/v1/projects/WEB/csv-export/?text=NoExiste").json()
        self.assertEqual(body["count"], 0)

    def test_download_serves_a_file_with_the_csv_content_type(self):
        r = self.c.get("/api/v1/projects/WEB/csv-export/download/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("WEB-1,Existente", r.content.decode())

    def test_a_viewer_can_export_but_not_import(self):
        ProjectMembership.objects.create(project=self.project, user=_make_user("vic"), role="viewer")
        c2 = Client()
        c2.login(username="vic", password="pw")
        self.assertEqual(c2.get("/api/v1/projects/WEB/csv-export/").status_code, 200)
        r = c2.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary\nX"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403, r.content[:200])

    def test_import_previews_without_creating_anything(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary,priority\nNueva 1,High\nNueva 2,Medium"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["creatable"], 2)
        self.assertEqual(body["created_keys"], [])
        self.assertEqual(Issue.objects.filter(project=self.project).count(), 1)

    def test_import_creates_only_when_told_to(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary,priority\nNueva 1,High", "dry_run": False}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(len(body["created_keys"]), 1)
        created = Issue.objects.get(key=body["created_keys"][0])
        self.assertEqual(created.summary, "Nueva 1")
        self.assertEqual(created.priority.name, "High")
        # Issue.save() is what mints the key; a row without one would be invisible
        # to every JQL query and to the board.
        self.assertTrue(created.key.startswith("WEB-"))
        self.assertEqual(created.reporter, self.user)

    def test_import_fires_the_receivers_rather_than_bulk_inserting(self):
        """The whole reason this is a loop of asave().

        bulk_create would create the rows and skip notifications, audit, webhooks
        and the websocket broadcast, so the board would not show them until a
        refresh and nobody watching would know.
        """
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary\nCon auditoría", "dry_run": False}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        key = r.json()["created_keys"][0]
        issue = Issue.objects.get(key=key)
        # target_type is the model name lowercased, and verb is created/updated:
        # core.audit writes one row per save from post_save, so its presence is
        # the evidence that the save went through asave and not bulk_create.
        self.assertTrue(
            AuditEntry.objects.filter(
                project=self.project,
                target_type="issue",
                target_id=issue.pk,
                verb="created",
            ).exists(),
            "an import must leave audit rows; bulk_create would leave none",
        )
        self.assertIsNotNone(issue.created_at)
        self.assertIsNotNone(issue.updated_at)

    def test_import_reports_the_row_number_of_a_row_without_a_summary(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary,priority\nBuena,High\n,Medium\nOtra,High"}),
            content_type="application/json",
        )
        body = r.json()
        self.assertEqual(body["total_rows"], 3)
        self.assertEqual(body["creatable"], 2)
        skipped = [row for row in body["rows"] if row["skipped"]]
        self.assertEqual([r["row"] for r in skipped], [2])
        self.assertIn("summary", skipped[0]["problem"])

    def test_import_reports_values_it_could_not_resolve(self):
        """A typo in a priority must not silently become the default one."""
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary,priority\nNueva,Altissima"}),
            content_type="application/json",
        )
        body = r.json()
        self.assertEqual(body["unknown_values"]["priority"], ["Altissima"])
        self.assertEqual(body["creatable"], 1, "it still falls back rather than failing the row")

    def test_import_of_an_empty_csv_is_a_400(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "  "}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_import_tolerates_a_malformed_story_point_or_date(self):
        """Both are parsed from text, so both are somebody's typo waiting to
        happen. A row must not be lost to one."""
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps(
                {"csv": "summary,story_points,due_date\nNueva,muchos,31-12-2026", "dry_run": False}
            ),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        key = r.json()["created_keys"][0]
        issue = Issue.objects.get(key=key)
        self.assertIsNone(issue.story_points)
        self.assertIsNone(issue.due_date)

    def test_import_parses_a_good_story_point_and_date(self):
        r = self.c.post(
            "/api/v1/projects/WEB/csv-import/",
            data=json.dumps({"csv": "summary,story_points,due_date\nNueva,3,2026-12-31", "dry_run": False}),
            content_type="application/json",
        )
        issue = Issue.objects.get(key=r.json()["created_keys"][0])
        self.assertEqual(issue.story_points, 3)
        self.assertEqual(issue.due_date.isoformat(), "2026-12-31")


class APIBoardViewTests(TestCase):
    """Saved board views. The board GET params are the source of truth, so a view
    is a URL rather than a query an agent has to know how to resolve."""

    def setUp(self):
        _seed_lookups()
        self.user = _make_user("alice")
        self.project = _make_project(self.user, key="WEB")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_create_list_and_apply(self):
        r = self.c.post(
            "/api/v1/projects/WEB/board-views/",
            data=json.dumps({"name": "Mías", "filters": {"assignee": "me", "type": "Task"}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["name"], "Mías")
        self.assertIn("assignee=me", body["url"])
        listed = self.c.get("/api/v1/projects/WEB/board-views/").json()
        self.assertEqual(listed["count"], 1)

    def test_saving_the_same_name_twice_replaces_it(self):
        for assignee in ("me", "otro"):
            self.c.post(
                "/api/v1/projects/WEB/board-views/",
                data=json.dumps({"name": "Mías", "filters": {"assignee": assignee}}),
                content_type="application/json",
            )
        listed = self.c.get("/api/v1/projects/WEB/board-views/").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["filters"]["assignee"], "otro")

    def test_an_unrecognised_filter_is_refused_rather_than_stored(self):
        """A stored key the board never reads is a view that silently does
        nothing when applied."""
        r = self.c.post(
            "/api/v1/projects/WEB/board-views/",
            data=json.dumps({"name": "Rota", "filters": {"inventado": "x"}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertIn("assignee", r.json()["detail"])

    def test_only_one_default_per_project(self):
        for name in ("A", "B"):
            self.c.post(
                "/api/v1/projects/WEB/board-views/",
                data=json.dumps({"name": name, "is_default": True}),
                content_type="application/json",
            )
        items = self.c.get("/api/v1/projects/WEB/board-views/").json()["items"]
        self.assertEqual(sum(1 for i in items if i["is_default"]), 1)

    def test_views_are_per_user_and_per_project(self):
        from board.models import SavedBoardView

        self.c.post(
            "/api/v1/projects/WEB/board-views/",
            data=json.dumps({"name": "Mía"}),
            content_type="application/json",
        )
        # A member of the same project sees none of it.
        ProjectMembership.objects.create(project=self.project, user=_make_user("bob"), role="member")
        c2 = Client()
        c2.login(username="bob", password="pw")
        self.assertEqual(c2.get("/api/v1/projects/WEB/board-views/").json()["count"], 0)
        # And somebody else's view cannot be deleted by id.
        theirs = SavedBoardView.objects.get()
        r = c2.delete(f"/api/v1/projects/WEB/board-views/{theirs.pk}/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(SavedBoardView.objects.count(), 1)

    def test_a_project_you_cannot_see_is_404(self):
        hidden = _make_project(_make_user("mallory"), key="HID")
        self.assertEqual(self.c.get(f"/api/v1/projects/{hidden.key}/board-views/").status_code, 404)


class APIRecentTests(TestCase):
    """The dashboard's "Recientes" list. Each test opens an issue through the
    detail view, because that is what writes the Visit row."""

    def setUp(self):
        _seed_lookups()
        # A plain member, not the lead: filter_visible admits a lead regardless of
        # membership, so a lead's project cannot be "lost".
        self.lead = _make_user("lead")
        self.user = _make_user("alice")
        self.project = _make_project(self.lead, key="WEB")
        ProjectMembership.objects.create(project=self.project, user=self.user, role="member")
        self.issue = _make_issue(self.project, self.user, summary="Consultado")
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def _open(self, key=None):
        return self.c.get(f"/issues/{key or self.issue.key}/")

    def test_opening_an_issue_records_a_visit(self):
        self._open()
        body = self.c.get("/api/v1/recent/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["issue"], self.issue.key)
        self.assertEqual(body["items"][0]["summary"], "Consultado")

    def test_reopening_moves_it_to_the_top_rather_than_duplicating(self):
        second = _make_issue(self.project, self.user, summary="Segundo")
        self._open()
        self._open(second.key)
        body = self.c.get("/api/v1/recent/").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["items"][0]["issue"], second.key)

    def test_an_archived_issue_is_reported_as_such(self):
        self._open()
        self.issue.archived = True
        self.issue.save()
        item = self.c.get("/api/v1/recent/").json()["items"][0]
        self.assertTrue(item["archived"], "an archived issue can be in Recientes and off the board")

    def test_a_project_lost_mid_session_drops_out(self):
        """filter_visible, or the list hands back a key that 404s on click."""
        from projects.models import ProjectMembership

        self._open()
        ProjectMembership.objects.filter(project=self.project, user=self.user).delete()
        self.assertEqual(self.c.get("/api/v1/recent/").json()["count"], 0)

    def test_forgetting_one_and_clearing_all(self):
        other = _make_issue(self.project, self.user, summary="Otro")
        self._open()
        self._open(other.key)
        self.assertEqual(self.c.delete(f"/api/v1/recent/{self.issue.key}/").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/v1/recent/{self.issue.key}/").status_code, 404)
        self.assertEqual(self.c.get("/api/v1/recent/").json()["count"], 1)
        self.assertEqual(self.c.delete("/api/v1/recent/").json()["cleared"], 1)
        self.assertEqual(self.c.get("/api/v1/recent/").json()["count"], 0)


class APIMentionTests(TestCase):
    """Whether an @mention was read."""

    def setUp(self):
        from accounts.models import MentionReceipt

        _seed_lookups()
        self.user = _make_user("alice")
        self.other = _make_user("bob")
        self.third = _make_user("carol")
        self.project = _make_project(self.user, key="WEB")
        ProjectMembership.objects.create(project=self.project, user=self.other, role="member")
        ProjectMembership.objects.create(project=self.project, user=self.third, role="member")
        self.issue = _make_issue(self.project, self.user, summary="Con mención")
        # The receipt is created by the notification pass reading the @mention out
        # of the body, not here. Creating one by hand collides on the
        # (mentioned, comment) unique constraint, which is the constraint working.
        self.comment = Comment.objects.create(issue=self.issue, author=self.user, body="@bob mira esto")
        self.receipt = MentionReceipt.objects.get(comment=self.comment, mentioned=self.other)
        self.c = Client()
        self.c.login(username="alice", password="pw")

    def test_the_author_sees_who_was_mentioned_and_whether_they_read_it(self):
        body = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/mentions/").json()
        self.assertEqual(body["count"], 1)
        mention = body["mentions"][0]
        self.assertEqual(mention["actor"], "alice")
        self.assertEqual(mention["mentioned"], "bob")
        self.assertFalse(mention["seen"])
        self.assertEqual(body["seenCount"], 0)

    def test_a_seen_mention_says_so(self):
        from django.utils import timezone

        from accounts.models import MentionReceipt

        MentionReceipt.objects.filter(comment=self.comment).update(seen_at=timezone.now())
        body = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/mentions/").json()
        self.assertTrue(body["mentions"][0]["seen"])
        self.assertEqual(body["seenCount"], 1)

    def test_a_stranger_sees_that_a_mention_exists_but_not_whether_it_was_read(self):
        """Naming somebody is already public in the comment body. Reading it is
        the author's business."""
        c3 = Client()
        c3.login(username="carol", password="pw")
        body = c3.get(f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/mentions/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["mentions"][0]["mentioned"], "bob")
        self.assertFalse(body["mentions"][0]["seen"])
        self.assertEqual(body["mentions"][0]["seen_at"], "")

    def test_the_mentioned_person_themselves_sees_their_own_receipt(self):
        c2 = Client()
        c2.login(username="bob", password="pw")
        body = c2.get(f"/api/v1/issues/{self.issue.key}/comments/{self.comment.pk}/mentions/").json()
        self.assertTrue(body["mentions"][0]["seen"] is False)
        self.assertEqual(body["mentions"][0]["mentioned"], "bob")

    def test_a_comment_from_another_issue_is_404(self):
        elsewhere = _make_issue(self.project, self.user, summary="Otro")
        other_comment = Comment.objects.create(issue=elsewhere, author=self.user, body="x")
        r = self.c.get(f"/api/v1/issues/{self.issue.key}/comments/{other_comment.pk}/mentions/")
        self.assertEqual(r.status_code, 404)

    def test_a_comment_somebody_cannot_see_is_404(self):
        hidden = _make_project(_make_user("mallory"), key="HID")
        secret = _make_issue(hidden, hidden.lead, summary="Secreto")
        r = self.c.get(f"/api/v1/issues/{secret.key}/comments/{self.comment.pk}/mentions/")
        self.assertEqual(r.status_code, 404)


class APIInviteTests(TestCase):
    """Invite-only registration had a UI and no API, so a scripted onboarding run
    could not use the one thing that keeps an instance closed."""

    def setUp(self):
        _seed_lookups()
        self.root = User.objects.create_superuser("root", password="pw", email="root@x.com")
        self.plain = _make_user("alice")
        self.c = Client()
        self.c.login(username="root", password="pw")

    def test_minting_an_invite_returns_the_token_and_the_url(self):
        r = self.c.post(
            "/api/v1/admin/invites/",
            data=json.dumps({"email": "nuevo@x.com", "role": "member", "days": 3}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertTrue(body["token"])
        self.assertIn(body["token"], body["registration_url"])
        self.assertTrue(body["valid"])

    def test_listing_never_returns_the_token(self):
        """A list that handed out tokens would be a page of bearer credentials for
        anyone who could open it."""
        self.c.post("/api/v1/admin/invites/", data=json.dumps({}), content_type="application/json")
        body = self.c.get("/api/v1/admin/invites/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["token"], "")
        self.assertEqual(body["items"][0]["registration_url"], "")

    def test_revoking_expires_rather_than_deletes(self):
        """A registration already in flight should fail as "expired", not as "no
        such invite", and the row stays as a record."""
        from accounts.models import InviteToken

        created = self.c.post(
            "/api/v1/admin/invites/", data=json.dumps({}), content_type="application/json"
        ).json()
        r = self.c.delete(f"/api/v1/admin/invites/{created['id']}/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertFalse(r.json()["valid"])
        self.assertFalse(InviteToken.objects.get(pk=created["id"]).is_valid)

    def test_revoking_an_unknown_invite_is_404(self):
        self.assertEqual(self.c.delete("/api/v1/admin/invites/9999/").status_code, 404)

    def test_a_bad_role_is_refused(self):
        r = self.c.post(
            "/api/v1/admin/invites/",
            data=json.dumps({"role": "root"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400, r.content[:200])

    def test_days_is_clamped(self):
        created = self.c.post(
            "/api/v1/admin/invites/",
            data=json.dumps({"days": 99999}),
            content_type="application/json",
        ).json()
        self.assertTrue(created["valid"])

    def test_a_non_superuser_can_do_nothing_here(self):
        c2 = Client()
        c2.login(username="alice", password="pw")
        self.assertEqual(c2.get("/api/v1/admin/invites/").status_code, 403)
        self.assertEqual(
            c2.post(
                "/api/v1/admin/invites/", data=json.dumps({}), content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(c2.delete("/api/v1/admin/invites/1/").status_code, 403)


class APIUniqueConstraintTests(TestCase):
    """A duplicate on a unique column must be a 409 with a reason.

    These were 500s with a traceback. `Team.slug` is unique, and neither the
    create nor the rename checked it, so a second team called "platform" — or a
    rename onto an existing slug — reached the insert and came back as an
    IntegrityError. The web UI's own form never showed it, which is how it
    survived: the API was the only way to hit it.
    """

    def setUp(self):
        _seed_lookups()
        self.root = User.objects.create_superuser("root", password="pw", email="root@x.com")
        self.c = Client()
        self.c.login(username="root", password="pw")

    def test_creating_a_team_with_a_taken_slug_is_409(self):
        first = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "platform", "name": "Platform"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200, first.content[:200])
        again = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "platform", "name": "Otra"}),
            content_type="application/json",
        )
        self.assertEqual(again.status_code, 409, again.content[:200])
        self.assertIn("platform", again.json()["detail"])
        self.assertNotIn(b"Traceback", again.content)

    def test_renaming_a_team_onto_a_taken_slug_is_409(self):
        from accounts.models import Team

        a = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "plataforma", "name": "A"}),
            content_type="application/json",
        ).json()
        self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "otra", "name": "B"}),
            content_type="application/json",
        )
        r = self.c.patch(
            f"/api/v1/teams/{a['id']}/",
            data=json.dumps({"slug": "otra", "name": "A"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])
        self.assertEqual(Team.objects.get(pk=a["id"]).slug, "plataforma", "the rename must not apply")

    def test_renaming_a_team_to_its_own_slug_is_fine(self):
        """The check is for a collision, not for sameness — refusing this would
        make a description-only edit impossible."""
        a = self.c.post(
            "/api/v1/teams/",
            data=json.dumps({"slug": "estable", "name": "A"}),
            content_type="application/json",
        ).json()
        r = self.c.patch(
            f"/api/v1/teams/{a['id']}/",
            data=json.dumps({"slug": "estable", "name": "A renombrada"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(r.json()["name"], "A renombrada")

    def test_a_duplicate_project_key_is_still_409(self):
        """The same class of check, which the project create already had. Pinned
        so a refactor does not lose it."""
        lead = _make_user("alice")
        _make_project(lead, key="WEB")
        r = self.c.post(
            "/api/v1/projects/",
            data=json.dumps({"key": "WEB", "name": "Otro"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 409, r.content[:200])
