/**
 * Command palette (Ctrl/Cmd+K).
 *
 * Companion to the keyboard-shortcut layer in ``ux.js``. Opens a modal
 * search over issues, projects, sprints and saved filters. Backed by
 * ``/search/quickswitch/``.
 */
(function () {
  function inEditable(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select";
  }

  let paletteAbort = null;

  function openPalette() {
    if (document.getElementById("cmd-palette")) return;
    const wrap = document.createElement("div");
    wrap.id = "cmd-palette";
    wrap.className = "kbd-overlay";
    wrap.innerHTML = `
      <div class="cmd-modal" role="dialog" aria-label="Paleta de comandos">
        <input type="search" placeholder="Buscar tareas, proyectos, filtros…" autocomplete="off" autofocus>
        <ul class="cmd-results" role="listbox"></ul>
        <footer>↑↓ navegar · ↵ abrir · Esc cerrar</footer>
      </div>`;
    wrap.addEventListener("click", e => { if (e.target === wrap) close(); });
    document.body.appendChild(wrap);
    const input = wrap.querySelector("input");
    const list = wrap.querySelector(".cmd-results");
    let selected = 0;

    function close() {
      if (paletteAbort) paletteAbort.abort();
      wrap.remove();
    }

    function render(items) {
      if (!items.length) {
        list.innerHTML = `<li class="empty">Sin resultados</li>`;
        return;
      }
      list.innerHTML = items.map((it, idx) => `
        <li role="option" data-url="${it.url}" data-idx="${idx}" class="${idx === 0 ? 'active' : ''}">
          <span class="cmd-type">${it.type}</span>
          <span class="cmd-label">${it.label}</span>
          <span class="cmd-hint">${it.hint || ""}</span>
        </li>
      `).join("");
      selected = 0;
      list.querySelectorAll("li[data-url]").forEach(li => {
        li.onclick = () => location.assign(li.dataset.url);
      });
    }

    let timer;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { list.innerHTML = ""; return; }
      timer = setTimeout(async () => {
        if (paletteAbort) paletteAbort.abort();
        paletteAbort = new AbortController();
        try {
          const res = await fetch(`/search/quickswitch/?q=${encodeURIComponent(q)}`,
            { signal: paletteAbort.signal });
          if (!res.ok) return;
          const data = await res.json();
          render(data.items || []);
        } catch (e) { /* aborted */ }
      }, 150);
    });

    input.addEventListener("keydown", e => {
      const items = list.querySelectorAll("li[data-url]");
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (e.key === "Enter") {
        e.preventDefault();
        const li = items[selected];
        if (li) location.assign(li.dataset.url);
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!items.length) return;
        items[selected]?.classList.remove("active");
        selected = (selected + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[selected].classList.add("active");
        items[selected].scrollIntoView({ block: "nearest" });
      }
    });
  }

  document.addEventListener("keydown", e => {
    if (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      openPalette();
    } else if (e.key === "Escape") {
      const open = document.getElementById("cmd-palette");
      if (open) open.remove();
    }
  }, true);
})();
