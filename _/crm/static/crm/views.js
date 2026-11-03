// The view bar (partials/view_bar.html): open your default view, hide table columns, compact rows.
(function () {
  const bar = document.querySelector('[data-view-bar]');
  if (!bar) return;
  const page = bar.dataset.viewBar;

  // Coming to the page with no filters opens your default view (the "All" chip adds _v=all to skip it).
  if (bar.dataset.default && !location.search) {
    location.replace(location.pathname + '?' + bar.dataset.default);
    return;
  }

  // Compact rows.
  const main = document.querySelector('main');
  const density = bar.querySelector('.vb-density');
  const setCompact = on => {
    main.classList.toggle('compact-rows', on);
    density.textContent = on ? 'Comfortable rows' : 'Compact rows';
    density.setAttribute('aria-pressed', on);
  };
  setCompact(bar.hasAttribute('data-compact'));
  density.addEventListener('click', () => {
    const on = !main.classList.contains('compact-rows');
    setCompact(on);
    window.acmiPrefs?.save('density:' + page, on ? 'compact' : null);
  });

  // Columns: offered for the page's data tables (Companies and People choose columns in their filters instead).
  const tables = [...document.querySelectorAll('main table.data')].filter(t => t.tHead);
  if (!tables.length || document.querySelector('details.columns')) return;
  const labels = [];
  tables.forEach(t => [...t.tHead.rows[0].cells].forEach(c => {
    const l = c.textContent.trim();
    if (l && !labels.includes(l)) labels.push(l);
  }));
  if (labels.length < 3) return;
  let hidden = new Set((bar.dataset.hiddenCols || '').split('|').filter(Boolean));
  function apply() {
    tables.forEach(t => {
      const cells = [...t.tHead.rows[0].cells];
      let col = 0;
      const spans = cells.map(c => { const start = col; col += c.colSpan || 1; return {label: c.textContent.trim(), start, span: c.colSpan || 1}; });
      const off = new Set();
      spans.forEach(s => { if (hidden.has(s.label)) for (let i = 0; i < s.span; i++) off.add(s.start + i); });
      [...t.rows].forEach(row => {
        let i = 0;
        [...row.cells].forEach(cell => {
          const span = cell.colSpan || 1;
          let all = true;
          for (let k = 0; k < span; k++) if (!off.has(i + k)) all = false;
          cell.hidden = all && span > 0 && off.size > 0;
          i += span;
        });
      });
    });
  }
  const menu = bar.querySelector('.vb-cols');
  const list = menu.querySelector('.vb-col-list');
  labels.forEach((l, n) => {
    const lab = document.createElement('label');
    lab.className = 'vb-check';
    lab.innerHTML = '<input type="checkbox"> <span></span>';
    lab.querySelector('span').textContent = l;
    const box = lab.querySelector('input');
    box.checked = !hidden.has(l);
    box.disabled = n === 0;  // the first column names the row
    box.addEventListener('change', () => {
      box.checked ? hidden.delete(l) : hidden.add(l);
      apply();
      window.acmiPrefs?.save('cols:' + page, [...hidden]);
    });
    list.appendChild(lab);
  });
  menu.hidden = false;
  apply();
})();
