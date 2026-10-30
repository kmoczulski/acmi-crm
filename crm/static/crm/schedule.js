// The Schedule page (docs/SCHEDULING.md): pick who, see when they are free in a week grid, click a time, and either
// have Google Calendar send the invitations (if the person allowed it) or open Google's own new-event screen filled in.
// The server does the arithmetic (crm/schedule/slots.py); this draws what it says.
(function () {
  'use strict';
  const root = document.querySelector('[data-sched]');
  const configEl = document.getElementById('sched-config');
  if (!root || !configEl) return;
  const cfg = JSON.parse(configEl.textContent);
  const $ = (sel, scope) => (scope || root).querySelector(sel);
  const csrf = () => (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  const LENGTHS = {15: '15 minutes', 30: '30 minutes', 45: '45 minutes', 60: '1 hour', 90: '1½ hours', 120: '2 hours'};

  function h(tag, attrs, ...kids) {  // text only: nothing from Google or the CRM is ever written as HTML
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === false || v == null) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v === true ? '' : v);
    });
    kids.flat().forEach(kid => { if (kid != null) node.append(kid); });
    return node;
  }

  const team = cfg.team.filter(t => !t.me);
  const known = new Map(cfg.team.map(t => [t.email, t.name]));
  cfg.invited.forEach(p => { if (!known.has(p.email)) known.set(p.email, p.name); });
  const initialsOf = name => {
    const parts = (name || '').split(/[\s.@]+/).filter(Boolean);
    return ((parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || '?').slice(0, 2))).toUpperCase();
  };

  const state = {
    invited: cfg.invited.map(p => p.email),  // everyone but you, in the order added
    minutes: cfg.minutes, hours: cfg.hours.join('-'), weekend: false, start: cfg.start || cfg.today,
    data: null, pick: null, token: 0,
  };

  // ---- Who ---------------------------------------------------------------------------------------------------
  const whoList = $('[data-who]'), found = $('[data-found]'), addBox = $('[data-add]'), contacts = $('[data-contacts]');

  function drawWho() {
    const problems = new Map(((state.data && state.data.people) || []).map(p => [p.email, p.problem]));
    whoList.replaceChildren(h('li', {class: 'sched-person is-me'}, h('span', {class: 'sched-avatar', text: initialsOf(known.get(cfg.me) || cfg.me)}),
      h('span', {text: (known.get(cfg.me) || 'You') + ' (you)'})));
    const rows = [...team.map(t => t.email), ...state.invited.filter(e => !team.some(t => t.email === e))];
    rows.forEach(email => {
      const on = state.invited.includes(email), name = known.get(email) || email, problem = problems.get(email);
      const box = h('input', {type: 'checkbox', checked: on, 'aria-label': 'Include ' + name});
      box.addEventListener('change', () => toggle(email, box.checked));
      whoList.append(h('li', {class: 'sched-person' + (on ? ' on' : '')},
        h('label', null, box, h('span', {class: 'sched-avatar', text: initialsOf(name)}),
          h('span', {class: 'sched-who-text'}, h('span', {class: 'sched-name', text: name}),
            !team.some(t => t.email === email) && name !== email ? h('span', {class: 'sched-email muted lc-small', text: email}) : null)),
        on && problem ? h('span', {class: 'sched-warn', title: problem, text: '⚠ can’t see their calendar'}) : null));
    });
    drawInviting();
  }

  function toggle(email, on) {
    state.invited = state.invited.filter(e => e !== email);
    if (on) state.invited.push(email);
    drawWho();
    load();
  }

  function addPerson(email, name) {
    email = email.trim().toLowerCase();
    if (!email || email === cfg.me) return;
    if (name) known.set(email, name);
    if (!state.invited.includes(email)) state.invited.push(email);
    addBox.value = '';
    found.hidden = true;
    drawWho();
    drawContacts();
    load();
  }

  let searchTimer = 0;
  addBox.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = addBox.value.trim();
    if (q.length < 2) { found.hidden = true; return; }
    searchTimer = setTimeout(async () => {
      try {
        const r = await fetch(cfg.urls.people + '?' + new URLSearchParams({q}), {credentials: 'same-origin'});
        if (!r.ok || addBox.value.trim() !== q) return;
        const results = (await r.json()).results || [];
        found.replaceChildren(...results.map(p => {
          const b = h('button', {type: 'button', class: 'sched-hit'}, h('strong', {text: p.name}), ' ',
            h('span', {class: 'muted lc-small', text: p.detail ? p.email + ' · ' + p.detail : p.email}));
          b.addEventListener('click', () => addPerson(p.email, p.name === p.email ? '' : p.name));
          return h('li', null, b);
        }));
        found.hidden = !results.length;
      } catch (e) { /* offline: type the address and press Enter */ }
    }, 200);
  });
  addBox.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addBox.value.trim())) addPerson(addBox.value);
  });
  document.addEventListener('click', e => { if (!found.contains(e.target) && e.target !== addBox) found.hidden = true; });

  function drawContacts() {
    const left = cfg.contacts.filter(c => !state.invited.includes(c.email) && c.email !== cfg.me);
    contacts.hidden = !left.length;
    if (!left.length) return;
    contacts.replaceChildren(h('span', {class: 'muted lc-small', text: 'From ' + (cfg.company || 'the company') + ': '}),
      ...left.map(c => {
        const b = h('button', {type: 'button', class: 'btn btn-small', title: c.title || c.email, text: '+ ' + c.name});
        b.addEventListener('click', () => addPerson(c.email, c.name));
        return b;
      }));
  }

  // ---- Length, hours, week ---------------------------------------------------------------------------------------
  const minutesSel = $('[data-minutes]'), formMinutes = $('[data-form-minutes]');
  [minutesSel, formMinutes].forEach(sel => sel.append(...cfg.durations.map(m => h('option', {value: m, text: LENGTHS[m] || m + ' minutes', selected: m === state.minutes}))));
  const setMinutes = value => {
    state.minutes = Number(value);
    minutesSel.value = formMinutes.value = String(state.minutes);
    load();
  };
  minutesSel.addEventListener('change', () => setMinutes(minutesSel.value));
  formMinutes.addEventListener('change', () => setMinutes(formMinutes.value));
  $('[data-hours]').addEventListener('change', e => { state.hours = e.target.value; state.pick = null; hideForm(); load(); });
  $('[data-weekend]').addEventListener('change', e => { state.weekend = e.target.checked; load(); });
  $('[data-prev]').addEventListener('click', () => { if (state.data) { state.start = state.data.previous; state.pick = null; hideForm(); load(); } });
  $('[data-next]').addEventListener('click', () => { if (state.data) { state.start = state.data.next; state.pick = null; hideForm(); load(); } });
  $('[data-today]').addEventListener('click', () => { state.start = cfg.today; state.pick = null; hideForm(); load(); });

  // ---- Asking the server ---------------------------------------------------------------------------------------
  const errorBox = $('[data-error]'), gridBox = $('[data-grid]');
  let loadTimer = 0;
  function load() {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(fetchWeek, 150);
  }

  async function fetchWeek() {
    const token = ++state.token, [from, to] = state.hours.split('-');
    gridBox.classList.add('is-loading');
    try {
      const r = await fetch(cfg.urls.availability + '?' + new URLSearchParams({
        emails: state.invited.join(','), start: state.start, days: state.weekend ? '7' : '5', from, to, minutes: state.minutes,
      }), {credentials: 'same-origin'});
      const data = await r.json().catch(() => ({}));
      if (token !== state.token) return;  // a newer question was asked meanwhile
      if (!r.ok) throw new Error(data.error || 'Couldn’t load the calendar.');
      errorBox.hidden = true;
      state.data = data;
      state.start = data.start;
      data.people.forEach(p => { if (!known.has(p.email)) known.set(p.email, p.name); });
      drawAll();
      if (!form.hidden) syncPickFromForm(true);  // a typed day on another week: show that week's cell
    } catch (e) {
      if (token !== state.token) return;
      errorBox.textContent = e.message || 'Couldn’t load the calendar.';
      errorBox.hidden = false;
    } finally {
      if (token === state.token) gridBox.classList.remove('is-loading');
    }
  }

  // ---- The grid ------------------------------------------------------------------------------------------------
  const need = () => Math.max(1, Math.ceil(state.minutes / 30));
  const counted = () => state.data.people.filter(p => !p.problem).length;

  function drawAll() {
    const d = state.data;
    $('[data-range]').textContent = d.range;
    $('[data-zone]').textContent = d.zone || 'Central';
    drawWho();
    drawContacts();
    drawGrid();
    drawSuggestions();
    if (state.pick) markPick();
  }

  function cellState(day, slot) {
    const c = state.data.cells[day][slot], total = counted();
    if (!c.ok) return 'sc-off';
    if (!c.busy.length) return 'sc-free';
    return c.busy.length >= total ? 'sc-all' : 'sc-some';
  }

  function drawGrid() {
    const d = state.data;
    const grid = h('div', {class: 'sc-grid', style: '--days:' + d.days.length});
    grid.append(h('div', {class: 'sc-corner'}),
      ...d.days.map(day => h('div', {class: 'sc-day' + (day.today ? ' is-today' : '') + (day.past ? ' is-past' : '')},
        h('strong', {text: day.short}), h('span', {text: day.num}))));
    d.slots.forEach((slot, s) => {
      grid.append(h('div', {class: 'sc-time' + (slot.hour ? ' is-hour' : ''), text: slot.hour ? slot.label : ''}));
      d.days.forEach((day, di) => {
        const c = d.cells[di][s], cls = cellState(di, s);
        const names = c.busy.map(i => d.people[i].name);
        const label = day.label + ', ' + slot.label + (c.ok ? (names.length ? ', busy: ' + names.join(', ') : ', everyone is free') : ', not available');
        grid.append(h('button', {type: 'button', class: 'sc-cell ' + cls + (slot.hour ? ' is-hour' : ''), 'data-d': di, 'data-s': s,
          title: label, 'aria-label': label, disabled: !c.ok}, c.ok && c.busy.length ? String(c.busy.length) : null));
      });
    });
    gridBox.replaceChildren(grid);
  }

  const cellAt = (di, s) => gridBox.querySelector('.sc-cell[data-d="' + di + '"][data-s="' + s + '"]');
  function span(di, s, cls, on) {
    for (let i = 0; i < need(); i++) { const c = cellAt(di, s + i); if (c) c.classList.toggle(cls, on); }
  }
  gridBox.addEventListener('mouseover', e => {
    const c = e.target.closest('.sc-cell:not(:disabled)');
    gridBox.querySelectorAll('.sc-hover').forEach(x => x.classList.remove('sc-hover'));
    if (c) span(+c.dataset.d, +c.dataset.s, 'sc-hover', true);
  });
  gridBox.addEventListener('mouseleave', () => gridBox.querySelectorAll('.sc-hover').forEach(x => x.classList.remove('sc-hover')));
  gridBox.addEventListener('click', e => {
    const c = e.target.closest('.sc-cell:not(:disabled)');
    if (c) choose(+c.dataset.d, +c.dataset.s);
  });

  function markPick() {
    gridBox.querySelectorAll('.sc-pick, .sc-pick-start').forEach(x => x.classList.remove('sc-pick', 'sc-pick-start'));
    if (!state.pick) return;
    span(state.pick.d, state.pick.s, 'sc-pick', true);
    const first = cellAt(state.pick.d, state.pick.s);
    if (first) first.classList.add('sc-pick-start');
  }

  function drawSuggestions() {
    const list = $('[data-suggest]'), d = state.data;
    list.replaceChildren(...d.suggestions.map(s => {
      const b = h('button', {type: 'button', class: 'btn-link', text: s.label});
      b.addEventListener('click', () => {
        const di = d.days.findIndex(x => x.date === s.day), si = d.slots.findIndex(x => x.time === s.time);
        if (di >= 0 && si >= 0) choose(di, si);
      });
      return h('li', null, b);
    }));
    $('[data-suggest-none]').hidden = d.suggestions.length > 0;
  }

  // ---- The form --------------------------------------------------------------------------------------------------
  const form = $('[data-form]'), doneBox = $('[data-done]'), titleBox = $('[data-title]');
  const dayBox = $('[data-day]'), timeBox = $('[data-time]'), formError = $('[data-form-error]'), conflict = $('[data-conflict]');
  titleBox.value = cfg.title || '';

  function drawInviting() {
    const names = state.invited.map(e => known.get(e) || e);
    $('[data-inviting]').textContent = names.length ? names.join(', ') : 'nobody yet: tick people on the left';
  }

  function choose(di, s) {
    const d = state.data;
    state.pick = {d: di, s};
    dayBox.value = d.days[di].date;
    timeBox.value = d.slots[s].time;
    formMinutes.value = String(state.minutes);
    doneBox.hidden = true;
    form.hidden = false;
    markPick();
    showConflict();
    form.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    (titleBox.value ? timeBox : titleBox).focus({preventScroll: true});
  }

  function showConflict() {
    const d = state.data, p = state.pick;
    conflict.hidden = !p;
    if (!p) return;
    const busy = d.cells[p.d][p.s].busy.map(i => d.people[i].name);
    conflict.className = 'sched-note lc-small ' + (busy.length ? 'is-busy' : 'is-free');
    conflict.textContent = busy.length ? '⚠ Busy then: ' + busy.join(', ') + '. You can still send it.' : '✓ Everyone you can see is free then.';
  }

  const inThisWeek = day => {  // is this YYYY-MM-DD one of the seven days the server sent a week for?
    const first = Date.parse(state.data.start + 'T12:00:00'), at = Date.parse(day + 'T12:00:00');
    return at >= first && at < first + 7 * 864e5;
  };
  function syncPickFromForm(afterLoad) {  // the day or time was typed: find that cell if it is on this week's grid
    const d = state.data;
    if (!d) return;
    const di = d.days.findIndex(x => x.date === dayBox.value), si = d.slots.findIndex(x => x.time === timeBox.value);
    state.pick = di >= 0 && si >= 0 ? {d: di, s: si} : null;
    markPick();
    showConflict();
    if (di < 0 && dayBox.value && afterLoad !== true && !inThisWeek(dayBox.value)) { state.start = dayBox.value; load(); }
  }
  dayBox.addEventListener('change', () => syncPickFromForm());
  timeBox.addEventListener('change', () => syncPickFromForm());

  function hideForm() { form.hidden = true; markPick(); }
  $('[data-cancel]').addEventListener('click', () => { state.pick = null; hideForm(); });

  function values() {
    const data = new FormData(form);
    return {
      title: (data.get('title') || '').toString(), start: dayBox.value + 'T' + timeBox.value, minutes: formMinutes.value,
      attendees: state.invited.join(','), location: (data.get('location') || '').toString(),
      description: (data.get('description') || '').toString(), meet: data.get('meet') ? '1' : '',
    };
  }

  const openLink = $('[data-open]');
  openLink.addEventListener('click', e => {  // fill in the address at the moment of the click
    const v = values();
    if (!v.title.trim() || !dayBox.value || !timeBox.value) { e.preventDefault(); showFormError('Give it a title, a day and a time first.'); return; }
    openLink.href = cfg.urls.open + '?' + new URLSearchParams(v);
  });

  function newRequestId() {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  }

  function showFormError(text) { formError.textContent = text; formError.hidden = !text; }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!cfg.canSchedule) return;
    const send = $('[data-send]');
    showFormError('');
    send.disabled = true;
    send.textContent = 'Sending…';
    try {
      const body = new URLSearchParams({...values(), request: $('[data-request]').value});
      const r = await fetch(cfg.urls.create, {method: 'POST', credentials: 'same-origin', body,
        headers: {'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest'}});
      const data = await r.json().catch(() => ({}));
      if (data.needs_permission) {  // Google says the hub may not write to this calendar (yet, or any more)
        cfg.canSchedule = false;
        const banner = document.querySelector('[data-allow]');  // above the page's columns, so not inside root
        if (banner) { banner.hidden = false; banner.scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
        send.hidden = true;
      }
      if (!r.ok) throw new Error(data.error || 'Couldn’t schedule that.');
      finished(data);
    } catch (err) {
      showFormError(err.message);
    } finally {
      send.disabled = false;
      send.textContent = 'Send invitations';
    }
  });

  function finished(result) {
    const who = result.invited.map(e => known.get(e) || e);
    const links = [];
    if (result.meet) links.push(h('a', {class: 'btn btn-small', href: result.meet, target: '_blank', rel: 'noopener noreferrer', text: 'Meet link ↗'}));
    if (result.link) links.push(h('a', {class: 'btn btn-small', href: result.link, target: '_blank', rel: 'noopener noreferrer', text: 'Open in Google Calendar ↗'}));
    if (result.meeting) links.push(h('a', {class: 'btn btn-small', href: result.meeting, text: 'See it on the hub calendar'}));
    const again = h('button', {type: 'button', class: 'btn btn-small', text: 'Schedule another'});
    again.addEventListener('click', () => {
      form.reset();
      $('[data-request]').value = newRequestId();  // a new meeting is a new request: the old id would find the old meeting
      titleBox.value = cfg.title || '';
      formMinutes.value = String(state.minutes);
      doneBox.hidden = true;
      state.pick = null;
      markPick();
      load();
    });
    doneBox.replaceChildren(
      h('h3', {text: result.created ? 'Invitations sent' : 'That meeting was already scheduled'}),
      h('p', null, h('strong', {text: result.title}), ' · ' + result.when),
      h('p', {class: 'muted', text: 'Invited: ' + who.join(', ') + (result.meet ? '' : '. A Meet link, if you asked for one, is in the invitation.')}),
      h('div', {class: 'sched-actions'}, ...links, again));
    form.hidden = true;
    doneBox.hidden = false;
    state.pick = null;
    markPick();
    doneBox.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    load();  // the new meeting now shows as busy
  }

  drawWho();
  drawContacts();
  fetchWeek();
})();
