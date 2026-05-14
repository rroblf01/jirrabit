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
  jirrabit.toast = function (text, kind) {
    const stack = ensureStack();
    const node = document.createElement("div");
    node.className = "toast " + (kind || "info");
    node.innerHTML = `<span>${text}</span><button aria-label="cerrar">×</button>`;
    node.querySelector("button").onclick = () => node.remove();
    stack.appendChild(node);
    setTimeout(() => node.remove(), 4000);
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
      jirrabit.toast(`Error ${status}: ${e.detail.xhr.statusText || ""}`, "err");
    } else if (flash) {
      jirrabit.toast(flash, "ok");
    }
  });
  document.body.addEventListener("htmx:responseError", (e) => {
    jirrabit.toast(`Error ${e.detail.xhr.status}`, "err");
  });
  document.body.addEventListener("htmx:sendError", () => {
    jirrabit.toast("Sin conexión con el servidor", "err");
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
