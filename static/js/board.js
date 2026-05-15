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
        if (dragged) col.appendChild(dragged);
        const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
        const form = new FormData();
        form.append('status', statusId);
        const res = await fetch(`/board/card/${key}/move/`, {
          method: 'POST',
          headers: { 'X-CSRFToken': csrf, 'HX-Request': 'true' },
          body: form,
        });
        if (res.ok && dragged) {
          const html = await res.text();
          const tmp = document.createElement('div');
          tmp.innerHTML = html.trim();
          const replacement = tmp.firstElementChild;
          if (replacement) {
            dragged.replaceWith(replacement);
            attach(replacement.parentElement);
          }
        }
        recountColumns();
      });
    });
    recountColumns(root);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.kanban').forEach(attach);
  });
  document.body.addEventListener('htmx:afterSwap', e => {
    if (e.target.classList && e.target.classList.contains('kanban')) attach(e.target);
    if (document.querySelector('.kanban')) recountColumns();
  });
})();
