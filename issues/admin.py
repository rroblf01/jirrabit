from django.contrib import admin

from .models import (
    Attachment,
    Comment,
    HistoryEntry,
    Issue,
    IssueType,
    Label,
    Priority,
    Status,
)


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("key", "summary", "project", "status", "priority", "assignee", "updated_at")
    list_filter = ("status", "priority", "issue_type", "project")
    search_fields = ("key", "summary", "description")
    autocomplete_fields = ()


admin.site.register(IssueType)
admin.site.register(Status)
admin.site.register(Priority)
admin.site.register(Label)
admin.site.register(Comment)
admin.site.register(Attachment)
admin.site.register(HistoryEntry)
