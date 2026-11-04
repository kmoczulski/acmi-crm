// Split view: a second hub page beside this one (Mail next to the Land map, say). The page you're on stays the page;
// the other side is a same-origin frame of any hub page, drawn without the top bar (?embed=1, EMBED in
// context_processors.py). Drag the divider to resize, ⇄ swaps the two pages, ⤢ keeps only the side page, ✕ closes
// the side. ⬓ stacks them top and bottom (a portrait monitor). Alt+click a link to open it in the other side. Remembered in this browser (localStorage hubSplit:
// {open, url, w, h, dir}); the head script in base.html applies it before the first paint. Phones get one page.
(function () {
  const KEY = 'hubSplit', MIN_SIDE = 320, MIN_MAIN = 360, MIN_H = 200, WIDE = 900, NARROW = 1230;  // NARROW: app.css's ☰ width
  const root = document.documentElement;
  const here = () => location.pathname + location.search + location.hash;
  const strip = url => {
    const u = new URL(url, location.origin);
    u.searchParams.delete('embed');
    return u.pathname + u.search + u.hash;
  };
  const sameOrigin = a => a && a.href && a.origin === location.origin && !a.hasAttribute('download') &&
    !/^(mailto|tel|javascript):/.test(a.getAttribute('href') || '') && !(a.getAttribute('href') || '').startsWith('#');

  // Inside the side frame: Alt+click opens the link on the main side.
  if (window.top !== window) {
    document.addEventListener('click', e => {
      const a = e.target.closest && e.target.closest('a[href]');
      if (!e.altKey || !sameOrigin(a)) return;
      try { if (!window.parent.hubSplit) return; } catch (err) { return; }
      e.preventDefault();
      window.parent.hubSplit.openMain(strip(a.href));
    }, true);
    return;
  }

  const pane = document.getElementById('split-pane');
  if (!pane) return;
  const frame = pane.querySelector('.split-frame');
  const pick = pane.querySelector('.split-pick');
  const title = pane.querySelector('.split-title');
  const handle = pane.querySelector('.split-handle');
  const wide = () => innerWidth > WIDE;

  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || 'null') || {}; } catch (e) { return {}; } };
  let state = Object.assign({open: false, url: '', w: Math.round(innerWidth * 0.45), h: Math.round(innerHeight * 0.5), dir: 'h'}, load());
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };

  // Where the side frame is now (the inbox moves between conversations with pushState, so read it live).
  function sideUrl() {
    try {
      const l = frame.contentWindow.location;
      if (l.href !== 'about:blank' && l.origin === location.origin) return strip(l.href);
    } catch (e) { /* left the hub */ }
    return state.url;
  }
  let leaving = false;  // swap / keep-only have saved the next page's layout: leaving mustn't overwrite it
  const remember = () => { if (state.open && !leaving) { state.url = sideUrl(); save(); } };

  function clampW(w) {
    return Math.round(Math.max(MIN_SIDE, Math.min(w, innerWidth - MIN_MAIN)));
  }
  const stacked = () => state.dir === 'v';
  function clampH(h) {
    return Math.round(Math.max(MIN_H, Math.min(h, innerHeight - MIN_H)));
  }
  function setW(w) {
    state.w = clampW(w);
    root.style.setProperty('--split-w', state.w + 'px');
    root.classList.toggle('split-narrow', !stacked() && innerWidth - state.w <= NARROW);
  }
  function setH(h) {
    state.h = clampH(h);
    root.style.setProperty('--split-h', state.h + 'px');
  }
  // Apply the current direction and size.
  function layout() {
    root.classList.toggle('split-v', stacked());
    pane.classList.toggle('split-pane-v', stacked());
    handle.setAttribute('aria-orientation', stacked() ? 'horizontal' : 'vertical');
    setW(state.w); setH(state.h);
  }
  // Maps and other width-aware pages on the main side only hear about window resizes.
  let nudge = null;
  const relayout = () => { cancelAnimationFrame(nudge); nudge = requestAnimationFrame(() => window.dispatchEvent(new Event('resize'))); };

  function show(url) {
    if (!wide()) { location.href = url; return; }  // a phone: one page at a time
    state.open = true;
    state.url = strip(url);
    layout();
    root.classList.add('split-on');
    pane.hidden = false;
    const src = new URL(state.url, location.origin);
    src.searchParams.set('embed', '1');
    if (frame.getAttribute('src') !== src.pathname + src.search + src.hash) frame.setAttribute('src', src.pathname + src.search + src.hash);
    save();
    relayout();
  }
  function close() {
    remember();
    state.open = false;
    save();
    pane.hidden = true;
    root.classList.remove('split-on', 'split-narrow', 'split-v');
    frame.removeAttribute('src');
    title.textContent = '';
    relayout();
  }
  function defaultUrl() {
    if (state.url) return state.url;
    const pages = JSON.parse(document.getElementById('nav-pages')?.textContent || '[]');
    return (pages.find(p => p.url.startsWith('/inbox/')) || pages[0] || {url: '/'}).url;
  }

  window.hubSplit = {
    open: show,
    close,
    // Load a page on the main side, keeping the side as it is.
    openMain(url) { remember(); location.href = url; },
    toggle() { if (pane.hidden) show(defaultUrl()); else close(); },
  };

  // The page picker: every page the search knows, by section.
  const pages = JSON.parse(document.getElementById('nav-pages')?.textContent || '[]');
  const sections = {};
  pages.forEach(p => { (sections[p.section] = sections[p.section] || []).push(p); });
  Object.entries(sections).forEach(([name, list]) => {
    const g = document.createElement('optgroup');
    g.label = name;
    list.forEach(p => { const o = document.createElement('option'); o.value = p.url; o.textContent = p.label; g.appendChild(o); });
    pick.appendChild(g);
  });
  const here_ = document.createElement('option');
  here_.value = '@here'; here_.textContent = 'This page (a second copy)';
  pick.options[0].after(here_);
  pick.addEventListener('change', () => {
    if (pick.value) show(pick.value === '@here' ? here() : pick.value);
    pick.value = '';
  });

  frame.addEventListener('load', () => {
    if (pane.hidden) return;
    let doc = null;
    try { doc = frame.contentDocument; } catch (e) { doc = null; }
    title.textContent = doc ? (doc.title || '').replace(/ · ACMI Hub$/, '') : '';
    title.title = title.textContent;
    remember();
  });

  pane.querySelector('.split-bar').addEventListener('click', e => {
    const act = e.target.closest('[data-split]')?.dataset.split;
    if (!act) return;
    if (act === 'close') close();
    else if (act === 'reload') { try { frame.contentWindow.location.reload(); } catch (err) { show(state.url); } }
    else if (act === 'back') { try { frame.contentWindow.history.back(); } catch (err) {} }
    else if (act === 'orient') {
      state.dir = stacked() ? 'h' : 'v'; save(); layout(); relayout();
    } else if (act === 'swap') {
      const side = sideUrl();
      state.url = strip(here()); state.open = true; save();
      leaving = true; location.href = side;
    } else if (act === 'only') {
      const side = sideUrl();
      state.url = side; state.open = false; save();
      leaving = true; location.href = side;
    }
  });

  // The divider: drag, or focus it and use the arrow keys.
  handle.addEventListener('pointerdown', e => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    root.classList.add('split-dragging');
    const move = ev => { if (stacked()) setH(innerHeight - ev.clientY); else setW(innerWidth - ev.clientX); relayout(); };
    const up = () => {
      root.classList.remove('split-dragging');
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      save();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('keydown', e => {
    const step = e.shiftKey ? 80 : 20;
    if (stacked()) {
      if (e.key === 'ArrowUp') setH(state.h + step);
      else if (e.key === 'ArrowDown') setH(state.h - step);
      else return;
    } else if (e.key === 'ArrowLeft') setW(state.w + step);
    else if (e.key === 'ArrowRight') setW(state.w - step);
    else return;
    e.preventDefault(); save(); relayout();
  });

  document.querySelectorAll('[data-split-toggle]').forEach(b => b.addEventListener('click', () => window.hubSplit.toggle()));

  // Alt+click a hub link: open it beside.
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!e.altKey || !sameOrigin(a) || !wide()) return;
    e.preventDefault();
    show(a.href);
  }, true);

  // Too narrow for two pages: the side hides (and comes back when there's room again).
  addEventListener('resize', () => {
    if (!state.open) return;
    if (!wide()) { pane.hidden = true; root.classList.remove('split-on', 'split-narrow', 'split-v'); }
    else { if (pane.hidden) show(state.url); else layout(); }
  });
  addEventListener('pagehide', remember);

  if (state.open && state.url && wide()) show(state.url);
  else root.classList.remove('split-on', 'split-narrow', 'split-v');
})();
