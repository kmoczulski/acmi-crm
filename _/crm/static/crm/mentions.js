// @mention picker for comment boxes: textarea[data-mentions] gets a list of teammates while typing "@na…".
// The people come from a json_script with id "cm-users": [{"handle": "simon", "name": "Simon Shewmaker"}].
(function () {
  const src = document.getElementById('cm-users');
  if (!src) return;
  const users = JSON.parse(src.textContent);
  const menu = document.createElement('ul');
  menu.className = 'mention-menu';
  menu.hidden = true;
  document.body.appendChild(menu);
  let box = null, at = -1, picked = 0, shown = [];

  function close() { menu.hidden = true; box = null; }
  function choose(u) {
    const v = box.value, caret = box.selectionStart;
    box.value = v.slice(0, at) + '@' + u.handle + ' ' + v.slice(caret);
    const pos = at + u.handle.length + 2;
    box.setSelectionRange(pos, pos);
    box.focus();
    close();
  }
  function render() {
    menu.replaceChildren(...shown.map((u, i) => {
      const li = document.createElement('li');
      li.className = i === picked ? 'on' : '';
      li.innerHTML = '<b></b> <span class="muted"></span>';
      li.querySelector('b').textContent = '@' + u.handle;
      li.querySelector('span').textContent = u.name;
      li.addEventListener('mousedown', e => { e.preventDefault(); choose(u); });
      return li;
    }));
  }
  document.addEventListener('input', e => {
    const t = e.target;
    if (!t.matches || !t.matches('textarea[data-mentions]')) return;
    const before = t.value.slice(0, t.selectionStart);
    const m = before.match(/(^|\s)@([\w.-]*)$/);
    if (!m) { close(); return; }
    const q = m[2].toLowerCase();
    shown = users.filter(u => u.handle.startsWith(q) || u.name.toLowerCase().includes(q)).slice(0, 8);
    if (!shown.length) { close(); return; }
    box = t; at = before.length - m[2].length - 1; picked = 0;
    render();
    const r = t.getBoundingClientRect();
    menu.style.left = (window.scrollX + r.left) + 'px';
    menu.style.top = (window.scrollY + r.bottom + 2) + 'px';
    menu.hidden = false;
  });
  document.addEventListener('keydown', e => {
    if (menu.hidden || e.target !== box) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      picked = (picked + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
      render();
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      choose(shown[picked]);
    } else if (e.key === 'Escape') {
      close();
    }
  });
  document.addEventListener('focusout', e => { if (e.target === box) setTimeout(close, 150); });
})();
