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
    "a": ["href", "title", "rel", "class"],
    "img": ["src", "alt", "title", "width", "height"],
    "input": ["type", "checked", "disabled"],
    "span": ["class"],
}
_ALLOWED_PROTOCOLS = ("http", "https", "mailto")
_MENTION_RE = re.compile(r"(?<!\w)@([\w._-]+)(?![\w._:-])")
_TEAM_RE = re.compile(r"(?<!\w)@team:([\w._-]+)")
# Issue keys look like ``PRJ-123``: 2+ uppercase letters, a dash, digits.
# We only autolink when surrounded by whitespace/punctuation so we don't
# mangle file paths like ``BUILD-456-debug.log`` (which would still match
# but appear inside an inline-code span — markdown protects those before
# this filter runs because we operate on the rendered HTML).
_ISSUE_KEY_RE = re.compile(r"(?<![A-Za-z0-9>/_-])([A-Z][A-Z0-9]+-\d+)(?![A-Za-z0-9_-])")


def render_markdown(text: str) -> str:
    """Markdown → safe HTML.

    Only ``http``, ``https`` and ``mailto`` URL schemes survive, so a
    payload like ``[click](javascript:alert(1))`` is stripped to text by
    bleach. Mentions get rewritten to relative links pointing at the user
    search page.
    """
    if not text:
        return ""
    html = md.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html",
    )
    html = _TEAM_RE.sub(
        r'<a href="/accounts/teams/\1/" class="mention team" rel="noopener">@team:\1</a>', html
    )
    html = _MENTION_RE.sub(
        r'<a href="/accounts/users/?q=\1" class="mention" rel="noopener">@\1</a>', html
    )
    html = _ISSUE_KEY_RE.sub(
        r'<a href="/issues/\1/" class="issue-key" rel="noopener">\1</a>', html
    )
    cleaned = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return cleaned


def extract_mentions(text: str) -> list[str]:
    # ``@team:foo`` would otherwise capture ``team``; strip team mentions
    # first so they don't pollute the user mention list.
    stripped = _TEAM_RE.sub("", text or "")
    return _MENTION_RE.findall(stripped)


def extract_teams(text: str) -> list[str]:
    return _TEAM_RE.findall(text or "")


def extract_issue_keys(text: str) -> list[str]:
    return _ISSUE_KEY_RE.findall(text or "")
