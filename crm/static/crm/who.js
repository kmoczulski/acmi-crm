// A name in an email (From, To, Cc: buttons marked data-who) opens a small card: who the address is in the hub, or a
// form that puts them in the directory as a contact at a firm (crm/directory/views.py who, who_add). Works on the inbox
// page and in the dock's email boxes.
(function () {
  const script = document.currentScript;
  const url = script && script.dataset.url;
  if (!url) return;
  const csrf = () => (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';
  let dlg = null;

  function dialog() {
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.className = 'who-dialog';
    dlg.setAttribute('aria-label', 'Person');
    dlg.addEventListener('click', e => {
      if (e.target === dlg || e.target.closest('[data-who-close]')) dlg.close();  // the backdrop, or ✕
    });
    dlg.addEventListener('submit', add);
    document.body.append(dlg);
    return dlg;
  }

  function say(html) { dialog().innerHTML = html; }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  async function open(b) {
    const d = dialog();
    say('<p class="muted" style="margin:0">Looking them up…</p>');
    if (!d.open) d.showModal();
    const q = new URLSearchParams({email: b.dataset.who, name: b.dataset.name || '', next: location.pathname + location.search});
    try {
      const r = await fetch(`${url}?${q}`, {credentials: 'same-origin'});
      if (!r.ok) throw new Error(r.status);
      say(await r.text());
      const first = d.querySelector('input[name=firm]:invalid, input[name=name]');
      if (first && matchMedia('(pointer: fine)').matches) first.focus();
    } catch (e) {
      say('<div class="who-card"><p style="margin:0">Couldn\'t look that up. Try again.</p></div>');
    }
  }

  async function add(e) {
    const form = e.target.closest('[data-who-add]');
    if (!form) return;
    e.preventDefault();
    const btn = form.querySelector('button.btn-primary'), err = form.querySelector('.who-error');
    btn.disabled = true;
    try {
      const r = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin',
                                          headers: {'X-CSRFToken': csrf(), 'X-Requested-With': 'fetch'}});
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.error || 'Not added. Try again.');
      say(`<div class="who-card"><div class="who-head"><strong>${esc(data.message)}</strong>`
          + '<button type="button" class="btn-link who-x" data-who-close aria-label="Close">✕</button></div>'
          + `<p style="margin:0"><a class="btn btn-small" href="${esc(data.url)}" target="_top">Open ${esc(data.firm)}</a></p></div>`);
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
    }
  }

  // Capture, so a name inside a message's summary line opens the card instead of folding the message.
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-who]');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    open(b);
  }, true);
})();
