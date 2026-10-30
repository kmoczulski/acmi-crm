// Chat in the hub (crm/chat/): the Chat page, the list in the dock's Chat box, and a conversation, which works the same on
// the page, in the split view's side and in a box of the dock. Everything is small JSON calls to /chat/... (crm/chat/views.py).
// Nothing from Chat is ever written as HTML here: the server escapes message text, and this script builds the rest from text.
(function () {
  const enc = encodeURIComponent;
  const dock = () => { try { return window.top !== window ? window.top.acmiDock : null; } catch (e) { return null; } };
  const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  function h(tag, attrs, ...kids) {
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

  async function call(url, opts) {
    const o = Object.assign({credentials: 'same-origin'}, opts);
    const r = await fetch(url, o);
    let d = {};
    try { d = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) { const err = new Error(d.error || 'Chat didn’t answer. Try again.'); err.data = d; throw err; }
    return d;
  }

  // ---- The emoji picker: a reaction, or an emoji in a message -----------------------------------------------------
  const Picker = (() => {
    let el = null, anchor = null;
    const data = () => window.ACMI_EMOJI || {quick: [], groups: []};
    const recent = () => { try { return JSON.parse(localStorage.getItem('chatRecentEmoji') || '[]'); } catch (e) { return []; } };
    const remember = e => { try { localStorage.setItem('chatRecentEmoji', JSON.stringify([e, ...recent().filter(x => x !== e)].slice(0, 16))); } catch (err) { /* no storage */ } };
    function close() {
      if (el) el.remove();
      el = null; anchor = null;
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', escape, true);
    }
    const outside = e => { if (el && !el.contains(e.target) && !(anchor && anchor.contains(e.target))) close(); };
    const escape = e => { if (e.key === 'Escape' && el) { e.stopPropagation(); close(); } };

    function open(button, onPick) {
      if (el && anchor === button) { close(); return; }
      close();
      anchor = button;
      const d = data(), grid = h('div', {class: 'chat-pick-grid'});
      const pick = e => { remember(e); close(); onPick(e); };
      const cell = (e, words) => h('button', {type: 'button', class: 'chat-emo-btn', title: (words || '').split(' ').slice(0, 3).join(' '), 'data-e': e, text: e});
      const draw = term => {
        grid.replaceChildren();
        const t = term.trim().toLowerCase();
        if (t) {
          const words = t.split(/\s+/);
          const hits = d.groups.flatMap(g => g.items).filter(([, w]) => words.every(x => w.includes(x)));
          grid.append(...(hits.length ? hits.map(([e, w]) => cell(e, w)) : [h('p', {class: 'muted lc-small', text: 'No emoji found.'})]));
          return;
        }
        const seen = recent();
        if (seen.length) grid.append(h('div', {class: 'chat-pick-head', text: 'Recent'}), ...seen.map(e => cell(e, '')));
        d.groups.forEach(g => grid.append(h('div', {class: 'chat-pick-head', 'data-group': g.name, text: g.name}), ...g.items.map(([e, w]) => cell(e, w))));
      };
      const search = h('input', {type: 'search', class: 'chat-pick-search', placeholder: 'Search emoji', 'aria-label': 'Search emoji'});
      search.addEventListener('input', () => draw(search.value));
      const tabs = h('div', {class: 'chat-pick-tabs'}, ...d.groups.map(g => {
        const b = h('button', {type: 'button', class: 'chat-pick-tab', title: g.name, 'aria-label': g.name, text: g.icon});
        b.addEventListener('click', () => { search.value = ''; draw(''); const head = grid.querySelector(`[data-group="${g.name}"]`); if (head) grid.scrollTop = head.offsetTop; });
        return b;
      }));
      const quick = h('div', {class: 'chat-pick-quick'}, ...d.quick.map(e => cell(e, '')));
      el = h('div', {class: 'chat-picker', role: 'dialog', 'aria-label': 'Pick an emoji'}, quick, search, tabs, grid);
      el.addEventListener('click', ev => { const b = ev.target.closest('.chat-emo-btn'); if (b) pick(b.dataset.e); });
      draw('');
      document.body.append(el);
      const r = button.getBoundingClientRect(), w = el.offsetWidth, hh = el.offsetHeight;
      el.style.left = Math.max(6, Math.min(r.left, innerWidth - w - 6)) + 'px';
      el.style.top = (r.bottom + 6 + hh > innerHeight - 6 ? Math.max(6, r.top - hh - 6) : r.bottom + 6) + 'px';
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', escape, true);
      if (!('ontouchstart' in window)) search.focus();
    }
    return {open, close, isOpen: () => !!el};
  })();

  // ---- One conversation: its messages, the box to write in, live updates, reactions, quotes, files ----------------
  function initConvo(root) {
    if (!root) return null;
    if (root._chat) root._chat.stop();  // the page swapped to another conversation: the old one's timer ends
    const q = sel => root.querySelector(sel);
    const scroll = q('[data-scroll]'), list = q('[data-list]'), form = q('[data-compose-form]'), text = q('[data-text]');
    const sendBtn = q('[data-send]'), errBox = q('[data-chat-error]'), none = q('[data-none]');
    const older = q('[data-older]'), fresh = q('[data-new]'), note = q('[data-thread-note]'), mute = q('[data-mute]');
    const quoteBar = q('[data-quote-bar]'), filesBox = q('[data-files]'), fileInput = q('[data-file-input]'), suggest = q('[data-suggest]');
    const space = root.dataset.space, maxMb = Number(root.dataset.maxMb || 25);
    const members = (() => { try { return JSON.parse((q('#chat-members') || {textContent: '[]'}).textContent); } catch (e) { return []; } })();
    if (root.dataset.everyone) members.unshift({id: 'users/all', name: 'all', email: '', everyone: true});
    const state = {latest: root.dataset.latest || '', earliest: root.dataset.earliest || '', thread: '', busy: false, sig: root.dataset.sig || '',
                   stopped: false, timer: null, rtimer: null, unseen: false, quote: null, files: [], editing: null, noTail: !root.dataset.sig};
    const mentions = new Map();
    const token = () => (form.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
    const post = (url, fields) => call(url, {method: 'POST', headers: {'X-CSRFToken': token()},
      body: new URLSearchParams(Object.assign({s: space}, fields || {}))});
    // In a box of the dock that is folded the frame has no size; a hidden tab counts as not shown too.
    const shown = () => !document.hidden && (!window.frameElement || window.frameElement.getClientRects().length > 0);
    const nearBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
    const toBottom = () => { scroll.scrollTop = scroll.scrollHeight; };
    const msgs = () => list.querySelectorAll('.chat-msg');
    const last = () => { const all = msgs(); return all.length ? all[all.length - 1] : null; };
    const byId = id => [...msgs()].find(n => n.dataset.id === id) || null;
    const context = () => {  // the message above new ones, so grouping carries on
      const m = last();
      return m ? {prev_uid: m.dataset.uid, prev_time: m.dataset.iso, prev_reply: m.classList.contains('in-thread') ? '1' : ''} : {};
    };
    const showError = msg => { errBox.textContent = msg; errBox.hidden = !msg; };
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

    // New messages, every few seconds.
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

    // What others changed in the newest messages (a reaction, an edit, a deletion), every half minute.
    const selecting = () => { const s = window.getSelection(); return !!(s && !s.isCollapsed && list.contains(s.anchorNode)); };
    async function refreshTail() {
      if (state.stopped || !root.isConnected) return;
      let delay = shown() ? 30000 : 90000;
      if (shown() && !state.busy && !state.editing && !Picker.isOpen() && !selecting() && !state.noTail) {
        try {
          const d = await post(root.dataset.refreshUrl, {sig: state.sig});
          if (d.changed) {
            const first = byId(d.first);
            if (!first) state.noTail = true;  // the newest messages aren't on this page (it opened at an older one)
            else if (!state.editing && !Picker.isOpen()) {
              const stick = nearBottom();
              let node = first.previousElementSibling;
              if (node && node.classList.contains('chat-day')) node.remove();
              node = first;
              while (node) { const next = node.nextElementSibling; node.remove(); node = next; }
              list.insertAdjacentHTML('beforeend', d.html);
              state.latest = d.latest || state.latest;
              if (stick) toBottom();
            }
          }
          state.sig = d.sig || state.sig;
        } catch (e) { /* the next time */ }
      }
      state.rtimer = setTimeout(refreshTail, delay);
    }

    // ---- Writing -----------------------------------------------------------------------------------------------------
    function withMentions(body) {  // "@Simon" typed from the suggestions becomes Chat's <users/ID>
      let out = body;
      [...mentions.entries()].sort((a, b) => b[0].length - a[0].length).forEach(([name, id]) => {
        out = out.replace(new RegExp('(^|[\\s(])@' + escapeRe(name) + '(?![\\w])', 'g'), (m, pre) => pre + '<' + id + '>');
      });
      return out;
    }

    function drawQuote() {
      quoteBar.hidden = !state.quote;
      if (!state.quote) return;
      q('[data-quote-who]').textContent = state.quote.author;
      q('[data-quote-text]').textContent = state.quote.snippet;
    }
    function clearQuote() { state.quote = null; drawQuote(); }
    function clearThread() { state.thread = ''; note.hidden = true; }

    form.addEventListener('submit', async e => {
      e.preventDefault();
      if (state.busy) return;
      if (state.files.some(f => f.status === 'uploading')) { showError('Wait for the files to finish uploading.'); return; }
      if (state.files.some(f => f.status === 'error')) { showError('Remove the file that didn’t upload, or try it again.'); return; }
      const body = withMentions(text.value.trim()), tokens = state.files.map(f => f.token);
      if (!body && !tokens.length) return;
      state.busy = true;
      sendBtn.disabled = true;
      showError('');
      try {
        const d = await post(root.dataset.sendUrl, Object.assign({text: body, thread: state.thread, quote: state.quote ? state.quote.id : '',
          files: JSON.stringify(tokens)}, context()));
        append(d.html);
        text.value = '';
        mentions.clear();
        grow();
        clearThread();
        clearQuote();
        state.files.forEach(f => f.url && URL.revokeObjectURL(f.url));
        state.files = [];
        drawFiles();
        toBottom();
      } catch (err) { showError(err.message); }
      state.busy = false;
      sendBtn.disabled = false;
      text.focus();
    });

    // ---- @mentions and :emoji: as you type -------------------------------------------------------------------------
    let options = [], chosen = 0, kind = '';
    const closeSuggest = () => { suggest.hidden = true; options = []; };
    function drawSuggest() {
      suggest.replaceChildren(...options.map((o, i) => {
        const li = h('li', {class: 'chat-sug' + (i === chosen ? ' on' : ''), role: 'option'}, h('span', {class: 'chat-sug-icon', text: o.icon || '@'}), h('span', {text: o.label}), o.hint ? h('small', {class: 'muted', text: o.hint}) : null);
        li.addEventListener('mousedown', ev => { ev.preventDefault(); chosen = i; takeSuggestion(); });
        return li;
      }));
      suggest.hidden = !options.length;
    }
    function lookAtCaret() {
      const upto = text.value.slice(0, text.selectionStart);
      let m = /(^|\s)@([^\s@]{0,30})$/.exec(upto);
      if (m && members.length) {
        const t = m[2].toLowerCase();
        options = members.filter(p => p.name.toLowerCase().includes(t) || (p.email || '').toLowerCase().startsWith(t)).slice(0, 6)
          .map(p => ({label: (p.everyone ? '@all' : p.name), hint: p.everyone ? 'Notify everyone' : '', value: p, start: upto.length - m[2].length - 1}));
        kind = 'mention'; chosen = 0; drawSuggest();
        return;
      }
      m = /(^|\s):([a-z0-9_+-]{2,20})$/i.exec(upto);
      if (m && window.ACMI_EMOJI) {
        const t = m[2].toLowerCase();
        options = window.ACMI_EMOJI.groups.flatMap(g => g.items).filter(([, w]) => w.split(' ').some(x => x.startsWith(t))).slice(0, 6)
          .map(([e, w]) => ({label: e, icon: e, hint: w.split(' ').slice(0, 2).join(' '), value: e, start: upto.length - m[2].length - 1}));
        kind = 'emoji'; chosen = 0; drawSuggest();
        return;
      }
      closeSuggest();
    }
    function takeSuggestion() {
      const o = options[chosen];
      if (!o) return;
      const caret = text.selectionStart, insert = kind === 'mention' ? '@' + o.value.name + ' ' : o.value + ' ';
      text.value = text.value.slice(0, o.start) + insert + text.value.slice(caret);
      text.selectionStart = text.selectionEnd = o.start + insert.length;
      if (kind === 'mention') mentions.set(o.value.name, o.value.id);
      closeSuggest();
      grow();
      text.focus();
    }

    text.addEventListener('keydown', e => {
      if (!suggest.hidden) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); chosen = (chosen + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length; drawSuggest(); return; }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); takeSuggestion(); return; }
        if (e.key === 'Escape') { e.preventDefault(); closeSuggest(); return; }
      }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === 'b' || e.key === 'i')) { e.preventDefault(); format(e.key === 'b' ? '*' : '_'); return; }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    text.addEventListener('input', () => { grow(); lookAtCaret(); });
    text.addEventListener('click', lookAtCaret);
    text.addEventListener('blur', () => setTimeout(closeSuggest, 120));

    // ---- Formatting: the buttons put Chat's own markup around what is selected ----------------------------------
    function format(mark) {
      const a = text.selectionStart, b = text.selectionEnd, v = text.value, sel = v.slice(a, b);
      if (mark === 'list') {
        const start = v.lastIndexOf('\n', a - 1) + 1, endAt = v.indexOf('\n', b), end = endAt < 0 ? v.length : endAt;
        const lines = v.slice(start, end).split('\n'), on = lines.every(l => /^[*-] /.test(l));
        const next = lines.map(l => (on ? l.replace(/^[*-] /, '') : '* ' + l)).join('\n');
        text.value = v.slice(0, start) + next + v.slice(end);
        text.selectionStart = start; text.selectionEnd = start + next.length;
      } else if (mark === 'link') {
        const url = (window.prompt('Link address (starting with https://)', 'https://') || '').trim();
        if (!/^https?:\/\/\S+$/i.test(url)) return;
        const ins = sel ? `<${url}|${sel}>` : `<${url}>`;
        text.value = v.slice(0, a) + ins + v.slice(b);
        text.selectionStart = text.selectionEnd = a + ins.length;
      } else {
        const ins = mark + (sel || '') + mark;
        text.value = v.slice(0, a) + ins + v.slice(b);
        text.selectionStart = a + mark.length; text.selectionEnd = a + mark.length + sel.length;
      }
      text.focus();
      grow();
    }
    root.addEventListener('click', e => {
      const f = e.target.closest('[data-fmt]');
      if (f) { e.preventDefault(); format(f.dataset.fmt); return; }
      const em = e.target.closest('[data-emoji-insert]');
      if (em) {
        Picker.open(em, ch => { const a = text.selectionStart; text.value = text.value.slice(0, a) + ch + text.value.slice(text.selectionEnd); text.selectionStart = text.selectionEnd = a + ch.length; text.focus(); grow(); });
        return;
      }
      if (e.target.closest('[data-attach]')) fileInput.click();
    });

    // ---- Files: pick, drop or paste; each goes to Chat as soon as it is chosen --------------------------------------
    const sizeLabel = n => (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB');
    function drawFiles() {
      filesBox.hidden = !state.files.length;
      filesBox.replaceChildren(...state.files.map(f => {
        const x = h('button', {type: 'button', class: 'btn-link chat-file-x', 'aria-label': 'Remove ' + f.name, title: 'Remove', text: '✕'});
        x.addEventListener('click', () => {
          if (f.xhr && f.status === 'uploading') f.xhr.abort();
          if (f.url) URL.revokeObjectURL(f.url);
          state.files = state.files.filter(g => g !== f);
          drawFiles();
        });
        const status = f.status === 'done' ? '✓' : f.status === 'error' ? '⚠ ' + f.error : (f.pct || 0) + '%';
        return h('span', {class: 'chat-file-chip ' + f.status},
          f.url ? h('img', {src: f.url, alt: ''}) : h('span', {class: 'chat-file-ico', text: '📄'}),
          h('span', {class: 'chat-file-name', text: f.name}), h('small', {class: 'muted', text: sizeLabel(f.size) + ' · ' + status}), x);
      }));
    }
    function upload(f) {
      const fd = new FormData();
      fd.append('s', space);
      fd.append('file', f.file);
      const xhr = new XMLHttpRequest();
      f.xhr = xhr;
      xhr.open('POST', root.dataset.uploadUrl);
      xhr.setRequestHeader('X-CSRFToken', token());
      xhr.upload.onprogress = ev => { if (ev.lengthComputable) { f.pct = Math.round(ev.loaded / ev.total * 100); drawFiles(); } };
      xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && d.token) { f.status = 'done'; f.token = d.token; }
        else { f.status = 'error'; f.error = d.error || 'It didn’t upload.'; }
        drawFiles();
      };
      xhr.onerror = () => { f.status = 'error'; f.error = 'The connection dropped.'; drawFiles(); };
      xhr.send(fd);
    }
    function addFiles(picked) {
      showError('');
      for (const file of picked) {
        if (state.files.length >= 10) { showError('Ten files on one message is the most.'); break; }
        if (file.size > maxMb * 1048576) { showError(`“${file.name}” is bigger than ${maxMb} MB, which is as much as the hub can pass on.`); continue; }
        const f = {file, name: file.name || 'pasted file', size: file.size, status: 'uploading', pct: 0, token: '',
                   url: /^image\/(png|jpe?g|gif|webp)$/.test(file.type) ? URL.createObjectURL(file) : ''};
        state.files.push(f);
        upload(f);
      }
      drawFiles();
    }
    fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value = ''; });
    text.addEventListener('paste', e => {
      const pasted = [...((e.clipboardData && e.clipboardData.files) || [])];
      if (pasted.length) { e.preventDefault(); addFiles(pasted); }
    });
    const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    let dragDepth = 0;
    root.addEventListener('dragenter', e => { if (hasFiles(e)) { dragDepth++; root.classList.add('dropping'); } });
    root.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
    root.addEventListener('dragleave', e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; root.classList.remove('dropping'); } });
    root.addEventListener('drop', e => { if (hasFiles(e)) { e.preventDefault(); dragDepth = 0; root.classList.remove('dropping'); addFiles([...e.dataTransfer.files]); } });

    // ---- A message's own buttons: react, reply in a thread, quote, edit, delete -------------------------------------
    const msgOf = el => el.closest('.chat-msg');
    async function reactTo(m, emoji) {
      try {
        const d = await post(root.dataset.reactUrl, {m: m.dataset.id, emoji});
        m.querySelector('[data-react]').innerHTML = d.html;
        m._who = null;
        state.sig = '';  // the page's own picture of reactions is newer than the last reading: read again at the next refresh
      } catch (err) { showError(err.message); }
    }

    function startEdit(m) {
      if (state.editing) cancelEdit();
      const body = m.querySelector('.chat-text'), box = h('textarea', {rows: '2', maxlength: '4000', 'aria-label': 'Edit message'});
      box.value = m.dataset.raw || '';
      const err = h('p', {class: 'mail-error', hidden: true});
      const save = h('button', {type: 'button', class: 'btn btn-small btn-primary', text: 'Save'});
      const cancel = h('button', {type: 'button', class: 'btn btn-small', text: 'Cancel'});
      const editor = h('div', {class: 'chat-edit'}, box, err, h('div', {class: 'chat-edit-actions'}, save, cancel, h('small', {class: 'muted', text: 'Enter to save · Esc to cancel'})));
      const finish = () => { editor.remove(); body.hidden = false; m.classList.remove('editing'); state.editing = null; };
      state.editing = {m, finish};
      async function go() {
        const value = box.value.trim();
        if (!value) { err.textContent = 'A message can’t be empty. Delete it instead.'; err.hidden = false; return; }
        save.disabled = true;
        try {
          const d = await post(root.dataset.editUrl, {m: m.dataset.id, text: value});
          body.innerHTML = d.html + (d.edited ? ' <span class="chat-edited muted">(edited)</span>' : '');
          m.dataset.raw = d.raw;
          finish();
          state.sig = '';
        } catch (e2) { err.textContent = e2.message; err.hidden = false; save.disabled = false; }
      }
      save.addEventListener('click', go);
      cancel.addEventListener('click', finish);
      box.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); }
        if (e.key === 'Escape') { e.preventDefault(); finish(); }
      });
      body.hidden = true;
      m.classList.add('editing');
      body.after(editor);
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    }
    function cancelEdit() { if (state.editing) state.editing.finish(); }

    async function remove(m) {
      const replies = !m.classList.contains('in-thread') && m.dataset.thread && [...msgs()].some(n => n !== m && n.dataset.thread === m.dataset.thread);
      if (!window.confirm(replies ? 'Delete this message and the replies in its thread? This can’t be undone.' : 'Delete this message? This can’t be undone.')) return;
      try {
        await post(root.dataset.deleteUrl, {m: m.dataset.id, force: replies ? '1' : ''});
        m.classList.add('is-deleted');
        ['.chat-react', '.chat-acts', '.chat-quote', '.chat-img', '.chat-file'].forEach(s => m.querySelectorAll(s).forEach(n => n.remove()));
        m.querySelector('.chat-text').innerHTML = '<em class="muted">This message was deleted.</em>';
        state.sig = '';
      } catch (err) { showError(err.message); }
    }

    function flash(m) { m.classList.add('hit'); setTimeout(() => m.classList.remove('hit'), 2500); }

    root.addEventListener('click', e => {
      const b = e.target.closest('[data-reply-thread]');
      if (b) { state.thread = b.dataset.replyThread; note.hidden = false; text.focus(); return; }
      if (e.target.closest('[data-thread-clear]')) { clearThread(); return; }
      if (e.target.closest('[data-quote-clear]')) { clearQuote(); return; }
      const chip = e.target.closest('.chat-chip');
      if (chip && !chip.disabled) { reactTo(msgOf(chip), chip.dataset.emoji); return; }
      const act = e.target.closest('[data-act]');
      if (act) {
        const m = msgOf(act);
        if (act.dataset.act === 'react') Picker.open(act, emoji => reactTo(m, emoji));
        else if (act.dataset.act === 'quote') {
          state.quote = {id: m.dataset.id, author: m.dataset.author, snippet: m.dataset.snippet};
          if (m.classList.contains('in-thread') && m.dataset.thread) { state.thread = m.dataset.thread; note.hidden = false; }  // Chat only quotes within a thread
          drawQuote();
          text.focus();
        } else if (act.dataset.act === 'edit') startEdit(m);
        else if (act.dataset.act === 'delete') remove(m);
        return;
      }
      const quoted = e.target.closest('.chat-quote[data-quote-id]');
      if (quoted) { const target = byId(quoted.dataset.quoteId); if (target) { target.scrollIntoView({block: 'center', behavior: 'smooth'}); flash(target); } return; }
      if (window.matchMedia && matchMedia('(hover: none)').matches) {  // a touch screen has no hover: a tap shows a message's buttons
        const m = msgOf(e.target);
        msgs().forEach(n => { if (n !== m) n.classList.remove('touched'); });
        if (m && !e.target.closest('a, button, textarea')) m.classList.toggle('touched');
      }
    });

    // Who gave which emoji: found when the pointer rests on a chip, and shown as its tooltip.
    let whoTimer = 0;
    list.addEventListener('mouseover', e => {
      const chip = e.target.closest('.chat-chip');
      if (!chip) return;
      const m = msgOf(chip);
      const show = () => m.querySelectorAll('.chat-chip').forEach(c => { const hit = (m._who.emojis || []).find(x => x.label === c.dataset.emoji); if (hit) c.title = hit.names.join(', '); });
      if (m._who && Date.now() - m._who.at < 20000) { show(); return; }
      clearTimeout(whoTimer);
      whoTimer = setTimeout(async () => {
        try {
          const d = await call(`${root.dataset.reactionsUrl}?${new URLSearchParams({s: space, m: m.dataset.id})}`);
          m._who = {at: Date.now(), emojis: d.emojis};
          show();
        } catch (err) { /* no tooltip */ }
      }, 350);
    });
    list.addEventListener('error', e => { if (e.target.matches && e.target.matches('.chat-av img')) e.target.remove(); }, true);  // no picture: the initial stays

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

    const hit = root.dataset.hit && byId(root.dataset.hit);  // opened at a message a search found
    const place = () => { if (hit) { hit.scrollIntoView({block: 'center'}); flash(hit); } else toBottom(); };
    requestAnimationFrame(place);
    setTimeout(place, 150);
    if (shown() && !('ontouchstart' in window) && !hit) setTimeout(() => text.focus(), 60);
    state.timer = setTimeout(tick, 6000);
    state.rtimer = setTimeout(refreshTail, 30000);
    root._chat = {stop() {
      state.stopped = true; clearTimeout(state.timer); clearTimeout(state.rtimer);
      document.removeEventListener('visibilitychange', onVisible);
      Picker.close();
      state.files.forEach(f => f.url && URL.revokeObjectURL(f.url));
    }};
    return root._chat;
  }

  // ---- The list of conversations: a search box, rows that open a conversation, and a refresh every 20 seconds ---------
  function initRows(box, onOpen, current) {
    const search = document.querySelector('[data-chat-search]'), hint = document.querySelector('[data-search-hint]');
    const results = document.querySelector('[data-results]');
    const app = document.querySelector('[data-chat-app]') || {dataset: {}};
    const url = box.dataset.rowsUrl || app.dataset.rowsUrl;
    const searchUrl = box.dataset.searchUrl || app.dataset.searchUrl;
    let searching = false;
    const term = () => ((search && search.value) || '').trim();
    const apply = () => {
      const t = term().toLowerCase();
      box.querySelectorAll('.chat-row').forEach(r => { r.hidden = !!t && !(r.dataset.find || '').includes(t); });
      if (hint) { hint.hidden = term().length < 2 || !searchUrl; if (!hint.hidden) hint.querySelector('[data-search-term]').textContent = term(); }
    };
    function leaveSearch() {
      searching = false;
      if (results) { results.hidden = true; results.replaceChildren(); }
      box.hidden = false;
    }
    async function runSearch(page) {
      if (!searchUrl || term().length < 2) return;
      searching = true;
      box.hidden = true;
      results.hidden = false;
      if (!page) results.replaceChildren(h('p', {class: 'muted chat-empty lc-small', text: 'Searching…'}));
      try {
        const d = await call(`${searchUrl}?${new URLSearchParams(Object.assign({q: term()}, page ? {page} : {}))}`);
        const bar = h('div', {class: 'chat-results-bar'}, h('button', {type: 'button', class: 'btn btn-small', 'data-back': '1', text: '← Chats'}), h('span', {class: 'muted lc-small', text: `Messages matching “${term()}”`}));
        const holder = h('div');
        holder.innerHTML = d.html;
        const more = d.next ? h('button', {type: 'button', class: 'btn btn-small chat-more', text: 'More results'}) : null;
        if (more) more.addEventListener('click', () => { more.remove(); runSearchMore(d.next); });
        if (page) { results.querySelector('.chat-more') && results.querySelector('.chat-more').remove(); results.append(...holder.children, ...(more ? [more] : [])); }
        else results.replaceChildren(bar, ...holder.children, ...(more ? [more] : []));
      } catch (err) { results.replaceChildren(h('p', {class: 'mail-error', text: err.message})); }
    }
    const runSearchMore = page => runSearch(page);
    if (search) {
      search.addEventListener('input', () => { apply(); if (searching && !term()) leaveSearch(); });
      search.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); runSearch(); } if (e.key === 'Escape' && searching) { search.value = ''; leaveSearch(); apply(); } });
    }
    if (hint) hint.addEventListener('click', () => runSearch());
    if (results) results.addEventListener('click', e => {
      if (e.target.closest('[data-back]')) { if (search) search.value = ''; leaveSearch(); apply(); return; }
      const a = e.target.closest('.chat-hit');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      onOpen(a.dataset.space, a.dataset.title, a.dataset.at);
    });
    box.addEventListener('click', e => {
      const a = e.target.closest('.chat-row');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      onOpen(a.dataset.space, a.dataset.title);
    });
    setInterval(async () => {
      if (document.hidden || !url || searching) return;
      if (window.frameElement && !window.frameElement.getClientRects().length) return;  // the dock's Chat box is folded
      try {
        const d = await call(`${url}?current=${enc(current())}`);
        box.innerHTML = d.html;
        apply();
      } catch (e) { /* the next refresh tries again */ }
    }, 20000);
    return {apply};
  }

  // ---- Starting a chat or a space: one person is a direct message, several a group chat, a name a space -------------
  function initNew(onOpen) {
    const dialog = document.querySelector('[data-new-dialog]'), open = document.querySelector('[data-new-chat]');
    if (!dialog || !open) return;
    const app = document.querySelector('[data-chat-app]') || document.querySelector('[data-rows]') || {dataset: {}};
    const url = app.dataset.newUrl, find = dialog.querySelector('[data-new-find]'), people = dialog.querySelector('[data-new-people]');
    const err = dialog.querySelector('[data-new-error]'), go = dialog.querySelector('[data-new-go]'), name = dialog.querySelector('[data-new-name]');
    const token = () => (dialog.querySelector('[name=csrfmiddlewaretoken]') || document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
    const showErr = m => { err.textContent = m; err.hidden = !m; };
    const filter = () => { const t = find.value.trim().toLowerCase(); people.querySelectorAll('li').forEach(li => { li.hidden = !!t && !(li.dataset.find || '').includes(t); }); };
    find.addEventListener('input', filter);
    find.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const v = find.value.trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return;
      if (![...people.querySelectorAll('input')].some(i => i.value === v)) {
        people.prepend(h('li', {'data-find': v}, h('label', null, h('input', {type: 'checkbox', value: v, checked: true}), h('span', {class: 'chat-pn', text: v}))));
      } else people.querySelectorAll('input').forEach(i => { if (i.value === v) i.checked = true; });
      find.value = '';
      filter();
    });
    open.addEventListener('click', () => {
      showErr('');
      people.querySelectorAll('input').forEach(i => { i.checked = false; });
      name.value = ''; find.value = ''; filter();
      if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', '');
      find.focus();
    });
    dialog.querySelector('[data-new-cancel]').addEventListener('click', () => dialog.close());
    go.addEventListener('click', async () => {
      const picked = [...people.querySelectorAll('input:checked')].map(i => i.value);
      if (!picked.length && !name.value.trim()) { showErr('Pick at least one person.'); return; }
      go.disabled = true;
      showErr('');
      try {
        const d = await call(url, {method: 'POST', headers: {'X-CSRFToken': token()}, body: new URLSearchParams({people: picked.join(','), name: name.value.trim()})});
        dialog.close();
        onOpen(d.id, d.title);
      } catch (e) {
        showErr(e.message);
        if (e.data && e.data.needs_permission) dialog.querySelector('.chat-allow') && dialog.querySelector('.chat-allow').classList.add('flash');
      }
      go.disabled = false;
    });
  }

  // ---- The Chat page (also the split view's side): the list on the left, the open conversation on the right ---------
  const app = document.querySelector('[data-chat-app]');
  if (app) {
    const pane = app.querySelector('[data-pane]'), box = app.querySelector('[data-rows]'), base = app.dataset.chatUrl;
    let current = (pane.querySelector('[data-chat-convo]') || {dataset: {}}).dataset.space || '';
    const mark = () => box.querySelectorAll('.chat-row').forEach(r => {
      r.classList.toggle('on', r.dataset.space === current);
      if (r.dataset.space === current) r.classList.remove('unread');
    });
    const where = (space, at) => `${base}?s=${enc(space)}${at ? '&at=' + enc(at) : ''}`;
    async function openPane(space, push, at) {
      try {
        const r = await fetch(where(space, at), {headers: {'X-Chat-Pane': '1'}, credentials: 'same-origin'});
        if (!r.ok) { location.href = where(space, at); return; }
        const html = await r.text();
        const old = pane.querySelector('[data-chat-convo]');
        if (old && old._chat) old._chat.stop();  // the conversation being left stops polling (and marking things read)
        pane.innerHTML = html;
      } catch (e) { location.href = where(space, at); return; }
      current = space;
      app.classList.add('has-convo');
      mark();
      initConvo(pane.querySelector('[data-chat-convo]'));
      if (push) history.pushState({space}, '', where(space, at));
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
    initRows(box, (space, title, at) => openPane(space, true, at), () => current);
    initNew(space => openPane(space, true));
    initConvo(pane.querySelector('[data-chat-convo]'));
    pane.addEventListener('click', e => { if (e.target.closest('[data-chat-back]')) { e.preventDefault(); showList(true); } });
    addEventListener('popstate', () => {
      const p = new URLSearchParams(location.search), s = p.get('s');
      if (s) openPane(s, false, p.get('at') || ''); else showList(false);
    });
  }

  // The dock's Chat box: the list; a click opens that conversation in a box of its own.
  if (document.body.hasAttribute('data-chat-list')) {
    const box = document.querySelector('[data-rows]');
    const open = (space, title, at) => {
      const d = dock();
      if (d) d.chat(space, title, at); else window.top.location.href = `/chat/?s=${enc(space)}${at ? '&at=' + enc(at) : ''}`;
    };
    initRows(box, open, () => '');
    initNew((space, title) => open(space, title));
  }

  // A conversation in a box of the dock: it names the box and keeps its own timers.
  if (document.body.hasAttribute('data-chat-box')) {
    initConvo(document.querySelector('[data-chat-convo]'));
    const d = dock(), id = document.body.dataset.window;
    if (d && id) d.title(id, document.body.dataset.windowTitle);
  }
})();
