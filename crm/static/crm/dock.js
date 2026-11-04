// The dock: small boxes along the bottom of the page, like Gmail's compose and chat windows. A New message, Forward or
// Reply from a person's page opens as a box instead of a full page, a new-email alert opens the conversation in one, and
// Chat is a pinned "Chat" box at the right (the list of your conversations) that opens each conversation in a box of its
// own, so you can answer mail and chat without leaving the Land map or the pipeline. Each box is a frame of a hub page
// drawn without the top bar (window_base.html): /inbox/compose/?window=1 for writing, /inbox/window/ for reading an
// email, /chat/list/ and /chat/window/ for Chat.
// What's open is kept for this tab (sessionStorage hubDock), so the boxes come back on the next page you open, and
// compose.js keeps what was typed in each mail box (hubDock:<id>). Links to the compose page open a box; so do links that
// carry data-window (new-email and new-chat alerts, the bell's list). Frames of the split view hand such links up here.
(function () {
  const script = document.currentScript;
  if (!script) return;
  const cfg = script.dataset, pathOf = u => (u ? new URL(u, location.origin).pathname : '');
  const PATH = {compose: pathOf(cfg.composeUrl), thread: pathOf(cfg.threadUrl), chat: pathOf(cfg.chatUrl), chats: pathOf(cfg.chatListUrl)};
  const CHAT_POLL = cfg.chatPollUrl || '';
  const PARAMS = ['to', 'cc', 'bcc', 'subject', 'reply', 'mode'];
  const kindOf = p => (p && p === PATH.thread ? 'thread' : p && p === PATH.chat ? 'chat' : '');

  // Which box (if any) a click on a link should open instead of leaving the page.
  function route(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return null;
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download') || a.hasAttribute('data-no-dock')) return null;
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch (err) { return null; }
    if (u.origin !== location.origin) return null;
    if (a.dataset.window) {
      let kind = '';
      try { kind = kindOf(new URL(a.dataset.window, location.origin).pathname); } catch (err) { /* not an address */ }
      return kind ? {kind, url: a.dataset.window, read: a.getAttribute('href'), link: a} : null;
    }
    if (!PATH.compose || u.pathname !== PATH.compose || location.pathname === PATH.compose) return null;
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

  const KEY = 'hubDock', MAX = 4, GAP = 12, MIN_W = 260, PILL_W = 190, PHONE = 700;
  const WIDTH = {compose: 500, thread: 460, chat: 340, chats: 300};
  const NAMES = {compose: 'New message', thread: 'Email', chat: 'Chat', chats: 'Chat'};
  const dock = document.createElement('div');
  dock.className = 'mail-dock no-print';
  dock.setAttribute('aria-label', 'Mail and chat windows');
  dock.hidden = true;
  const hide = document.createElement('button');
  hide.type = 'button';
  hide.className = 'dock-hide';
  hide.textContent = 'Hide';
  hide.title = 'Hide mail windows';
  hide.addEventListener('click', () => {
    const away = dock.classList.toggle('away');
    hide.textContent = away ? 'Mail' : 'Hide';
    document.body.classList.toggle('dock-away', away);
  });
  dock.append(hide);
  document.body.append(dock);
  let wins = [];  // oldest first; the newest sits at the right edge, and the Chat list is always last

  const rand = () => Math.random().toString(36).slice(2, 8);
  const stored = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch (e) { return []; } };
  const forget = id => { try { sessionStorage.removeItem('hubDock:' + id); } catch (e) {} };
  const persist = () => {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(wins.map(w => ({id: w.id, kind: w.kind, url: w.url.replace(/([?&])at=[^&]*&?/, '$1').replace(/[?&]$/, ''), key: w.key || '',
        reply: w.reply || '', mode: w.mode || '', title: w.title, min: w.min, big: w.big}))));
    } catch (e) { /* private window: the boxes just don't come back on the next page */ }
  };
  // Only the hub's own pages are ever framed, whatever sessionStorage or the address bar hands us.
  const valid = (kind, url) => {
    try {
      const u = new URL(url, location.origin);
      return PATH[kind] && u.origin === location.origin && u.pathname === PATH[kind] ? u : null;
    } catch (e) { return null; }
  };
  const frameApi = w => { try { return w.frame.contentWindow.acmiWindow || null; } catch (e) { return null; } };
  const others = () => wins.filter(w => w.kind !== 'chats');

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
  const span = w => (w.min ? (w.kind === 'chats' ? PILL_W : MIN_W) : w.big ? Math.min(900, Math.round(innerWidth * 0.7)) : WIDTH[w.kind]) + GAP;
  function makeRoom(keep) {
    if (innerWidth <= PHONE) {
      wins.forEach(w => { if (w !== keep) w.min = true; });
    } else {
      const room = innerWidth - (parseFloat(getComputedStyle(dock).right) || 0) - 16 + GAP;
      let total = wins.reduce((n, w) => n + span(w), 0);
      for (const w of wins) {
        if (total <= room) break;
        if (w === keep || w.min || w.kind === 'chats') continue;
        total -= span(w) - (MIN_W + GAP);
        w.min = true;
      }
    }
    wins.forEach(draw);
  }

  function paintTitle(w) {
    w.el.querySelector('.dock-title').textContent = w.kind === 'chats' && w.count ? `Chat (${w.count})` : w.title;
    if (w.kind === 'chats') w.el.classList.toggle('unread', !!w.count);
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
    if (!w.min && w.kind === 'chat') w.el.classList.remove('unread');  // you're looking at it
  }

  function layout() {
    dock.hidden = !wins.length;
    document.body.classList.toggle('has-dock', wins.length > 0);
    // How much of the foot of the page the folded boxes cover: on a phone they span the screen, so the page gets that
    // much room below its end (app.css, --dock-h) and nothing stays hidden under them.
    document.documentElement.style.setProperty('--dock-h', (dock.hidden ? 0 : dock.offsetHeight) + 'px');
    persist();
  }

  function add(spec) {
    const u = valid(spec.kind, spec.url);
    if (!u || (spec.kind !== 'chats' && others().length >= MAX)) {
      if (u) toast('Close one of the open windows first.');
      return null;
    }
    const w = Object.assign({min: false, big: false, title: NAMES[spec.kind] || 'Mail'}, spec);
    const el = document.createElement('section');
    el.className = 'dock-win dock-' + w.kind;
    el.dataset.id = w.id;
    el.setAttribute('role', 'dialog');
    el.innerHTML = '<header class="dock-head"><button type="button" class="dock-title" data-act="min"></button>'
      + '<button type="button" class="dock-btn" data-act="min" aria-label="Minimize"></button>'
      + '<button type="button" class="dock-btn dock-big" data-act="big" aria-label="Make it bigger or smaller"></button>'
      + '<button type="button" class="dock-btn dock-x" data-act="close" title="Close" aria-label="Close">✕</button></header>'
      + '<iframe class="dock-frame" title=""></iframe>';
    w.el = el;
    w.frame = el.querySelector('iframe');
    w.frame.title = w.title;
    paintTitle(w);
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
    const pinned = wins.find(x => x.kind === 'chats');  // the Chat list stays at the right edge
    if (w.kind !== 'chats' && pinned) { dock.insertBefore(el, pinned.el); wins.splice(wins.indexOf(pinned), 0, w); }
    else { dock.append(el); wins.push(w); }
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
    if (!w || w.kind === 'chats') return;
    const api = frameApi(w);
    if (api && !(opts && opts.force)) {
      if (api.sending && api.sending() && !confirm('This message is still waiting to send. Close it and cancel the send?')) return;
      try { if (api.flush && await api.flush()) toast('Draft saved.'); } catch (e) { /* it stays as last saved */ }
    }
    remove(w);
  }

  function title(id, text) {
    const w = wins.find(x => x.id === id);
    if (!w || !text || w.kind === 'chats') return;
    w.title = text;
    paintTitle(w);
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

  // A Chat conversation in a box of its own (space: the id after spaces/).
  // at: the createTime of a message a search found; the box opens at it (and doesn't again on the next page).
  async function chat(space, name, at) {
    if (!PATH.chat || !space) return;
    const same = wins.find(w => w.kind === 'chat' && w.key === space);
    if (same && !at) { show(same); return; }
    if (same) await close(same.id, {force: true});
    const id = rand();
    show(add({id, kind: 'chat', url: PATH.chat + '?' + new URLSearchParams(Object.assign({s: space, w: id}, at ? {at} : {})), key: space, title: name || 'Chat'}));
  }

  // A box opened from an address: an alert's /inbox/window/ or /chat/window/ link.
  function openUrl(url, read) {
    let kind = '', u = null;
    try { u = new URL(url, location.origin); kind = kindOf(u.pathname); } catch (e) { return; }
    if (kind === 'thread') thread(url, read);
    else if (kind === 'chat' && u.searchParams.get('s')) { if (read) markRead(read); chat(u.searchParams.get('s'), ''); }
  }

  const open = r => {
    if (r.kind === 'compose') compose(r.params);
    else if (r.kind === 'thread' || r.kind === 'chat') { openUrl(r.url, r.read); if (r.link) r.link.classList.remove('unread'); }  // the bell's list
  };
  window.acmiDock = {open, openUrl, compose, thread, chat, title, sent, close, markRead};

  document.addEventListener('click', e => {
    const r = route(e);
    if (r) { e.preventDefault(); open(r); }
  });
  // Clicking a browser notification while a hub page is open (the service worker, crm/push_views.py).
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', e => {
      const d = e.data || {};
      if (d.type === 'hub-window' && d.window) openUrl(d.window, d.read);
    });
  }
  let resizing = null;
  addEventListener('resize', () => { clearTimeout(resizing); resizing = setTimeout(() => { makeRoom(null); layout(); }, 150); });

  // The boxes this tab had open on the last page, then anything the address asks for. The Chat list lives in the toolbar
  // now, not as a bar at the bottom.
  stored().forEach(r => {
    if (!r || !r.id || r.kind === 'chats') return;
    if (r.kind === 'compose' || r.kind === 'thread' || r.kind === 'chat') add(r);
  });
  makeRoom(null);
  layout();

  const chatBtn = document.querySelector('[data-chat-toggle]');
  let chatPanel = null;
  function toggleChat() {
    if (!PATH.chats || !chatBtn) return;
    if (chatPanel && !chatPanel.hidden) { chatPanel.hidden = true; chatBtn.setAttribute('aria-expanded', 'false'); return; }
    if (!chatPanel) {
      chatPanel = document.createElement('div');
      chatPanel.className = 'chat-toolbar no-print';
      chatPanel.hidden = true;
      chatPanel.innerHTML = '<iframe class="chat-toolbar-frame" title="Chat"></iframe>';
      chatPanel.querySelector('iframe').src = PATH.chats + '?window=1&w=chats';
      document.body.append(chatPanel);
    }
    chatPanel.hidden = false;
    chatBtn.setAttribute('aria-expanded', 'true');
  }
  if (chatBtn) chatBtn.addEventListener('click', toggleChat);
  document.addEventListener('click', e => {
    if (!chatPanel || chatPanel.hidden) return;
    if (e.target.closest('[data-chat-toggle]') || e.target.closest('.chat-toolbar')) return;
    chatPanel.hidden = true;
    chatBtn.setAttribute('aria-expanded', 'false');
  });

  // Chat: how many conversations are unread, on the toolbar button and on any folded conversation box. Asking also
  // turns new messages into alerts while the hub is open (crm/chat/views.py poll).
  async function pollChat() {
    if (!CHAT_POLL || document.hidden) return;
    try {
      const r = await fetch(CHAT_POLL, {credentials: 'same-origin'});
      if (!r.ok) return;
      const d = await r.json(), ids = d.ids || [];
      const count = chatBtn && chatBtn.querySelector('[data-chat-count]');
      if (count) { count.textContent = d.unread || ''; count.hidden = !d.unread; chatBtn.classList.toggle('has-unread', !!d.unread); }
      const nav = cfg.chatPageUrl && document.querySelector(`.topnav a.tn-link[href="${cfg.chatPageUrl}"]`);
      if (nav) {  // the number of unread conversations beside "Chat" in the top bar, like My tasks
        let n = nav.querySelector('.tn-count');
        if (d.unread && !n) { n = document.createElement('span'); n.className = 'tn-count'; nav.append(' ', n); }
        if (n) { if (d.unread) n.textContent = d.unread; else { n.previousSibling && n.previousSibling.remove(); n.remove(); } }
      }
      wins.forEach(w => {
        if (w.kind === 'chat') w.el.classList.toggle('unread', w.min && ids.includes(w.key));
      });
      if (d.alerts && window.acmiAlerts && window.acmiAlerts.refresh) window.acmiAlerts.refresh();
    } catch (e) { /* offline: the next time */ }
  }
  if (PATH.chats && CHAT_POLL) {
    setInterval(pollChat, 20000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollChat(); });
    pollChat();
  }

  const q = new URLSearchParams(location.search);
  if (q.get('dock')) {
    openUrl(q.get('dock'), q.get('read'));
    q.delete('dock');
    q.delete('read');
    history.replaceState(null, '', location.pathname + (q.toString() ? '?' + q : '') + location.hash);
  }
})();
