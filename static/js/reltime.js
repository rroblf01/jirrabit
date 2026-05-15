/**
 * Convert any ``<time data-rel datetime="ISO">`` into a relative phrasing
 * ("hace 3 min", "en 2 días"). Re-runs on htmx swaps and every 60s.
 */
(function () {
  const MIN = 60, HOUR = 3600, DAY = 86400, WEEK = 604800, MONTH = 2629800, YEAR = 31557600;
  const PAST_UNITS = [
    [YEAR, "año", "años"],
    [MONTH, "mes", "meses"],
    [WEEK, "sem", "sem"],
    [DAY, "día", "días"],
    [HOUR, "h", "h"],
    [MIN, "min", "min"],
  ];

  function relative(iso) {
    const t = new Date(iso);
    if (isNaN(t.getTime())) return null;
    const diff = Math.round((Date.now() - t.getTime()) / 1000);
    const abs = Math.abs(diff);
    if (abs < 45) return diff >= 0 ? "ahora" : "en un instante";
    for (const [s, sing, plur] of PAST_UNITS) {
      if (abs >= s) {
        const n = Math.floor(abs / s);
        const label = n === 1 ? sing : plur;
        return diff >= 0 ? `hace ${n} ${label}` : `en ${n} ${label}`;
      }
    }
    return diff >= 0 ? "hace un momento" : "en unos segundos";
  }

  function scan(root) {
    (root || document).querySelectorAll('time[data-rel][datetime]').forEach(el => {
      const iso = el.getAttribute("datetime");
      const rel = relative(iso);
      if (rel) {
        if (!el.dataset.absolute) el.dataset.absolute = el.textContent.trim();
        el.textContent = rel;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.body.addEventListener("htmx:afterSwap", e => scan(e.target));
  setInterval(() => scan(document), 60000);
})();
