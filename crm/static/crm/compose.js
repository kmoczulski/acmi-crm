// Writing email in the hub (crm/mail/compose.py): the full compose page ([data-compose]) and the reading pane's
// inline reply box ([data-inline]). Drafts save to Gmail a few seconds after you stop typing; Send waits ten seconds
// with Undo; the inline box sends in the background and reloads the thread. The body is a small rich-text editor
// (bold, italic, underline, lists, links): it posts body_html, and the textarea it replaces still carries the text.
(function () {
  const csrf = form => form.querySelector('[name=csrfmiddlewaretoken]').value;
  const textToHtml = text => String(text || '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]))
    .replace(/\n/g, '<br>');

  const TOOLS = [
    ['bold', '<b>B</b>', 'Bold (Ctrl+B)'], ['italic', '<i>I</i>', 'Italic (Ctrl+I)'],
    ['underline', '<u>U</u>', 'Underline (Ctrl+U)'], ['strikeThrough', '<s>S</s>', 'Strikethrough'], null,
    ['insertUnorderedList', '•&thinsp;≡', 'Bulleted list'], ['insertOrderedList', '1.&thinsp;≡', 'Numbered list'],
    ['outdent', '⇤', 'Indent less'], ['indent', '⇥', 'Indent more'], null,
    ['createLink', '🔗', 'Link (Ctrl+K)'], ['removeFormat', 'T̸', 'Clear formatting'],
  ];

  function editor(form, textarea) {
    const bar = document.createElement('div');
    bar.className = 'compose-toolbar';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'Formatting');
    bar.innerHTML = TOOLS.map(t => t ? `<button type="button" class="compose-tool" data-cmd="${t[0]}" title="${t[2]}" aria-label="${t[2]}">${t[1]}</button>`
      : '<span class="compose-tool-gap"></span>').join('');
    const area = document.createElement('div');
    area.className = textarea.className + ' compose-editor';
    area.contentEditable = 'true';
    area.setAttribute('role', 'textbox');
    area.setAttribute('aria-multiline', 'true');
    area.setAttribute('aria-label', textarea.getAttribute('aria-label') || 'Message');
    area.innerHTML = textarea.dataset.html || textToHtml(textarea.value);  // data-html is cleaned by the server
    const html = document.createElement('input');
    html.type = 'hidden';
    html.name = 'body_html';
    const sync = () => { html.value = area.innerHTML; textarea.value = area.innerText.replace(/\n$/, ''); };
    textarea.hidden = true;
    textarea.before(bar, area, html);
    sync();

    const link = () => {
      const sel = window.getSelection();
      const range = sel.rangeCount ? sel.getRangeAt(0) : null;
      let url = (window.prompt('Link address', 'https://') || '').trim();
      if (!url || url === 'https://') return;
      if (!/^(https?:|mailto:|tel:)/i.test(url)) url = (url.includes('@') && !url.includes('/') ? 'mailto:' : 'https://') + url;
      area.focus();
      if (range) { sel.removeAllRanges(); sel.addRange(range); }
      if (range && range.collapsed) document.execCommand('insertHTML', false, `<a href="${url.replace(/"/g, '%22')}">${textToHtml(url)}</a>`);
      else document.execCommand('createLink', false, url);
    };
    const run = cmd => {
      area.focus();
      if (cmd === 'createLink') link();
      else {
        document.execCommand(cmd, false, null);
        if (cmd === 'removeFormat') document.execCommand('unlink', false, null);
      }
      sync();
      area.dispatchEvent(new Event('input', {bubbles: true}));
      mark();
    };
    const mark = () => bar.querySelectorAll('[data-cmd]').forEach(b => {
      let on = false;
      try { on = document.queryCommandState(b.dataset.cmd); } catch (e) { /* not a state command */ }
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    bar.addEventListener('mousedown', e => { if (e.target.closest('[data-cmd]')) e.preventDefault(); });  // keep the selection
    bar.addEventListener('click', e => { const b = e.target.closest('[data-cmd]'); if (b) run(b.dataset.cmd); });
    area.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); run('createLink'); }
    });
    area.addEventListener('input', sync);
    document.addEventListener('selectionchange', () => { if (area.contains(document.getSelection().anchorNode)) mark(); });
    form.addEventListener('submit', sync, true);
    if (textarea.autofocus) setTimeout(() => area.focus(), 0);
    return {
      area, sync,
      prepend(text) { area.innerHTML = textToHtml(text) + (area.innerText.trim() ? '<br><br>' + area.innerHTML : ''); sync(); },
      clear() { area.innerHTML = ''; sync(); },
    };
  }

  function setup(form, inline) {
    const state = form.querySelector('[data-draft-state]') || document.querySelector('[data-draft-state]');
    const draftId = form.querySelector('[data-draft-id]');
    const sendBtn = form.querySelector('[data-send]');
    const countdown = form.querySelector('[data-countdown]');
    const seconds = form.querySelector('[data-seconds]');
    const error = form.querySelector('[data-compose-error]');
    const body = form.querySelector('textarea[name=body]');
    try { document.execCommand('styleWithCSS', false, false); } catch (e) { /* older browsers */ }
    const ed = editor(form, body);
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
      ed.prepend(fill(t.body_plain));
      pick.value = '';
      ed.area.dispatchEvent(new Event('input', {bubbles: true}));
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
    return {save, ed};
  }

  document.querySelectorAll('[data-compose]').forEach(form => setup(form, false));

  // The reading pane's inline reply box: Reply / Reply all / Forward fill it in place, under the message. The inbox
  // calls initInlineReply again when it swaps in another conversation (and with it a new box).
  let box = null, inlineEd = null;
  function initInline() {
    box = document.querySelector('[data-inline]');
    if (!box || box.dataset.ready) return;
    box.dataset.ready = '1';
    inlineEd = setup(box, true).ed;
    box.querySelector('[data-inline-close]').addEventListener('click', () => { box.hidden = true; });
  }
  window.initInlineReply = initInline;
  initInline();
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
    inlineEd.clear();
    box.querySelector('[name=bcc]').value = '';
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
    const focus = mode === 'forward' ? box.querySelector('[name=to]') : inlineEd.area;
    box.scrollIntoView({behavior: 'smooth', block: 'center'});
    setTimeout(() => focus.focus(), 250);
  }
  document.addEventListener('click', e => {
    const a = e.target.closest('[data-reply]');
    if (!a || !box || !box.isConnected) return;
    e.preventDefault();
    open(a.dataset.reply, a.dataset.mode || 'reply');
  });
})();
