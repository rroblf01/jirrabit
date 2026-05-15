/**
 * UX layer for jirrabit.
 *
 * - htmx loading bar: toggles ``body.htmx-busy`` while a request is in flight.
 * - Toast system: ``window.jirrabit.toast(text, kind)``. Auto-dismiss after 4s.
 * - Confirm modal: replaces native ``confirm`` for elements with
 *   ``data-confirm`` (preferred over ``hx-confirm``). Falls back to the
 *   native dialog if the modal can't be mounted.
 * - Mention autocomplete: any ``textarea[data-mentions]`` triggers a
 *   suggestion dropdown when typing ``@``.
 * - Copy-to-clipboard: ``button.copy-btn[data-copy="<text>"]``.
 */
(function () {
  const jirrabit = (window.jirrabit = window.jirrabit || {});

  // --- Toasts ------------------------------------------------------------
  function ensureStack() {
    let s = document.getElementById("toast-stack");
    if (!s) {
      s = document.createElement("div");
      s.id = "toast-stack";
      document.body.appendChild(s);
    }
    return s;
  }
  jirrabit.toast = function (text, kind, opts) {
    const stack = ensureStack();
    const node = document.createElement("div");
    node.className = "toast " + (kind || "info");
    const ttl = (opts && opts.ttl) || 4000;
    let inner = `<span>${text}</span>`;
    if (opts && opts.action && opts.actionLabel) {
      inner += `<button class="toast-action" type="button">${opts.actionLabel}</button>`;
    }
    inner += `<button class="toast-close" aria-label="cerrar">×</button>`;
    node.innerHTML = inner;
    node.querySelector(".toast-close").onclick = () => node.remove();
    if (opts && opts.action) {
      node.querySelector(".toast-action").onclick = () => {
        try { opts.action(); } catch (_e) {}
        node.remove();
      };
    }
    stack.appendChild(node);
    setTimeout(() => { if (node.isConnected) node.remove(); }, ttl);
    return node;
  };

  // --- Global spinner ----------------------------------------------------
  if (!document.getElementById("htmx-spinner")) {
    const bar = document.createElement("div");
    bar.id = "htmx-spinner";
    document.body.appendChild(bar);
  }
  document.body.addEventListener("htmx:beforeRequest", () => {
    document.body.classList.add("htmx-busy");
  });
  document.body.addEventListener("htmx:afterRequest", (e) => {
    document.body.classList.remove("htmx-busy");
    const status = e.detail.xhr.status;
    const trigger = e.detail.elt;
    const flash = trigger && trigger.dataset && trigger.dataset.toast;
    if (status >= 400) {
      jirrabit.toast(prettyError(e.detail.xhr, status), "err");
    } else {
      // Undo support: server sets X-Undo-URL + X-Undo-Message to surface a
      // 6-second toast with an "Undo" button that POSTs to the given URL.
      const undoUrl = e.detail.xhr.getResponseHeader("X-Undo-URL");
      const undoMsg = e.detail.xhr.getResponseHeader("X-Undo-Message");
      if (undoUrl && undoMsg) {
        const csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
        jirrabit.toast(undoMsg, "info", {
          ttl: 6000,
          actionLabel: "Deshacer",
          action: () => {
            fetch(undoUrl, {
              method: "POST",
              headers: csrf ? { "X-CSRFToken": csrf.value, "HX-Request": "true" } : { "HX-Request": "true" },
            }).then(r => {
              if (r.ok) {
                jirrabit.toast("Restaurado", "ok");
                setTimeout(() => location.reload(), 300);
              } else {
                jirrabit.toast("No se pudo deshacer", "err");
              }
            });
          },
        });
      } else if (flash) {
        jirrabit.toast(flash, "ok");
      }
    }
  });
  document.body.addEventListener("htmx:responseError", (e) => {
    jirrabit.toast(prettyError(e.detail.xhr, e.detail.xhr.status), "err");
  });
  function prettyError(xhr, status) {
    const txt = (xhr.responseText || "").trim();
    if (txt) {
      // Try JSON {"error": "..."} or {"detail": "..."}.
      try {
        const j = JSON.parse(txt);
        if (j && typeof j === "object") {
          if (j.error) return j.error;
          if (j.detail) return j.detail;
          if (Array.isArray(j.messages) && j.messages.length) return j.messages.join(" · ");
        }
      } catch (_e) { /* not JSON */ }
      // Plain text body, truncate to a sensible length and strip HTML/whitespace.
      const plain = txt.replace(/<[^>]+>/g, "").trim();
      if (plain && plain.length < 240) return plain;
    }
    const map = {
      400: "Datos inválidos. Revisa el formulario.",
      401: "Sesión expirada. Vuelve a iniciar sesión.",
      403: "No tienes permiso para hacer eso.",
      404: "No encontrado.",
      409: "Conflicto: el recurso ha cambiado, recarga.",
      413: "Archivo demasiado grande.",
      422: "Datos no válidos.",
      429: "Demasiadas peticiones, espera un momento.",
      500: "Error interno del servidor.",
      503: "Servicio no disponible.",
    };
    return map[status] || `Error ${status}`;
  }
  document.body.addEventListener("htmx:sendError", () => {
    jirrabit.toast("Sin conexión con el servidor", "err");
  });

  // --- Optimistic UI helper --------------------------------------------
  // Any element with ``data-optimistic="<selector>"`` (or just ``data-optimistic``)
  // triggers a class swap on the closest ``.card-issue``/``.comment`` while
  // the request is in flight. Reverts automatically on response.
  document.body.addEventListener("htmx:beforeRequest", (e) => {
    const el = e.detail && e.detail.elt;
    if (!el) return;
    const trigger = el.closest("[data-optimistic]");
    if (!trigger) return;
    const sel = trigger.dataset.optimistic;
    const target = sel
      ? trigger.closest(sel)
      : (trigger.closest(".card-issue, .comment, .issue-detail") || trigger);
    if (!target) return;
    target.classList.add("optimistic");
    e.detail.optimisticTarget = target;
  });
  document.body.addEventListener("htmx:afterRequest", (e) => {
    document.querySelectorAll(".optimistic").forEach(el => el.classList.remove("optimistic"));
  });

  // --- Online / offline indicator --------------------------------------
  function setOnlineState(online) {
    const old = document.getElementById("net-status");
    if (online) { if (old) old.remove(); return; }
    if (old) return;
    const bar = document.createElement("div");
    bar.id = "net-status";
    bar.textContent = "⚠ Sin conexión — los cambios se guardarán al recuperar la red";
    document.body.appendChild(bar);
  }
  window.addEventListener("online", () => {
    setOnlineState(true);
    jirrabit.toast("Conexión restablecida", "ok");
  });
  window.addEventListener("offline", () => setOnlineState(false));
  if (!navigator.onLine) setOnlineState(false);

  // --- Desktop notifications (opt-in) ----------------------------------
  jirrabit.enableDesktopNotifications = async function () {
    if (!("Notification" in window)) {
      jirrabit.toast("Tu navegador no soporta notificaciones de escritorio", "err");
      return;
    }
    if (Notification.permission === "granted") {
      jirrabit.toast("Notificaciones ya activadas", "ok");
      return;
    }
    const p = await Notification.requestPermission();
    if (p === "granted") {
      jirrabit.toast("Notificaciones activadas", "ok");
      new Notification("Jirrabit", { body: "Te avisaremos cuando haya novedades." });
    } else {
      jirrabit.toast("Permiso denegado", "err");
    }
  };

  // After a bell-badge swap, fire a Notification if count went up.
  let _lastUnread = parseInt(document.querySelector(".notif-link")?.dataset.count || "0", 10);
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const link = e.target && e.target.classList && e.target.classList.contains("notif-link")
      ? e.target
      : (e.target.querySelector ? e.target.querySelector(".notif-link") : null);
    if (!link) return;
    const n = parseInt(link.dataset.count || "0", 10);
    if (n > _lastUnread && Notification.permission === "granted") {
      try {
        new Notification("Jirrabit", {
          body: `Tienes ${n} notificaciones sin leer`,
          tag: "jirrabit-unread",
        });
      } catch (_e) { /* ignore */ }
    }
    _lastUnread = n;
  });

  // --- Topbar user-menu dropdown --------------------------------------
  document.addEventListener("click", (e) => {
    const menu = document.getElementById("user-menu");
    if (!menu) return;
    const btn = document.getElementById("user-menu-btn");
    if (btn && btn.contains(e.target)) {
      const open = menu.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      const pop = menu.querySelector(".user-menu-pop");
      if (pop) pop.hidden = !open;
      return;
    }
    if (!menu.contains(e.target)) {
      menu.classList.remove("open");
      const pop = menu.querySelector(".user-menu-pop");
      if (pop) pop.hidden = true;
      if (btn) btn.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const menu = document.getElementById("user-menu");
      if (menu && menu.classList.contains("open")) {
        menu.classList.remove("open");
        const pop = menu.querySelector(".user-menu-pop");
        if (pop) pop.hidden = true;
      }
    }
  });

  // --- Confirm modal -----------------------------------------------------
  // Intercept HTMX requests on elements with ``data-confirm`` and ask for
  // confirmation via our custom modal instead of native ``window.confirm``.
  document.body.addEventListener("htmx:confirm", (evt) => {
    const msg = evt.detail.question;
    if (!msg) return; // no hx-confirm set, let it pass
    evt.preventDefault();
    openConfirmModal(msg, () => evt.detail.issueRequest(true));
  });

  function openConfirmModal(message, onConfirm) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <h3>Confirmar</h3>
        <p style="margin:0; color:var(--ink-700);">${message}</p>
        <div class="actions">
          <button class="btn ghost" data-action="cancel">Cancelar</button>
          <button class="btn danger" data-action="ok">Aceptar</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    backdrop.querySelector('[data-action="ok"]').focus();
    function close() { backdrop.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) {
      if (e.key === "Escape") { close(); }
      else if (e.key === "Enter") { close(); onConfirm(); }
    }
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
    backdrop.querySelector('[data-action="cancel"]').onclick = close;
    backdrop.querySelector('[data-action="ok"]').onclick = () => { close(); onConfirm(); };
  }
  jirrabit.confirm = openConfirmModal;

  // --- Mention autocomplete ---------------------------------------------
  function attachMentions(textarea) {
    if (textarea.dataset.mentionsBound === "1") return;
    textarea.dataset.mentionsBound = "1";
    const wrap = document.createElement("span");
    wrap.className = "mention-wrap";
    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);

    let dropdown = null;
    let cursor = -1;
    let activeIdx = 0;

    function close() {
      if (dropdown) { dropdown.remove(); dropdown = null; }
      cursor = -1;
    }

    function render(users, prefix) {
      close();
      if (!users.length) return;
      dropdown = document.createElement("ul");
      dropdown.className = "mention-suggestions";
      users.forEach((u, i) => {
        const li = document.createElement("li");
        li.dataset.username = u.username;
        li.innerHTML = `@${u.username} <span style="color:var(--ink-500); font-size:12px;">— ${u.display}</span>`;
        if (i === activeIdx) li.classList.add("active");
        li.onmousedown = (e) => { e.preventDefault(); pick(u.username, prefix); };
        dropdown.appendChild(li);
      });
      const rect = textarea.getBoundingClientRect();
      const parentRect = wrap.getBoundingClientRect();
      dropdown.style.left = "0px";
      dropdown.style.top = (textarea.offsetTop + textarea.offsetHeight) + "px";
      wrap.appendChild(dropdown);
    }

    function pick(username, prefix) {
      const before = textarea.value.slice(0, cursor);
      const after = textarea.value.slice(textarea.selectionStart);
      const stripped = before.replace(/@[\w._-]*$/, "@" + username + " ");
      textarea.value = stripped + after;
      textarea.focus();
      const pos = stripped.length;
      textarea.setSelectionRange(pos, pos);
      close();
    }

    textarea.addEventListener("input", async () => {
      const pos = textarea.selectionStart;
      const slice = textarea.value.slice(0, pos);
      const m = slice.match(/@([\w._-]*)$/);
      if (!m) { close(); return; }
      cursor = pos - m[0].length;
      const q = m[1];
      if (!q) { close(); return; }
      const res = await fetch(`/accounts/mentions/search/?q=${encodeURIComponent(q)}`, {
        headers: { "HX-Request": "true" },
      });
      const html = await res.text();
      // Parse the server-rendered fragment to extract username + display.
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      const users = Array.from(tmp.querySelectorAll("li[data-username]")).map((li) => ({
        username: li.dataset.username,
        display: (li.textContent || "").replace(/^@\S+\s*—?\s*/, "").trim(),
      }));
      activeIdx = 0;
      render(users, q);
    });

    textarea.addEventListener("keydown", (e) => {
      if (!dropdown) return;
      const items = dropdown.querySelectorAll("li[data-username]");
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = (activeIdx + 1) % items.length; redrawActive(items); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = (activeIdx - 1 + items.length) % items.length; redrawActive(items); }
      else if (e.key === "Enter" || e.key === "Tab") {
        const active = items[activeIdx];
        if (active) { e.preventDefault(); pick(active.dataset.username, ""); }
      } else if (e.key === "Escape") { close(); }
    });
    textarea.addEventListener("blur", () => setTimeout(close, 120));

    function redrawActive(items) {
      items.forEach((li, i) => li.classList.toggle("active", i === activeIdx));
    }
  }
  function scanMentions(root) {
    (root || document).querySelectorAll("textarea[data-mentions]").forEach(attachMentions);
  }
  document.addEventListener("DOMContentLoaded", () => scanMentions(document));
  document.body.addEventListener("htmx:afterSwap", (e) => scanMentions(e.target));

  // --- Keyboard shortcuts ------------------------------------------------
  // Jira-style: ``g`` starts a navigation sequence (``g i`` → issues,
  // ``g p`` → projects, ``g b`` → board, ``g h`` → home, ``g a`` → help).
  // ``c`` opens "create issue" on the current project, ``/`` focuses the
  // global search. Disabled while typing in an input.
  const shortcutHelp = [
    ["Ctrl/⌘ K", "Paleta de comandos"],
    ["g h", "Home"],
    ["g p", "Proyectos"],
    ["g b", "Board del proyecto actual"],
    ["g i", "Lista de issues del proyecto actual"],
    ["c",   "Crear tarea en el proyecto actual"],
    ["e",   "(en una tarea) Avanzar estado"],
    ["a",   "(en una tarea) Asignar a mí"],
    ["s",   "(en una tarea) Empezar trabajo (asignar + iniciar timer)"],
    ["/",   "Foco en la búsqueda"],
    ["?",   "Mostrar esta ayuda"],
  ];

  function inTypingContext(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }

  function projectKeyFromUrl() {
    const m = location.pathname.match(/\/(?:projects|board|issues\/projects)\/([A-Z][A-Z0-9]+)/);
    return m ? m[1] : null;
  }

  function go(url) { window.location.href = url; }

  let gPending = false;
  let gTimeout = null;
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (inTypingContext(e.target)) return;

    if (e.key === "/") {
      e.preventDefault();
      const search = document.querySelector('.topbar input[type="search"]');
      if (search) search.focus();
      return;
    }
    if (e.key === "?") { e.preventDefault(); openShortcutHelp(); return; }
    if (e.key === "c") {
      const key = projectKeyFromUrl();
      if (key) { e.preventDefault(); go(`/issues/projects/${key}/new/`); }
      return;
    }
    // Issue-level shortcuts: only when viewing an issue detail page.
    const issueMatch = location.pathname.match(/^\/issues\/([A-Z0-9_-]+-\d+)\//i);
    if (issueMatch) {
      const issueKey = issueMatch[1];
      const csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
      const csrfVal = csrf ? csrf.value : "";
      if (e.key === "e") {
        e.preventDefault();
        fetch(`/issues/${issueKey}/advance/`, {
          method: "POST", headers: { "X-CSRFToken": csrfVal, "HX-Request": "true" },
        }).then(r => { if (r.ok) location.reload(); else jirrabit.toast("No se pudo avanzar", "err"); });
        return;
      }
      if (e.key === "a") {
        e.preventDefault();
        const uid = document.body.dataset.userId;
        if (!uid) return;
        const fd = new FormData();
        fd.append("value", uid);
        fetch(`/issues/${issueKey}/inline/assignee/`, {
          method: "POST", body: fd, headers: { "X-CSRFToken": csrfVal, "HX-Request": "true" },
        }).then(r => {
          if (r.ok) { jirrabit.toast("Asignado a ti", "ok"); setTimeout(() => location.reload(), 400); }
          else jirrabit.toast("No se pudo asignar", "err");
        });
        return;
      }
      if (e.key === "s") {
        e.preventDefault();
        fetch(`/issues/${issueKey}/start-work/`, {
          method: "POST", headers: { "X-CSRFToken": csrfVal, "HX-Request": "true" },
        }).then(r => {
          if (r.ok) { jirrabit.toast("Comenzando trabajo", "ok"); setTimeout(() => location.reload(), 400); }
          else jirrabit.toast("No se pudo iniciar", "err");
        });
        return;
      }
    }
    if (gPending) {
      gPending = false; clearTimeout(gTimeout);
      const key = projectKeyFromUrl();
      if (e.key === "h") { e.preventDefault(); go("/"); }
      else if (e.key === "p") { e.preventDefault(); go("/projects/"); }
      else if (e.key === "a") { e.preventDefault(); go("/help/"); }
      else if (e.key === "b" && key) { e.preventDefault(); go(`/board/${key}/`); }
      else if (e.key === "i" && key) { e.preventDefault(); go(`/issues/projects/${key}/list/`); }
      return;
    }
    if (e.key === "g") {
      gPending = true;
      gTimeout = setTimeout(() => { gPending = false; }, 800);
    }
  });

  function openShortcutHelp() {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    const rows = shortcutHelp.map(([k, d]) =>
      `<tr><td><kbd style="background:var(--ink-100); padding:2px 6px; border-radius:4px; font-family:monospace;">${k}</kbd></td><td style="padding-left:12px;">${d}</td></tr>`
    ).join("");
    backdrop.innerHTML = `
      <div class="modal">
        <h3>Atajos de teclado</h3>
        <table><tbody>${rows}</tbody></table>
        <div class="actions"><button class="btn" data-close>Cerrar</button></div>
      </div>`;
    document.body.appendChild(backdrop);
    function close() { backdrop.remove(); }
    backdrop.querySelector("[data-close]").onclick = close;
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
    document.addEventListener("keydown", function onEsc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", onEsc); }
    });
  }

  // --- Done celebration --------------------------------------------------
  // Fires a small particle burst from the center of an element. Triggered
  // by elements with ``data-celebrate`` or after an HTMX swap returns a
  // status badge in the "done" category.
  function celebrate(origin) {
    const rect = origin.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const colors = ["#22c55e", "#3b82f6", "#f59e0b", "#ec4899", "#8b5cf6"];
    for (let i = 0; i < 24; i++) {
      const d = document.createElement("div");
      d.className = "celebrate-burst";
      d.style.left = cx + "px";
      d.style.top = cy + "px";
      d.style.background = colors[i % colors.length];
      const angle = (Math.PI * 2 * i) / 24;
      const dist = 80 + Math.random() * 80;
      d.style.setProperty("--dx", Math.cos(angle) * dist + "px");
      d.style.setProperty("--dy", Math.sin(angle) * dist + "px");
      document.body.appendChild(d);
      setTimeout(() => d.remove(), 900);
    }
  }
  document.body.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-celebrate]");
    if (trigger) celebrate(trigger);
  });
  // Auto-celebrate after a status change that returns a 'done' badge.
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const badge = (e.detail && e.detail.target) ? e.detail.target.querySelector(".badge.done") : null;
    if (badge && e.detail.target.dataset.lastCategory !== "done") {
      celebrate(e.detail.target);
      e.detail.target.dataset.lastCategory = "done";
    }
  });
  jirrabit.celebrate = celebrate;

  // --- Mobile board gestures --------------------------------------------
  // On touch devices the kanban card supports horizontal swipe:
  //   left  → advance to next status (calls /issues/<key>/advance/)
  //   right → no-op (reserved for future "back to previous status")
  function attachSwipe(card) {
    if (card.dataset.swipeBound === "1") return;
    card.dataset.swipeBound = "1";
    let startX = null, startY = null, tracking = false;
    card.addEventListener("touchstart", (ev) => {
      if (ev.touches.length !== 1) return;
      startX = ev.touches[0].clientX;
      startY = ev.touches[0].clientY;
      tracking = true;
    }, { passive: true });
    card.addEventListener("touchend", async (ev) => {
      if (!tracking || startX === null) return;
      tracking = false;
      const t = ev.changedTouches[0];
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;
      if (Math.abs(dx) < 60 || Math.abs(dy) > 40) return;
      if (dx < 0) {
        // Swipe left → advance.
        const key = card.dataset.key;
        if (!key) return;
        const csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
        const fd = new FormData();
        const r = await fetch(`/issues/${key}/advance/`, {
          method: "POST", body: fd, headers: csrf ? { "X-CSRFToken": csrf.value } : {},
        });
        if (r.ok) {
          card.style.transition = "transform .2s, opacity .2s";
          card.style.transform = "translateX(-100%)";
          card.style.opacity = "0";
          setTimeout(() => location.reload(), 220);
        }
      }
    });
  }
  function scanSwipe(root) {
    (root || document).querySelectorAll(".kanban .card-issue").forEach(attachSwipe);
  }
  if ("ontouchstart" in window) {
    document.addEventListener("DOMContentLoaded", () => scanSwipe(document));
    document.body.addEventListener("htmx:afterSwap", (e) => scanSwipe(e.target));
  }

  // --- Comment permalink copy -------------------------------------------
  // Click a comment timestamp → smooth scroll + copy URL to clipboard.
  document.body.addEventListener("click", async (e) => {
    const anchor = e.target.closest("a.comment-anchor");
    if (!anchor) return;
    e.preventDefault();
    const id = anchor.dataset.commentId;
    if (!id) return;
    const url = location.origin + location.pathname + "#comment-" + id;
    try {
      await navigator.clipboard.writeText(url);
      jirrabit.toast("Enlace copiado", "ok");
    } catch (_) {}
    const target = document.getElementById("comment-" + id);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
    history.replaceState(null, "", "#comment-" + id);
  });
  // Scroll to #comment-N on page load.
  document.addEventListener("DOMContentLoaded", () => {
    const m = location.hash.match(/^#comment-(\d+)$/);
    if (!m) return;
    const target = document.getElementById("comment-" + m[1]);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("highlight");
      setTimeout(() => target.classList.remove("highlight"), 2200);
    }
  });

  // --- Side panel close button -------------------------------------------
  document.addEventListener("click", (e) => {
    if (e.target && e.target.id === "side-panel-close") {
      const panel = document.getElementById("side-panel");
      if (panel) panel.classList.remove("open");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const panel = document.getElementById("side-panel");
      if (panel && panel.classList.contains("open")) panel.classList.remove("open");
    }
  });

  // --- Markdown live preview + slash commands ----------------------------
  // Any textarea with ``data-md-preview="1"`` gets a sibling preview pane
  // that re-renders on input (debounced) via the server's markdown endpoint.
  function attachMdPreview(textarea) {
    if (textarea.dataset.mdPreviewBound === "1") return;
    textarea.dataset.mdPreviewBound = "1";
    const wrap = document.createElement("div");
    wrap.className = "md-editor";
    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);
    const pane = document.createElement("div");
    pane.className = "md-preview-pane";
    pane.setAttribute("aria-live", "polite");
    pane.innerHTML = '<div class="md-preview-empty">Vista previa…</div>';
    wrap.appendChild(pane);
    let t = null;
    async function update() {
      const fd = new FormData();
      fd.append("body", textarea.value || "");
      try {
        const csrf = document.querySelector('input[name=csrfmiddlewaretoken]');
        const headers = {};
        if (csrf) headers["X-CSRFToken"] = csrf.value;
        const r = await fetch("/md/preview/", { method: "POST", body: fd, headers });
        pane.innerHTML = (await r.text()) || '<div class="md-preview-empty">Vista previa…</div>';
      } catch (e) { /* ignore */ }
    }
    textarea.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(update, 250);
    });
    update();
  }

  // Slash commands: typing "/" at the start of a line opens a tiny menu.
  function attachSlash(textarea) {
    if (textarea.dataset.slashBound === "1") return;
    textarea.dataset.slashBound = "1";
    const snippets = [
      { key: "/code",  label: "Bloque de código",  insert: "```\n\n```" },
      { key: "/quote", label: "Cita",              insert: "> " },
      { key: "/h2",    label: "Encabezado H2",     insert: "## " },
      { key: "/h3",    label: "Encabezado H3",     insert: "### " },
      { key: "/list",  label: "Lista",             insert: "- " },
      { key: "/task",  label: "Lista de tareas",   insert: "- [ ] " },
      { key: "/link",  label: "Enlace",            insert: "[texto](url)" },
      { key: "/issue", label: "Referencia KEY-123", insert: "KEY-123" },
    ];
    let menu = null;
    let active = 0;
    function close() { if (menu) { menu.remove(); menu = null; } }
    function render(filtered, prefix) {
      close();
      menu = document.createElement("ul");
      menu.className = "slash-menu";
      menu.innerHTML = filtered.map((s, i) => `
        <li data-idx="${i}" ${i === active ? 'class="active"' : ""}>
          <code>${s.key}</code> <span style="color:var(--ink-500);">— ${s.label}</span>
        </li>`).join("");
      textarea.parentNode.appendChild(menu);
      const rect = textarea.getBoundingClientRect();
      menu.style.left = "0px";
      menu.style.top = (textarea.offsetTop + textarea.offsetHeight + 4) + "px";
      menu.querySelectorAll("li").forEach(li => {
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          pick(filtered[parseInt(li.dataset.idx)], prefix);
        });
      });
    }
    function pick(s, prefix) {
      const cur = textarea.selectionStart;
      const before = textarea.value.slice(0, cur).replace(/\/\w*$/, "");
      const after = textarea.value.slice(textarea.selectionStart);
      textarea.value = before + s.insert + after;
      textarea.focus();
      const pos = before.length + s.insert.length;
      textarea.setSelectionRange(pos, pos);
      close();
    }
    textarea.addEventListener("input", () => {
      const cur = textarea.selectionStart;
      const slice = textarea.value.slice(0, cur);
      const m = slice.match(/\/(\w*)$/);
      if (!m) { close(); return; }
      const q = m[1].toLowerCase();
      const filtered = snippets.filter(s => s.key.slice(1).startsWith(q));
      active = 0;
      if (filtered.length) render(filtered, q);
      else close();
    });
    textarea.addEventListener("keydown", (e) => {
      if (!menu) return;
      const items = menu.querySelectorAll("li");
      if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; render([...items].map(li => snippets[parseInt(li.textContent.match(/\/(\w+)/)[0].slice(1) === "" ? 0 : 0)]), ""); }
      else if (e.key === "Escape") { close(); }
    });
    textarea.addEventListener("blur", () => setTimeout(close, 120));
  }

  function scanMdAndSlash(root) {
    (root || document).querySelectorAll("textarea[data-md-preview]").forEach(attachMdPreview);
    (root || document).querySelectorAll("textarea[data-slash]").forEach(attachSlash);
  }
  document.addEventListener("DOMContentLoaded", () => scanMdAndSlash(document));
  document.body.addEventListener("htmx:afterSwap", (e) => scanMdAndSlash(e.target));

  // --- Quick switcher (Ctrl/Cmd+K) ---------------------------------------
  let qsOpen = false;
  let qsItems = [];
  let qsActive = 0;
  let qsTimer = null;

  function openQuickSwitch() {
    if (qsOpen) return;
    qsOpen = true;
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.id = "qs-backdrop";
    backdrop.innerHTML = `
      <div class="modal qs-modal" role="dialog" aria-modal="true" aria-label="Buscador rápido">
        <input id="qs-input" type="text" placeholder="Issue, proyecto, filtro…" autocomplete="off">
        <ul id="qs-list" class="typeahead-list" style="position:static; box-shadow:none; max-height:340px;"></ul>
        <div style="font-size:11px; color:var(--ink-500); margin-top:8px;">
          ↑↓ navegar · ↵ abrir · esc cerrar
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    const input = document.getElementById("qs-input");
    input.focus();
    function close() {
      qsOpen = false;
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) {
      if (e.key === "Escape") { close(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); qsActive = Math.min(qsActive + 1, qsItems.length - 1); renderItems(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); qsActive = Math.max(qsActive - 1, 0); renderItems(); }
      else if (e.key === "Enter") {
        if (qsItems[qsActive]) { window.location.href = qsItems[qsActive].url; }
      }
    }
    function renderItems() {
      const list = document.getElementById("qs-list");
      if (!list) return;
      if (!qsItems.length) { list.innerHTML = ""; return; }
      list.innerHTML = qsItems.map((it, i) => `
        <li data-idx="${i}" ${i === qsActive ? 'class="active"' : ""}>
          <a href="${it.url}">
            <span class="badge" style="font-size:9px;">${it.type}</span>
            <span style="flex:1;">${it.label.replace(/</g, "&lt;")}</span>
            <span style="color:var(--ink-500); font-size:11px;">${it.hint || ""}</span>
          </a>
        </li>`).join("");
      list.querySelectorAll("li").forEach(li => {
        li.addEventListener("mouseenter", () => { qsActive = parseInt(li.dataset.idx); renderItems(); });
      });
    }
    input.addEventListener("input", () => {
      clearTimeout(qsTimer);
      qsTimer = setTimeout(async () => {
        const q = input.value.trim();
        if (!q) { qsItems = []; qsActive = 0; renderItems(); return; }
        const res = await fetch("/search/quickswitch/?q=" + encodeURIComponent(q));
        const json = await res.json();
        qsItems = json.items || [];
        qsActive = 0;
        renderItems();
      }, 150);
    });
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
    document.addEventListener("keydown", onKey);
  }
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      openQuickSwitch();
    }
  });
  jirrabit.openQuickSwitch = openQuickSwitch;

  // --- Topbar typeahead: close on outside click ---------------------------
  document.addEventListener("click", (e) => {
    const wrap = document.getElementById("topbar-typeahead");
    if (!wrap) return;
    const form = wrap.closest("form");
    if (form && !form.contains(e.target)) {
      wrap.innerHTML = "";
    }
  });

  // --- Copy to clipboard -------------------------------------------------
  document.body.addEventListener("click", async (e) => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    const text = btn.dataset.copy || btn.previousElementSibling?.textContent || "";
    try {
      await navigator.clipboard.writeText(text);
      const orig = btn.textContent;
      btn.textContent = "✓ Copiado";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1500);
    } catch (err) {
      jirrabit.toast("No se pudo copiar al portapapeles", "err");
    }
  });
})();

// --- Inline edit save indicator ------------------------------------------
// Flash a small ✓ next to any ``.inline-edit`` that was just swapped in by
// a successful inline-edit response.
(function () {
  function flashSaved(el) {
    el.classList.add("just-saved");
    setTimeout(() => el.classList.remove("just-saved"), 1100);
  }
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const t = e.detail && e.detail.target;
    if (!t) return;
    if (t.classList && t.classList.contains("inline-edit")) {
      flashSaved(t);
    } else if (t.querySelector) {
      const ie = t.querySelector(".inline-edit");
      if (ie) flashSaved(ie);
    }
  });
})();
