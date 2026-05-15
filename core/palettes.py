"""User-selectable color palettes.

Each palette overrides the ``--blue-*`` and (optionally) ``--ink-*`` CSS
variables defined in [static/css/jirrabit.css](../static/css/jirrabit.css).
Only the keys listed in a palette override the defaults, so adding a new
palette is just a matter of supplying the swatch.
"""
from django.utils.translation import gettext_lazy as _

# (slug, label, swatch) — swatch is the dominant accent color, used to
# render a preview chip in the profile form.
PALETTE_CHOICES = [
    ("blue", _("Azul (predeterminada)"), "#2563eb"),
    ("ocean", _("Océano"), "#0284c7"),
    ("forest", _("Bosque"), "#16a34a"),
    ("violet", _("Violeta"), "#9333ea"),
    ("sunset", _("Atardecer"), "#ea580c"),
    ("rose", _("Rosa"), "#e11d48"),
    ("midnight", _("Medianoche"), "#0f172a"),
    ("contrast", _("Alto contraste"), "#000000"),
]

# Each palette maps CSS variable name → value. Missing keys keep the
# default declared in the stylesheet.
PALETTES: dict[str, dict[str, str]] = {
    "blue": {},  # default — no overrides
    "ocean": {
        "--blue-50": "#f0f9ff",
        "--blue-100": "#e0f2fe",
        "--blue-200": "#bae6fd",
        "--blue-300": "#7dd3fc",
        "--blue-400": "#38bdf8",
        "--blue-500": "#0ea5e9",
        "--blue-600": "#0284c7",
        "--blue-700": "#0369a1",
        "--blue-800": "#075985",
        "--blue-900": "#0c4a6e",
    },
    "forest": {
        "--blue-50": "#f0fdf4",
        "--blue-100": "#dcfce7",
        "--blue-200": "#bbf7d0",
        "--blue-300": "#86efac",
        "--blue-400": "#4ade80",
        "--blue-500": "#22c55e",
        "--blue-600": "#16a34a",
        "--blue-700": "#15803d",
        "--blue-800": "#166534",
        "--blue-900": "#14532d",
    },
    "violet": {
        "--blue-50": "#faf5ff",
        "--blue-100": "#f3e8ff",
        "--blue-200": "#e9d5ff",
        "--blue-300": "#d8b4fe",
        "--blue-400": "#c084fc",
        "--blue-500": "#a855f7",
        "--blue-600": "#9333ea",
        "--blue-700": "#7e22ce",
        "--blue-800": "#6b21a8",
        "--blue-900": "#581c87",
    },
    "sunset": {
        "--blue-50": "#fff7ed",
        "--blue-100": "#ffedd5",
        "--blue-200": "#fed7aa",
        "--blue-300": "#fdba74",
        "--blue-400": "#fb923c",
        "--blue-500": "#f97316",
        "--blue-600": "#ea580c",
        "--blue-700": "#c2410c",
        "--blue-800": "#9a3412",
        "--blue-900": "#7c2d12",
    },
    "rose": {
        "--blue-50": "#fff1f2",
        "--blue-100": "#ffe4e6",
        "--blue-200": "#fecdd3",
        "--blue-300": "#fda4af",
        "--blue-400": "#fb7185",
        "--blue-500": "#f43f5e",
        "--blue-600": "#e11d48",
        "--blue-700": "#be123c",
        "--blue-800": "#9f1239",
        "--blue-900": "#881337",
    },
    # WCAG-AAA contrast: pure black/white with no gradients. Targets users
    # who need maximum contrast (vision impairment, glare, e-ink).
    "contrast": {
        "--blue-50": "#ffffff",
        "--blue-100": "#ffffff",
        "--blue-200": "#000000",
        "--blue-300": "#000000",
        "--blue-400": "#000000",
        "--blue-500": "#000000",
        "--blue-600": "#000000",
        "--blue-700": "#000000",
        "--blue-800": "#000000",
        "--blue-900": "#000000",
        "--ink-50": "#ffffff",
        "--ink-100": "#ffffff",
        "--ink-300": "#000000",
        "--ink-500": "#000000",
        "--ink-700": "#000000",
        "--ink-900": "#000000",
    },
    # Dark mode: flips the ink palette and overrides the body gradient
    # plus card backgrounds (handled by base.html via extra rules).
    "midnight": {
        "--blue-50": "#1e293b",
        "--blue-100": "#1e3a8a",
        "--blue-200": "#1e40af",
        "--blue-300": "#2563eb",
        "--blue-400": "#3b82f6",
        "--blue-500": "#60a5fa",
        "--blue-600": "#93c5fd",
        "--blue-700": "#bfdbfe",
        "--blue-800": "#dbeafe",
        "--blue-900": "#eff6ff",
        "--ink-50": "#0f172a",
        "--ink-100": "#1e293b",
        "--ink-300": "#475569",
        "--ink-500": "#94a3b8",
        "--ink-700": "#cbd5e1",
        "--ink-900": "#f1f5f9",
    },
}

# Extra ad-hoc CSS appended after the variable overrides. Used by the
# dark palette to invert backgrounds without inventing new variables.
PALETTE_EXTRAS: dict[str, str] = {
    "contrast": """
        body { background: white !important; color: black; }
        a, a:visited { color: #000080; text-decoration: underline; }
        a:hover { background: yellow; }
        .card, .sidebar { background: white !important; border: 2px solid black; box-shadow: none !important; }
        .btn { background: black !important; color: white !important; border: 2px solid black; }
        .btn.ghost { background: white !important; color: black !important; }
        .badge { background: white !important; color: black !important; border: 1px solid black; }
        .topbar { background: black !important; color: white; }
        .topbar a, .user-chip { color: white !important; background: black !important; }
        :focus { outline: 3px solid #ffbf00 !important; outline-offset: 2px; }
    """,
    "midnight": """
        body { background: linear-gradient(180deg, #0f172a 0%, #020617 220px) !important; color: #f1f5f9; }
        .card, .sidebar, .kanban .card-issue { background: #1e293b !important; color: #f1f5f9; }
        .kanban .column { background: #0f172a !important; }
        input, textarea, select { background: #1e293b !important; color: #f1f5f9; border-color: #334155; }
        th, td { color: #cbd5e1; border-color: #334155; }
        tr:hover td { background: #1e3a8a !important; }
        .comment .body, .issue-detail .sidebar-info { background: #0f172a !important; }
    """,
}


def palette_css(slug: str) -> str:
    """Build the ``<style>`` body for the given palette slug.

    Unknown slugs fall back to the default blue palette (empty output).
    """
    overrides = PALETTES.get(slug, {})
    parts = []
    if overrides:
        body = " ".join(f"{k}: {v};" for k, v in overrides.items())
        parts.append(f":root {{ {body} }}")
    extra = PALETTE_EXTRAS.get(slug)
    if extra:
        parts.append(extra.strip())
    return "\n".join(parts)


def palette_choices_simple() -> list[tuple[str, str]]:
    """``(slug, label)`` pairs for Django ``choices``."""
    return [(slug, str(label)) for slug, label, _swatch in PALETTE_CHOICES]
