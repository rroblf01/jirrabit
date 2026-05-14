"""Drop old activity / notification rows.

Run from a daily cron / k8s CronJob:

    python manage.py purge_old_data --days 90

Targets:
- ``issues.AuditEntry`` older than ``--days``
- ``accounts.Notification`` older than ``--days`` AND already read
  (unread ones stay regardless of age — the user may not have seen them).

Use ``--dry-run`` to see what would be deleted without touching the DB.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete AuditEntry and read Notification rows older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help="Retention window in days (default 90).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without changing the DB.",
        )

    def handle(self, *args, **opts):
        from accounts.models import Notification
        from issues.models import AuditEntry

        cutoff = timezone.now() - timedelta(days=opts["days"])
        dry = opts["dry_run"]
        self.stdout.write(f"Cutoff: {cutoff.isoformat()} ({'dry-run' if dry else 'apply'})")

        audit_qs = AuditEntry.objects.filter(created_at__lt=cutoff)
        notif_qs = Notification.objects.filter(created_at__lt=cutoff, read=True)
        audit_count = audit_qs.count()
        notif_count = notif_qs.count()

        if dry:
            self.stdout.write(f"  Would delete {audit_count} AuditEntry rows")
            self.stdout.write(f"  Would delete {notif_count} read Notification rows")
            return

        # Chunked deletes so we don't materialise millions of PKs in
        # memory and so each transaction stays short.
        deleted_audit = self._chunked_delete(audit_qs)
        deleted_notif = self._chunked_delete(notif_qs)
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted_audit} AuditEntry + {deleted_notif} Notification rows."
        ))

    def _chunked_delete(self, qs, chunk: int = 5_000) -> int:
        """Delete ``qs`` in ``chunk``-sized batches by primary key.

        Avoids loading the full PK list in memory and bypasses the
        ``Collector`` machinery's expensive cascade pre-fetch on huge
        tables.
        """
        deleted_total = 0
        while True:
            batch = list(qs.values_list("pk", flat=True)[:chunk])
            if not batch:
                break
            deleted, _ = qs.model.objects.filter(pk__in=batch).delete()
            deleted_total += deleted
            if len(batch) < chunk:
                break
        return deleted_total
