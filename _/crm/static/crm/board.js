/**
 * ACMI CRM - Universal Kanban Board Engine (Desktop & Touch)
 * Location: crm/static/crm/board.js
 */
(function () {
  const board = document.getElementById('board');
  if (!board) return;

  const csrf = board.dataset.csrf;
  const live = document.getElementById('bl-live');
  const say = text => { 
    if (live) { 
      live.textContent = ''; 
      setTimeout(() => { live.textContent = text; }, 30); 
    } 
  };

  const post = (url, data, headers) => fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: Object.assign({
      'X-CSRFToken': csrf, 
      'X-Requested-With': 'fetch'
    }, headers || {}),
    body: data instanceof FormData ? data : new URLSearchParams(data),
  });

  const listName = ol => ol.closest('.bl-list').querySelector('.bl-name').firstChild.textContent.trim();
  const cardsOf = ol => [...ol.querySelectorAll(':scope > .bl-card')];
  const indexIn = (ol, card) => cardsOf(ol).indexOf(card);

  function recount() {
    board.querySelectorAll('.bl-list[data-list]').forEach(l => {
      const n = l.querySelector('[data-count]');
      if (n) n.textContent = cardsOf(l.querySelector('.bl-cards')).length;
    });
  }

  // --- Move Persistence ----------------------------------------------------
  async function saveMove(card, ol, undo) {
    const r = await post(`/boards/cards/${card.dataset.card}/move/`, {
      list: ol.dataset.list, 
      index: indexIn(ol, card)
    });
    if (!r.ok) {
      undo();
      let why = 'The move was not saved. Try again.';
      try { why = (await r.json()).error || why; } catch (err) {}
      say(why);
      if (r.status === 400) alert(why);
      card.classList.add('bl-failed'); 
      setTimeout(() => card.classList.remove('bl-failed'), 1200);
      return false;
    }
    recount();
    return true;
  }

  // --- Edge Auto-scrolling ------------------------------------------------
  let autoScrollTimer = null;
  function handleAutoScroll(clientX) {
    const rect = board.getBoundingClientRect();
    const threshold = 70;
    const speed = 14;
    clearInterval(autoScrollTimer);
    autoScrollTimer = null;

    if (clientX < rect.left + threshold) {
      autoScrollTimer = setInterval(() => { board.scrollLeft -= speed; }, 16);
    } else if (clientX > rect.right - threshold) {
      autoScrollTimer = setInterval(() => { board.scrollLeft += speed; }, 16);
    }
  }

  function stopAutoScroll() {
    if (autoScrollTimer) {
      clearInterval(autoScrollTimer);
      autoScrollTimer = null;
    }
  }

  // --- Pointer & HTML5 Desktop Drag ---------------------------------------
  let dragged = null, from = null, fromNext = null;
  const placeholder = document.createElement('li');
  placeholder.className = 'bl-placeholder';
  placeholder.setAttribute('aria-hidden', 'true');

  board.addEventListener('dragstart', e => {
    const card = e.target.closest('.bl-card');
    if (!card || card.classList.contains('grabbed') || card.closest('[data-feed]')) { 
      if (card) e.preventDefault(); 
      return; 
    }
    dragged = card; 
    from = card.parentElement; 
    fromNext = card.nextElementSibling;
    placeholder.style.height = card.getBoundingClientRect().height + 'px';
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', card.dataset.card); } catch (err) {}
    setTimeout(() => card.classList.add('dragging'), 0);
  });

  board.addEventListener('dragover', e => {
    if (!dragged) return;
    const ol = e.target.closest('.bl-cards');
    if (!ol || ol.hasAttribute('data-feed')) return;
    e.preventDefault();
    handleAutoScroll(e.clientX);

    const after = cardsOf(ol).filter(c => c !== dragged).find(c => {
      const r = c.getBoundingClientRect();
      return e.clientY < r.top + r.height / 2;
    });

    if (after) ol.insertBefore(placeholder, after);
    else ol.appendChild(placeholder);
    ol.classList.add('drop-on');
  });

  board.addEventListener('dragleave', e => {
    const ol = e.target.closest('.bl-cards');
    if (ol && !ol.contains(e.relatedTarget)) ol.classList.remove('drop-on');
  });

  board.addEventListener('drop', e => {
    stopAutoScroll();
    if (!dragged || !placeholder.parentElement) return;
    e.preventDefault();
    const ol = placeholder.parentElement;
    ol.insertBefore(dragged, placeholder);
    placeholder.remove();
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on'));

    const card = dragged, f = from, fn = fromNext;
    saveMove(card, ol, () => f.insertBefore(card, fn)).then(ok => {
      if (ok) say(`Moved ${card.querySelector('.bl-title').textContent} to ${listName(ol)}, position ${indexIn(ol, card) + 1}.`);
    });
  });

  board.addEventListener('dragend', () => {
    stopAutoScroll();
    if (dragged) dragged.classList.remove('dragging');
    placeholder.remove();
    dragged = null;
    board.querySelectorAll('.drop-on').forEach(x => x.classList.remove('drop-on'));
  });

  // --- Mobile Touch Drag & Drop -------------------------------------------
  let touchCard = null, touchTimer = null, isTouchDragging = false;
  let touchStartX = 0, touchStartY = 0;

  board.addEventListener('touchstart', e => {
    const card = e.target.closest('.bl-card');
    if (!card || card.classList.contains('bl-feed-item') || card.closest('[data-feed]')) return;
    if (e.target.closest('a, button, input, textarea, form')) return;

    const t = e.touches[0];
    touchStartX = t.clientX;
    touchStartY = t.clientY;

    touchTimer = setTimeout(() => {
      isTouchDragging = true;
      touchCard = card;
      from = card.parentElement;
      fromNext = card.nextElementSibling;
      card.classList.add('dragging', 'touch-lifted');
      placeholder.style.height = card.getBoundingClientRect().height + 'px';
      card.parentElement.insertBefore(placeholder, card.nextElementSibling);
      if (navigator.vibrate) navigator.vibrate(35);
    }, 180);
  }, { passive: true });

  board.addEventListener('touchmove', e => {
    const t = e.touches[0];
    if (!isTouchDragging) {
      if (Math.abs(t.clientX - touchStartX) > 10 || Math.abs(t.clientY - touchStartY) > 10) {
        clearTimeout(touchTimer);
      }
      return;
    }

    e.preventDefault(); // Lock screen scroll while dragging card
    handleAutoScroll(t.clientX);

    touchCard.style.transform = `translate3d(${t.clientX - touchStartX}px, ${t.clientY - touchStartY}px, 0)`;

    const elementBelow = document.elementFromPoint(t.clientX, t.clientY);
    if (!elementBelow) return;

    const targetList = elementBelow.closest('.bl-cards:not([data-feed])');
    if (targetList) {
      const after = cardsOf(targetList).filter(c => c !== touchCard).find(c => {
        const r = c.getBoundingClientRect();
        return t.clientY < r.top + r.height / 2;
      });
      if (after) targetList.insertBefore(placeholder, after);
      else targetList.appendChild(placeholder);
    }
  }, { passive: false });

  board.addEventListener('touchend', () => {
    clearTimeout(touchTimer);
    stopAutoScroll();
    if (!isTouchDragging || !touchCard) return;

    isTouchDragging = false;
    touchCard.classList.remove('dragging', 'touch-lifted