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

from issues.models import Issue


class JQLError(ValueError):
    """Raised when the query references an unknown field or is malformed."""


FIELD_MAP = {
    "project": "project__key",
    "status": "status__name",
    "statusCategory": "status__category",
    "priority": "priority__name",
    "type": "issue_type__name",
    "label": "labels__name",
    "sprint": "sprint__name",
    "epic": "epic__name",
    "key": "key",
    # Added so `archived = true` is answerable. It was missing while the field
    # existed and was already filterable in the board, which meant an archived
    # issue could be found in one place and not the other.
    "archived": "archived",
}

#: Fields whose stored values differ from what callers write. Jira reports a
#: status category by display name ("To Do", "In Progress", "Done") while
#: ``Status.category`` stores the slug, so the values are translated rather than
#: matched literally. An agent writing ``statusCategory != Done`` is the single
#: most common JQL fragment there is.
CATEGORY_ALIASES = {
    "to do": "todo",
    "todo": "todo",
    "new": "todo",
    "backlog": "todo",
    "open": "todo",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "indeterminate": "in_progress",
    "done": "done",
    "closed": "done",
    "complete": "done",
    "completed": "done",
    "resolved": "done",
}

#: Fields backed by a many-to-many relation. "is EMPTY" means "has no related
#: rows", which needs a negated Exists rather than a negated __isnull — see
#: _m2m_empty_q.
M2M_FIELDS = {"label"}

USER_FIELDS = {"assignee", "reporter"}

#: Fields whose value is a boolean, so the string a query carries has to be
#: translated rather than handed to the ORM. Without this, `archived = false`
#: would return the *archived* issues: Django coerces a BooleanField value with
#: bool(), and bool("false") is True. A query that says the opposite of what it
#: means is the worst failure this parser can have.
BOOLEAN_FIELDS = {"archived"}

_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def _parse_bool(value: str, chunk: str) -> bool:
    """Translate a JQL boolean literal, refusing anything ambiguous."""
    text = str(value).strip().strip("\"'").lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise JQLError(f"'{column_named_in(chunk)}' es un campo booleano: usa true o false, no '{value}'.")


def column_named_in(chunk: str) -> str:
    """Best-effort field name for an error message, from the front of a clause."""
    return chunk.split("=")[0].split("!")[0].split("~")[0].strip() or chunk.strip()


#: JQL field names are case-insensitive, and the parser lowercases what it
#: captures, so lookups go through this map. FIELD_MAP keeps the canonical
#: spellings because that is what the "valid fields" error message should show.
FIELD_COLUMNS = {name.lower(): column for name, column in FIELD_MAP.items()}


def _column_for(field: str) -> str:
    """Resolve a (lowercased) JQL field name to its lookup path."""
    return FIELD_COLUMNS[field]


ORDER_MAP = {
    "created": "created_at",
    "updated": "updated_at",
    # Direction is applied below, so this must not carry its own sign. It used
    # to, which made "ORDER BY priority DESC" sort ascending.
    "priority": "priority__weight",
    "key": "key",
}

VALID_FIELDS = set(FIELD_MAP) | USER_FIELDS | {"text"}


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        inner = raw[1:-1]
        parts = [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]
        return parts
    return raw.strip("\"'")


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


def _translate_category(value):
    """Map a Jira status-category display name onto jirrabit's stored slug.

    An unrecognised value is returned lowercased rather than rejected, so a
    future category does not turn into a confusing "Campo desconocido" error
    about a field the caller spelled correctly.
    """
    return CATEGORY_ALIASES.get(value.strip().lower(), value.strip().lower())


def _validate_field(field: str):
    if field not in FIELD_COLUMNS and field not in USER_FIELDS and field != "text":
        valid = ", ".join(sorted(VALID_FIELDS))
        raise JQLError(f"Campo desconocido: '{field}'. Válidos: {valid}.")
    return field


def _m2m_empty_q(field: str, negate: bool) -> Q:
    """Build the Q for ``field is EMPTY`` on a many-to-many field.

    A negated ``__isnull`` cannot be used here. Django compiles a nullable
    relation as a LEFT OUTER JOIN, so ``NOT (col IS NOT NULL)`` is NULL — not
    true — for a row with no related object, and the negation silently excludes
    exactly the rows it is meant to match. A subquery has no such
    three-valued-logic trap.

    The related model and its back-reference are read off the model metadata so
    this stays generic as fields are added.
    """
    from django.db.models import Exists, OuterRef

    # FIELD_MAP maps a JQL name to a lookup path, so resolve the attribute name
    # back off Issue: "labels__name" -> "labels".
    attr_name = _column_for(field).split("__")[0]
    field_obj = Issue._meta.get_field(attr_name)
    related = field_obj.related_model
    back_reference = field_obj.remote_field.get_accessor_name()
    if back_reference is None:
        # A ManyToMany declared with related_name="+" has no reverse accessor to
        # subquery against. Django's own answer here is a TypeError about
        # keyword names, which says nothing about the actual problem.
        raise JQLError(
            f"Campo '{field}': no se puede comprobar si está vacío, "
            f"la relación no define un accessor inverso."
        )
    has_related = Exists(related.objects.filter(**{back_reference: OuterRef("pk")}))
    # "is EMPTY" is the absence of related rows; "is not EMPTY" is their
    # presence. ``has_related`` is negated exactly once, here.
    #
    # The declared return type is wider than Q on purpose: Exists and
    # ~Exists are expression objects, not Q, though Django's filter() accepts
    # either.
    return has_related if negate else ~has_related  # ty: ignore[invalid-return-type]


