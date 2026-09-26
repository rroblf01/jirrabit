"""Give every issue a rank inside its board column.

Issue.rank existed but nothing ever wrote it, so the kanban fell back to
``-updated_at`` and cards could not be placed by hand. Now that rank is a dense
0-based index within a ``(project, status)`` column, existing rows all hold the
0 default and would collide on that ordering.

This assigns each card the position it already effectively had, reading the old
``-updated_at`` order, so a board looks identical the moment the migration
lands and only changes once somebody drags something. The reverse is not true:
backdating the ranks would silently reorder every existing board.

Doing this with bulk ``UPDATE`` rather than ``save()`` matters twice over: the
four post_save receivers would otherwise write an audit row and fire a webhook
for every issue in the database during a migration, and ``Issue.save()`` now
computes a key and a rank of its own.
"""

from django.db import migrations, models


def assign_ranks(apps, schema_editor):
    Issue = apps.get_model("issues", "Issue")

    # One pass to find the columns, then rank inside each. ``updated_at``
    # descending is the order the board was already rendering, so the numbers
    # assigned here are the order users were looking at.
    groups = Issue.objects.values_list("project_id", "status_id").distinct().order_by()
    for project_id, status_id in groups:
        issues = (
            Issue.objects.filter(project_id=project_id, status_id=status_id)
            .order_by("-updated_at", "pk")
            .values_list("pk", flat=True)
        )
        for index, pk in enumerate(issues):
            Issue.objects.filter(pk=pk).update(rank=index)


def noop(apps, schema_editor):
    """Nothing to undo: rank's previous value was always the 0 default."""


class Migration(migrations.Migration):

    dependencies = [
        ("issues", "0007_comment_deleted_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="issue",
            name="rank",
            field=models.FloatField(
                default=0, help_text="Position within its board column, 0-based"
            ),
        ),
        migrations.RunPython(assign_ranks, noop),
    ]
