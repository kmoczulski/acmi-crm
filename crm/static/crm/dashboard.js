// The dashboard grid: every widget has a column (x), a row (y), a width in columns (w, 2-12) and a height (h, px;
// 0 = fit its content). Widgets go anywhere: side by side, stacked beside a tall one. Moving one pushes the others
// out of its way and everything floats up to close gaps. Drag by the title (mouse) or the ⠿ handle (mouse or touch),
// any time; the corner resizes. Keyboard: focus ⠿, arrows move, Shift + arrows resize. Below 1000px wide the
// widgets stack in their order, with ↑ ↓ to reorder. Saved per person (crm/prefs.py, key "dashboard").
(function () {
  const grid = document.getElementById('widgets');
  if (!grid) return;
  const bar = document.querySelector('.customize-bar');
  const live = document.getElementById('widgets-live');
  const COLS = 12, ROW = 8, GAP = 16;  // a grid row is 8px; widgets keep 16px between them
  const say = t => { if (live) { live.textContent = ''; setTimeout(() => { live.textContent = t; }, 30); } };
  const widgets = () => [...grid.querySelectorAll(':scope > .widget')];
  const narrow = () => window.matchMedia('(max-width: 1000px)').matches;
  const title = w => w.querySelector('.widget-head h3')?.textContent.trim() || 'Widget';
  const num = (v, d) => (v === undefined || v === '' || isNaN(+v)) ? d : +v;
  let dragging = null;  // the widget being dragged, while it is

  // --- Layout ---------------------------------------------------------------
  const pos = w => ({x: num(w.dataset.x, null), y: num(w.dataset.y, null), w: num(w.dataset.w, 6)});
  const rowsOf = w => Math.max(1, Math.ceil((w.offsetHeight + GAP) / ROW));
  const hits = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.rows && b.y < a.y + a.rows;

  // Place `moving` (if any) where it was put, then the rest in reading order: each pushed below anything it hits,
  // then floated up as far as it goes.
  function pack(moving) {
    const items = widgets().map(el => ({el, ...pos(el), rows: rowsOf(el)}));
    // Widgets with no saved place (old layouts, newly added) go to the first free spot, in order.
    const placed = items.filter(i => i.y !== null);
    for (const i of items.filter(i => i.y === null)) {
      i.w = Math.min(i.w, COLS);
      outer: for (let y = 0; ; y += 1) {
        for (let x = 0; x + i.w <= COLS; x += 1) {
          if (!placed.some(p => hits({...i, x, y}, p))) { i.x = x; i.y = y; break outer; }
        }
      }
      placed.push(i);
    }
    const order = items.slice().sort((a, b) => (a.el === moving ? -1 : b.el === moving ? 1 : a.y - b.y || a.x - b.x));
    const done = [];
    for (const i of order) {
      i.x = Math.max(0, Math.min(i.x, COLS - i.w));
      i.y = Math.max(0, i.y);
      let bump;
      while ((bump = done.find(d => hits(i, d)))) i.y = bump.y + bump.rows;
      while (i.y > 0 && !done.some(d => hits({...i, y: i.y - 1}, d))) i.y -= 1;
      done.push(i);
    }
    for (const i of items) {
      i.el.dataset.x = i.x; i.el.dataset.y = i.y;
      i.el.style.gridColumn = `${i.x + 1} / span ${i.w}`;
      i.el.style.gridRow = `${i.y + 1} / span ${i.rows}`;
    }
    // Reading order for small screens, screen readers and the ↑ ↓ buttons.
    items.sort((a, b) => a.y - b.y || a.x - b.x).forEach((i, n) => { i.el.style.order = n; });
    grid.classList.add('placed');
    return items;
  }

  const layout = () => widgets().map(w => ({id: w.dataset.widget, x: +w.dataset.x, y: +w.dataset.y, w: +w.dataset.w,
                                             h: +w.dataset.h || 0}));
  let saving = null;
  const save = () => { clearTimeout(saving); saving = setTimeout(() => window.acmiPrefs.save('dashboard', layout()), 300); };

  function setWidth(w, n) {
    n = Math.max(2, Math.min(COLS, n));
    w.dataset.w = n; w.style.setProperty('--w', n);
    w.querySelectorAll('[data-width]').forEach(b => b.classList.toggle('on', +b.dataset.width === n));
  }
  function setHeight(w, h) {
    w.dataset.h = h;
    h ? w.style.setProperty('--h', h + 'px') : w.style.removeProperty('--h');
  }

  pack();
  // Content that grows or shrinks (a week with more meetings, a list that loads) re-packs the grid.
  let repack = null;
  const ro = new ResizeObserver(() => { if (dragging) return; clearTimeout(repack); repack = setTimeout(() => pack(), 60); });
  widgets().forEach(w => ro.observe(w));
  window.addEventListener('resize', () => { clearTimeout(repack); repack = setTimeout(() => pack(), 120); });

  // --- Customize mode: add, hide, reset, width buttons -----------------------
  function setMode(on) {
    grid.classList.toggle('customizing', on);
    bar.hidden = !on;
    document.querySelectorAll('.hero-btn[data-customize]').forEach(b => { b.textContent = on ? 'Done customizing' : 'Customize dashboard'; });
    const url = new URL(location.href);
    on ? url.searchParams.set('customize', '1') : url.searchParams.delete('customize');
    history.replaceState(null, '', url);
    setTimeout(() => pack(), 0);
  }
  document.querySelectorAll('[data-customize]').forEach(b => b.addEventListener('click', () => setMode(!grid.classList.contains('customizing'))));

  grid.addEventListener('click', e => {
    const w = e.target.closest('.widget');
    if (!w) return;
    const width = e.target.closest('[data-width]');
    if (width) { setWidth(w, +width.dataset.width); pack(w); save(); return; }
    const order = e.target.closest('[data-order]');
    if (order) { swap(w, +order.dataset.order); return; }
    if (e.target.closest('[data-hide]')) {
      ro.unobserve(w); w.remove(); pack(); save();
      const add = document.querySelector(`[data-add-widget="${w.dataset.widget}"]`);
      if (add) add.disabled = false;
      say(`${title(w)} hidden. + Add widget brings it back.`);
    }
  });
  // Double-clicking the corner goes back to fitting the content.
  grid.addEventListener('dblclick', e => { const w = e.target.closest('.widget-resize')?.closest('.widget'); if (w) { setHeight(w, 0); pack(); save(); } });

  // ↑ ↓: trade places with the widget before / after in reading order.
  function swap(w, dir) {
    const list = widgets().sort((a, b) => +a.style.order - +b.style.order);
    const other = list[list.indexOf(w) + dir];
    if (!other) return;
    const a = pos(w), b = pos(other);
    w.dataset.x = b.x; w.dataset.y = b.y; other.dataset.x = a.x; other.dataset.y = a.y;
    pack(w); save();
    say(`${title(w)} moved ${dir < 0 ? 'up' : 'down'}.`);
    w.querySelector(`[data-order="${dir}"]`)?.focus();
  }

  document.querySelectorAll('[data-add-widget]').forEach(b => b.addEventListener('click', async () => {
    await window.acmiPrefs.save('dashboard', [...layout(), {id: b.dataset.addWidget, w: 6, h: 0}]);
    location.href = location.pathname + '?customize=1#widgets';  // the new widget's content comes from the server
  }));
  document.querySelector('[data-reset-dashboard]')?.addEventListener('click', async () => {
    if (!confirm('Put the dashboard back to the standard layout?')) return;
    await window.acmiPrefs.save('dashboard', null);
    location.href = location.pathname;
  });

  // --- Moving: pointer drag by the title (mouse) or the ⠿ handle (mouse, pen or touch) ---------------------
  const colWidth = () => (grid.getBoundingClientRect().width + GAP) / COLS;

  grid.addEventListener('pointerdown', e => {
    if (e.button !== 0 || narrow()) return;
    const grip = e.target.closest('[data-grip]');
    const head = e.target.closest('.widget-head');
    if (!grip && !(head && e.pointerType === 'mouse' && !e.target.closest('a, button, input, select, details'))) return;
    const w = (grip || head).closest('.widget');
    if (w.classList.contains('headless') && !grip && !grid.classList.contains('customizing')) return;
    const r = w.getBoundingClientRect();
    dragging = {w, startX: e.clientX, startY: e.clientY, dx: e.clientX - r.left, dy: e.clientY - r.top, started: false,
                width: r.width, height: r.height, before: layout()};
    (grip || head).setPointerCapture(e.pointerId);
  });
  grid.addEventListener('pointermove', e => {
    if (!dragging) return;
    const d = dragging;
    if (!d.started) {
      if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < 6) return;  // a click, not a drag
      d.started = true;
      d.ghost = document.createElement('div');
      d.ghost.className = 'widget-ghost';
      d.ghost.style.gridColumn = d.w.style.gridColumn; d.ghost.style.gridRow = d.w.style.gridRow;
      grid.appendChild(d.ghost);
      Object.assign(d.w.style, {position: 'fixed', width: d.width + 'px', height: d.height + 'px', zIndex: 50, margin: 0});
      d.w.classList.add('dragging');
    }
    d.w.style.left = (e.clientX - d.dx) + 'px';
    d.w.style.top = (e.clientY - d.dy) + 'px';
    const g = grid.getBoundingClientRect();
    const x = Math.round((e.clientX - d.dx - g.left) / colWidth());
    const y = Math.round((e.clientY - d.dy - g.top) / ROW);
    if (x !== +d.w.dataset.x || y !== +d.w.dataset.y) {
      d.w.dataset.x = Math.max(0, Math.min(x, COLS - +d.w.dataset.w)); d.w.dataset.y = Math.max(0, y);
      const items = pack(d.w);
      const me = items.find(i => i.el === d.w);
      d.ghost.style.gridColumn = `${me.x + 1} / span ${me.w}`; d.ghost.style.gridRow = `${me.y + 1} / span ${me.rows}`;
    }
  });
  function endDrag() {
    if (!dragging) return;
    const d = dragging; dragging = null;
    if (!d.started) return;
    d.ghost.remove();
    ['position', 'width', 'height', 'zIndex', 'margin', 'left', 'top'].forEach(p => { d.w.style[p] = ''; });
    d.w.classList.remove('dragging');
    d.w.classList.add('just-dragged'); setTimeout(() => d.w.classList.remove('just-dragged'), 0);
    pack(d.w); save();
    say(`${title(d.w)} moved.`);
  }
  grid.addEventListener('pointerup', endDrag);
  grid.addEventListener('pointercancel', endDrag);
  // A drag swallows the click that ends it, so a title link isn't followed by accident.
  grid.addEventListener('click', e => { if (e.target.closest('.widget.just-dragged')) e.preventDefault(); }, true);

  // --- Resizing: the bottom-right corner; width snaps to columns, height is free -------------------------
  grid.addEventListener('pointerdown', e => {
    const handle = e.target.closest('.widget-resize');
    if (!handle || narrow()) return;
    e.preventDefault(); e.stopPropagation();
    const w = handle.closest('.widget'), body = w.querySelector('.widget-body');
    const left = w.getBoundingClientRect().left, top = body.getBoundingClientRect().top;
    handle.setPointerCapture(e.pointerId);
    w.classList.add('resizing');
    const move = ev => {
      const n = Math.round((ev.clientX - left + GAP) / colWidth());
      const cols = Math.max(2, Math.min(n, COLS - +w.dataset.x));
      if (cols !== +w.dataset.w) setWidth(w, cols);
      setHeight(w, Math.max(120, Math.round(ev.clientY - top)));
      pack(w);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up);
      w.classList.remove('resizing'); pack(w); save();
      say(`${title(w)}: ${w.dataset.w} of 12 columns wide.`);
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
  });

  // --- Keyboard: on ⠿, arrows move (a column, or past the neighbour), Shift + arrows resize ----------------
  grid.addEventListener('keydown', e => {
    const grip = e.target.closest('[data-grip]');
    if (!grip || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
    e.preventDefault();
    const w = grip.closest('.widget');
    if (narrow()) { if (e.key === 'ArrowUp' || e.key === 'ArrowDown') swap(w, e.key === 'ArrowUp' ? -1 : 1); grip.focus(); return; }
    const x = +w.dataset.x, y = +w.dataset.y, cols = +w.dataset.w;
    if (e.shiftKey) {
      if (e.key === 'ArrowRight') setWidth(w, Math.min(cols + 1, COLS - x));
      if (e.key === 'ArrowLeft') setWidth(w, cols - 1);
      const h = +w.dataset.h || w.querySelector('.widget-body').offsetHeight;
      if (e.key === 'ArrowDown') setHeight(w, Math.min(h + 40, 2000));
      if (e.key === 'ArrowUp') setHeight(w, Math.max(h - 40, 120));
      pack(w); save();
      say(`${title(w)}: ${w.dataset.w} of 12 columns wide, ${w.querySelector('.widget-body').offsetHeight} pixels tall.`);
    } else {
      if (e.key === 'ArrowLeft') w.dataset.x = Math.max(0, x - 1);
      if (e.key === 'ArrowRight') w.dataset.x = Math.min(COLS - cols, x + 1);
      if (e.key === 'ArrowUp') w.dataset.y = Math.max(0, y - Math.max(4, Math.round(rowsOf(w) / 2)));
      if (e.key === 'ArrowDown') w.dataset.y = y + rowsOf(w) + 1;
      pack(w); save();
      say(`${title(w)}: column ${+w.dataset.x + 1}, ${+w.style.order + 1} of ${widgets().length} in reading order.`);
    }
    grip.focus();
  });
})();
