// Live alerts: polls for new notifications, updates the bell, plays a chime and shows a browser notification.
// Preferences are per browser (localStorage): alerts:desktop = "1" | "0", alerts:sound = "1" | "0".
// alerts:push = "1" when this browser gets Web Push (push.js): the service worker shows those, so no second pop-up here.
// alerts:last = the id of the newest alert this browser has handled, shared by every open tab (so only one tab alerts). Missing
// means this browser has no baseline yet; 0 is a real baseline (a browser that started with no alerts). alerts:nudge = until when
// "Not now" on the turn-on-banners bar holds.
(function () {
  const POLL_MS = 30000;
  const root = document.getElementById('alerts-config');
  if (!root || window.top !== window) return;  // the split view's side frame: the main page polls
  const pollUrl = root.dataset.poll;
  const pref = (k, d) => { try { const v = localStorage.getItem('alerts:' + k); return v === null ? d : v === '1'; } catch (e) { return d; } };
  const setPref = (k, on) => { try { localStorage.setItem('alerts:' + k, on ? '1' : '0'); } catch (e) {} };
  // The baseline, or null when there is none. Where localStorage is blocked or was cleared, this page keeps its own, so it still
  // alerts for as long as it is open.
  let kept = null;
  const lastSeen = () => {
    try { const v = localStorage.getItem('alerts:last'); if (v !== null && v !== '' && !isNaN(+v)) return +v; } catch (e) { /* blocked */ }
    return kept;
  };
  const setLastSeen = id => { kept = id; try { localStorage.setItem('alerts:last', String(id)); } catch (e) {} };

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

  // The × on a pop-up: mark that one alert read without opening it (notifications/<id>/dismiss/); the bell follows the answer.
  const csrf = () => (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';
  async function dismiss(url) {
    try {
      const r = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'X-Requested-With': 'fetch', 'X-CSRFToken': csrf()}});
      if (r.ok) setBadge((await r.json()).unread);
    } catch (e) { /* offline or signed out: the next poll puts the bell right */ }
  }

  function toast(item) {
    let box = document.getElementById('alert-toasts');
    if (!box) { box = document.createElement('div'); box.id = 'alert-toasts'; document.body.appendChild(box); }
    const t = document.createElement('div');  // the link and the × sit side by side: a button can't live inside a link
    t.className = 'alert-toast';
    const a = document.createElement('a');
    a.className = 'alert-toast-link'; a.href = item.url;
    if (item.window) a.dataset.window = item.window;  // a new email opens in a box of the mail dock (dock.js)
    a.innerHTML = '<b></b><span></span>';
    a.querySelector('b').textContent = item.kind; a.querySelector('span').textContent = item.text;
    t.appendChild(a);
    if (item.dismiss) {  // a saved alert can be marked read right here (the settings page's test alert is not saved)
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'alert-toast-x'; x.textContent = '×';
      x.title = 'Dismiss: mark as read'; x.setAttribute('aria-label', 'Dismiss: ' + item.text);
      x.addEventListener('click', () => { t.remove(); dismiss(item.dismiss); });
      t.appendChild(x);
    }
    box.appendChild(t);
    setTimeout(() => t.classList.add('gone'), 9000);
    setTimeout(() => t.remove(), 9600);
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

  // Show one alert the way this page can right now: a pop-up while the page is on screen, and an OS banner as well when it is
  // hidden or its window is not the one in front (a browser on one monitor, other work on the other). True if either was shown.
  function deliver(item) {
    let shown = false;
    if ((document.hidden || (document.hasFocus && !document.hasFocus())) && desktop(item)) shown = true;
    if (!document.hidden) { toast(item); shown = true; }
    return shown;
  }

  async function poll() {
    if (signedOut) return;
    try {
      const base = lastSeen();  // null: no baseline yet, so ask where things stand and replay nothing
      const r = await fetch(pollUrl + (base === null ? '' : '?after=' + base), {headers: {'X-Requested-With': 'fetch'}, credentials: 'same-origin'});
      if (r.status === 401 || r.status === 403 || (r.redirected && /\/accounts\/login/.test(r.url))) { showSignedOut(); return; }
      if (!r.ok) return;
      if (!/json/.test(r.headers.get('content-type') || '')) { showSignedOut(); return; }
      const d = await r.json();
      setBadge(d.unread);
      if (base === null) { setLastSeen(d.latest); return; }  // a browser's first look (even if it finds 0): start from here
      const seen = lastSeen();  // another tab may have handled some while this answer was on its way
      const fresh = d.items.filter(i => i.id > (seen === null ? base : seen));
      if (!fresh.length) return;
      // A page that is hidden and cannot raise an OS banner leaves the alerts alone: no pop-up nobody sees, and the shared
      // baseline stays put, so the tab in front (or this one, when you come back: visibilitychange asks at once) shows them.
      // Web Push has its own banner, so a hidden page of a push browser has nothing to add and moves the baseline.
      const shown = fresh.filter((i, n) => fresh.findIndex(x => x.text === i.text) === n).slice(-3).map(deliver);
      if (!shown.some(Boolean) && !(document.hidden && pref('push', false))) return;
      setLastSeen(Math.max(...fresh.map(i => i.id)));
      if (pref('sound', true)) chime();
    } catch (e) { /* offline: try again next time */ }
  }

  // Browser banners need a one-time yes from the person (Settings > Notifications, push.js). Until then this browser only gets
  // the pop-ups inside the page, so offer it once in a small bar. "Not now" holds for two weeks.
  const NUDGE_DAYS = 14;
  function offerBanners() {
    const settings = root.dataset.settings || '';
    try {
      if (!('Notification' in window) || Notification.permission !== 'default' || pref('push', false)) return;
      if (!settings || location.pathname.startsWith(settings)) return;  // not on the page that turns them on
      if (+localStorage.getItem('alerts:nudge') > Date.now()) return;
    } catch (e) { return; }
    const main = document.getElementById('main');
    if (!main || main.querySelector('[data-notify-nudge]')) return;
    const bar = document.createElement('div');
    bar.className = 'notify-nudge no-print';
    bar.setAttribute('data-notify-nudge', '');
    bar.setAttribute('role', 'region');
    bar.setAttribute('aria-label', 'Browser notifications');
    const text = document.createElement('span');
    text.innerHTML = '<strong>Get a banner when something new arrives.</strong> Turn on browser notifications and alerts reach you even when the hub is in another window.';
    const on = document.createElement('a');
    on.className = 'btn btn-small btn-primary'; on.href = settings; on.textContent = 'Turn on';
    const later = document.createElement('button');
    later.type = 'button'; later.className = 'btn btn-small'; later.textContent = 'Not now';
    later.addEventListener('click', () => {
      try { localStorage.setItem('alerts:nudge', String(Date.now() + NUDGE_DAYS * 864e5)); } catch (e) { /* asks again next page */ }
      bar.remove();
    });
    bar.append(text, on, later);
    main.prepend(bar);
  }

  window.acmiAlerts = {
    pref, setPref, chime, setBadge, offerBanners, refresh: () => poll(),  // setBadge: the Notifications page sets the bell after a ×
    test() { const i = {id: 0, kind: 'Test alert', text: 'Notifications and sounds are working.', url: location.href}; if (pref('sound', true)) chime(); if (!desktop(i)) toast(i); },
    async enableDesktop() {
      if (!('Notification' in window)) return 'unsupported';
      const p = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
      setPref('desktop', p === 'granted');
      return p;
    },
  };
  poll();
  offerBanners();
  setInterval(poll, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  // A phone wakes a frozen app, restores it from its page cache, or gets its connection back: ask again right away.
  window.addEventListener('pageshow', e => { if (e.persisted) poll(); });
  window.addEventListener('online', () => poll());
})();
