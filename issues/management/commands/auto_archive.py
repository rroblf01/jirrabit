"""Mark done issues older than N days as ``archived=True``.

Run via cron or systemd timer:

    uv run python manage.py auto_archive --days 30
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from issues.models import Issue


class Command(BaseCommand):
    help = "Archive issues whose status category is 'done' and resolved_at < today - N days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, days, dry_run, **kwargs):
        cutoff = timezone.now() - timedelta(days=days)
        qs = Issue.objects.filter(
            archived=False, status__category="done", resolved_at__lt=cutoff,
        )
        count = qs.count()
        if dry_run:
            self.stdout.write(self.style.WARNING(f"would archive {count} issues"))
            return
        updated = qs.update(archived=True)
        self.stdout.write(self.style.SUCCESS(f"archived {updated} issues older than {days}d"))
