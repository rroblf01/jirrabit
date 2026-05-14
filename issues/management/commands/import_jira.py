"""Import issues from a Jira CSV export.

Jira's "Export issues" produces a wide CSV with a fixed-ish header. This
command maps the subset we care about and creates equivalent issues in a
target jirrabit project. Unknown statuses / priorities / types are
created on the fly so that the import never fails on lookup.

Usage::

    python manage.py import_jira data.csv --project DEMO --reporter alice_pm
    python manage.py import_jira data.csv --project DEMO --reporter alice_pm --dry-run

The CSV is expected to have at least these columns (case-insensitive):
``Issue key``, ``Summary``, ``Issue Type``, ``Status``, ``Priority``,
``Assignee``, ``Reporter``, ``Description``, ``Story Points`` (optional),
``Due Date`` (optional), ``Sprint`` (optional), ``Labels`` (optional;
multiple ``Labels`` columns are merged), ``Comment`` (optional; multiple
``Comment`` columns are merged — Jira splits one per column).
"""
import csv
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from accounts.models import User
from issues.models import Comment, Issue, IssueType, Label, Priority, Status
from projects.models import Project, Sprint


def _norm(name: str) -> str:
    return (name or "").strip().lower()


class Command(BaseCommand):
    help = "Import a Jira CSV export into a jirrabit project."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)
        parser.add_argument("--project", required=True, help="Target project key (e.g. DEMO).")
        parser.add_argument(
            "--reporter",
            default=None,
            help="Username used as fallback reporter when the CSV's value is unknown.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse the CSV and report counts without touching the DB.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["csv_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        try:
            project = Project.objects.get(key=opts["project"])
        except Project.DoesNotExist:
            raise CommandError(f"Project '{opts['project']}' not found. Create it first.")

        fallback_reporter = None
        if opts["reporter"]:
            try:
                fallback_reporter = User.objects.get(username=opts["reporter"])
            except User.DoesNotExist:
                raise CommandError(f"Fallback reporter '{opts['reporter']}' not found.")
        if fallback_reporter is None:
            fallback_reporter = User.objects.filter(is_superuser=True).first()
        if fallback_reporter is None:
            raise CommandError("No fallback reporter could be resolved.")

        users = {u.username.lower(): u for u in User.objects.all()}
        type_cache = {t.name.lower(): t for t in IssueType.objects.all()}
        status_cache = {s.name.lower(): s for s in Status.objects.all()}
        priority_cache = {p.name.lower(): p for p in Priority.objects.all()}
        sprint_cache = {s.name.lower(): s for s in project.sprints.all()}
        label_cache = {l.name.lower(): l for l in Label.objects.all()}

        # ``utf-8-sig`` peels the BOM Jira's exporter adds, otherwise the
        # first header is read as ``﻿Issue key`` and never matches.
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = [_norm(h) for h in next(reader)]
            created, skipped = 0, 0
            for row in reader:
                if not row:
                    continue
                cells = {h: [] for h in set(headers)}
                for header, value in zip(headers, row):
                    cells[header].append(value or "")

                def first(col):
                    vals = cells.get(col, [])
                    return vals[0].strip() if vals else ""

                def all_of(col):
                    return [v.strip() for v in cells.get(col, []) if v.strip()]

                summary = first("summary")
                if not summary:
                    skipped += 1
                    continue
                type_name = first("issue type") or "Task"
                status_name = first("status") or "To Do"
                priority_name = first("priority") or "Medium"
                assignee_name = first("assignee")
                reporter_name = first("reporter")
                description = first("description")
                story_points_raw = first("story points") or first("custom field (story points)")
                due_date_raw = first("due date")
                sprint_name = first("sprint")
                labels = all_of("labels")
                comments = all_of("comment")

                itype = type_cache.get(type_name.lower()) or self._make_type(type_name, type_cache, opts["dry_run"])
                status = status_cache.get(status_name.lower()) or self._make_status(status_name, status_cache, opts["dry_run"])
                priority = priority_cache.get(priority_name.lower()) or self._make_priority(priority_name, priority_cache, opts["dry_run"])

                assignee = users.get(assignee_name.lower()) if assignee_name else None
                reporter = users.get(reporter_name.lower()) if reporter_name else None
                reporter = reporter or fallback_reporter

                sprint = sprint_cache.get(sprint_name.lower()) if sprint_name else None
                if sprint_name and not sprint and not opts["dry_run"]:
                    sprint = Sprint.objects.create(project=project, name=sprint_name)
                    sprint_cache[sprint_name.lower()] = sprint

                story_points = None
                try:
                    if story_points_raw:
                        story_points = int(float(story_points_raw))
                except ValueError:
                    pass

                due_date = None
                for fmt in ("%Y-%m-%d", "%d/%b/%y %I:%M %p", "%d/%m/%Y", "%m/%d/%Y"):
                    try:
                        if due_date_raw:
                            due_date = datetime.strptime(due_date_raw, fmt).date()
                            break
                    except ValueError:
                        continue

                if opts["dry_run"]:
                    created += 1
                    continue

                issue = Issue.objects.create(
                    project=project,
                    issue_type=itype,
                    status=status,
                    priority=priority,
                    summary=summary,
                    description=description,
                    reporter=reporter,
                    assignee=assignee,
                    sprint=sprint,
                    story_points=story_points,
                    due_date=due_date,
                )
                for raw in labels:
                    for tag in raw.split():
                        slug = tag.lower()
                        lab = label_cache.get(slug)
                        if not lab:
                            lab = Label.objects.create(name=tag[:40])
                            label_cache[slug] = lab
                        issue.labels.add(lab)
                for body in comments:
                    Comment.objects.create(issue=issue, author=reporter, body=body)
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"{'[dry-run] ' if opts['dry_run'] else ''}Imported {created} issues, skipped {skipped}."
        ))

    def _make_type(self, name, cache, dry):
        if dry:
            return next(iter(cache.values()), None)
        t = IssueType.objects.create(name=name[:40], category="task")
        cache[name.lower()] = t
        return t

    def _make_status(self, name, cache, dry):
        if dry:
            return next(iter(cache.values()), None)
        category = "done" if "done" in name.lower() or "closed" in name.lower() else "todo"
        s = Status.objects.create(name=name[:40], category=category)
        cache[name.lower()] = s
        return s

    def _make_priority(self, name, cache, dry):
        if dry:
            return next(iter(cache.values()), None)
        p = Priority.objects.create(name=name[:20])
        cache[name.lower()] = p
        return p
