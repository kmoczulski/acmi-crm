// Mail search box, Gmail style: click it for quick filters and your recent searches; type and it offers the people
// and companies it knows (From: / To: / just their mail) next to a plain "Search for ..." row. Arrows and Enter work.
(function () {
  const box = document.querySelector('[data-search-box]');
  const pop = document.querySelector('[data-search-pop]');
  if (!box || !pop) return;
  const url = document.currentScript.dataset.url;
  const form = box.closest('form');
  const KEY = 'acmi-mail-recent';
  const FILTERS = [
    ['Unread', 'is:unread'], ['Has attachment', 'has:attachment'], ['Starred', 'is:starred'],
    ['From me', 'from:me'], ['Last 7 days', 'newer_than:7d'], ['PDFs', 'filename:pdf'],
    ['Word', 'filename:docx'], ['Excel', 'filename:xlsx'],
  ];
  let rows = [], on = -1, timer = 0, seq = 0;

  const recent = () => { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } };
  const remember = q => {
    if (!q) return;
    try { localStorage.setItem(KEY, JSON.stringify([q, ...recent().filter(x => x !== q)].slice(0, 6))); } catch (e) {}
  };
  const go = q => { box.value = q; remember(q); const inp = form.querySelector('[data-adv-in]'); if (inp) inp.disabled = true; form.submit(); };
  // The word being typed: everything after the last space that is not inside quotes.
  const lastWord = () => { const m = box.value.match(/(?:^|\s)([^\s"]*)$/); return m ? m[1] : ''; };
  const replaceLast = text => box.value.replace(/([^\s"]*)$/, '') + text + ' ';

  function draw() {
    pop.innerHTML = '';
    rows.forEach((r, i) => {
      const el = document.createElement('button');
      el.type = 'button'; el.role = 'option'; el.className = 'msp-row' + (i === on ? ' on' : '');
      el.dataset.i = i;
      if (r.head) { el.className = 'msp-head'; el.disabled = true; el.textContent = r.head; }
      else {
        const t = document.createElement('span'); t.className = 'msp-text'; t.textContent = r.text;
        el.appendChild(t);
        if (r.hint) { const h = document.createElement('span'); h.className = 'msp-hint muted'; h.textContent = r.hint; el.appendChild(h); }
      }
      pop.appendChild(el);
    });
    pop.hidden = !rows.length;
    box.setAttribute('aria-expanded', String(!pop.hidden));
  }
  const picks = () => rows.map((r, i) => r.head ? -1 : i).filter(i => i >= 0);

  function idle() {
    rows = [];
    const r = recent();
    if (r.length) { rows.push({head: 'Recent searches'}); r.forEach(q => rows.push({text: q, run: () => go(q)})); }
    rows.push({head: 'Narrow it down'});
    FILTERS.forEach(([name, op]) => rows.push({text: name, hint: op, run: () => go((box.value.trim() + ' ' + op).trim())}));
    on = -1; draw();
  }

  async function typing() {
    const q = box.value.trim(), word = lastWord(), mine = ++seq;
    if (!q) return idle();
    const base = [{text: `Search for “${q}”`, hint: 'Enter', run: () => go(q)}];
    rows = base; on = 0; draw();
    if (word.length < 2 || word.includes(':')) return;
    try {
      const r = await fetch(`${url}?q=${encodeURIComponent(word)}&records=1`, {headers: {'X-Requested-With': 'fetch'}});
      if (!r.ok || mine !== seq) return;
      const d = await r.json();
      const out = [...base];
      const people = (d.people || []).filter(p => p.email).slice(0, 4);
      if (people.length) out.push({head: 'People'});
      people.forEach(p => {
        out.push({text: `From ${p.name || p.email}`, hint: p.email, run: () => { box.value = replaceLast('from:' + p.email); box.focus(); typing(); }});
        out.push({text: `To ${p.name || p.email}`, hint: p.email, run: () => { box.value = replaceLast('to:' + p.email); box.focus(); typing(); }});
      });
      const recs = (d.records || []).slice(0, 3);
      if (recs.length) out.push({head: 'Companies and firms (all their mail)'});
      recs.forEach(c => out.push({text: c.label, hint: c.kind, run: () => go(c.label)}));
      rows = out; on = 0; draw();
    } catch (e) { /* the plain search row stays */ }
  }

  const show = () => { if (box.value.trim()) typing(); else idle(); };
  box.addEventListener('focus', show);
  box.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(typing, 150); });
  box.addEventListener('keydown', e => {
    if (pop.hidden) return;
    const p = picks(); if (!p.length) return;
    const at = p.indexOf(on);
    if (e.key === 'ArrowDown') { e.preventDefault(); on = p[(at + 1) % p.length]; draw(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); on = p[(at - 1 + p.length) % p.length]; draw(); }
    else if (e.key === 'Escape') { pop.hidden = true; box.setAttribute('aria-expanded', 'false'); }
    else if (e.key === 'Enter' && on > 0 && rows[on] && rows[on].run) { e.preventDefault(); rows[on].run(); }
  });
  pop.addEventListener('mousedown', e => {
    const b = e.target.closest('.msp-row'); if (!b) return;
    e.preventDefault(); const r = rows[+b.dataset.i]; if (r && r.run) r.run();
  });
  form.addEventListener('submit', () => remember(box.value.trim()));
  document.addEventListener('click', e => { if (!form.contains(e.target)) pop.hidden = true; });
})();
