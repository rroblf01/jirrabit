"""Minimal JQL-lite parser.

Grammar (subset):
    expr     := clause ( AND clause )*
    clause   := field op value
    field    := project | status | priority | assignee | reporter | type | label | sprint | epic | text
    op       := '=' | '!=' | '~' | 'in'
    value    := bare_word | "quoted string" | (item, item, ...)
    text "free text"  -> ICONTAINS over summary/description
    ORDER BY field [ASC|DESC]
"""
import re
import shlex

from django.db.models import Q

FIELD_MAP = {
    "project": "project__key",
    "status": "status__name",
    "priority": "priority__name",
    "assignee": "assignee__username",
    "reporter": "reporter__username",
    "type": "issue_type__name",
    "label": "labels__name",
    "sprint": "sprint__name",
    "epic": "epic__name",
}

ORDER_MAP = {
    "created": "created_at",
    "updated": "updated_at",
    "priority": "-priority__weight",
    "key": "key",
}


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        inner = raw[1:-1]
        parts = [p.strip().strip('"\'') for p in inner.split(",") if p.strip()]
        return parts
    return raw.strip('"\'')


def parse_jql(query: str):
    """Return (Q object, order_list)."""
    q = Q()
    order: list[str] = []
    if not query:
        return q, order
    query = query.strip()

    m = re.search(r"\bORDER BY\b\s+(.*)$", query, re.IGNORECASE)
    if m:
        order_clause = m.group(1)
        query = query[: m.start()].strip()
        for piece in [p.strip() for p in order_clause.split(",") if p.strip()]:
            parts = piece.split()
            field = parts[0].lower()
            direction = parts[1].upper() if len(parts) > 1 else "ASC"
            mapped = ORDER_MAP.get(field, field)
            order.append(("-" + mapped) if direction == "DESC" else mapped)

    if not query:
        return q, order

    chunks = [c.strip() for c in re.split(r"\s+AND\s+", query, flags=re.IGNORECASE) if c.strip()]
    for chunk in chunks:
        m = re.match(
            r'^(\w+)\s*(=|!=|~|\bin\b)\s*(.+)$',
            chunk,
            flags=re.IGNORECASE,
        )
        if not m:
            q &= Q(summary__icontains=chunk) | Q(description__icontains=chunk)
            continue
        field, op, value = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        if field == "text":
            v = _parse_value(value)
            q &= Q(summary__icontains=v) | Q(description__icontains=v)
            continue
        column = FIELD_MAP.get(field)
        if not column:
            continue
        v = _parse_value(value)
        if op == "=":
            q &= Q(**{column: v})
        elif op == "!=":
            q &= ~Q(**{column: v})
        elif op == "~":
            q &= Q(**{column + "__icontains": v})
        elif op == "in" and isinstance(v, list):
            q &= Q(**{column + "__in": v})
    return q, order
