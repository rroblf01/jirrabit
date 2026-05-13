from django.contrib import admin

from .models import Epic, Project, Sprint


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "lead", "issue_counter")
    search_fields = ("key", "name")


@admin.register(Epic)
class EpicAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "done")


@admin.register(Sprint)
class SprintAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "status", "start_date", "end_date")
    list_filter = ("status", "project")