def _empty_q(field: str, negate: bool) -> Q:
    """Build the Q for ``field is EMPTY`` / ``field is not EMPTY``."""
    if field == "text":
        # No meaningful "empty description" semantics: an empty text search would
        # match every issue, so treat it as matching nothing.
        q = Q(pk__in=[])
    elif field in M2M_FIELDS:
        return _m2m_empty_q(field, negate)
    elif field in USER_FIELDS:
        q = Q(**{f"{field}__isnull": True})
    else:
        column = _column_for(field)
        q = Q(**{f"{column}__isnull": True})
    return ~q if negate else q


#: Operators that real JQL has and this subset does not, longest first so that
#: "=~" is never mistaken for "=" followed by a value beginning with "~".
UNSUPPORTED_OPERATORS = ("=~", "!~", "==", ">=", "<=", ">", "<")

#: What a caller gets told when it reaches for one of them. Naming the operator
#: and saying what to use instead is the whole point: "not supported" on its own
#: leaves a caller with no next move, and the obvious fallback is to try the same
#: query again.
_OPERATOR_HINT = {
    "~": " Para filtrar por texto usa '~', que ya hace coincidencia parcial.",
}


def _unsupported_operator_message(chunk: str) -> str:
    """Name the operator this subset lacks, and the ones it has."""
    found = ""
    for candidate in UNSUPPORTED_OPERATORS:
        if candidate in chunk:
            found = candidate
            break
    if found and "~" in found:
        # "=~" and "!~": the useful advice is the partial match, not the regex
        # the caller asked for.
        hint = _OPERATOR_HINT["~"]
    elif found.startswith(">"):
        hint = " No hay comparación numérica ni de fechas; usa el campo 'text' para buscar texto."
    else:
        hint = ""
    return (
        f"Operador no admitido: '{found or '?'}' en la cláusula '{chunk}'. "
        f"Operadores admitidos: = != ~ in, o 'is EMPTY' / 'is not EMPTY'." + hint
    )


def parse_jql(query: str):
    """Parse ``query`` and return ``(Q object, [order_fields])``.

    Raises :class:`JQLError` if the query references an unknown field or cannot
    be parsed. Empty queries return an empty ``Q`` and ``[]``.
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
        # ``field is EMPTY`` / ``field is not EMPTY``, checked before the
        # operator regex because "is" is not an operator it recognises.
        empty = re.match(r"^(\w+)\s+is\s+(not\s+)?empty$", chunk, flags=re.IGNORECASE)
        if empty:
            field = empty.group(1).lower()
            _validate_field(field)
            q &= _empty_q(field, negate=bool(empty.group(2)))
            continue

        m = re.match(r"^(\w+)\s*(=|!=|~|\bin\b)\s*(.+)$", chunk, flags=re.IGNORECASE)
        if not m:
            # Free text, but only when the fragment could not have been meant as
            # a clause. Treating a malformed clause as prose used to return an
            # empty result instead of an error, which reads to a caller as
            # "no matches" when the truth is "I did not understand you".
            #
            # The character class has to include < and >. The clause regex above
            # does not accept them as operators, so "storyPoints > 3" and
            # "created < 2026-01-01" arrived here and were searched for as
            # literal prose -- the same silent wrong answer, one step earlier.
            if re.search(r"[=!~<>]", chunk):
                raise JQLError(_unsupported_operator_message(chunk))
            q &= Q(summary__icontains=chunk) | Q(description__icontains=chunk)
            continue
        field, op, value = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()

        # A value made only of operator characters means the clause was cut
        # short, e.g. "assignee ==". The operator regex still matches those, so
        # the free-text guard below never sees them.
        if not value.strip("\"'") or not value.strip("\"'").strip("=!~<>"):
            raise JQLError(
                f"No se pudo interpretar la cláusula: '{chunk}'. Falta un valor. "
                f"Operadores admitidos: = != ~ in, o 'is EMPTY' / 'is not EMPTY'."
            )

        # A value that *starts* with operator characters is a longer operator the
        # subset does not have, not a value: "project =~ /x/" matched here as
        # project = "= /x/". `=~` is a real Jira operator, so an agent trained on
        # it sends it routinely, and the query then matched nothing at all --
        # "no results" where the truth is "I do not support that". Silently
        # absorbing the operator into the value is the one failure mode worth
        # being loud about.
        if value[0] in "=!~<>" and not value.startswith(('"', "'")):
            raise JQLError(_unsupported_operator_message(chunk))

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

        if field not in FIELD_COLUMNS:
            valid = ", ".join(sorted(VALID_FIELDS))
            raise JQLError(f"Campo desconocido: '{field}'. Válidos: {valid}.")

        column = _column_for(field)
        v = _parse_value(value)
        # ``field`` is lowercased by now, so compare against the lowercased name.
        # Translation is per-scalar here; the list case is handled in the "in"
        # branch below, since _parse_value has already turned "(a, b)" into a
        # list by this point.
        is_category = field == "statuscategory"
        if is_category and not isinstance(v, list):
            v = _translate_category(v)
        if field in BOOLEAN_FIELDS and not isinstance(v, list):
            # `is EMPTY` was already handled above, and `in` is left to the ORM
            # below: both carry a real boolean already.
            v = _parse_bool(v, chunk)
        if op == "=":
            q &= Q(**{column: v})
        elif op == "!=":
            q &= ~Q(**{column: v})
        elif op == "~":
            q &= Q(**{column + "__icontains": v})
        elif op == "in" and isinstance(v, list):
            if is_category:
                v = [_translate_category(item) for item in v]
            q &= Q(**{column + "__in": v})

    return q, order
