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
    ['underline', '<u>U</u>', 'Underline (Ctrl+U)'], ['strikeThrough', '<s>S</s>', 'Strikethrough'],
    ['foreColor', '<span class="compose-a">A</span>', 'Text colour'], ['hiliteColor', '<span class="compose-hl">H</span>', 'Highlight'], null,
    ['insertUnorderedList', '•&thinsp;≡', 'Bulleted list'], ['insertOrderedList', '1.&thinsp;≡', 'Numbered list'],
    ['outdent', '⇤', 'Indent less'], ['indent', '⇥', 'Indent more'], null,
    ['createLink', '🔗', 'Link (Ctrl+K)'], ['removeFormat', 'T̸', 'Clear formatting'],
  ];


  // Times: your calendar for a week beside the email. Click free time to offer a slot; Insert puts the picked
  // slots into the email where the cursor was, in your time zone.
  const times = (function () {
    const FIRST = 7, LAST = 20, SNAP = 15, HEAD_PX = 38, MIN_HOUR = 56, WIDTH_KEY = 'times-panel-width';
    let HOUR_PX = MIN_HOUR;
    let panel = null, target = null, url = '', peopleUrl = '', availUrl = '', createUrl = '', weekStart = null, events = [], picked = [], length = 30, weekends = false, people = [];
    // In a box of the mail dock (dock.js) the box is too small to hold the panel, so it is drawn on the hub page around it.
    let host = window;
    try { if (window.top !== window && window.top.acmiDock) host = window.top; } catch (e) { /* not the hub page */ }
    const doc = host.document;
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
      panel = doc.createElement('aside');
      panel.className = 'times-panel';
      panel.setAttribute('aria-label', 'Pick meeting times');
      panel.hidden = true;
      panel.innerHTML = `
        <div class="times-resize" data-resize title="Drag to resize" aria-hidden="true"></div>
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
        <div class="times-people">
          <ul class="times-who" data-who></ul>
          <div class="times-add">
            <input type="text" data-add placeholder="Add a person or address" aria-label="Add a person" autocomplete="off">
            <ul class="times-found" data-found hidden></ul>
          </div>
        </div>
        <p class="muted lc-small times-hint">Green is free for everyone you added. Click a time to offer it.</p>
        <p class="mail-error" data-times-error hidden></p>
        <div class="times-grid-wrap"><div class="times-grid" data-grid></div></div>
        <div class="times-foot">
          <ul class="times-picked" data-picked></ul>
          <div class="times-actions">
            <button type="button" class="btn-primary" data-insert disabled>Insert into email</button>
            <button type="button" class="btn btn-small" data-invite disabled>Send invite</button>
            <select data-video aria-label="Video">
              <option value="meet">Google Meet</option>
              <option value="teams">Microsoft Teams</option>
              <option value="">No video</option>
            </select>
            <button type="button" class="btn btn-small" data-clear>Clear</button>
          </div>
        </div>`;
      doc.body.append(panel);
      try { const w = +localStorage.getItem(WIDTH_KEY); if (w) setWidth(w); } catch (e) { /* private window */ }
      // Drag the left edge to make the panel wider or narrower; remembered in this browser.
      panel.querySelector('[data-resize]').addEventListener('pointerdown', e => {
        e.preventDefault();
        const right = panel.getBoundingClientRect().right;  // not the window's edge when a split view is open (split.js)
        const move = ev => setWidth(right - ev.clientX);
        const up = () => {
          host.removeEventListener('pointermove', move);
          host.removeEventListener('pointerup', up);
          doc.body.classList.remove('times-resizing');
          try { localStorage.setItem(WIDTH_KEY, String(panel.offsetWidth)); } catch (err) {}
          fit();
        };
        doc.body.classList.add('times-resizing');
        host.addEventListener('pointermove', move);
        host.addEventListener('pointerup', up);
      });
      host.addEventListener('resize', () => { if (!panel.hidden) fit(); });
      panel.querySelector('[data-zone]').textContent = zone();
      panel.querySelector('[data-close]').addEventListener('click', close);
      panel.querySelectorAll('[data-week]').forEach(b => b.addEventListener('click', () => {
        weekStart = addDays(weekStart, 7 * +b.dataset.week);
        load();
      }));
      panel.querySelector('[data-length]').addEventListener('change', e => { length = +e.target.value; load(); });
      panel.querySelector('[data-weekends]').addEventListener('change', e => { weekends = e.target.checked; draw(); });
      panel.querySelector('[data-clear]').addEventListener('click', () => { picked = []; draw(); });
      panel.querySelector('[data-insert]').addEventListener('click', insert);
      panel.querySelector('[data-invite]').addEventListener('click', invite);
      panel.querySelector('[data-grid]').addEventListener('click', pick);
      const addBox = panel.querySelector('[data-add]'), found = panel.querySelector('[data-found]');
      let searchTimer = 0;
      addBox.addEventListener('input', () => {
        clearTimeout(searchTimer);
        const q = addBox.value.trim();
        if (q.length < 2 || !peopleUrl) { found.hidden = true; return; }
        searchTimer = setTimeout(async () => {
          try {
            const r = await fetch(peopleUrl + '?' + new URLSearchParams({q}), {credentials: 'same-origin'});
            if (!r.ok || addBox.value.trim() !== q) return;
            const results = (await r.json()).results || [];
            found.innerHTML = results.map(p => `<li><button type="button" data-email="${esc(p.email)}" data-name="${esc(p.name)}"><strong>${esc(p.name)}</strong> <span class="muted lc-small">${esc(p.email)}</span></button></li>`).join('');
            found.hidden = !results.length;
          } catch (e) { found.hidden = true; }
        }, 200);
      });
      addBox.addEventListener('keydown', e => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addBox.value.trim())) addPerson(addBox.value.trim(), '');
      });
      found.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (b) addPerson(b.dataset.email, b.dataset.name);
      });
      panel.querySelector('[data-who]').addEventListener('click', e => {
        const x = e.target.closest('[data-drop-person]');
        if (!x) return;
        people = people.filter(p => p.email !== x.dataset.dropPerson);
        drawWho();
        load();
      });
      doc.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) close(); });
      // The box that opened the panel is gone (sent, closed, or the page changed): the panel goes with it.
      if (host !== window) window.addEventListener('pagehide', () => { panel.remove(); doc.body.classList.remove('times-open'); });
    }

    function setWidth(px) {
      const w = Math.round(Math.max(380, Math.min(px, host.innerWidth - 320, 1100)));
      doc.documentElement.style.setProperty('--times-w', w + 'px');
    }
    // The hours stretch to fill the panel's height, so the whole day shows without scrolling when there's room.
    function fit() {
      const wrap = panel.querySelector('.times-grid-wrap');
      const head = parseInt(panel.querySelector('[data-grid]').style.getPropertyValue('--head'), 10) || HEAD_PX;
      const hour = Math.max(MIN_HOUR, Math.floor((wrap.clientHeight - head - 4) / (LAST - FIRST)));
      if (hour !== HOUR_PX) { HOUR_PX = hour; draw(); }
    }

    function addPerson(email, name) {
      email = (email || '').trim().toLowerCase();
      if (!email || people.some(p => p.email === email)) return;
      people.push({email, name: name || email});
      panel.querySelector('[data-add]').value = '';
      panel.querySelector('[data-found]').hidden = true;
      drawWho();
      load();
    }
    function drawWho() {
      const list = panel.querySelector('[data-who]');
      list.innerHTML = people.map(p => `<li class="times-chip">${esc(p.name)} <button type="button" class="btn-link" data-drop-person="${esc(p.email)}" aria-label="Remove ${esc(p.name)}">×</button></li>`).join('')
        || '<li class="muted lc-small">Add people to see when they are free.</li>';
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
        if (availUrl && people.length) {
          const ar = await fetch(availUrl + '?' + new URLSearchParams({
            emails: people.map(p => p.email).join(','), start: iso(weekStart), days: weekends ? '7' : '5', minutes: String(length),
          }), {credentials: 'same-origin'});
          const ad = await ar.json();
          if (ar.ok) panel._avail = ad;
        } else panel._avail = null;
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
      grid.style.setProperty('--hour', HOUR_PX + 'px');
      let html = '<div class="times-hours">';
      for (let h = FIRST; h < LAST; h++) html += `<span style="top:${(h - FIRST) * HOUR_PX}px">${h % 12 || 12}${h < 12 ? 'a' : 'p'}</span>`;
      html += '</div>';
      const allDayRows = Math.max(0, ...list.map(d => events.filter(e => e.allDay && e.start < addDays(d, 1) && e.end > d).length));
      grid.style.setProperty('--head', (HEAD_PX + allDayRows * 14) + 'px');
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
          if (b > a) html += `<div class="times-busy${b - a < 22 ? ' short' : ''}${b - a < 40 ? ' brief' : ''}" style="top:${a}px;height:${Math.max(b - a, 12)}px" title="${esc(e.title)} · ${esc(span(e))}"><span class="times-busy-time">${esc(span(e))}</span> ${esc(e.title)}</div>`;
        });
        const avail = panel._avail;
        if (avail) {
          const di = avail.days.findIndex(x => x.date === iso(d));
          if (di >= 0) avail.slots.forEach((slot, s) => {
            const c = avail.cells[di][s];
            if (!c.ok) return;
            const [hh, mm] = slot.time.split(':').map(Number);
            const y = ((hh + mm / 60) - FIRST) * HOUR_PX;
            const names = c.busy.map(i => avail.people[i].name).join(', ');
            html += `<div class="times-free${c.busy.length ? ' some' : ''}" style="top:${y}px;height:${HOUR_PX / 2}px" title="${c.busy.length ? 'Busy: ' + esc(names) : 'Everyone is free'}"></div>`;
          });
        }
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
      panel.querySelector('[data-invite]').disabled = !picked.length || !people.length;
    }

    async function invite() {
      if (!picked.length || !people.length || !createUrl) return;
      const video = panel.querySelector('[data-video]').value;
      const first = picked[0];
      const err = panel.querySelector('[data-times-error]');
      err.hidden = true;
      if (video === 'teams') first.teams = '';
      const body = new URLSearchParams({
        request: Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''),
        title: ((target && target.form && target.form.querySelector('[name=subject]')) || {}).value || 'Meeting',
        start: first.start.toISOString(),
        minutes: String(length),
        attendees: people.map(p => p.email).join(','),
        video,
        location: first.teams || '',
        csrfmiddlewaretoken: (doc.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '',
      });
      try {
        const r = await fetch(createUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Could not send the invite.');
        const line = `Invite sent: ${d.when || ''}${d.meet ? ' · ' + d.meet : ''}`;
        err.textContent = line;
        err.hidden = false;
        err.className = 'muted lc-small';
        if (target) target.insertHtml(`<p>${esc(line)}</p>`, true);
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
        err.className = 'mail-error';
      }
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

    function open(ed, calendarUrl, avail, peopleSearch, create) {
      if (!panel) build();
      target = ed;
      url = calendarUrl;
      availUrl = avail || '';
      peopleUrl = peopleSearch || '';
      createUrl = create || '';
      if (!weekStart) weekStart = monday(new Date());
      panel.hidden = false;
      doc.body.classList.add('times-open');
      drawWho();
      fit();
      load().then(() => {
        fit();
        const wrap = panel.querySelector('.times-grid-wrap');
        if (wrap.scrollHeight > wrap.clientHeight + 2) wrap.scrollTop = Math.max(0, (Math.min(Math.max(new Date().getHours(), 9), LAST - 4) - FIRST - 1) * HOUR_PX);
      });
    }
    function close() {
      if (!panel) return;
      panel.hidden = true;
      doc.body.classList.remove('times-open');
    }
    const api = {open(ed, calendarUrl, avail, peopleSearch, create) {  // one panel at a time on the page, whichever box asked
      try { if (host.acmiTimesOpen && host.acmiTimesOpen !== api) host.acmiTimesOpen.close(); } catch (e) { /* that box is gone */ }
      host.acmiTimesOpen = api;
      open(ed, calendarUrl, avail, peopleSearch, create);
    }, close};
    return api;
  })();

  // The colours offered for text and for highlighting; "" takes the colour off.
  const SWATCHES = {
    foreColor: [['', 'Default'], ['#1d2330', 'Black'], ['#6b7280', 'Grey'], ['#c0392b', 'Red'], ['#d35400', 'Orange'],
      ['#1e7a46', 'Green'], ['#1f5fbf', 'Blue'], ['#7b3fb5', 'Purple']],
    hiliteColor: [['', 'None'], ['#fff176', 'Yellow'], ['#b9f6ca', 'Green'], ['#b3e5fc', 'Blue'], ['#f8bbd0', 'Pink'],
      ['#ffe0b2', 'Orange'], ['#e0e0e0', 'Grey']],
  };

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
      bar.querySelector('[data-times]').addEventListener('click', () => times.open(api, form.dataset.calendarUrl, form.dataset.availUrl, form.dataset.peopleUrl, form.dataset.createUrl));
    }
    const propose = form.querySelector('[data-open-times]');
    if (propose) propose.addEventListener('click', () => times.open(api, form.dataset.calendarUrl, form.dataset.availUrl, form.dataset.peopleUrl, form.dataset.createUrl));

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
    // Text colour and highlight: a row of swatches under the button; the selection stays put while you pick.
    const pop = document.createElement('div');
    pop.className = 'compose-swatches';
    pop.hidden = true;
    bar.append(pop);
    let popFor = '';
    const swatches = cmd => {
      if (!pop.hidden && popFor === cmd) { pop.hidden = true; return; }
      popFor = cmd;
      pop.innerHTML = SWATCHES[cmd].map(([c, name]) =>
        `<button type="button" class="compose-swatch${c ? '' : ' none'}" data-color="${c}" title="${name}" aria-label="${name}" style="${c ? 'background:' + c : ''}"></button>`).join('');
      const b = bar.querySelector(`[data-cmd="${cmd}"]`);
      pop.style.left = b.offsetLeft + 'px';
      pop.hidden = false;
    };
    const colour = c => {
      area.focus();
      document.execCommand('styleWithCSS', false, true);  // inline styles, which every mail app shows
      if (popFor === 'hiliteColor') document.execCommand('hiliteColor', false, c || 'transparent');
      else document.execCommand('foreColor', false, c || '#1d2330');
      document.execCommand('styleWithCSS', false, false);
      pop.hidden = true;
      sync();
      area.dispatchEvent(new Event('input', {bubbles: true}));
    };
    pop.addEventListener('click', e => { const s = e.target.closest('[data-color]'); if (s) colour(s.dataset.color); });
    document.addEventListener('mousedown', e => { if (!pop.hidden && !bar.contains(e.target)) pop.hidden = true; });

    const run = cmd => {
      area.focus();
      if (cmd === 'foreColor' || cmd === 'hiliteColor') { swatches(cmd); return; }
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
    bar.addEventListener('mousedown', e => { if (e.target.closest('[data-cmd], [data-color]')) e.preventDefault(); });  // keep the selection
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

  function setup(form, kind) {
    // kind: false = the full compose page, true = the reading pane's reply box, 'window' = a box of the mail dock.
    const inline = !!kind, windowed = kind === 'window';
    const winId = windowed ? form.dataset.window : '';
    const dock = () => { try { return window.top !== window ? window.top.acmiDock : null; } catch (e) { return null; } };
    const state = form.querySelector('[data-draft-state]') || document.querySelector('[data-draft-state]');
    const draftId = form.querySelector('[data-draft-id]');
    const sendBtn = form.querySelector('[data-send]');
    const countdown = form.querySelector('[data-countdown]');
    const seconds = form.querySelector('[data-seconds]');
    const error = form.querySelector('[data-compose-error]');
    const body = form.querySelector('textarea[name=body]');
    try { document.execCommand('styleWithCSS', false, false); } catch (e) { /* older browsers */ }
    const ed = editor(form, body);
    let timer = null, saveTimer = null, snapTimer = null, confirmed = false, inflight = null, rev = 0, savedRev = 0;
    const say = text => { if (state) state.textContent = text; };
    const field = name => form.elements[name];
    const initial = {to: field('to').value, subject: field('subject').value};
    // Worth keeping as a draft: something typed beyond what the reply or the link filled in.
    const hasContent = () => !!(ed.area.innerText.trim() || field('subject').value !== initial.subject || field('to').value !== initial.to);

    // A box of the dock keeps what's typed for this tab, so it is still there on the next page (dock.js brings the box
    // back; this puts the text back, and the draft id, so autosave goes on updating the same draft in Gmail).
    const SNAP = 'hubDock:' + winId;
    const snapshot = () => {
      if (!windowed) return;
      try {
        sessionStorage.setItem(SNAP, JSON.stringify({
          to: field('to').value, cc: field('cc').value, bcc: field('bcc').value, subject: field('subject').value,
          html: ed.area.innerHTML, draft: draftId.value,
          off: [...form.querySelectorAll('[name=forward_part]:not(:checked)')].map(i => i.value),
          files: [...(field('files') ? field('files').files : [])].map(f => f.name),
        }));
      } catch (e) { /* storage full or blocked: the draft in Gmail is still there */ }
    };

    const save = () => {
      if (inflight) return inflight;
      if (confirmed || !form.dataset.draftUrl) return Promise.resolve(false);
      const at = rev, data = new FormData(form);
      data.delete('files');
      inflight = (async () => {
        try {
          const r = await fetch(form.dataset.draftUrl, {method: 'POST', body: data, headers: {'X-CSRFToken': csrf(form)}});
          const d = await r.json();
          if (d.draft) {
            draftId.value = d.draft;
            savedRev = at;
            say('Draft saved ' + new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}));
            snapshot();
            return true;
          }
          if (d.error) say(d.error);
        } catch (e) { say('Draft not saved (offline?)'); }
        return false;
      })().finally(() => {
        inflight = null;
        if (rev !== at && !confirmed) { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1500); }  // typed while it saved
      });
      return inflight;
    };
    form.addEventListener('input', e => {
      if (e.target.type === 'file') return;
      rev++;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 3000);
      if (windowed) { clearTimeout(snapTimer); snapTimer = setTimeout(snapshot, 250); }
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
        if (windowed && dock()) { dock().sent(winId, d); return; }  // the box closes and the page says "Message sent"
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

    if (windowed) {
      let s = null;
      try { s = JSON.parse(sessionStorage.getItem(SNAP) || 'null'); } catch (e) { /* none kept */ }
      if (s) {
        for (const n of ['to', 'cc', 'bcc', 'subject']) if (field(n) && typeof s[n] === 'string') field(n).value = s[n];
        ed.area.innerHTML = s.html || '';
        ed.sync();
        draftId.value = s.draft || '';
        (s.off || []).forEach(v => { const c = [...form.querySelectorAll('[name=forward_part]')].find(i => i.value === v); if (c) c.checked = false; });
        const more = form.querySelector('.compose-more');
        if (more && (s.cc || s.bcc)) more.open = true;
        if ((s.files || []).length) say('Attach again: ' + s.files.join(', '));  // files can't be kept across pages
        rev = 1;
      }
      const retitle = () => { if (dock()) dock().title(winId, field('subject').value.trim() || 'New message'); };
      field('subject').addEventListener('input', retitle);
      retitle();
      window.addEventListener('pagehide', snapshot);
      // Discard throws the draft away in Gmail and closes the box.
      const discard = form.querySelector('[data-discard]');
      if (discard) discard.addEventListener('click', async e => {
        e.preventDefault();
        if (hasContent() && !confirm('Discard this message?')) return;
        clearTimeout(saveTimer);
        confirmed = true;  // no more autosaves
        if (draftId.value) {
          const data = new FormData();
          data.set('draft_id', draftId.value);
          data.set('inline', '1');
          try { await fetch(discard.getAttribute('formaction'), {method: 'POST', body: data, headers: {'X-CSRFToken': csrf(form)}}); }
          catch (err) { /* it stays in Gmail's Drafts */ }
        }
        if (dock()) dock().close(winId, {force: true});
      });
      form.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !sendBtn.disabled) { e.preventDefault(); sendBtn.click(); }
      });
      // What the dock asks before it closes the box: is a send waiting, and keep what was typed as a draft in Gmail.
      window.acmiWindow = {
        sending: () => !!timer,
        flush: async () => {
          clearTimeout(saveTimer);
          if (inflight) await inflight;
          if (rev !== savedRev && hasContent()) await save();
          return !!draftId.value;  // true when a draft of it is in Gmail, so the page can say so
        },
      };
      setTimeout(() => { const to = field('to'); (to.value ? ed.area : to).focus(); }, 60);
    }
    return {save, ed};
  }

  document.querySelectorAll('[data-compose]').forEach(form => setup(form, form.dataset.window ? 'window' : false));

  // The reading pane's inline reply box: Reply / Reply all / Forward fill it in place, under the message. The inbox
  // calls initInlineReply again when it swaps in another conversation (and with it a new box).
  let box = null, inlineEd = null;
  function initInline() {
    box = document.querySelector('[data-inline]');
    if (!box || box.dataset.ready) return;
    box.dataset.ready = '1';
    inlineEd = setup(box, true).ed;
    box.querySelector('[data-inline-close]').addEventListener('click', () => { box.hidden = true; });
    // Esc closes the box (an empty one, or after confirming); Ctrl/Cmd+Enter sends.
    box.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !e.target.closest('.addr-suggest')) {
        if (!inlineEd.area.textContent.trim() || confirm('Close the reply? Your draft stays saved in Gmail.')) box.hidden = true;
      } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        box.querySelector('[data-send]').click();
      }
    });
  }
  window.initInlineReply = initInline;
  initInline();
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const titles = {reply: 'Reply', all: 'Reply all', forward: 'Forward'};
  async function open(messageId, mode, from) {
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
    const end = from && from.closest('[data-reply-end]');  // from the bar under the thread: open right there
    (end || msg || document.querySelector('[data-reader] .mail-reader-head')).after(box);
    box.hidden = false;
    const focus = mode === 'forward' ? box.querySelector('[name=to]') : inlineEd.area;
    box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    setTimeout(() => focus.focus(), 250);
  }
  document.addEventListener('click', e => {
    const a = e.target.closest('[data-reply]');
    if (!a || !box || !box.isConnected) return;
    e.preventDefault();
    open(a.dataset.reply, a.dataset.mode || 'reply', a);
  });
})();
