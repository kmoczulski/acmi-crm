// Getting around: the top menus, search (the Ctrl+K palette and the big box on the dashboard) and per-person
// preferences.
(function () {
  const cfg = document.getElementById('nav-config');
  if (!cfg) return;
  const root = document.documentElement;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  const csrf = () => document.cookie.split('; ').find(c => c.startsWith('csrftoken='))?.split('=')[1] || '';

  // Dark mode: the profile menu's button flips it; the early script in base.html applies it before first paint.
  document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
    const show = () => { btn.textContent = root.getAttribute('data-theme') === 'dark' ? 'Light mode' : 'Dark mode'; };
    show();
    document.addEventListener('acmi-theme', show);
    btn.addEventListener('click', () => window.acmiTheme.set(root.getAttribute('data-theme') !== 'dark'));
  });

  // Saved on the server (crm/prefs.py), so a layout follows the person to any computer.
  window.acmiPrefs = {
    save(key, value) {
      return fetch(cfg.dataset.prefs, {
        method: 'POST', headers: {'X-CSRFToken': csrf()}, credentials: 'same-origin',
        body: new URLSearchParams({key, value: JSON.stringify(value)}),
      });
    },
  };

  // Numbers with thousands separators. A type=number field can't show "25,000,000", so amount fields are text
  // fields marked data-commas (crm/forms.py comma_numbers, and the hand-written forms): they show separators, accept
  // them typed, and the form posts plain digits. Scripts that read such a field use acmiNum.parse.
  const numParse = s => { const t = String(s ?? '').replace(/[,\s$]/g, ''); return t === '' || isNaN(+t) ? null : +t; };
  const numFormat = v => { const n = numParse(v); return n === null ? String(v ?? '') : n.toLocaleString('en-US', {maximumFractionDigits: 10}); };
  window.acmiNum = {parse: numParse, format: numFormat};
  const showCommas = el => { const v = numParse(el.value); if (v !== null) el.value = numFormat(v); };
  document.querySelectorAll('input[data-commas]').forEach(showCommas);
  document.addEventListener('focusout', e => { if (e.target instanceof HTMLInputElement && e.target.hasAttribute('data-commas')) showCommas(e.target); });
  document.addEventListener('formdata', e => {
    e.target.querySelectorAll('input[data-commas]').forEach(el => {
      if (!el.name || el.disabled) return;
      const v = numParse(el.value);
      e.formData.set(el.name, v === null ? el.value.trim() : String(v));
    });
  });

  // Top menus: one open at a time; on a wide screen with a mouse they open on hover, like CoStar's.
  const menus = [...document.querySelectorAll('.tn-menu, .menu-wrap')];
  menus.forEach(d => d.addEventListener('toggle', () => { if (d.open) menus.forEach(o => { if (o !== d) o.open = false; }); }));
  const hoverable = () => matchMedia('(min-width: 1181px) and (hover: hover)').matches && !root.classList.contains('split-narrow');
  document.querySelectorAll('.tn-menu').forEach(d => {
    let t = null;
    d.addEventListener('mouseenter', () => { if (!hoverable()) return; clearTimeout(t); t = setTimeout(() => { d.open = true; }, 80); });
    d.addEventListener('mouseleave', () => { if (!hoverable()) return; clearTimeout(t); t = setTimeout(() => { d.open = false; }, 220); });
  });
  document.addEventListener('click', e => menus.forEach(d => { if (d.open && !d.contains(e.target)) d.open = false; }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') menus.forEach(d => { d.open = false; }); });
  // Narrow screens: ☰ shows the menus stacked.
  document.querySelectorAll('[data-nav-toggle]').forEach(b => b.addEventListener('click', () => root.classList.toggle('nav-open')));

  // Recently opened records, newest first (this browser only).
  const RECORD = /^\/(companies|people|opportunities|land\/sites|contracts|projects|tasks|directory|capital|entitlements|land\/assignments|boards)\/\d+\/?$/;
  const KIND = {companies: 'Company', people: 'Person', opportunities: 'Tenant deal', 'land/sites': 'Site', contracts: 'Contract',
    projects: 'Project', tasks: 'Task', directory: 'Directory', capital: 'Capital', entitlements: 'Entitlement', 'land/assignments': 'Client search', boards: 'Board'};
  const m = location.pathname.match(RECORD);
  if (m) {
    const title = (document.querySelector('main h1')?.textContent || document.title.replace(/ · ACMI Hub$/, '')).trim().replace(/\s+/g, ' ');
    const recent = store.get('nav:recent', []).filter(r => r.url !== location.pathname);
    recent.unshift({title: title.slice(0, 90), url: location.pathname, sub: KIND[m[1]] || ''});
    store.set('nav:recent', recent.slice(0, 8));
  }

  // "+ New › Task" lands on My tasks with the new-task box ready to type in.
  if (location.hash === '#new-task') document.querySelector('main input[name=title]')?.focus();

  const pages = JSON.parse(document.getElementById('nav-pages')?.textContent || '[]');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const pageHits = q => {
    q = q.toLowerCase();
    return pages.filter(p => (p.label + ' ' + p.words + ' ' + p.section).toLowerCase().includes(q))
      .slice(0, 5).map(p => ({title: p.label, url: p.url, sub: p.section, badge: 'Page'}));
  };

  // One search box: typing shows matching pages at once and records from the server a moment later.
  // `start` is what shows before anything is typed (null = nothing).
  function searchBox(input, list, start) {
    let items = [], active = 0, timer = null, ctrl = null;
    function render(groups) {
      items = [];
      const html = groups.filter(g => g.items.length).map(g => `<div class="pal-group">${esc(g.label)}</div>` + g.items.map(it => {
        const i = items.push(it) - 1;
        const badge = it.badge ? `<span class="stage ${esc(it.tone)}">${esc(it.badge)}</span>` : '';
        return `<a class="pal-item" role="option" data-i="${i}" href="${esc(it.url)}"><span class="pal-title">${esc(it.title)}</span>` +
          `<span class="pal-sub">${esc(it.sub)}</span>${badge}</a>`;
      }).join('')).join('');
      list.innerHTML = html || (input.value.trim().length >= 2 ? '<p class="muted pal-empty">Nothing found. Try part of a name, an email domain or a page.</p>' : '');
      list.hidden = !list.innerHTML;
      active = 0; mark();
    }
    function mark() {
      list.querySelectorAll('.pal-item').forEach(a => a.classList.toggle('on', +a.dataset.i === active));
      list.querySelector('.pal-item.on')?.scrollIntoView({block: 'nearest'});
    }
    function run() {
      const q = input.value.trim();
      clearTimeout(timer);
      if (!q) return start ? render(start()) : render([]);
      const local = pageHits(q);
      render(local.length ? [{label: 'Pages', items: local}] : []);
      if (q.length < 2) return;
      timer = setTimeout(async () => {
        ctrl?.abort(); ctrl = new AbortController();
        try {
          const r = await fetch(cfg.dataset.search + '?q=' + encodeURIComponent(q), {signal: ctrl.signal, credentials: 'same-origin'});
          const d = await r.json();
          if (input.value.trim() === q) render([...d.groups, {label: 'Pages', items: local}]);
        } catch (e) { /* superseded or offline */ }
      }, 140);
    }
    input.addEventListener('input', run);
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, items.length - 1); mark(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); mark(); }
      else if (e.key === 'Enter' && items[active]) { e.preventDefault(); location.href = items[active].url; }
    });
    list.addEventListener('mousemove', e => { const a = e.target.closest('.pal-item'); if (a && +a.dataset.i !== active) { active = +a.dataset.i; mark(); } });
    return {run, render};
  }

  const startState = () => [
    {label: 'Recently opened', items: store.get('nav:recent', [])},
    {label: 'Go to', items: pages.slice(0, 8).map(p => ({title: p.label, url: p.url, sub: p.section}))},
  ];

  // The Ctrl+K palette.
  const dlg = document.getElementById('palette');
  const palInput = document.getElementById('palette-input');
  const palette = searchBox(palInput, document.getElementById('palette-results'), startState);
  function open() {
    if (dlg.open) return;
    dlg.showModal(); palInput.value = ''; palette.run(); palInput.focus();
  }
  document.querySelectorAll('[data-search-open]').forEach(b => b.addEventListener('click', open));
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });

  // The dashboard's big search box: results drop down under it.
  const hero = document.querySelector('[data-hero-search]');
  if (hero) {
    const input = hero.querySelector('input'), list = hero.querySelector('.hero-results');
    const box = searchBox(input, list, startState);
    input.addEventListener('focus', () => box.run());
    document.addEventListener('click', e => { if (!hero.contains(e.target)) list.hidden = true; });
    input.addEventListener('keydown', e => { if (e.key === 'Escape') { list.hidden = true; input.blur(); } });
  }

  document.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
    const focusHero = () => { const i = hero?.querySelector('input'); if (i) { i.focus(); i.select(); return true; } return false; };
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); if (!focusHero()) open(); }
    else if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey) { e.preventDefault(); if (!focusHero()) open(); }
  });
})();

