from django.core.management.base import BaseCommand

from issues.models import IssueType, Priority, Status


class Command(BaseCommand):
    help = "Seed initial issue types, statuses, priorities."

    def handle(self, *args, **opts):
        defaults_types = [
            ("Epic", "epic", "▲", "#7c3aed"),
            ("Story", "story", "★", "#22c55e"),
            ("Task", "task", "✓", "#1e6fff"),
            ("Bug", "bug", "✦", "#ef4444"),
            ("Subtask", "subtask", "↳", "#0ea5e9"),
        ]
        for name, cat, icon, color in defaults_types:
            IssueType.objects.get_or_create(
                name=name, defaults={"category": cat, "icon": icon, "color": color}
            )

        statuses = [
            ("To Do", "todo", 10),
            ("In Progress", "in_progress", 20),
            ("In Review", "in_progress", 30),
            ("Blocked", "in_progress", 40),
            ("Done", "done", 50),
        ]
        for name, cat, order in statuses:
            Status.objects.get_or_create(name=name, defaults={"category": cat, "order": order})

        priorities = [
            ("Highest", 50, "#dc2626"),
            ("High", 40, "#f97316"),
            ("Medium", 30, "#1e6fff"),
            ("Low", 20, "#22c55e"),
            ("Lowest", 10, "#94a3b8"),
        ]
        for name, w, color in priorities:
            Priority.objects.get_or_create(name=name, defaults={"weight": w, "color": color})

        self.stdout.write(self.style.SUCCESS("Seed data ready."))
