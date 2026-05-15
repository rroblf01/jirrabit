"""Template tags/filters for jirrabit UI."""
from django import template
from django.utils.dateformat import format as date_format
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def rel_time(value, fmt="d M H:i"):
    """Render a datetime wrapped in ``<time>`` so the client JS can swap it
    for a relative phrasing (e.g. "hace 3 min"). Returns "—" when empty.
    """
    if not value:
        return "—"
    try:
        iso = value.isoformat()
    except AttributeError:
        return value
    pretty = date_format(value, fmt)
    return mark_safe(
        f'<time datetime="{iso}" data-rel="1" title="{pretty}">{pretty}</time>'
    )
