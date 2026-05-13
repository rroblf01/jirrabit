"""Markdown rendering with HTML sanitisation."""
import re

import bleach
import markdown as md

_ALLOWED_TAGS = [
    "p", "br", "hr", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img", "table", "thead", "tbody", "tr", "th", "td",
    "del", "input", "span",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "input": ["type", "checked", "disabled"],
    "span": ["class"],
}
_MENTION_RE = re.compile(r"(?<!\w)@([\w._-]+)")


def render_markdown(text: str) -> str:
    if not text:
        return ""
    html = md.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html",
    )
    html = _MENTION_RE.sub(
        r'<a href="/accounts/users/?q=\1" class="mention">@\1</a>', html
    )
    cleaned = bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)
    return cleaned


def extract_mentions(text: str) -> list[str]:
    return _MENTION_RE.findall(text or "")
