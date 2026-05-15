"""Loose natural-language date parser.

Handles common english+spanish quick phrases that show up in due-date
fields: "today", "hoy", "tomorrow", "mañana", "next friday", "viernes",
"in 3 days", "en 5 días", "5d", "2w" and ISO ``YYYY-MM-DD``.

Returns a ``datetime.date`` or ``None`` if nothing matches. Designed to
be forgiving: if the input is unparseable, the caller falls back to the
original picker.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

_DAY_NAMES = {
    # english
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    # spanish
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
    "lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6,
}

_TODAY_WORDS = {"today", "hoy"}
_TOMORROW_WORDS = {"tomorrow", "manana", "mañana"}
_NEXT_PREFIXES = ("next ", "próximo ", "proximo ")
_IN_DAYS_RE = re.compile(r"^(?:in|en)\s+(\d+)\s*(?:days?|días?|dia)?\s*$", re.I)
_SHORT_RE = re.compile(r"^(\d+)\s*([dwm])$", re.I)
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_due_date(text: str, today: date | None = None) -> date | None:
    if not text:
        return None
    s = text.strip().lower()
    today = today or date.today()

    if iso := _ISO_RE.match(s):
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    if s in _TODAY_WORDS:
        return today
    if s in _TOMORROW_WORDS:
        return today + timedelta(days=1)
    if m := _IN_DAYS_RE.match(s):
        return today + timedelta(days=int(m[1]))
    if m := _SHORT_RE.match(s):
        n, unit = int(m[1]), m[2].lower()
        if unit == "d":
            return today + timedelta(days=n)
        if unit == "w":
            return today + timedelta(weeks=n)
        if unit == "m":
            return today + timedelta(days=30 * n)
    # Bare or "next" prefixed weekday.
    bare = s
    is_next = False
    for prefix in _NEXT_PREFIXES:
        if s.startswith(prefix):
            bare = s[len(prefix):]
            is_next = True
            break
    if bare in _DAY_NAMES:
        target = _DAY_NAMES[bare]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7  # "monday" means *next* monday if today is monday too
        if is_next and delta < 7:
            delta += 7
        return today + timedelta(days=delta)
    return None
