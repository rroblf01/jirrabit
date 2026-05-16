/**
 * Form draft autosave to localStorage.
 *
 * Auto-saves the value of any ``textarea[name]`` and any
 * ``input[name="summary"]`` inside a ``<form>`` while the user types.
 * Restores on page load if the field is empty. Cleared after a successful
 * submit. Shows a "Guardado a las HH:MM" indicator at the bottom of the
 * form while editing.
 *
 * Keys: ``draft:<path>:<form-action>:<field-name>``.
 */
(function () {
  const PREFIX = "draft:";
  const DEBOUNCE = 700;

  function keyFor(field) {
    const form = field.closest("form");
    if (!form) return null;
    const action = form.getAttribute("action") || location.pathname;
    return `${PREFIX}${location.pathname}::${action}::${field.name || "_"}`;
  }

  function indicator(form) {
    let el = form.querySelector(".draft-indicator");
    if (!el) {
      el = document.createElement("div");
      el.className = "draft-indicator";
      form.appendChild(el);
    }
    return el;
  }

  function note(form, text, kind) {
    const el = indicator(form);
    el.textContent = text;
    el.dataset.kind = kind || "";
  }

  function watch(field) {
    if (field.dataset.draftBound === "1") return;
    field.dataset.draftBound = "1";
    const k = keyFor(field);
    if (!k) return;

    // Restore.
    if (!field.value.trim()) {
      try {
        const saved = localStorage.getItem(k);
        if (saved) {
          field.value = saved;
          note(field.closest("form"), "Borrador recuperado · " + new Date().toLocaleTimeString().slice(0, 5), "restored");
        }
      } catch (_e) {}
    }

    let timer;
    field.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        try {
          if (field.value.trim()) {
            localStorage.setItem(k, field.value);
            note(field.closest("form"),
                 "Guardado como borrador " + new Date().toLocaleTimeString().slice(0, 5),
                 "saved");
          } else {
            localStorage.removeItem(k);
          }
        } catch (_e) {}
      }, DEBOUNCE);
    });

    // Clear on successful submit.
    const form = field.closest("form");
    if (form && !form.dataset.draftSubmitBound) {
      form.dataset.draftSubmitBound = "1";
      const clear = () => {
        try {
          Object.keys(localStorage)
            .filter(key => key.startsWith(PREFIX) && key.includes("::" + (form.getAttribute("action") || location.pathname) + "::"))
            .forEach(key => localStorage.removeItem(key));
        } catch (_e) {}
        const el = form.querySelector(".draft-indicator");
        if (el) el.remove();
      };
      form.addEventListener("submit", clear);
      // htmx: clear after successful 2xx response.
      form.addEventListener("htmx:afterRequest", e => {
        if (e.detail && e.detail.xhr && e.detail.xhr.status >= 200 && e.detail.xhr.status < 400) clear();
      });
    }
  }

  function scan(root) {
    (root || document).querySelectorAll("form textarea[name], form input[name='summary']").forEach(watch);
  }

  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.body.addEventListener("htmx:afterSwap", e => scan(e.target));

  // Expose for the profile/drafts page.
  window.jirrabit = window.jirrabit || {};
  window.jirrabit.listDrafts = function () {
    const out = [];
    try {
      Object.keys(localStorage).forEach(k => {
        if (k.startsWith(PREFIX)) out.push({ key: k, value: localStorage.getItem(k) });
      });
    } catch (_e) {}
    return out;
  };
  window.jirrabit.deleteDraft = function (k) {
    try { localStorage.removeItem(k); } catch (_e) {}
  };
})();
