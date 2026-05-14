"""Minimal JQL-lite parser.

Grammar (subset):
    expr     := clause ( AND clause )*
    clause   := field op value
    field    := project | status | priority | assignee | reporter | type
              | label | sprint | epic | text
    op       := '=' | '!=' | '~' | 'in'
    value    := bare_word | "quoted string" | (item, item, ...)
    ORDER BY field [ASC|DESC]

User fields (``assignee``, ``reporter``) match against ``username``,
``display_name`` and ``first_name + last_name`` so callers can type either
``erin_ux``, ``"Erin Soto"`` or just ``erin``.
"""
import re

from django.db.models import Q


class JQLError(ValueError):
    """Raised when the query references an unknown field or is malformed."""


FIELD_MAP = {
    "project": "project__key",
    "status": "status__name",
    "priority": "priority__name",
    "type": "issue_type__name",
    "label": "labels__name",
    "sprint": "sprint__name",
    "epic": "epic__name",
}

USER_FIELDS = {"assignee", "reporter"}

ORDER_MAP = {
    "created": "created_at",
    "updated": "updated_at",
    "priority": "-priority__weight",
    "key": "key",
}

VALID_FIELDS = set(FIELD_MAP) | USER_FIELDS | {"text"}


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        inner = raw[1:-1]
        parts = [p.strip().strip('"\'') for p in inner.split(",") if p.strip()]
        return parts
    return raw.strip('"\'')


def _user_match(prefix: str, value: str, op: str) -> Q:
    """Build a Q that matches a user across ``username``, ``display_name``
    and the concatenation of first+last name.

    ``op`` controls case sensitivity:
    - ``=``  → ``iexact`` against any of the three columns.
    - ``!=`` → negation of the above.
    - ``~``  → ``icontains`` against any of the three columns.
    """
    if op == "~":
        lookup = "icontains"
    else:
        lookup = "iexact"
    q = (
        Q(**{f"{prefix}__username__{lookup}": value})
        | Q(**{f"{prefix}__display_name__{lookup}": value})
        | Q(**{f"{prefix}__first_name__{lookup}": value})
        | Q(**{f"{prefix}__last_name__{lookup}": value})
    )
    # Also try first_name + " " + last_name combined for "Erin Soto" style values.
    if " " in value and op != "~":
        first, _, last = value.partition(" ")
        q |= Q(**{f"{prefix}__first_name__iexact": first, f"{prefix}__last_name__iexact": last})
    if op == "!=":
        return ~q
    return q


def parse_jql(query: str):
    """Parse ``query`` and return ``(Q object, [order_fields])``.

    Raises :class:`JQLError` if the query references an unknown field or
    cannot be parsed. Empty queries return an empty ``Q`` and ``[]``.
    """
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
        m = re.match(r'^(\w+)\s*(=|!=|~|\bin\b)\s*(.+)$', chunk, flags=re.IGNORECASE)
        if not m:
            # Free text fragment.
            q &= Q(summary__icontains=chunk) | Q(description__icontains=chunk)
            continue
        field, op, value = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()

        if field == "text":
            v = _parse_value(value)
            q &= Q(summary__icontains=v) | Q(description__icontains=v)
            continue

        if field in USER_FIELDS:
            v = _parse_value(value)
            if op == "in" and isinstance(v, list):
                sub = Q()
                for item in v:
                    sub |= _user_match(field, item, "=")
                q &= sub
            else:
                q &= _user_match(field, v, op)
            continue

        if field not in FIELD_MAP:
            valid = ", ".join(sorted(VALID_FIELDS))
            raise JQLError(f"Campo desconocido: '{field}'. Válidos: {valid}.")

        column = FIELD_MAP[field]
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
