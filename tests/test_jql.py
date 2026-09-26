"""Tests for the JQL-lite parser.

The parser is the layer an agent talks to most, and its failure mode is
dangerous rather than loud: a clause it cannot parse used to become a free-text
search, so a typo returned "no matches" instead of "I did not understand you".
Most of what follows pins that distinction down.
"""

from django.test import TestCase

from issues.models import Issue, IssueType, Label, Priority, Status
from projects.models import Project, ProjectMembership, Sprint
from search.jql import JQLError, parse_jql
from tests.test_smoke import _make_user


class JQLParserTests(TestCase):
    """Pure parser behaviour: does it build the right Q, or raise?"""

    def test_unknown_field_raises(self):
        with self.assertRaises(JQLError):
            parse_jql("statuss = Done")

    def test_key_field_parses(self):
        q, _ = parse_jql("key = WEB-1")
        self.assertIn("key", str(q))

    def test_key_in_list_parses(self):
        q, _ = parse_jql("key in (WEB-1, WEB-2)")
        self.assertIn("key__in", str(q))

    def test_status_category_translates_display_names(self):
        """Jira reports categories by display name; jirrabit stores slugs."""
        done, _ = parse_jql('statusCategory = "Done"')
        in_progress, _ = parse_jql('statusCategory = "In Progress"')
        todo, _ = parse_jql('statusCategory = "To Do"')
        self.assertIn("status__category", str(done))
        # The slug is what reaches the query, not the display name.
        self.assertIn("'done'", str(done))
        self.assertIn("'in_progress'", str(in_progress))
        self.assertIn("'todo'", str(todo))

    def test_status_category_negation_is_the_common_case(self):
        q, _ = parse_jql("statusCategory != Done")
        self.assertIn("status__category", str(q))
        self.assertIn("'done'", str(q))

    def test_status_category_in_list(self):
        q, _ = parse_jql('statusCategory in ("To Do", "In Progress")')
        self.assertIn("'todo'", str(q))
        self.assertIn("'in_progress'", str(q))

    def test_status_category_fuzzy_does_not_translate(self):
        """A ~ search is a substring match against the stored slug, so
        translating would break it: "in_progress".startswith("In Progress") is
        false, but the reverse of the raw value is what the user typed."""
        q, _ = parse_jql("statusCategory ~ prog")
        self.assertIn("icontains", str(q))

    def test_is_empty_and_is_not_empty_parse(self):
        for field in ("assignee", "reporter", "label", "sprint", "epic", "project"):
            q, _ = parse_jql(f"{field} is EMPTY")
            self.assertTrue(str(q), f"{field} is EMPTY produced an empty Q")
            q, _ = parse_jql(f"{field} is not EMPTY")
            self.assertTrue(str(q), f"{field} is not EMPTY produced an empty Q")

    def test_is_empty_on_many_to_many_uses_a_subquery(self):
        """M2M emptiness must be a negated Exists, not a negated __isnull.

        Django compiles a nullable relation as a LEFT OUTER JOIN, so
        ``~Q(label__isnull=False)`` becomes ``NOT (col IS NOT NULL)`` — which is
        NULL, not true, for an issue with no labels. The negation then excludes
        exactly the rows it should match. This test pins the behaviour rather
        than the query shape; the query-level test below is what would catch a
        regression.
        """
        from issues.models import Issue as IssueModel
        from issues.models import Label as LabelModel
        from projects.models import Project as ProjectModel

        relation = IssueModel._meta.get_field("labels")
        self.assertIs(relation.related_model, LabelModel)
        self.assertEqual(relation.remote_field.get_accessor_name(), "issues")
        # The lookup must resolve rather than raise.
        parse_jql("label is EMPTY")
        parse_jql("label is not EMPTY")
        self.assertIsNotNone(ProjectModel)

    def test_is_empty_on_foreign_key_is_isnull(self):
        empty, _ = parse_jql("assignee is EMPTY")
        self.assertIn("assignee__isnull", str(empty))

    def test_malformed_clause_raises_instead_of_searching_free_text(self):
        """The regression this whole change exists for.

        "assignee is EMPTY" used to miss the operator regex, fall through to the
        free-text branch, and become a summary search for the literal string
        "assignee is EMPTY" — returning nothing, which reads as a correct answer.
        """
        for bad in ("statuss = Done", "priority != ", "assignee =="):
            with self.assertRaises(JQLError, msg=f"{bad!r} was accepted as free text"):
                parse_jql(bad)

    def test_genuine_free_text_still_works(self):
        """The fallback must survive for the search box: prose has no operator."""
        q, _ = parse_jql("login button")
        self.assertIn("summary__icontains", str(q))
        self.assertIn("'login button'", str(q))

    def test_free_text_mixed_with_clauses(self):
        q, _ = parse_jql('project = WEB AND "some words"')
        self.assertIn("project__key", str(q))
        self.assertIn("summary__icontains", str(q))

    def test_order_by_still_works(self):
        q, order = parse_jql("project = WEB ORDER BY priority DESC, created")
        self.assertEqual(order, ["-priority__weight", "created_at"])

    def test_empty_query_is_a_noop(self):
        q, order = parse_jql("")
        self.assertEqual(str(q), str(parse_jql("")[0]))
        self.assertEqual(order, [])


