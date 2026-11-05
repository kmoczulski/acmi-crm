// Undo send: the notification in the bottom left corner (crm/mail/outbox.py). Send queues the message on the server and the
// page is free at once; this counts down with Undo, in a teal that no other alert uses, on whichever hub page is open.
// compose.js hands over a new one with hubOutbox.add(item); every page also asks the server for any still waiting, so the
// notification follows you from page to page. When the time runs out it asks the server to send (the server also sends
// anything due on its own, so a closed page never loses a message).
(function () {
  if (window.top !== window) return;  // a frame (the dock's box, the split view) hands over to the top page
  const cfg = document.getElementById('outbox-config');
  if (!cfg) return;
  const url = (tpl, id) => tpl.replace(/\/0\//, '/' + id + '/');
  const csrf = () => (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';
  const post = u => fetch(u, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': csrf(), 'X-Requested-With': 'fetch'}})
    .then(r => r.json().then(d => ({ok: r.ok, d})).catch(() => ({ok: r.ok, d: {}})));

  let box = null;
  const shown = new Map();  // id -> {el, deadline, timer, state}
  function container() {
    if (box) return box;
    box = document.createElement('div');
    box.className = 'outbox-toasts no-print';
    box.id = 'outbox-toasts';
    box.setAttribute('role', 'status');
    box.setAttribute('aria-live', 'polite');
    document.body.append(box);
    return box;
  }
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

  function draw(rec, mode, extra) {
    const item = rec.item, el = rec.el;
    el.className = 'outbox-toast outbox-' + mode;
    el.replaceChildren();
    const msg = document.createElement('span');
    msg.className = 'outbox-msg';
    el.append(msg);
    const add = (label, cls, fn) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'outbox-btn ' + (cls || '');
      b.textContent = label;
      b.addEventListener('click', fn);
      el.append(b);
      return b;
    };
    const link = (label, href) => {
      const a = document.createElement('a');
      a.className = 'outbox-btn';
      a.href = href;
      a.textContent = label;
      el.append(a);
    };
    const subject = '“' + clip(item.subject, 48) + '”';
    if (mode === 'waiting') {
      const left = Math.max(Math.ceil((rec.deadline - Date.now()) / 1000), 0);
      msg.textContent = 'Sending ' + subject + (item.to ? ' to ' + clip(item.to, 40) : '') + ' in ' + left + 's';
      add('Undo', 'outbox-undo', () => undo(rec));
      add('Send now', '', () => { rec.deadline = Date.now(); tick(rec); });
    } else if (mode === 'sending') {
      msg.textContent = 'Sending ' + subject + '…';
    } else if (mode === 'sent') {
      msg.textContent = '✓ Sent ' + subject;
      if (item.url) link('View', item.url);
      add('✕', 'outbox-x', () => dismiss(rec));
      rec.dismissAt = setTimeout(() => dismiss(rec), 6000);
    } else if (mode === 'undone') {
      msg.textContent = 'Not sent. ' + (extra || '');
      link('Edit message', item.reopen);
      add('✕', 'outbox-x', () => dismiss(rec));
      rec.dismissAt = setTimeout(() => dismiss(rec), 30000);
    } else if (mode === 'failed') {
      msg.textContent = '⚠ ' + subject + ' wasn’t sent: ' + (item.error || extra || 'something went wrong.');
      link('Open it again', item.reopen);
      add('✕', 'outbox-x', () => dismiss(rec));
    } else {  // late: it was already on its way
      msg.textContent = 'Too late to undo: ' + subject + ' has already gone.';
      add('✕', 'outbox-x', () => dismiss(rec));
      rec.dismissAt = setTimeout(() => dismiss(rec), 6000);
    }
  }

  function dismiss(rec) {
    clearInterval(rec.timer);
    clearTimeout(rec.dismissAt);
    rec.el.remove();
    shown.delete(rec.item.id);
  }

  async function undo(rec) {
    clearInterval(rec.timer);
    draw(rec, 'sending');
    try {
      const {d} = await post(url(cfg.dataset.undo, rec.item.id));
      if (d.ok) { rec.item = d.item || rec.item; draw(rec, 'undone', 'It is back to edit.'); }
      else { rec.item = d.item || rec.item; settle(rec, rec.item); }
    } catch (e) { draw(rec, 'failed', 'Couldn’t reach the hub to undo it. Try again.'); }
  }

  // The server's word on where it stands: sent, failed, still waiting (not due yet), or someone else is sending it.
  function settle(rec, item, tries) {
    rec.item = item;
    if (item.status === 'sent') draw(rec, 'sent');
    else if (item.status === 'failed') draw(rec, 'failed');
    else if (item.status === 'cancelled') draw(rec, 'undone', 'It is back to edit.');
    else if ((tries || 0) < 12) setTimeout(() => commit(rec, (tries || 0) + 1), 1200);  // queued (clock a hair early) or sending
    else draw(rec, 'failed', 'Check your Sent folder, then try again.');
  }

  async function commit(rec, tries) {
    draw(rec, 'sending');
    try {
      const {d} = await post(url(cfg.dataset.send, rec.item.id));
      if (d.item) settle(rec, d.item, tries); else dismiss(rec);
    } catch (e) { setTimeout(() => commit(rec, (tries || 0) + 1), 2000); }  // offline: keep trying; the server sends it anyway
  }

  function tick(rec) {
    if (rec.deadline - Date.now() <= 0) { clearInterval(rec.timer); commit(rec, 0); }
    else draw(rec, 'waiting');
  }

  function add(item) {
    if (!item || shown.has(item.id)) return;
    const rec = {item, el: document.createElement('div'), deadline: Date.now() + (item.seconds_left || 0) * 1000};
    shown.set(item.id, rec);
    container().append(rec.el);
    if (item.status === 'queued') {
      draw(rec, 'waiting');
      rec.timer = setInterval(() => tick(rec), 250);
    } else if (item.status === 'sent') draw(rec, 'sent');
    else if (item.status === 'failed') draw(rec, 'failed');
    else dismiss(rec);
  }

  window.hubOutbox = {add};
  // Anything still waiting from a page you left a moment ago.
  fetch(cfg.dataset.pending, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}})
    .then(r => (r.ok ? r.json() : {items: []}))
    .then(d => (d.items || []).forEach(add))
    .catch(() => { /* offline: nothing to show */ });
})();