// Record cards ("peek"): a plain click on a company, deal or person link opens that record's card over the page
// you're on (fetched from <record url>card/, crm/cards.py), with Open page for the full record. Modifier or middle
// clicks, links in breadcrumbs, record headers, the record nav and search results, and links marked data-page go
// straight to the page as before. A link inside a card opens the next card, with Back.
(function () {
  const peek = document.getElementById('peek');
  if (!peek) return;
  const body = document.getElementById('peek-body');
  const CARD = /^\/(companies|opportunities|people)\/\d+\/$/;
  const SKIP = '.crumbs, .record-head, .record-nav, .palette, .hero-results, [data-no-peek]';
  const stack = [];
  const backButton = () => body.querySelector('[data-card-back]')?.toggleAttribute('hidden', !stack.length);
  async function show(url, chain) {
    if (chain && body.innerHTML) stack.push(body.innerHTML); else if (!chain) stack.length = 0;
    body.innerHTML = '<p class="muted" style="padding:30px">Loading…</p>';
    if (!peek.open) peek.showModal();
    try {
      const r = await fetch(url, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}});
      body.innerHTML = r.ok ? await r.text() : '<p class="scard-error" style="margin:20px">Could not load that card (' + r.status + ').</p>';
    } catch (e) { body.innerHTML = '<p class="scard-error" style="margin:20px">Could not reach the server.</p>'; }
    backButton();
    peek.scrollTop = 0;
    body.querySelector('[data-page]')?.focus();
  }
  document.addEventListener('click', e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('data-page') || a.closest(SKIP)) return;
    let url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin || !CARD.test(url.pathname) || url.hash) return;
    e.preventDefault();
    show(url.pathname + 'card/' + url.search, !!a.closest('#peek'));
  });
  peek.addEventListener('click', e => {
    if (e.target === peek || e.target.closest('[data-card-close]')) { peek.close(); return; }
    if (e.target.closest('[data-card-back]')) { e.preventDefault(); body.innerHTML = stack.pop() || ''; backButton(); }
  });
  // The deal card's stage selector: saved in the background, then the card reloads in place (scripts inside fetched HTML
  // never run, so this lives here). Without scripting the form still posts and redirects back to the card.
  peek.addEventListener('submit', async e => {
    const form = e.target.closest('[data-stage-form]'); if (!form) return;
    e.preventDefault();
    const msg = form.querySelector('[data-stage-msg]');
    try {
      const r = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}});
      if (!r.ok) throw new Error(r.status);
      const c = await fetch(form.dataset.card, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}});
      if (!c.ok) throw new Error(c.status);
      body.innerHTML = await c.text(); backButton();
      const m = body.querySelector('[data-stage-msg]'); if (m) m.textContent = 'Stage updated';
    } catch (err) { if (msg) msg.textContent = 'Could not change the stage. Try again.'; }
  });
  peek.addEventListener('close', () => { body.innerHTML = ''; stack.length = 0; });
})();

