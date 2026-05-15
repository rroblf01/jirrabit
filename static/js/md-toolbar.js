/**
 * Lightweight markdown toolbar. Attaches buttons to any
 * ``textarea[data-md-toolbar]``. Operates on the current selection via
 * ``setRangeText`` so undo/redo keep working.
 */
(function () {
  const BUTTONS = [
    { label: "B", title: "Negrita (Ctrl+B)", key: "b", before: "**", after: "**" },
    { label: "I", title: "Cursiva (Ctrl+I)", key: "i", before: "*",  after: "*"  },
    { label: "</>", title: "Código",         key: null, before: "`",  after: "`"  },
    { label: "•",  title: "Lista",            key: null, before: "- ", after: "",   line: true },
    { label: "1.", title: "Lista numerada",   key: null, before: "1. ", after: "",  line: true },
    { label: "[]", title: "Checkbox",         key: null, before: "- [ ] ", after: "", line: true },
    { label: "🔗", title: "Enlace",           key: "k", before: "[",  after: "](url)" },
    { label: "{ }", title: "Bloque de código", key: null, before: "\n```\n", after: "\n```\n" },
    { label: "❝",  title: "Cita",             key: null, before: "> ", after: "",  line: true },
  ];

  function wrap(ta, before, after, isLine) {
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const value = ta.value;
    if (isLine) {
      // Prefix each selected line; if no selection, prefix current line.
      let lineStart = value.lastIndexOf("\n", start - 1) + 1;
      const sel = value.slice(lineStart, end || start);
      const replaced = sel.split("\n").map(l => before + l).join("\n");
      ta.setRangeText(replaced, lineStart, end || start, "end");
    } else {
      const sel = value.slice(start, end);
      ta.setRangeText(before + sel + after, start, end, "end");
      if (!sel) {
        // Place cursor between markers.
        const pos = start + before.length;
        ta.setSelectionRange(pos, pos);
      }
    }
    ta.focus();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function attach(ta) {
    if (ta.dataset.mdToolbarBound === "1") return;
    ta.dataset.mdToolbarBound = "1";
    const bar = document.createElement("div");
    bar.className = "md-toolbar";
    BUTTONS.forEach(b => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "md-tool";
      btn.textContent = b.label;
      btn.title = b.title;
      btn.onclick = () => wrap(ta, b.before, b.after, !!b.line);
      bar.appendChild(btn);
    });
    ta.parentNode.insertBefore(bar, ta);
    // Keyboard binds (Ctrl+B / Ctrl+I / Ctrl+K).
    ta.addEventListener("keydown", e => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const b = BUTTONS.find(x => x.key && x.key === e.key.toLowerCase());
      if (b) { e.preventDefault(); wrap(ta, b.before, b.after, !!b.line); }
    });
  }

  function scan(root) {
    (root || document).querySelectorAll("textarea[data-md-toolbar]").forEach(attach);
  }
  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.body.addEventListener("htmx:afterSwap", e => scan(e.target));
})();
