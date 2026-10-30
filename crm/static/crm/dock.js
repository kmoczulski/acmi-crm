// The mail dock: small boxes along the bottom of the page, like Gmail's compose windows. A New message, Forward or
// Reply from a person's page opens as a box instead of a full page, and a new-email alert opens the conversation in
// one, so you can answer mail without leaving the Land map or the pipeline. Each box is a frame of a hub page drawn
// without the top bar (window_base.html): /inbox/compose/?window=1 for writing, /inbox/window/ for reading.
// What's open is kept for this tab (sessionStorage hubDock), so the boxes come back on the next page you open, and
// compose.js keeps what was typed in each one (hubDock:<id>). Links to the compose page open a box; so do links that
// carry data-window (new-email alerts, the bell's list). Frames of the split view hand such links up to this page.
(function () {
  const script = document.currentScript;
  if (!script || !script.dataset.composeUrl) return;
  const PATH = {compose: new URL(script.dataset.composeUrl, location.origin).pathname,
                thread: new URL(script.dataset.threadUrl, location.origin).pathname};
  const PARAMS = ['to', 'cc', 'bcc', 'subject', 'reply', 'mode'];

  // Which box (if any) a click on a link should open instead of leaving the page.
  function route(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return null;
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download') || a.hasAttribute('data-no-dock')) return null;
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch (err) { return null; }
    if (u.origin !== location.origin) return null;
    if (a.dataset.window) return {kind: 'thread', url: a.dataset.window, read: a.getAttribute('href'), link: a};
    if (u.pathname !== PATH.compose || location.pathname === PATH.compose) return null;
    if (a.hasAttribute('data-reply') && document.querySelector('[data-inline]')) return null;  // the reading pane's own reply box
    const params = {};
    PARAMS.forEach(k => { if (u.searchParams.get(k)) params[k] = u.searchParams.get(k); });
    return {kind: 'compose', params};
  }

  // A frame (the split view's side) has no dock of its own: its links open boxes on the page around it.
  if (window.top !== window) {
    document.addEventListener('click', e => {
      let dock = null;
      try { dock = window.top.acmiDock; } catch (err) { return; }
      const r = dock && route(e);
      if (r) { e.preventDefault(); dock.open(r); }
    });
    return;
  }

  const KEY = 'hubDock', MAX = 4, GAP = 12, MIN_W = 260, WIDTH = {compose: 500, thread: 460}, PHONE = 700;
  const dock = document.createElement('div');
  dock.className = 'mail-dock no-print';
  dock.setAttribute('aria-label', 'Mail windows');
  dock.hidden = true;
  document.body.append(dock);
  let wins = [];  // oldest first; the newest sits at the right edge

  const rand = () => Math.random().toString(36).slice(2, 8);
  const stored = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) { return []; } };
  const forget = id => { try { sessionStorage.removeItem('hubDock:' + id); } catch (e) {} };
  const persist = () => {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(wins.map(w => ({id: w.id, kind: w.kind, url: w.url, key: w.key || '',
        reply: w.reply || '', mode: w.mode || '', title: w.title, min: w.min, big: w.big}))));
    } catch (e) { /* private window: the boxes just don't come back on the next page */ }
  };
  // Only the hub's own two pages are ever framed, whatever sessionStorage or the address bar hands us.
  const valid = (kind, url) => {
    try {
      const u = new URL(url, location.origin);
      return u.origin === location.origin && u.pathname === PATH[kind] ? u : null;
    } catch (e) { return null; }
  };
  const frameApi = w => { try { return w.frame.contentWindow.acmiWindow || null; } catch (e) { return null; } };

  function toast(text, link) {
    let box = document.getElementById('dock-toasts');
    if (!box) { box = document.createElement('div'); box.id = 'dock-toasts'; box.setAttribute('role', 'status'); document.body.append(box); }
    const t = document.createElement('div');
    t.className = 'dock-toast';
    t.append(text);
    if (link) {
      const a = document.createElement('a');
      a.href = link.href;
      a.textContent = link.label;
      a.setAttribute('data-no-dock', '');
      t.append(' ', a);
    }
    box.append(t);
    setTimeout(() => t.remove(), 6500);
  }

  // How wide a box is now; boxes fold to their title bar, oldest first, when the row won't fit the window.
  const span = w => (w.min ? MIN_W : w.big ? Math.min(900, Math.round(innerWidth * 0.7)) : WIDTH[w.kind]) + GAP;
  function makeRoom(keep) {
    if (innerWidth <= PHONE) {
      wins.forEach(w => { if (w !== keep) w.min = true; });
    } else {
      const room = innerWidth - (parseFloat(getComputedStyle(dock).right) || 0) - 16 + GAP;
      let total = wins.reduce((n, w) => n + span(w), 0);
      for (const w of wins) {
        if (total <= room) break;
        if (w === keep || w.min) continue;
        total -= span(w) - (MIN_W + GAP);
        w.min = true;
      }
    }
    wins.forEach(draw);
  }

  function draw(w) {
    w.el.classList.toggle('min', w.min);
    w.el.classList.toggle('big', w.big);
    const fold = w.el.querySelector('.dock-btn[data-act=min]'), grow = w.el.querySelector('.dock-btn[data-act=big]');
    w.el.querySelector('.dock-title').setAttribute('aria-expanded', w.min ? 'false' : 'true');
    fold.textContent = w.min ? '▢' : '–';
    fold.title = w.min ? 'Open' : 'Minimize';
    grow.textContent = w.big ? '⤡' : '⤢';
    grow.title = w.big ? 'Smaller' : 'Bigger';
    if (!w.min && !w.frame.getAttribute('src')) w.frame.src = w.url;  // a folded box waits until it's opened
  }

  function layout() {
    dock.hidden = !wins.length;
    document.body.classList.toggle('has-dock', wins.length > 0);
    persist();
  }

  function add(spec) {
    const u = valid(spec.kind, spec.url);
    if (!u || wins.length >= MAX) {
      if (u) toast('Close one of the open mail windows first.');
      return null;
    }
    const w = Object.assign({min: false, big: false, title: spec.kind === 'compose' ? 'New message' : 'Email'}, spec);
    const el = document.createElement('section');
    el.className = 'dock-win dock-' + w.kind;
    el.dataset.id = w.id;
    el.setAttribute('role', 'dialog');
    el.innerHTML = '<header class="dock-head"><button type="button" class="dock-title" data-act="min"></button>'
      + '<button type="button" class="dock-btn" data-act="min" aria-label="Minimize"></button>'
      + '<button type="button" class="dock-btn dock-big" data-act="big" aria-label="Make it bigger or smaller"></button>'
      + '<button type="button" class="dock-btn" data-act="close" title="Close" aria-label="Close">✕</button></header>'
      + '<iframe class="dock-frame" title=""></iframe>';
    el.querySelector('.dock-title').textContent = w.title;
    w.el = el;
    w.frame = el.querySelector('iframe');
    w.frame.title = w.title;
    w.frame.addEventListener('load', () => {
      try { const t = w.frame.contentDocument.body.dataset.windowTitle; if (t) title(w.id, t); } catch (e) { /* not ours */ }
    });
    el.addEventListener('click', e => {
      const b = e.target.closest('[data-act]');
      if (!b) return;
      if (b.dataset.act === 'min') { w.min = !w.min; if (!w.min) makeRoom(w); else draw(w); layout(); }
      else if (b.dataset.act === 'big') { w.big = !w.big; w.min = false; makeRoom(w); layout(); }
      else close(w.id);
    });
    dock.append(el);
    wins.push(w);
    return w;
  }

  // Bring a box forward: open it if it was folded, and make room for it.
  function show(w) {
    if (!w) return;
    w.min = false;
    makeRoom(w);
    layout();
    const focus = () => { try { w.frame.contentWindow.focus(); } catch (e) {} };
    if (w.frame.contentDocument && w.frame.contentDocument.readyState === 'complete' && w.frame.getAttribute('src')) focus();
    else w.frame.addEventListener('load', focus, {once: true});
  }

  function remove(w) {
    wins = wins.filter(x => x !== w);
    w.el.remove();
    forget(w.id);  // after the frame is gone, so its own last write can't bring the text back
    layout();
  }

  async function close(id, opts) {
    const w = wins.find(x => x.id === id);
    if (!w) return;
    const api = frameApi(w);
    if (api && !(opts && opts.force)) {
      if (api.sending && api.sending() && !confirm('This message is still waiting to send. Close it and cancel the send?')) return;
      try { if (api.flush && await api.flush()) toast('Draft saved.'); } catch (e) { /* it stays as last saved */ }
    }
    remove(w);
  }

  function title(id, text) {
    const w = wins.find(x => x.id === id);
    if (!w || !text) return;
    w.title = text;
    w.el.querySelector('.dock-title').textContent = text;
    w.frame.title = text;
    persist();
  }

  function sent(id, data) {
    const w = wins.find(x => x.id === id);
    if (w) remove(w);
    toast('Message sent.', data && data.url ? {href: data.url, label: 'View'} : null);
  }

  function markRead(url) {
    if (!url || url[0] !== '/' || url.startsWith('//')) return;
    fetch(url, {credentials: 'same-origin', redirect: 'manual', headers: {'X-Requested-With': 'fetch'}})
      .then(() => { if (window.acmiAlerts && window.acmiAlerts.refresh) window.acmiAlerts.refresh(); })
      .catch(() => {});
  }

  function compose(p) {
    p = p || {};
    const reply = p.reply || '', mode = p.mode || (reply ? 'reply' : 'new');
    const same = reply && wins.find(w => w.kind === 'compose' && w.reply === reply && w.mode === mode);
    if (same) { show(same); return; }
    const id = rand(), q = new URLSearchParams();
    PARAMS.forEach(k => { if (p[k]) q.set(k, p[k]); });
    q.set('window', '1');
    q.set('w', id);
    show(add({id, kind: 'compose', url: PATH.compose + '?' + q, reply, mode,
      title: reply ? (mode === 'forward' ? 'Forward' : 'Reply') : 'New message'}));
  }

  function thread(url, read) {
    const u = valid('thread', url);
    if (!u) return;
    if (read) markRead(read);
    const key = u.searchParams.get('t') || u.searchParams.get('m') || '';
    const same = key && wins.find(w => w.kind === 'thread' && w.key === key);
    if (same) { show(same); return; }
    const id = rand();
    u.searchParams.set('w', id);
    show(add({id, kind: 'thread', url: u.pathname + u.search, key}));
  }

  const open = r => {
    if (r.kind === 'compose') compose(r.params);
    else if (r.kind === 'thread') { thread(r.url, r.read); if (r.link) r.link.classList.remove('unread'); }  // the bell's list
  };
  window.acmiDock = {open, compose, thread, title, sent, close, markRead};

  document.addEventListener('click', e => {
    const r = route(e);
    if (r) { e.preventDefault(); open(r); }
  });
  // Clicking a browser notification while a hub page is open (the service worker, crm/push_views.py).
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', e => {
      const d = e.data || {};
      if (d.type === 'hub-window' && d.window) thread(d.window, d.read);
    });
  }
  let resizing = null;
  addEventListener('resize', () => { clearTimeout(resizing); resizing = setTimeout(() => { makeRoom(null); layout(); }, 150); });

  // The boxes this tab had open on the last page, then anything the address asks for (a browser notification that
  // had to open the hub: /?dock=<box address>&read=<alert address>).
  stored().slice(0, MAX).forEach(r => { if (r && r.id && (r.kind === 'compose' || r.kind === 'thread')) add(r); });
  makeRoom(null);
  layout();
  const q = new URLSearchParams(location.search);
  if (q.get('dock')) {
    thread(q.get('dock'), q.get('read'));
    q.delete('dock');
    q.delete('read');
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  }
})();
