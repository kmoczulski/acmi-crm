// A board: lists of cards. Cards move by pointer (drag) or keyboard (Space picks up, arrows move, Space drops,
// Escape cancels); every move is announced in a live region and saved with one POST. Cards open in a dialog that
// is fetched from the server; the forms inside post normally and come back to the board with ?card=<id>.
(function () {
  const board = document.getElementById('board');
  if (!board) return;
  const csrf = board.dataset.csrf;
  const live = document.getElementById('bl-live');
  const say = text => { if (live) { live.textContent = ''; setTimeout(() => { live.textContent = text; }, 30); } };
  const post = (url, data, headers) => fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: Object.assign({'X-CSRFToken': csrf, 'X-Requested-With': 'fetch'}, headers || {}),
    body: data instanceof FormData ? data : new URLSearchParams(data),
  });
  const listName = ol => ol.closest('.bl-list').querySelector('.bl-name').firstChild.textContent.trim();
  const cardsOf = ol => [...ol.querySelectorAll(':scope > .bl-card')];
  const indexIn = (ol, card) => cardsOf(ol).indexOf(card);
  function recount() {
    board.querySelectorAll('.bl-list[data-list]').forEach(l => {
      const n = l.querySelector('[data-count]'); if (n) n.textContent = cardsOf(l.querySelector('.bl-cards')).length;
    });
  }

  // --- Saving a move ------------------------------------------------------
  async function saveMove(card, ol, undo) {
    const r = await post(`/boards/cards/${card.dataset.card}/move/`, {list: ol.dataset.list, index: indexIn(ol, card)});
    if (!r.ok) {
      undo();
      let why = 'The move was not saved. Try again.';
      try { why = (await r.json()).error || why; } catch (err) {}
      say(why);
      if (r.status === 400) alert(why);
      card.classList.add('bl-failed'); setTimeout(() => card.classList.remove('bl-failed'), 1200);
      return false;
    }
    recount();
    return true;
  }

  // --- Pointer drag -------------------------------------------------------
  let dragged = null, from = null, fromNext = null;
  const placeholder = document.createElement('li');
  placeholder.className = 'bl-placeholder'; placeholder.setAttribute('aria-hidden', 'true');
  board.addEventListener('dragstart', e => {
    const card = e.target.closest('.bl-card'); if (!card || card.classList.contains('grabbed') || card.closest('[data-feed]')) { if (card) e.preventDefault(); return; }
    dragged = card; from = card.parentElement; fromNext = card.nextElementSibling;
    placeholder.style.height = card.getBoundingClientRect().height + 'px';
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', card.dataset.card); } catch (err) {}
    setTimeout(() => card.classList.add('dragging'), 0);
  });
  board.addEventListener('dragover', e => {
    if (!dragged) return;
    const ol = e.target.closest('.bl-cards'); if (!ol || ol.hasAttribute('data-feed')) return;  // feeds fill themselves
    e.preventDefault();
    const after = cardsOf(ol).filter(c => c !== dragged).find(c => { const r = c.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    if (after) ol.insertBefore(placeholder, after); else ol.appendChild(placeholder);
    ol.classList.add('drop-on');
  });
  board.addEventListener('dragleave', e => { const ol = e.target.closest('.bl-cards'); if (ol && !ol.contains(e.relatedTarget)) ol.classList.remove('drop-on'); });
  board.addEventListener('drop', e => {
    if (!dragged || !placeholder.parentElement) return;
    e.preventDefault();
    const ol = placeholder.parentElement;
    ol.insertBefore(dragged, placeholder); placeholder.remove();
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on'));
    const card = dragged, f = from, fn = fromNext;
    saveMove(card, ol, () => f.insertBefore(card, fn)).then(ok => { if (ok) say(`Moved ${card.querySelector('.bl-title').textContent} to ${listName(ol)}, position ${indexIn(ol, card) + 1}.`); });
  });
  board.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('dragging');
    placeholder.remove(); dragged = null;
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on'));
  });

  // --- Touch drag ---------------------------------------------------------
  // Press and hold a card for a moment to lift it, then drag it over a list. Moving before the hold ends is an ordinary
  // scroll. The lifted card floats under the finger (fixed position) while a placeholder holds its place; near the left
  // or right edge the board scrolls sideways. A tap still opens the card.
  let tCard = null, tTimer = null, tLifted = false, tX = 0, tY = 0, tBox = null, tScroll = null, tSwallow = false;
  const HOLD_MS = 250;
  function stopEdgeScroll() { if (tScroll) { clearInterval(tScroll); tScroll = null; } }
  function edgeScroll(x) {
    const r = board.getBoundingClientRect(), edge = 56;
    stopEdgeScroll();
    if (x < r.left + edge) tScroll = setInterval(() => { board.scrollLeft -= 12; }, 16);
    else if (x > r.right - edge) tScroll = setInterval(() => { board.scrollLeft += 12; }, 16);
  }
  function dropTouch(commit) {
    clearTimeout(tTimer); stopEdgeScroll(); board.style.scrollSnapType = '';
    const card = tCard; tCard = null;
    if (!card || !tLifted) return;
    tLifted = false;
    card.classList.remove('dragging', 'touch-lifted');
    card.style.cssText = '';
    tSwallow = true; setTimeout(() => { tSwallow = false; }, 400);   // the tap that ends a drag must not open the card
    const ol = placeholder.parentElement;
    if (!commit || !ol) { from.insertBefore(card, fromNext); placeholder.remove(); return; }
    ol.insertBefore(card, placeholder); placeholder.remove();
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on'));
    const f = from, fn = fromNext;
    saveMove(card, ol, () => f.insertBefore(card, fn)).then(ok => { if (ok) say(`Moved ${card.querySelector('.bl-title').textContent} to ${listName(ol)}, position ${indexIn(ol, card) + 1}.`); });
  }
  board.addEventListener('touchstart', e => {
    if (e.touches.length !== 1 || tCard) return;
    const card = e.target.closest('.bl-card');
    if (!card || card.classList.contains('bl-feed-item') || card.hasAttribute('data-fixed') || card.closest('[data-feed]') || grabbed) return;
    if (e.target.closest('a, button, input, textarea, select, form')) return;
    tX = e.touches[0].clientX; tY = e.touches[0].clientY; tCard = card;
    tTimer = setTimeout(() => {
      tLifted = true;
      from = card.parentElement; fromNext = card.nextElementSibling;
      tBox = card.getBoundingClientRect();
      placeholder.style.height = tBox.height + 'px';
      from.insertBefore(placeholder, fromNext);
      card.classList.add('dragging', 'touch-lifted');
      board.style.scrollSnapType = 'none';   // snapping would undo the edge scrolling
      card.style.cssText = `left:${tBox.left}px;top:${tBox.top}px;width:${tBox.width}px;`;
      if (navigator.vibrate) navigator.vibrate(30);
      say(`Picked up ${card.querySelector('.bl-title').textContent}. Drag it to a list, then let go.`);
    }, HOLD_MS);
  }, {passive: true});
  board.addEventListener('touchmove', e => {
    const t = e.touches[0];
    if (!tLifted) {   // still deciding: a move this early is a scroll, not a drag
      if (tCard && (Math.abs(t.clientX - tX) > 8 || Math.abs(t.clientY - tY) > 8)) { clearTimeout(tTimer); tCard = null; }
      return;
    }
    e.preventDefault();   // the page stays put while a card is in hand
    tCard.style.left = (tBox.left + t.clientX - tX) + 'px';
    tCard.style.top = (tBox.top + t.clientY - tY) + 'px';
    edgeScroll(t.clientX);
    const under = document.elementFromPoint(t.clientX, t.clientY);   // the lifted card ignores pointers, so this sees the list below
    const ol = under && under.closest('.bl-cards'); if (!ol || ol.hasAttribute('data-feed')) return;
    const after = cardsOf(ol).filter(c => c !== tCard).find(c => { const r = c.getBoundingClientRect(); return t.clientY < r.top + r.height / 2; });
    if (after) ol.insertBefore(placeholder, after); else ol.appendChild(placeholder);
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on')); ol.classList.add('drop-on');
  }, {passive: false});
  board.addEventListener('touchend', () => { if (tLifted) dropTouch(true); else { clearTimeout(tTimer); tCard = null; } });
  board.addEventListener('touchcancel', () => { if (tLifted) dropTouch(false); else { clearTimeout(tTimer); tCard = null; } });
  board.addEventListener('click', e => { if (tSwallow) { e.stopImmediatePropagation(); e.preventDefault(); } }, true);
  board.addEventListener('contextmenu', e => { if (tLifted || tCard) e.preventDefault(); });   // long-press menu on phones

  // --- Keyboard moves -----------------------------------------------------
  let grabbed = null, gFrom = null, gNext = null;
  function grab(card) {
    grabbed = card; gFrom = card.parentElement; gNext = card.nextElementSibling;
    card.classList.add('grabbed'); card.setAttribute('aria-pressed', 'true');
    say(`Picked up ${card.querySelector('.bl-title').textContent}. Arrow keys move it, Space drops it, Escape puts it back.`);
  }
  function release(card, cancelled) {
    card.classList.remove('grabbed'); card.removeAttribute('aria-pressed');
    const ol = card.parentElement;
    if (cancelled) { gFrom.insertBefore(card, gNext); say('Move cancelled.'); card.focus(); grabbed = null; return; }
    const f = gFrom, fn = gNext; grabbed = null;
    saveMove(card, ol, () => f.insertBefore(card, fn)).then(ok => { if (ok) say(`Dropped in ${listName(ol)}, position ${indexIn(ol, card) + 1}.`); card.focus(); });
  }
  board.addEventListener('keydown', e => {
    const card = e.target.closest('.bl-card'); if (!card || card.classList.contains('bl-feed-item')) return;
    if (e.key === 'Enter' && !grabbed) { e.preventDefault(); openCard(card); return; }
    if (e.key === ' ') {
      e.preventDefault();
      if (card.hasAttribute('data-fixed')) { say('This card belongs to a list that fills itself; it can\'t be moved.'); return; }
      if (grabbed === card) release(card, false); else if (!grabbed) grab(card);
      return;
    }
    if (!grabbed || grabbed !== card) return;
    if (e.key === 'Escape') { e.preventDefault(); release(card, true); return; }
    const ol = card.parentElement, lists = [...board.querySelectorAll('.bl-cards:not([data-feed])')], li = lists.indexOf(ol);
    if (e.key === 'ArrowUp' && card.previousElementSibling?.classList.contains('bl-card')) { ol.insertBefore(card, card.previousElementSibling); }
    else if (e.key === 'ArrowDown' && card.nextElementSibling?.classList.contains('bl-card')) { ol.insertBefore(card.nextElementSibling, card); }
    else if (e.key === 'ArrowLeft' && li > 0) { const t = lists[li - 1]; t.insertBefore(card, cardsOf(t)[indexIn(ol, card)] || null); }
    else if (e.key === 'ArrowRight' && li < lists.length - 1) { const t = lists[li + 1]; t.insertBefore(card, cardsOf(t)[indexIn(ol, card)] || null); }
    else return;
    e.preventDefault();
    card.focus();
    say(`${listName(card.parentElement)}, position ${indexIn(card.parentElement, card) + 1} of ${cardsOf(card.parentElement).length}.`);
  });

  // --- Add a card / a list --------------------------------------------------
  board.addEventListener('click', e => {
    const open = e.target.closest('[data-add-open]'), cancel = e.target.closest('[data-add-cancel]');
    if (open) { const f = open.closest('form'); open.hidden = true; const box = f.querySelector('.bl-add-box'); box.hidden = false; box.querySelector('textarea, input[name=name]').focus(); }
    if (cancel) { const f = cancel.closest('form'); f.querySelector('.bl-add-box').hidden = true; f.querySelector('[data-add-open]').hidden = false; f.reset(); }
  });
  board.addEventListener('keydown', e => {
    const box = e.target.closest('.bl-add-box'); if (!box) return;
    if (e.key === 'Escape') { box.closest('form').querySelector('[data-add-cancel]').click(); }
    if (e.key === 'Enter' && !e.shiftKey && e.target.tagName === 'TEXTAREA') { e.preventDefault(); box.closest('form').requestSubmit(); }
  });
  board.addEventListener('submit', async e => {
    const form = e.target;
    if (form.matches('[data-add-card]')) {
      e.preventDefault();
      const title = form.querySelector('textarea').value.trim(); if (!title) return;
      const r = await post(form.action, new FormData(form));
      if (!r.ok) { say('The card was not added.'); return; }
      const ol = form.closest('.bl-list').querySelector('.bl-cards');
      ol.insertAdjacentHTML('beforeend', await r.text());
      form.querySelector('textarea').value = ''; form.querySelector('textarea').focus();
      recount(); say(`Added ${title} to ${listName(ol)}.`);
    } else if (form.matches('[data-add-list]')) {
      e.preventDefault();
      const name = form.querySelector('input[name=name]').value.trim(); if (!name) return;
      const r = await post(form.action, new FormData(form));
      if (!r.ok) { say('The list was not added.'); return; }
      form.insertAdjacentHTML('beforebegin', await r.text());
      form.querySelector('[data-add-cancel]').click();
      board.querySelector('.bl-empty')?.remove();
      say(`Added the list ${name}.`);
    }
  });

  // --- The card dialog ----------------------------------------------------
  const dlg = document.getElementById('card-dialog'), body = document.getElementById('card-body');
  async function openCard(cardOrId) {
    const id = typeof cardOrId === 'string' ? cardOrId : cardOrId.dataset.card;
    body.innerHTML = '<p class="muted" style="padding:30px">Loading…</p>';
    if (!dlg.open) dlg.showModal();
    try {
      const r = await fetch(`/boards/cards/${id}/`, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}});
      body.innerHTML = r.ok ? await r.text() : `<p class="muted" style="padding:30px">Could not load the card (${r.status}).</p>`;
    } catch (err) { body.innerHTML = '<p class="muted" style="padding:30px">Could not reach the server.</p>'; }
    const q = new URLSearchParams(location.search); q.set('card', id);
    try { history.replaceState(null, '', '?' + q); } catch (err) {}
    wireDialog();
  }
  function closeCard() {
    dlg.close();
    const q = new URLSearchParams(location.search); q.delete('card');
    try { history.replaceState(null, '', q.toString() ? '?' + q : location.pathname); } catch (err) {}
  }
  board.addEventListener('click', e => {
    const card = e.target.closest('.bl-card');
    if (card && !card.classList.contains('bl-feed-item') && !e.target.closest('a, button, input, form') && !grabbed) openCard(card);
  });
  dlg.addEventListener('click', e => { if (e.target === dlg || e.target.closest('[data-card-close]')) closeCard(); });
  dlg.addEventListener('cancel', e => { e.preventDefault(); closeCard(); });
  if (board.dataset.open) openCard(board.dataset.open);

  function wireDialog() {
    // Rename and description: click to edit in place; Esc cancels, Ctrl+Enter saves a textarea.
    body.querySelectorAll('[data-edit-box]').forEach(box => {
      const view = box.querySelector('[data-view]'), form = box.querySelector('[data-form]');
      const field = form.querySelector('input[name=title], textarea'), original = field.value;
      const open = () => { view.hidden = true; form.hidden = false; box.classList.add('editing'); field.focus(); field.setSelectionRange(field.value.length, field.value.length); };
      const close = () => { field.value = original; form.hidden = true; view.hidden = false; box.classList.remove('editing'); };
      box.querySelectorAll('[data-open]').forEach(el => el.addEventListener('click', open));
      form.querySelector('[data-cancel]').addEventListener('click', close);
      field.addEventListener('keydown', e => {
        if (e.key === 'Escape') { e.stopPropagation(); close(); }
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && field.tagName === 'TEXTAREA') form.requestSubmit();
      });
    });
    // Checklist ticks save without a reload.
    body.querySelectorAll('[data-item-toggle]').forEach(f => f.addEventListener('submit', async e => {
      e.preventDefault();
      const r = await post(f.action, new FormData(f));
      if (r.ok) { f.closest('li').classList.toggle('done', f.querySelector('input[type=checkbox]').checked); }
      else { f.querySelector('input[type=checkbox]').checked = !f.querySelector('input[type=checkbox]').checked; say('Could not save the tick.'); }
    }));
    // Record picker: search as you type, pick a hit, the form posts the record's URL.
    const picker = body.querySelector('[data-record-picker]');
    if (picker) {
      const q = picker.querySelector('[data-record-q]'), hits = picker.querySelector('[data-record-hits]'), url = picker.querySelector('[name=record_url]');
      let timer = null;
      q.addEventListener('input', () => {
        clearTimeout(timer);
        const text = q.value.trim();
        if (text.length < 2) { hits.innerHTML = ''; return; }
        timer = setTimeout(async () => {
          const r = await fetch('/search/?q=' + encodeURIComponent(text), {credentials: 'same-origin'});
          const d = await r.json();
          const allowed = /^\/(companies|opportunities|land\/sites|contracts|directory|entitlements|capital|tasks)\/\d+\/|^\/meetings\/\?.*open=\d+/;
          const items = d.groups.filter(g => g.label !== 'Pages').flatMap(g => g.items.filter(i => allowed.test(i.url)).map(i => ({...i, group: g.label}))).slice(0, 12);
          hits.innerHTML = items.map(i => `<button type="button" class="cd-hit" role="option" data-url="${i.url.replace(/"/g, '&quot;')}"><b>${esc(i.title)}</b> <span class="muted">${esc(i.group)}${i.sub ? ' · ' + esc(i.sub) : ''}</span></button>`).join('') || '<p class="muted lc-small">Nothing found.</p>';
        }, 160);
      });
      hits.addEventListener('click', e => { const b = e.target.closest('.cd-hit'); if (!b) return; url.value = b.dataset.url; picker.requestSubmit(); });
    }
    body.querySelector('h2')?.focus?.();
  }
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  // Filters apply on change.
  const filters = document.querySelector('[data-filters]');
  filters?.querySelectorAll('select, input[type=checkbox]').forEach(el => el.addEventListener('change', () => filters.requestSubmit()));
})();
