// Chat in the hub (crm/chat/): the Chat page, the list in the dock's Chat box, and a conversation, which works the same on
// the page, in the split view's side and in a box of the dock. Everything is small JSON calls to /chat/... (crm/chat/views.py).
(function () {
  const enc = encodeURIComponent;
  const dock = () => { try { return window.top !== window ? window.top.acmiDock : null; } catch (e) { return null; } };

  async function call(url, opts) {
    const o = Object.assign({credentials: 'same-origin'}, opts);
    const r = await fetch(url, o);
    let d = {};
    try { d = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(d.error || 'Chat didn’t answer. Try again.');
    return d;
  }

  // One conversation: its messages, the box to write in, live updates, earlier messages, alerts on / off.
  function initConvo(root) {
    if (!root) return null;
    if (root._chat) root._chat.stop();  // the page swapped to another conversation: the old one's timer ends
    const q = sel => root.querySelector(sel);
    const scroll = q('[data-scroll]'), list = q('[data-list]'), form = q('[data-compose-form]'), text = q('[data-text]');
    const sendBtn = q('[data-send]'), errBox = q('[data-chat-error]'), none = q('[data-none]');
    const older = q('[data-older]'), fresh = q('[data-new]'), note = q('[data-thread-note]'), mute = q('[data-mute]');
    const space = root.dataset.space;
    const state = {latest: root.dataset.latest || '', earliest: root.dataset.earliest || '', thread: '', busy: false,
                   stopped: false, timer: null, unseen: false};
    const token = () => (form.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
    const post = (url, fields) => call(url, {method: 'POST', headers: {'X-CSRFToken': token()},
      body: new URLSearchParams(Object.assign({s: space}, fields || {}))});
    // In a box of the dock that is folded the frame has no size; a hidden tab counts as not shown too.
    const shown = () => !document.hidden && (!window.frameElement || window.frameElement.getClientRects().length > 0);
    const nearBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
    const toBottom = () => { scroll.scrollTop = scroll.scrollHeight; };
    const msgs = () => list.querySelectorAll('.chat-msg');
    const last = () => { const all = msgs(); return all.length ? all[all.length - 1] : null; };
    const context = () => {  // the message above new ones, so grouping carries on
      const m = last();
      return m ? {prev_uid: m.dataset.uid, prev_time: m.dataset.iso, prev_reply: m.classList.contains('in-thread') ? '1' : ''} : {};
    };
    const showError = msg => { errBox.textContent = msg; errBox.hidden = false; };
    const grow = () => { text.style.height = 'auto'; text.style.height = Math.min(text.scrollHeight, 140) + 'px'; };

    // Add messages the page doesn't have yet (a poll can return the one you just sent).
    function append(html) {
      const tpl = document.createElement('template');
      tpl.innerHTML = html;
      const have = new Set([...msgs()].map(n => n.dataset.id));
      let added = 0, day = null;
      for (const n of [...tpl.content.children]) {
        if (n.classList.contains('chat-day')) { day = n; continue; }
        if (!n.classList.contains('chat-msg')) continue;
        if (have.has(n.dataset.id)) { day = null; continue; }
        if (day) { list.append(day); day = null; }
        list.append(n);
        have.add(n.dataset.id);
        added++;
      }
      if (added) none.hidden = true;
      return added;
    }

    let reading = false;
    async function markRead() {
      if (reading) return;
      reading = true;
      try { await post(root.dataset.readUrl); state.unseen = false; } catch (e) { /* the list catches up later */ }
      reading = false;
    }

    async function tick() {
      if (state.stopped || !root.isConnected) return;  // swapped out of the page: nothing here is read any more
      let delay = shown() ? 6000 : 30000;
      if (!state.busy) {
        try {
          const d = await call(`${root.dataset.messagesUrl}?${new URLSearchParams(Object.assign({s: space, after: state.latest}, context()))}`);
          if (d.count) {
            const stick = nearBottom();
            if (append(d.html)) {
              if (stick) toBottom(); else fresh.hidden = false;
              if (d.from_others) { if (shown()) markRead(); else state.unseen = true; }
            }
            state.latest = d.latest || state.latest;
            if (d.more) delay = 300;  // a long catch-up comes in pieces
          }
        } catch (e) { /* offline, or Chat is busy: try again next time */ }
      }
      state.timer = setTimeout(tick, delay);
    }

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const body = text.value.trim();
      if (!body || state.busy) return;
      state.busy = true;
      sendBtn.disabled = true;
      errBox.hidden = true;
      try {
        const d = await post(root.dataset.sendUrl, Object.assign({text: body, thread: state.thread}, context()));
        append(d.html);
        text.value = '';
        grow();
        clearThread();
        toBottom();
      } catch (err) { showError(err.message); }
      state.busy = false;
      sendBtn.disabled = false;
      text.focus();
    });
    text.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    text.addEventListener('input', grow);

    function clearThread() { state.thread = ''; note.hidden = true; }
    root.addEventListener('click', e => {
      const b = e.target.closest('[data-reply-thread]');
      if (b) { state.thread = b.dataset.replyThread; note.hidden = false; text.focus(); }
      if (e.target.closest('[data-thread-clear]')) clearThread();
    });

    if (older) older.addEventListener('click', async () => {
      older.disabled = true;
      try {
        const d = await call(`${root.dataset.messagesUrl}?${new URLSearchParams({s: space, before: state.earliest})}`);
        const before = scroll.scrollHeight;
        list.insertAdjacentHTML('afterbegin', d.html);
        scroll.scrollTop += scroll.scrollHeight - before;  // what you were reading stays where it was
        state.earliest = d.earliest || state.earliest;
        if (!d.more) older.hidden = true;
      } catch (err) { showError(err.message); }
      older.disabled = false;
    });

    mute.addEventListener('click', async () => {
      try { mute.textContent = (await post(root.dataset.muteUrl)).muted ? '🔕 Alerts off' : '🔔 Alerts on'; }
      catch (err) { showError(err.message); }
    });
    fresh.addEventListener('click', () => { toBottom(); fresh.hidden = true; });
    scroll.addEventListener('scroll', () => { if (nearBottom()) fresh.hidden = true; }, {passive: true});
    const onVisible = () => { if (state.unseen && shown()) markRead(); };
    document.addEventListener('visibilitychange', onVisible);

    requestAnimationFrame(toBottom);
    setTimeout(toBottom, 150);
    if (shown() && !('ontouchstart' in window)) setTimeout(() => text.focus(), 60);
    state.timer = setTimeout(tick, 6000);
    root._chat = {stop() { state.stopped = true; clearTimeout(state.timer); document.removeEventListener('visibilitychange', onVisible); }};
    return root._chat;
  }

  // The list of conversations: a search box, rows that open a conversation, and a refresh every 20 seconds.
  function initRows(box, onOpen, current) {
    const search = document.querySelector('[data-chat-search]');
    const url = box.dataset.rowsUrl || (document.querySelector('[data-chat-app]') || {dataset: {}}).dataset.rowsUrl;
    const apply = () => {
      const t = ((search && search.value) || '').trim().toLowerCase();
      box.querySelectorAll('.chat-row').forEach(r => { r.hidden = !!t && !(r.dataset.find || '').includes(t); });
    };
    if (search) search.addEventListener('input', apply);
    box.addEventListener('click', e => {
      const a = e.target.closest('.chat-row');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      onOpen(a.dataset.space, a.dataset.title);
    });
    setInterval(async () => {
      if (document.hidden || !url) return;
      if (window.frameElement && !window.frameElement.getClientRects().length) return;  // the dock's Chat box is folded
      try {
        const d = await call(`${url}?current=${enc(current())}`);
        box.innerHTML = d.html;
        apply();
      } catch (e) { /* the next refresh tries again */ }
    }, 20000);
    return {apply};
  }

  // The Chat page (also the split view's side): the list on the left, the open conversation on the right.
  const app = document.querySelector('[data-chat-app]');
  if (app) {
    const pane = app.querySelector('[data-pane]'), box = app.querySelector('[data-rows]'), base = app.dataset.chatUrl;
    let current = (pane.querySelector('[data-chat-convo]') || {dataset: {}}).dataset.space || '';
    const mark = () => box.querySelectorAll('.chat-row').forEach(r => {
      r.classList.toggle('on', r.dataset.space === current);
      if (r.dataset.space === current) r.classList.remove('unread');
    });
    async function openPane(space, push) {
      try {
        const r = await fetch(`${base}?s=${enc(space)}`, {headers: {'X-Chat-Pane': '1'}, credentials: 'same-origin'});
        if (!r.ok) { location.href = `${base}?s=${enc(space)}`; return; }
        const html = await r.text();
        const old = pane.querySelector('[data-chat-convo]');
        if (old && old._chat) old._chat.stop();  // the conversation being left stops polling (and marking things read)
        pane.innerHTML = html;
      } catch (e) { location.href = `${base}?s=${enc(space)}`; return; }
      current = space;
      app.classList.add('has-convo');
      mark();
      initConvo(pane.querySelector('[data-chat-convo]'));
      if (push) history.pushState({space}, '', `${base}?s=${enc(space)}`);
    }
    function showList(push) {
      const old = pane.querySelector('[data-chat-convo]');
      if (old && old._chat) old._chat.stop();
      pane.innerHTML = '<div class="chat-blank muted">Pick a conversation.</div>';
      current = '';
      app.classList.remove('has-convo');
      mark();
      if (push) history.pushState({}, '', base);
    }
    initRows(box, space => openPane(space, true), () => current);
    initConvo(pane.querySelector('[data-chat-convo]'));
    pane.addEventListener('click', e => { if (e.target.closest('[data-chat-back]')) { e.preventDefault(); showList(true); } });
    addEventListener('popstate', () => {
      const s = new URLSearchParams(location.search).get('s');
      if (s) openPane(s, false); else showList(false);
    });
  }

  // The dock's Chat box: the list; a click opens that conversation in a box of its own.
  if (document.body.hasAttribute('data-chat-list')) {
    const box = document.querySelector('[data-rows]');
    initRows(box, (space, title) => {
      const d = dock();
      if (d) d.chat(space, title); else window.top.location.href = `/chat/?s=${enc(space)}`;
    }, () => '');
  }

  // A conversation in a box of the dock: it names the box and keeps its own timers.
  if (document.body.hasAttribute('data-chat-box')) {
    initConvo(document.querySelector('[data-chat-convo]'));
    const d = dock(), id = document.body.dataset.window;
    if (d && id) d.title(id, document.body.dataset.windowTitle);
  }
})();