// Accessible names for controls a template left unlabeled (filter dropdowns, row checkboxes): the first option's
// wording ("Every kind"), else the field name, so a screen reader doesn't just say "combo box".
(function () {
  const human = s => s.replace(/[_\[\]]+/g, ' ').trim().replace(/^./, c => c.toUpperCase());
  function nameControls(root) {
    root.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]),select,textarea').forEach(el => {
      if ((el.labels && el.labels.length) || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title || el.placeholder) return;
      let label = '';
      if (el.tagName === 'SELECT' && el.options.length) label = el.options[0].textContent.trim();
      else if (el.type === 'checkbox') label = 'Select row';
      else if (el.type === 'file') label = 'Choose a file';
      if (!label && el.name) label = human(el.name);
      if (label) el.setAttribute('aria-label', label);
    });
  }
  nameControls(document);
  let pending = false;  // cards, panels and the inbox pane add fields later; scan once per frame at most
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; nameControls(document); });
  }).observe(document.body, {childList: true, subtree: true});
})();

// Files and pictures open over the page with a Close button (Chat files, mail attachments). target=_blank, or a plain
// download link, on an iPhone's home-screen app opens the system preview, which has no way back (you had to swipe the
// whole app away). The viewer stays inside the app: a picture, or a file card with Download, and a big Close.
(function () {
  if (window.hubViewer) return;
  const el = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v !== false && v != null) { if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v; else n.setAttribute(k, v === true ? '' : v); } });
    kids.forEach(k => { if (k != null) n.append(k); });
    return n;
  };
  const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
  let view = null, back = null;
  function close() {
    if (!view) return;
    view.remove(); view = null;
    document.removeEventListener('keydown', onKey, true);
    if (back && back.focus) back.focus();
    back = null;
  }
  const onKey = e => { if (e.key === 'Escape') { e.preventDefault(); close(); } };
  // {href, name, image (shows the picture at src), src, icon}
  function open(o, opener) {
    close();
    back = opener || null;
    const name = o.name || 'File';
    const shut = el('button', {type: 'button', class: 'chat-viewer-close', 'aria-label': 'Close', text: '\u2715'});
    shut.addEventListener('click', close);
    let body;
    if (o.image) {
      body = el('div', {class: 'chat-viewer-body'}, el('img', {src: o.src || o.href, alt: name}));
    } else {
      body = el('div', {class: 'chat-viewer-body'}, el('div', {class: 'chat-viewer-card'},
        el('div', {class: 'chat-viewer-ico', 'aria-hidden': 'true', text: o.icon || '\ud83d\udcc4'}),
        el('div', {class: 'chat-viewer-name', text: name}),
        el('div', {class: 'chat-viewer-acts'},
          el('a', {class: 'btn btn-primary', href: o.href, download: name, text: 'Download'}),
          el('a', {class: 'btn', href: o.href, target: '_blank', rel: 'noopener noreferrer', text: 'Open in the browser'}))));
    }
    view = el('div', {class: 'chat-viewer', role: 'dialog', 'aria-modal': 'true', 'aria-label': name},
      el('div', {class: 'chat-viewer-bar'}, shut, el('span', {class: 'chat-viewer-title', text: name})), body);
    view.addEventListener('click', e => { if (e.target === view || e.target === body) close(); });
    document.body.append(view);
    document.addEventListener('keydown', onKey, true);
    shut.focus();
  }
  window.hubViewer = {open, close};
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a.chat-img, a.chat-file[href], a[data-viewer]');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;  // a new tab stays a new tab
    e.preventDefault();
    const href = a.getAttribute('href');
    if (a.classList.contains('chat-img')) {
      open({href, name: a.getAttribute('title'), image: true}, a);
    } else if (a.dataset.viewer !== undefined) {  // a mail attachment: the picture itself comes from ?inline=1
      const name = (a.dataset.name || a.textContent || 'Attachment').replace(/^\s*\ud83d\udcce\s*/, '').trim();
      const image = IMAGE.test(name);
      open({href, name, image, src: image ? href + (href.includes('?') ? '&' : '?') + 'inline=1' : '', icon: '\ud83d\udcce'}, a);
    } else {
      open({href, name: a.getAttribute('title') || (a.querySelector('.chat-file-name') || {}).textContent, icon: (a.querySelector('.chat-file-ico') || {}).textContent}, a);
    }
  });
})();
