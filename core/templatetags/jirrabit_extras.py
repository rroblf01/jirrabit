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
    return mark_safe(f'<time datetime="{iso}" data-rel="1" title="{pretty}">{pretty}</time>')


@register.filter
def markdown_text(value):
    """Markdown → plain text, for places too small for formatting.

    A table cell that truncates the raw source shows ``## Contexto`` and
    ``**en blanco**``; rendering it there would put a heading inside a row.
    This renders it, drops the tags and collapses whitespace, so the cell reads
    as prose. The result is a plain ``str``, so the template still escapes it.
    """
    if not value:
        return ""
    import html
    import re

    from django.utils.html import strip_tags

    from core.markdown import render_markdown

    return re.sub(r"\s+", " ", html.unescape(strip_tags(render_markdown(value)))).strip()
