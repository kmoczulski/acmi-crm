// Address suggestions (crm/mail/search.py, /inbox/suggest/): To, Cc and Bcc suggest people as you type the name or
// address after the last comma; ↑ ↓ to move, Enter or Tab to take one, Esc to close. The mail search box suggests
// people (searches their mail both ways) and says which companies, firms or people a plain search also covers.
(function () {
  const url = document.currentScript.dataset.url;
  const esc = s => String(s || '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  function attach(input, {search = false} = {}) {
    if (input.dataset.suggestOn) return;
    input.dataset.suggestOn = '1';
    const box = document.createElement('div');
    box.className = 'addr-suggest';
    box.hidden = true;
    box.setAttribute('role', 'listbox');
    input.insertAdjacentElement('afterend', box);
    input.parentElement.classList.add('addr-host');
    let items = [], at = -1, timer = null, seq = 0;

    const token = () => search ? input.value.trim() : input.value.split(/[,;]/).pop().trim();
    const close = () => { box.hidden = true; items = []; at = -1; };
    const mark = () => box.querySelectorAll('[data-i]').forEach(el => el.classList.toggle('on', +el.dataset.i === at));

    function take(i) {
      const p = items[i];
      if (!p) return;
      if (search) {
        input.value = p.query;
        close();
        input.form.requestSubmit ? input.form.requestSubmit() : input.form.submit();
        return;
      }
      const name = /[,;"<>@]/.test(p.name) ? '"' + p.name.replace(/"/g, '') + '"' : p.name;
      const done = input.value.split(/[,;]/).slice(0, -1).map(s => s.trim()).filter(Boolean);
      done.push(p.name ? `${name} <${p.email}>` : p.email);
      input.value = done.join(', ') + ', ';
      input.dispatchEvent(new Event('input', {bubbles: true}));  // the draft autosave listens for this
      close();
      input.focus();
    }

    async function lookup() {
      const words = token();
      if (words.length < 2) { close(); return; }
      const n = ++seq;
      const d = await fetch(`${url}?q=${encodeURIComponent(words)}${search ? '&records=1' : ''}`).then(r => r.json()).catch(() => null);
      if (n !== seq || !d) return;
      items = d.people.map(p => search ? {...p, query: `{from:${p.email} to:${p.email} cc:${p.email}}`} : p);
      const people = items.map((p, i) => `<div class="addr-row" role="option" data-i="${i}">
          <b>${esc(p.name || p.email)}</b>${p.name ? ` <span class="muted">${esc(p.email)}</span>` : ''}${p.detail ? ` <span class="addr-detail">${esc(p.detail)}</span>` : ''}
          ${search ? '<span class="muted lc-small"> · mail with them</span>' : ''}</div>`).join('');
      const covers = search && d.records.length ? `<div class="addr-note muted lc-small">Searching “${esc(words)}” also finds mail with
          ${d.records.map(r => esc(r.label) + ' (' + r.kind + ')').join(', ')}</div>` : '';
      if (!people && !covers) { close(); return; }
      box.innerHTML = people + covers;
      box.hidden = false;
      at = search ? -1 : 0;
      mark();
    }

    input.addEventListener('input', e => {
      if (!e.isTrusted && !e.inputType) return;  // our own dispatch after taking a suggestion
      clearTimeout(timer);
      timer = setTimeout(lookup, 150);
    });
    input.addEventListener('keydown', e => {
      if (box.hidden || !items.length) { if (e.key === 'Escape') close(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); at = (at + 1) % items.length; mark(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); at = (at - 1 + items.length) % items.length; mark(); }
      else if ((e.key === 'Enter' || (e.key === 'Tab' && !search)) && at >= 0) { e.preventDefault(); take(at); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    box.addEventListener('mousedown', e => {  // mousedown, so the input doesn't lose focus and close the box first
      const row = e.target.closest('[data-i]');
      if (row) { e.preventDefault(); take(+row.dataset.i); }
    });
    input.addEventListener('blur', () => setTimeout(close, 150));
  }

  document.querySelectorAll('input[name=to], input[name=cc], input[name=bcc]').forEach(i => {
    if (i.closest('form')?.matches('[data-compose], [data-inline]')) attach(i);
  });
  const q = document.querySelector('.mail-search input[name=q]');
  if (q) attach(q, {search: true});
})();
