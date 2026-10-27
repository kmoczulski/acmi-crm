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


  // Times: your calendar for a week beside the email. Click free time to offer a slot; Insert puts the picked
  // slots into the email where the cursor was, in your time zone.
  const times = (function () {
    const HOUR_PX = 44, FIRST = 7, LAST = 20, SNAP = 15;
    let panel = null, target = null, url = '', weekStart = null, events = [], picked = [], length = 30, weekends = false;
    const pad = n => String(n).padStart(2, '0');
    const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const monday = d => { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
    const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
    const zone = () => (new Intl.DateTimeFormat('en-US', {timeZoneName: 'short'}).formatToParts(new Date())
      .find(p => p.type === 'timeZoneName') || {}).value || '';
    const clock = d => d.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
    const dayName = d => d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'});
    const span = s => {
      const a = clock(s.start), b = clock(s.end);
      const same = a.slice(-2) === b.slice(-2);
      return `${same ? a.slice(0, -3) : a}–${b}`;
    };
    const line = s => `${dayName(s.start)}, ${span(s)}`;

    function build() {
      panel = document.createElement('aside');
      panel.className = 'times-panel';
      panel.setAttribute('aria-label', 'Pick meeting times');
      panel.hidden = true;
      panel.innerHTML = `
        <div class="times-head"><strong>Offer times</strong><span class="muted lc-small" data-zone></span>
          <button type="button" class="btn btn-small" data-close aria-label="Close">✕</button></div>
        <div class="times-bar">
          <button type="button" class="btn btn-small" data-week="-1" aria-label="Previous week">‹</button>
          <span data-week-label></span>
          <button type="button" class="btn btn-small" data-week="1" aria-label="Next week">›</button>
          <span class="spacer"></span>
          <select data-length aria-label="Meeting length">
            <option value="15">15 min</option><option value="30" selected>30 min</option><option value="45">45 min</option>
            <option value="60">1 hour</option><option value="90">1.5 hours</option><option value="120">2 hours</option>
          </select>
          <label class="lc-small"><input type="checkbox" data-weekends> Weekends</label>
        </div>
        <p class="muted lc-small times-hint">Click free time to offer it. Click a picked time to drop it.</p>
        <p class="mail-error" data-times-error hidden></p>
        <div class="times-grid-wrap"><div class="times-grid" data-grid></div></div>
        <div class="times-foot">
          <ul class="times-picked" data-picked></ul>
          <div class="times-actions">
            <button type="button" class="btn-primary" data-insert disabled>Insert into email</button>
            <button type="button" class="btn btn-small" data-clear>Clear</button>
          </div>
        </div>`;
      document.body.append(panel);
      panel.querySelector('[data-zone]').textContent = zone();
      panel.querySelector('[data-close]').addEventListener('click', close);
      panel.querySelectorAll('[data-week]').forEach(b => b.addEventListener('click', () => {
        weekStart = addDays(weekStart, 7 * +b.dataset.week);
        load();
      }));
      panel.querySelector('[data-length]').addEventListener('change', e => { length = +e.target.value; });
      panel.querySelector('[data-weekends]').addEventListener('change', e => { weekends = e.target.checked; draw(); });
      panel.querySelector('[data-clear]').addEventListener('click', () => { picked = []; draw(); });
      panel.querySelector('[data-insert]').addEventListener('click', insert);
      panel.querySelector('[data-grid]').addEventListener('click', pick);
      document.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) close(); });
    }

    async function load() {
      const label = panel.querySelector('[data-week-label]');
      const end = addDays(weekStart, 6);
      label.textContent = `${weekStart.toLocaleDateString('en-US', {month: 'short', day: 'numeric'})} – ${end.toLocaleDateString('en-US', {month: 'short', day: 'numeric'})}`;
      const err = panel.querySelector('[data-times-error]');
      err.hidden = true;
      events = [];
      draw(true);
      try {
        const r = await fetch(`${url}?start=${iso(weekStart)}`);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Couldn’t read your calendar.');
        events = d.events.map(e => ({
          title: e.title, allDay: e.all_day,
          start: e.all_day ? new Date(e.start + 'T00:00') : new Date(e.start),
          end: e.all_day ? new Date(e.end + 'T00:00') : new Date(e.end),
        }));
      } catch (e) {
        err.textContent = e.message + ' You can still pick times.';
        err.hidden = false;
      }
      draw();
    }

    function days() {
      const out = [];
      for (let i = 0; i < 7; i++) {
        const d = addDays(weekStart, i);
        if (weekends || (d.getDay() !== 0 && d.getDay() !== 6)) out.push(d);
      }
      return out;
    }
    const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
    const top = d => ((d.getHours() + d.getMinutes() / 60) - FIRST) * HOUR_PX;
    const busy = (a, b) => events.some(e => !e.allDay && e.start < b && e.end > a);

    function draw(loading) {
      const grid = panel.querySelector('[data-grid]');
      const now = new Date();
      const list = days();
      grid.style.setProperty('--days', list.length);
      let html = '<div class="times-hours">';
      for (let h = FIRST; h < LAST; h++) html += `<span style="top:${(h - FIRST) * HOUR_PX}px">${h % 12 || 12}${h < 12 ? 'a' : 'p'}</span>`;
      html += '</div>';
      list.forEach(d => {
        const next = addDays(d, 1);
        const allDay = events.filter(e => e.allDay && e.start < next && e.end > d);
        html += `<div class="times-day" data-day="${iso(d)}"><div class="times-day-head${iso(d) === iso(now) ? ' today' : ''}">${esc(d.toLocaleDateString('en-US', {weekday: 'short'}))} <strong>${d.getDate()}</strong>`
          + allDay.map(e => `<span class="times-allday" title="${esc(e.title)}">${esc(e.title)}</span>`).join('') + '</div>'
          + `<div class="times-col" style="height:${(LAST - FIRST) * HOUR_PX}px">`;
        if (d < now) {
          const past = iso(d) === iso(now) ? Math.max(0, Math.min(top(now), (LAST - FIRST) * HOUR_PX)) : (LAST - FIRST) * HOUR_PX;
          html += `<div class="times-past" style="height:${past}px"></div>`;
        }
        events.filter(e => !e.allDay && e.start < next && e.end > d).forEach(e => {
          const a = Math.max(0, top(e.start < d ? d : e.start)), b = Math.min((LAST - FIRST) * HOUR_PX, top(e.end > next ? next : e.end));
          if (b > a) html += `<div class="times-busy" style="top:${a}px;height:${Math.max(b - a, 12)}px" title="${esc(e.title)} · ${esc(span(e))}">${esc(e.title)}</div>`;
        });
        picked.forEach((s, i) => {
          if (iso(s.start) !== iso(d)) return;
          html += `<button type="button" class="times-slot${busy(s.start, s.end) ? ' clash' : ''}" data-slot="${i}" style="top:${top(s.start)}px;height:${Math.max(top(s.end) - top(s.start), 14)}px" title="Drop this time">${esc(span(s))}</button>`;
        });
        html += '</div></div>';
      });
      grid.innerHTML = html;
      grid.classList.toggle('loading', !!loading);
      picked.sort((a, b) => a.start - b.start);
      panel.querySelector('[data-picked]').innerHTML = picked.map((s, i) =>
        `<li>${esc(line(s))}${busy(s.start, s.end) ? ' <span class="tag">clashes</span>' : ''} <button type="button" class="btn-link" data-drop="${i}" aria-label="Drop ${esc(line(s))}">×</button></li>`).join('')
        || '<li class="muted lc-small">No times picked yet.</li>';
      panel.querySelectorAll('[data-drop]').forEach(b => b.addEventListener('click', () => { picked.splice(+b.dataset.drop, 1); draw(); }));
      panel.querySelector('[data-insert]').disabled = !picked.length;
    }

    function pick(e) {
      const slot = e.target.closest('[data-slot]');
      if (slot) { picked.splice(+slot.dataset.slot, 1); draw(); return; }
      const col = e.target.closest('.times-col');
      if (!col || e.target.closest('.times-busy')) return;
      const y = e.clientY - col.getBoundingClientRect().top;
      const minutes = Math.floor((y / HOUR_PX * 60) / SNAP) * SNAP;
      const start = new Date(col.parentElement.dataset.day + 'T00:00');
      start.setMinutes(FIRST * 60 + minutes);
      const end = new Date(start.getTime() + length * 60000);
      if (start < new Date()) return;
      if (picked.some(s => s.start < end && s.end > start)) return;
      picked.push({start, end});
      draw();
    }

    function insert() {
      if (!target || !picked.length) return;
      const tz = zone();
      const html = 'Would any of these times work for you?<ul>'
        + picked.map(s => `<li>${esc(line(s))}${tz ? ' ' + esc(tz) : ''}</li>`).join('') + '</ul><br>';
      target.insertHtml(html, true);
      picked = [];
      close();
    }

    function open(ed, calendarUrl) {
      if (!panel) build();
      target = ed;
      url = calendarUrl;
      if (!weekStart) weekStart = monday(new Date());
      panel.hidden = false;
      document.body.classList.add('times-open');
      load().then(() => {
        const wrap = panel.querySelector('.times-grid-wrap');
        wrap.scrollTop = Math.max(0, (Math.min(Math.max(new Date().getHours(), 9), LAST - 4) - FIRST - 1) * HOUR_PX);
      });
    }
    function close() {
      if (!panel) return;
      panel.hidden = true;
      document.body.classList.remove('times-open');
    }
    return {open, close};
  })();

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
    if (form.dataset.calendarUrl) {
      bar.insertAdjacentHTML('beforeend', '<span class="compose-tool-gap"></span><button type="button" class="compose-tool compose-times" data-times title="Offer meeting times from your calendar">📅 Times</button>');
      bar.querySelector('[data-times]').addEventListener('click', () => times.open(api, form.dataset.calendarUrl));
    }

    // Where the cursor was, so Times can put the slots there after you've clicked around the panel.
    let caret = null;
    const remember = () => {
      const sel = window.getSelection();
      if (sel.rangeCount && area.contains(sel.anchorNode)) caret = sel.getRangeAt(0).cloneRange();
    };
    area.addEventListener('keyup', remember);
    area.addEventListener('mouseup', remember);
    area.addEventListener('input', remember);

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
    const api = {
      area, sync,
      insertHtml(markup, ownLine) {
        area.focus();
        const sel = window.getSelection();
        sel.removeAllRanges();
        if (caret && area.contains(caret.startContainer)) sel.addRange(caret);
        else { const r = document.createRange(); r.selectNodeContents(area); r.collapse(false); sel.addRange(r); }
        if (ownLine) {  // start on a fresh line, after a blank one, unless the cursor already sits at the start of a line
          const at = sel.getRangeAt(0);
          const lineStart = at.startContainer.nodeType === 3 ? !at.startContainer.nodeValue.slice(0, at.startOffset).trim()
            && !(at.startContainer.previousSibling && at.startContainer.previousSibling.nodeName !== 'BR')
            : !(at.startContainer.childNodes[at.startOffset - 1]) || at.startContainer.childNodes[at.startOffset - 1].nodeName === 'BR';
          if (!lineStart && area.innerText.trim()) markup = '<br><br>' + markup;
        }
        document.execCommand('insertHTML', false, markup);
        sync();
        remember();
        area.dispatchEvent(new Event('input', {bubbles: true}));
      },
      prepend(text) { area.innerHTML = textToHtml(text) + (area.innerText.trim() ? '<br><br>' + area.innerHTML : ''); sync(); },
      clear() { area.innerHTML = ''; sync(); },
    };
    return api;
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
