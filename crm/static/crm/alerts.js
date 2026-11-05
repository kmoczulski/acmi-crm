// Live alerts: polls for new notifications, updates the bell, plays a chime and shows a browser notification.
// Preferences are per browser (localStorage): alerts:desktop = "1" | "0", alerts:sound = "1" | "0".
// alerts:push = "1" when this browser gets Web Push (push.js): the service worker shows those, so no second pop-up here.
(function () {
  const POLL_MS = 30000;
  const root = document.getElementById('alerts-config');
  if (!root || window.top !== window) return;  // the split view's side frame: the main page polls
  const pollUrl = root.dataset.poll;
  const pref = (k, d) => { try { const v = localStorage.getItem('alerts:' + k); return v === null ? d : v === '1'; } catch (e) { return d; } };
  const setPref = (k, on) => { try { localStorage.setItem('alerts:' + k, on ? '1' : '0'); } catch (e) {} };
  const lastSeen = () => { try { return +localStorage.getItem('alerts:last') || 0; } catch (e) { return 0; } };
  const setLastSeen = id => { try { localStorage.setItem('alerts:last', String(id)); } catch (e) {} };

  let ctx = null;
  function chime() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      const t = ctx.currentTime;
      [[880, 0], [1320, 0.16]].forEach(([f, d]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t + d);
        g.gain.exponentialRampToValueAtTime(0.25, t + d + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.45);
        o.connect(g).connect(ctx.destination); o.start(t + d); o.stop(t + d + 0.5);
      });
    } catch (e) { /* audio blocked until the page has been clicked */ }
  }
  // Browsers only allow sound after a click on the page: unlock the audio on the first one.
  document.addEventListener('click', () => { if (!ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} } }, {once: true});

  function toast(item) {
    let box = document.getElementById('alert-toasts');
    if (!box) { box = document.createElement('div'); box.id = 'alert-toasts'; document.body.appendChild(box); }
    const a = document.createElement('a');
    a.className = 'alert-toast'; a.href = item.url;
    if (item.window) a.dataset.window = item.window;  // a new email opens in a box of the mail dock (dock.js)
    a.innerHTML = '<b></b><span></span>';
    a.querySelector('b').textContent = item.kind; a.querySelector('span').textContent = item.text;
    box.appendChild(a);
    setTimeout(() => a.classList.add('gone'), 9000);
    setTimeout(() => a.remove(), 9600);
  }

  function desktop(item) {
    if (!pref('desktop', true) || pref('push', false) || !('Notification' in window) || Notification.permission !== 'granted') return false;
    try {
      const n = new Notification(item.kind, {body: item.text, tag: 'acmi-' + item.id, icon: root.dataset.icon || undefined});
      n.onclick = () => {
        window.focus();
        if (item.window && window.acmiDock) window.acmiDock.openUrl(item.window, item.url); else location.href = item.url;
        n.close();
      };
      return true;
    } catch (e) { return false; }
  }

  function setBadge(n) {
    const bell = document.querySelector('.appbar .bell');
    if (!bell) return;
    let tag = bell.querySelector('.count');
    bell.classList.toggle('has-unread', !!n);
    if (!n) { if (tag) tag.remove(); return; }
    if (!tag) { tag = document.createElement('span'); tag.className = 'count'; bell.append(tag); }
    tag.textContent = n;
  }

  // Signed out (the session ended) or the server wants a sign-in: the poll is answered with the sign-in page. Say so, once,
  // with a way back, instead of the page quietly going stale (a phone that "disconnects").
  let signedOut = false;
  function showSignedOut() {
    if (signedOut) return;
    signedOut = true;
    const bar = document.createElement('div');
    bar.className = 'signed-out-bar no-print';
    bar.setAttribute('role', 'alert');
    const text = document.createElement('span');
    text.textContent = 'You were signed out, so this page has stopped updating.';
    const a = document.createElement('a');
    a.className = 'btn btn-small btn-primary';
    a.href = '/accounts/login/?next=' + encodeURIComponent(location.pathname + location.search);
    a.target = '_top';
    a.textContent = 'Sign in again';
    bar.append(text, a);
    document.body.prepend(bar);
  }

  async function poll(first) {
    if (signedOut) return;
    try {
      const r = await fetch(pollUrl + '?after=' + lastSeen(), {headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin'});
      if (r.status === 401 || r.status === 403 || (r.redirected && /\/accounts\/login/.test(r.url))) { showSignedOut(); return; }
      if (!r.ok) return;
      if (!/json/.test(r.headers.get('content-type') || '')) { showSignedOut(); return; }
      const d = await r.json();
      setBadge(d.unread);
      if (first && !lastSeen()) { setLastSeen(d.latest); return; }  // first visit: don't replay old alerts
      const fresh = d.items.filter(i => i.id > lastSeen());
      if (!fresh.length) return;
      setLastSeen(Math.max(...fresh.map(i => i.id)));  // shared by every open tab, so only one alerts
      if (pref('sound', true)) chime();
      // Looking at the app: an in-page toast. In another tab or window: a desktop notification (toast if blocked).
      fresh.filter((i, n) => fresh.findIndex(x => x.text === i.text) === n).slice(-3).forEach(i => { if (!document.hidden || !desktop(i)) toast(i); });
    } catch (e) { /* offline: try again next time */ }
  }

  window.acmiAlerts = {
    pref, setPref, chime, refresh: () => poll(false),
    test() { const i = {id: 0, kind: 'Test alert', text: 'Notifications and sounds are working.', url: location.href}; if (pref('sound', true)) chime(); if (!desktop(i)) toast(i); },
    async enableDesktop() {
      if (!('Notification' in window)) return 'unsupported';
      const p = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
      setPref('desktop', p === 'granted');
      return p;
    },
  };
  poll(true);
  setInterval(() => poll(false), POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(false); });
  // A phone wakes a frozen app, restores it from its page cache, or gets its connection back: ask again right away.
  window.addEventListener('pageshow', e => { if (e.persisted) poll(false); });
  window.addEventListener('online', () => poll(false));
})();