class JQLQueryTests(TestCase):
    """The parser against real rows, so the generated Q is proven usable."""

    def setUp(self):
        self.user = _make_user("alice")
        self.project = Project.objects.create(key="WEB", name="Web", lead=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="admin")
        self.todo = Status.objects.create(name="To Do", category="todo", order=10)
        self.progress = Status.objects.create(name="In Progress", category="in_progress", order=20)
        self.done = Status.objects.create(name="Done", category="done", order=50)
        self.prio = Priority.objects.create(name="High", weight=40)
        self.itype = IssueType.objects.create(name="Task", category="task")
        self.other = _make_user("bob")

        self.mine = self._issue("Login broken", self.todo, assignee=self.user, summary_extra="login")
        self.theirs = self._issue("Logout broken", self.todo, assignee=self.other)
        self.finished = self._issue("Signup done", self.done)
        self.unassigned = self._issue("No owner", self.progress)

    def _issue(self, summary, status, assignee=None, summary_extra=""):
        return Issue.objects.create(
            project=self.project,
            reporter=self.user,
            summary=summary + (" " + summary_extra if summary_extra else ""),
            description=summary_extra or summary,
            status=status,
            priority=self.prio,
            issue_type=self.itype,
            assignee=assignee,
        )

    def _search(self, query):
        q, order = parse_jql(query)
        return set(Issue.objects.filter(q).values_list("key", flat=True))

    def test_key_matches_exactly(self):
        self.assertEqual(self._search(f"key = {self.mine.key}"), {self.mine.key})
        self.assertEqual(self._search("key = NOPE-1"), set())

    def test_status_category_done(self):
        self.assertEqual(self._search("statusCategory = Done"), {self.finished.key})

    def test_status_category_not_done(self):
        found = self._search("statusCategory != Done")
        self.assertIn(self.mine.key, found)
        self.assertNotIn(self.finished.key, found)

    def test_assignee_is_empty_finds_unassigned(self):
        found = self._search("assignee is EMPTY")
        self.assertIn(self.unassigned.key, found)
        self.assertNotIn(self.mine.key, found)

    def test_assignee_is_not_empty_finds_assigned(self):
        found = self._search("assignee is not EMPTY")
        self.assertIn(self.mine.key, found)
        self.assertIn(self.theirs.key, found)
        self.assertNotIn(self.unassigned.key, found)

    def test_label_is_empty_and_not_empty(self):
        label = Label.objects.create(name="frontend")
        self.mine.labels.add(label)
        self.assertEqual(self._search("label is EMPTY"), {self.theirs.key, self.finished.key, self.unassigned.key})
        self.assertEqual(self._search("label is not EMPTY"), {self.mine.key})

    def test_sprint_is_empty(self):
        sprint = Sprint.objects.create(project=self.project, name="S1", status="planned")
        self.finished.sprint = sprint
        self.finished.save()
        found = self._search("sprint is not EMPTY")
        self.assertEqual(found, {self.finished.key})

    def test_combined_clauses(self):
        found = self._search("statusCategory != Done AND assignee is EMPTY")
        self.assertIn(self.unassigned.key, found)
        self.assertNotIn(self.mine.key, found)

    def test_free_text_finds_by_summary(self):
        found = self._search("logout")
        self.assertIn(self.theirs.key, found)
