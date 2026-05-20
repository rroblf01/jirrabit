// Native HTML5 drag-and-drop wired against HTMX endpoints.
(function () {
  function recountColumns(root) {
    const cols = (root || document).querySelectorAll('.kanban .column');
    cols.forEach(col => {
      const cards = col.querySelectorAll('.card-issue').length;
      const limit = col.dataset.wipLimit ? parseInt(col.dataset.wipLimit, 10) : null;
      const span = col.querySelector('.count');
      if (span) span.textContent = limit ? `${cards} / ${limit}` : `${cards}`;
      col.classList.toggle('wip-over', limit !== null && cards > limit);
    });
  }
  window.jirrabit = window.jirrabit || {};
  window.jirrabit.recountColumns = recountColumns;

  function attach(root) {
    const cards = root.querySelectorAll('.card-issue');
    const cols = root.querySelectorAll('.kanban .column');

    cards.forEach(card => {
      card.setAttribute('draggable', 'true');
      card.addEventListener('dragstart', ev => {
        card.classList.add('dragging');
        ev.dataTransfer.setData('text/plain', card.dataset.key);
        ev.dataTransfer.effectAllowed = 'move';
      });
      card.addEventListener('dragend', () => card.classList.remove('dragging'));
    });

    cols.forEach(col => {
      col.addEventListener('dragover', ev => { ev.preventDefault(); col.classList.add('drag-over'); });
      col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
      col.addEventListener('drop', async ev => {
        ev.preventDefault();
        col.classList.remove('drag-over');
        const key = ev.dataTransfer.getData('text/plain');
        const statusId = col.dataset.statusId;
        if (!key || !statusId) return;
        const dragged = document.querySelector(`.card-issue[data-key="${key}"]`);
        if (!dragged) return;
        // Optimistic: remember origin column so we can revert on failure.
        const originCol = dragged.parentElement;
        const originNext = dragged.nextElementSibling;
        col.appendChild(dragged);
        recountColumns();
        const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
        const form = new FormData();
        form.append('status', statusId);
        let res;
        try {
          res = await fetch(`/board/card/${key}/move/`, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrf, 'HX-Request': 'true' },
            body: form,
          });
        } catch (e) {
          if (originCol) originCol.insertBefore(dragged, originNext || null);
          recountColumns();
          if (window.jirrabit && window.jirrabit.toast)
            window.jirrabit.toast('Sin conexión, movimiento cancelado', 'err');
          return;
        }
        if (!res.ok) {
          if (originCol) originCol.insertBefore(dragged, originNext || null);
          recountColumns();
          const msg = (await res.text().catch(() => '')) || ('Error ' + res.status);
          if (window.jirrabit && window.jirrabit.toast)
            window.jirrabit.toast(msg.slice(0, 140), 'err');
          return;
        }
        const html = await res.text();
        const tmp = document.createElement('div');
        tmp.innerHTML = html.trim();
        const replacement = tmp.firstElementChild;
        if (replacement) {
          dragged.replaceWith(replacement);
          attach(replacement.parentElement);
        }
        recountColumns();
      });
    });
    recountColumns(root);
  }

  // Mark a .kanban after binding so we don't double-attach listeners on
  // morph swaps that preserve the same DOM nodes.
  function scan(root) {
    (root || document).querySelectorAll('.kanban').forEach(k => {
      if (k.dataset.dndBound === '1') return;
      k.dataset.dndBound = '1';
      attach(k);
    });
  }

  document.addEventListener('DOMContentLoaded', () => scan(document));
  // hx-boost swaps the whole <body> via morph; the swap target is body,
  // not the .kanban itself. Re-scan globally on every settle so freshly
  // morphed kanbans get their drag-and-drop wired.
  document.body.addEventListener('htmx:afterSettle', () => {
    scan(document);
    if (document.querySelector('.kanban')) recountColumns();
  });
  document.body.addEventListener('htmx:afterSwap', e => {
    if (e.target.classList && e.target.classList.contains('kanban')) {
      delete e.target.dataset.dndBound;  // re-bind: morph may have replaced child cards
      scan(e.target.parentElement || document);
    }
    if (document.querySelector('.kanban')) recountColumns();
  });
})();
