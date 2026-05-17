"""Project-level permissions.

Roles (stored on ``projects.ProjectMembership.role``):

- ``admin``: read + write + manage settings, members, workflows, webhooks.
- ``member``: read + write issues/comments/attachments.
- ``viewer``: read only.

Superusers bypass all checks. Project leads are always treated as ``admin``.

These helpers are sync wrappers because they only touch local objects; the
async views that need them call them from inside ``arender``/``avalid``
already running in a thread pool, or fetch the ``ProjectMembership`` row
explicitly with ``await ... .aget()``.
"""


def is_super(user) -> bool:
    return bool(user and user.is_authenticated and user.is_superuser)


async def aget_role(user, project) -> str | None:
    """Return the user's role in ``project`` or ``None`` if not a member.

    Superusers and project leads always come back as ``"admin"``.
    """
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return "admin"
    if project.lead_id == user.pk:
        return "admin"
    from projects.models import ProjectMembership
    membership = await ProjectMembership.objects.filter(
        project=project, user=user
    ).afirst()
    return membership.role if membership else None


def can_view(role: str | None) -> bool:
    return role in {"admin", "member", "viewer"}


def can_edit(role: str | None) -> bool:
    return role in {"admin", "member"}


def can_admin(role: str | None) -> bool:
    return role == "admin"


async def aassert_can_view(user, project):
    if is_super(user):
        return "admin"
    role = await aget_role(user, project)
    if not can_view(role):
        from django.core.exceptions import PermissionDenied
        from django.utils.translation import gettext as _
        raise PermissionDenied(_("No tienes acceso a este proyecto."))
    return role


async def aassert_can_edit(user, project):
    if is_super(user):
        return "admin"
    role = await aget_role(user, project)
    if not can_edit(role):
        from django.core.exceptions import PermissionDenied
        from django.utils.translation import gettext as _
        raise PermissionDenied(_("Necesitas rol 'member' o superior."))
    return role


async def aassert_can_admin(user, project):
    if is_super(user):
        return "admin"
    role = await aget_role(user, project)
    if not can_admin(role):
        from django.core.exceptions import PermissionDenied
        from django.utils.translation import gettext as _
        raise PermissionDenied(_("Necesitas rol 'admin' en el proyecto."))
    return role
