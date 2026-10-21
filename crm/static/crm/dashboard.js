// Dashboard customizing: drag widgets to reorder, drag the corner to resize, hide, add, reset.
// The layout is saved per person on the server (crm/prefs.py, key "dashboard").
(function () {
  const grid = document.getElementById('widgets');
  if (!grid) return;
  const bar = document.querySelector('.customize-bar');
  const WIDTHS = [3, 4, 6, 8, 12];
  const widgets = () => [...grid.querySelectorAll('.widget')];
  const layout = () => widgets().map(w => ({id: w.dataset.widget, w: +w.dataset.w, h: +w.dataset.h || 0}));
  let saving = null;
  const save = () => { clearTimeout(saving); saving = setTimeout(() => window.acmiPrefs.save('dashboard', layout()), 250); };

  function setMode(on) {
    grid.classList.toggle('customizing', on);
    bar.hidden = !on;
    document.querySelectorAll('.hero-btn[data-customize]').forEach(b => { b.textContent = on ? 'Done customizing' : 'Customize dashboard'; });
    const url = new URL(location.href);
    on ? url.searchParams.set('customize', '1') : url.searchParams.delete('customize');
    history.replaceState(null, '', url);
  }
  document.querySelectorAll('[data-customize]').forEach(b => b.addEventListener('click', () => setMode(!grid.classList.contains('customizing'))));

  function setWidth(w, n) {
    w.dataset.w = n; w.style.setProperty('--w', n);
    w.querySelectorAll('[data-width]').forEach(b => b.classList.toggle('on', +b.dataset.width === n));
  }
  function setHeight(w, h) {
    w.dataset.h = h;
    h ? w.style.setProperty('--h', h + 'px') : w.style.removeProperty('--h');
  }

  grid.addEventListener('click', e => {
    const w = e.target.closest('.widget');
    if (!w || !grid.classList.contains('customizing')) return;
    const width = e.target.closest('[data-width]');
    if (width) { setWidth(w, +width.dataset.width); save(); }
    if (e.target.closest('[data-hide]')) {
      w.remove(); save();
      const add = document.querySelector(`[data-add-widget="${w.dataset.widget}"]`);
      if (add) add.disabled = false;
    }
  });
  // Double-clicking the corner goes back to fitting the content.
  grid.addEventListener('dblclick', e => { const w = e.target.closest('.widget-resize')?.closest('.widget'); if (w) { setHeight(w, 0); save(); } });

  document.querySelectorAll('[data-add-widget]').forEach(b => b.addEventListener('click', async () => {
    await window.acmiPrefs.save('dashboard', [...layout(), {id: b.dataset.addWidget, w: 6, h: 0}]);
    location.href = location.pathname + '?customize=1#widgets';  // the new widget's content comes from the server
  }));
  document.querySelector('[data-reset-dashboard]')?.addEventListener('click', async () => {
    if (!confirm('Put the dashboard back to the standard layout?')) return;
    await window.acmiPrefs.save('dashboard', null);
    location.href = location.pathname;
  });

  // Reorder: drag a widget by its title bar onto another widget.
  let dragged = null;
  grid.addEventListener('dragstart', e => {
    const head = e.target.closest?.('.widget-head');
    if (!head || !grid.classList.contains('customizing')) { if (head) e.preventDefault(); return; }
    dragged = head.closest('.widget');
    dragged.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragged.dataset.widget);
  });
  grid.addEventListener('dragover', e => {
    if (!dragged) return;
    const over = e.target.closest('.widget');
    if (!over || over === dragged) return;
    e.preventDefault();
    const r = over.getBoundingClientRect();
    const after = (e.clientY - r.top) > r.height / 2 || (e.clientX - r.left) > r.width * 0.66;
    over[after ? 'after' : 'before'](dragged);
  });
  grid.addEventListener('drop', e => { if (dragged) e.preventDefault(); });
  grid.addEventListener('dragend', () => { if (!dragged) return; dragged.classList.remove('dragging'); dragged = null; save(); });

  // Resize: drag the bottom-right corner. Width snaps to quarter, third, half, two thirds or full; height is free.
  grid.addEventListener('pointerdown', e => {
    const handle = e.target.closest('.widget-resize');
    if (!handle || !grid.classList.contains('customizing')) return;
    e.preventDefault();
    const w = handle.closest('.widget'), body = w.querySelector('.widget-body');
    const left = w.getBoundingClientRect().left, top = body.getBoundingClientRect().top;
    const col = grid.getBoundingClientRect().width / 12;
    handle.setPointerCapture(e.pointerId);
    w.classList.add('resizing');
    const move = ev => {
      const span = (ev.clientX - left) / col;
      const n = WIDTHS.reduce((best, x) => Math.abs(x - span) < Math.abs(best - span) ? x : best, 12);
      if (n !== +w.dataset.w) setWidth(w, n);
      setHeight(w, Math.max(120, Math.round(ev.clientY - top)));
    };
    const up = () => {
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up);
      w.classList.remove('resizing'); save();
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
  });
})();
