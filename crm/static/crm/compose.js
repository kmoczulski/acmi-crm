// Writing email in the hub (crm/mail/compose.py): the full compose page ([data-compose]) and the reading pane's
// inline reply box ([data-inline]). Drafts save to Gmail a few seconds after you stop typing; Send waits ten seconds
// with Undo; the inline box sends in the background and reloads the thread.
(function () {
  const csrf = form => form.querySelector('[name=csrfmiddlewaretoken]').value;

  function setup(form, inline) {
    const state = form.querySelector('[data-draft-state]') || document.querySelector('[data-draft-state]');
    const draftId = form.querySelector('[data-draft-id]');
    const sendBtn = form.querySelector('[data-send]');
    const countdown = form.querySelector('[data-countdown]');
    const seconds = form.querySelector('[data-seconds]');
    const error = form.querySelector('[data-compose-error]');
    const body = form.querySelector('textarea[name=body]');
    let timer = null, saveTimer = null, confirmed = false, saving = false;
    const say = text => { if (state) state.textContent = text; };

    const save = async () => {
      if (saving || confirmed || !form.dataset.draftUrl) return;
      saving = true;
      const data = new FormData(form);
      data.delete('files');
      try {
        const r = await fetch(form.dataset.draftUrl, {method: 'POST', body: data, headers: {'X-CSRFToken': csrf(form)}});
        const d = await r.json();
        if (d.draft) { draftId.value = d.draft; say('Draft saved ' + new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})); }
        else if (d.error) say(d.error);
      } catch (e) { say('Draft not saved (offline?)'); }
      saving = false;
    };
    form.addEventListener('input', e => {
      if (e.target.type === 'file') return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 3000);
    });

    // Attachments: every pick adds to the list (a bare file input replaces the earlier pick, so a second file would
    // have dropped the first), each can be taken off again, and the form sends them all. Browsers that can't set a
    // file input's files (no DataTransfer) keep the native behaviour and only show what's picked.
    const picker = form.querySelector('input[type=file][name=files]');
    const attachLabel = form.querySelector('[data-attach-label]');
    const MAX_MB = 25;
    const canSet = (() => { try { new DataTransfer(); return true; } catch (e) { return false; } })();
    const fmtSize = n => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
    let chosen = [], fileList = null;
    const showFiles = () => {
      if (!picker) return;
      if (canSet) {
        const dt = new DataTransfer();
        chosen.forEach(f => dt.items.add(f));
        picker.files = dt.files;
      } else {
        chosen = [...picker.files];
      }
      if (!fileList) {
        fileList = document.createElement('ul');
        fileList.className = 'compose-filelist lc-small';
        fileList.setAttribute('aria-label', 'Attached files');
        picker.closest('.compose-attach').after(fileList);
      }
      fileList.textContent = '';
      chosen.forEach((f, i) => {
        const li = document.createElement('li');
        const name = document.createElement('span');
        name.className = 'cf-name';
        name.textContent = f.name;
        const size = document.createElement('span');
        size.className = 'muted';
        size.textContent = fmtSize(f.size);
        li.append('📎 ', name, ' ', size);
        if (canSet) {
          const x = document.createElement('button');
          x.type = 'button';
          x.className = 'btn-link cf-remove';
          x.textContent = '✕';
          x.setAttribute('aria-label', `Remove ${f.name}`);
          x.addEventListener('click', () => { chosen.splice(i, 1); showFiles(); });
          li.append(' ', x);
        }
        fileList.append(li);
      });
      const total = chosen.reduce((n, f) => n + f.size, 0);
      if (total > MAX_MB * 1024 * 1024) {
        const li = document.createElement('li');
        li.className = 'mail-error';
        li.textContent = `That's ${fmtSize(total)} together; Gmail allows ${MAX_MB} MB. Take some off, or share a Drive link.`;
        fileList.append(li);
      }
      fileList.hidden = !chosen.length;
      if (attachLabel) attachLabel.textContent = chosen.length && canSet ? '📎 Add another file' : '📎 Attach files';
    };
    if (picker) picker.addEventListener('change', () => {
      for (const f of picker.files) {
        if (!chosen.some(c => c.name === f.name && c.size === f.size && c.lastModified === f.lastModified)) chosen.push(f);
      }
      showFiles();
    });
    const resetFiles = () => { chosen = []; if (picker) { picker.value = ''; showFiles(); } };

    // Templates (full page): the outreach templates' text, {first_name} filled in when the hub knows the person.
    const pick = form.querySelector('[data-template]');
    if (pick) pick.addEventListener('change', () => {
      const list = JSON.parse(document.getElementById('compose-templates').textContent);
      const t = list[+pick.value];
      if (!t) return;
      const first = body.dataset.firstName || 'there';
      const fill = s => (s || '').replaceAll('{first_name}', first).replaceAll('{greeting}', first);
      const subject = form.querySelector('[name=subject]');
      if (!subject.value) subject.value = fill(t.subject);
      body.value = fill(t.body_plain) + (body.value ? '\n\n' + body.value : '');
      pick.value = '';
      body.dispatchEvent(new Event('input', {bubbles: true}));
    });

    const sendNow = async () => {
      if (!inline) {
        confirmed = true;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Sending…';
        form.requestSubmit(sendBtn);
        sendBtn.disabled = true;
        return;
      }
      confirmed = true;
      sendBtn.textContent = 'Sending…';
      try {
        const r = await fetch(form.action, {method: 'POST', body: new FormData(form), headers: {'X-CSRFToken': csrf(form)}});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'Not sent. Try again.');
        location.href = d.url;
      } catch (err) {
        confirmed = false;
        countdown.hidden = true;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
        if (error) { error.textContent = err.message; error.hidden = false; }
      }
    };

    // Undo send: Send waits ten seconds, and Undo (or leaving the page) stops it.
    form.addEventListener('submit', e => {
      if (e.submitter && e.submitter.hasAttribute('data-discard')) return;
      if (confirmed && !inline) return;
      e.preventDefault();
      if (confirmed) return;
      if (error) error.hidden = true;
      let left = 10;
      seconds.textContent = left;
      countdown.hidden = false;
      sendBtn.disabled = true;
      timer = setInterval(() => {
        left -= 1;
        seconds.textContent = left;
        if (left <= 0) { clearInterval(timer); timer = null; sendNow(); }
      }, 1000);
    });
    form.querySelector('[data-undo-send]').addEventListener('click', () => {
      clearInterval(timer);
      timer = null;
      countdown.hidden = true;
      sendBtn.disabled = false;
      say('Not sent.');
    });
    window.addEventListener('beforeunload', e => {
      if (timer) { e.preventDefault(); e.returnValue = ''; }
    });
    return {save, resetFiles};
  }

  document.querySelectorAll('[data-compose]').forEach(form => setup(form, false));

  // The reading pane's inline reply box: Reply / Reply all / Forward fill it in place, under the message.
  const box = document.querySelector('[data-inline]');
  if (!box) return;
  const inlineBox = setup(box, true);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const titles = {reply: 'Reply', all: 'Reply all', forward: 'Forward'};
  async function open(messageId, mode) {
    const r = await fetch(`${box.dataset.fieldsUrl}?reply=${encodeURIComponent(messageId)}&mode=${mode}`);
    if (!r.ok) { location.href = `${box.action}?reply=${encodeURIComponent(messageId)}&mode=${mode}`; return; }
    const f = await r.json();
    for (const k of ['to', 'cc', 'subject', 'thread_id', 'in_reply_to', 'references', 'reply_to_id', 'mode']) {
      const input = box.querySelector(`[name=${k}]`);
      if (input) input.value = f[k] || '';
    }
    box.querySelector('[data-draft-id]').value = '';
    box.querySelector('[name=bcc]').value = '';
    inlineBox.resetFiles();  // files picked for the last reply don't carry over to this one
    box.querySelector('[data-inline-title]').textContent = titles[mode] || 'Reply';
    box.querySelector('[data-inline-more]').href = `${box.action}?reply=${encodeURIComponent(messageId)}&mode=${mode}&back=${encodeURIComponent(location.pathname + location.search)}`;
    box.querySelector('.compose-more').open = !!f.cc;
    box.querySelector('[data-inline-sig]').innerHTML = f.signature_html || '';
    box.querySelector('[data-inline-sig-row]').hidden = !f.signature_html;
    const files = box.querySelector('[data-inline-files]');
    files.innerHTML = (f.forward_files || []).map(a =>
      `<label class="lc-small"><input type="checkbox" name="forward_part" value="${esc(a.part)}" checked> 📎 ${esc(a.name)}</label>`).join('');
    files.hidden = !(f.forward_files || []).length;
    box.querySelector('[data-send]').disabled = !f.sending_on;
    const msg = document.getElementById('m-' + messageId);
    (msg || document.querySelector('[data-reader] .mail-reader-head')).after(box);
    box.hidden = false;
    const focus = mode === 'forward' ? box.querySelector('[name=to]') : box.querySelector('textarea[name=body]');
    box.scrollIntoView({behavior: 'smooth', block: 'center'});
    setTimeout(() => focus.focus(), 250);
  }
  document.addEventListener('click', e => {
    const a = e.target.closest('[data-reply]');
    if (!a) return;
    e.preventDefault();
    open(a.dataset.reply, a.dataset.mode || 'reply');
  });
  box.querySelector('[data-inline-close]').addEventListener('click', () => { box.hidden = true; });
})();
