// Notification settings page: turn browser notifications (Web Push, crm/push.py) on or off for this browser, and test.
// Remembers in localStorage (alerts:push) that this browser gets pushes, so alerts.js doesn't show a second pop-up.
document.addEventListener('DOMContentLoaded', function () {  // after alerts.js, for the sound switch
  const box = document.getElementById('push-panel');
  if (!box) return;
  const $ = id => document.getElementById(id);
  const on = $('push-on'), off = $('push-off'), test = $('push-test'), status = $('push-status'), sound = $('pref-sound');
  const csrf = () => document.cookie.split('; ').find(c => c.startsWith('csrftoken='))?.split('=')[1] || '';
  const post = (url, body) => fetch(url, {method: 'POST', credentials: 'same-origin',
    headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const remember = yes => { try { localStorage.setItem('alerts:push', yes ? '1' : '0'); } catch (e) {} };
  const say = (text, bad) => { status.textContent = text; status.classList.toggle('bad', !!bad); };

  function keyBytes(b64) {
    const s = atob((b64 + '='.repeat((4 - b64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(s, c => c.charCodeAt(0));
  }

  function problem() {
    if (!window.isSecureContext) return 'Browser notifications need a secure (https) address. They work on the laptop at http://localhost now, and on phones once the hub is hosted at hub.acmiproperties.com.';
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window))
      return 'This browser does not support browser notifications. On an iPhone, add the hub to the home screen first (Share, Add to Home Screen) and open it from there.';
    if (box.dataset.ready !== '1') return 'Browser notifications are not set up on the server yet (VAPID keys missing; see docs/HANDOFF.md).';
    return '';
  }

  async function current() {
    const reg = await navigator.serviceWorker.getRegistration(box.dataset.sw);
    return reg ? reg.pushManager.getSubscription() : null;
  }

  function show(sub) {
    on.hidden = !!sub; off.hidden = !sub;
    document.querySelectorAll('.push-devices tr[data-endpoint]').forEach(tr => {
      tr.querySelector('.push-this').hidden = !(sub && tr.dataset.endpoint === sub.endpoint);
    });
  }

  async function refresh() {
    const p = problem();
    if (p) { on.disabled = test.disabled = true; say(p, true); remember(false); return; }
    if (Notification.permission === 'denied') {
      say('Notifications are blocked for this site. Allow them in the browser\'s site settings (the icon left of the address), then reload.', true);
      remember(false); show(null); return;
    }
    let sub = await current();
    const listed = [...document.querySelectorAll('.push-devices tr[data-endpoint]')].some(tr => sub && tr.dataset.endpoint === sub.endpoint);
    if (sub && !listed) { await sub.unsubscribe(); sub = null; }  // removed from the list: stop this browser too
    remember(!!sub); show(sub);
    say(sub ? 'On for this browser.' : 'Off for this browser.');
  }

  on.addEventListener('click', async () => {
    on.disabled = true;
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { await refresh(); return; }
      const reg = await navigator.serviceWorker.register(box.dataset.sw, {scope: '/'});
      await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(box.dataset.key)});
      const r = await post(box.dataset.subscribe, sub.toJSON());
      if (!r.ok) throw new Error(await r.text());
      location.reload();  // shows this browser in the list
    } catch (e) {
      say('Could not turn them on: ' + (e.message || e), true);
    } finally { on.disabled = false; }
  });

  off.addEventListener('click', async () => {
    const sub = await current();
    if (sub) { await post(box.dataset.unsubscribe, {endpoint: sub.endpoint}); await sub.unsubscribe(); }
    location.reload();
  });

  test.addEventListener('click', async () => {
    test.disabled = true;
    try {
      const d = await (await post(box.dataset.test)).json();
      say(d.sent ? `Sent to ${d.sent} browser${d.sent === 1 ? '' : 's'}. It should pop up in a few seconds.`
        : 'No browser of yours has notifications on yet. Click "Turn on browser notifications" first.', !d.sent);
    } catch (e) { say('The test did not go through: ' + (e.message || e), true); }
    finally { test.disabled = false; }
  });

  const A = window.acmiAlerts;
  if (A) {
    sound.checked = A.pref('sound', true);
    sound.addEventListener('change', () => { A.setPref('sound', sound.checked); if (sound.checked) A.chime(); });
  } else sound.closest('label').hidden = true;

  refresh();
});
