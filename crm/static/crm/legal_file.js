// The "File to" picker for legal documents: type a tenant, a deal or a project, pick it from the list.
//  - A picker that is a form ([data-filepick][data-submit]) files its agreement the moment you pick.
//  - A picker beside the drop zone ([data-filepick][data-drop]) keeps its picks as chips; dropzone.js sends them with
//    every file, so the agreement is filed as it is made.
// The list comes from data-options (crm/contracts/filing_views.py): tenants and deals from two letters, and with
// nothing typed the projects and campuses, which are few enough to choose from.
(function () {
  const LABEL = {company: 'Tenant', opportunity: 'Deal', project: 'Project', site: 'Site', campus: 'Campus'};

  document.querySelectorAll('[data-filepick]').forEach(root => {
    const q = root.querySelector('.lg-q');
    const list = root.querySelector('.lg-opts');
    const chips = root.querySelector('.lg-chips');
    if (!q || !list) return;
    let timer = null, seq = 0, at = -1;

    function close() { list.hidden = true; at = -1; }

    function mark(i) {
      const buttons = [...list.querySelectorAll('button')];
      buttons.forEach((b, n) => b.classList.toggle('on', n === i));
      at = i;
      if (buttons[i]) buttons[i].scrollIntoView({block: 'nearest'});
    }

    function render(found) {
      list.innerHTML = '';
      found.forEach(o => {
        const li = document.createElement('li');
        const b = document.createElement('button');
        b.type = 'button';
        b.innerHTML = '<span></span><span class="lg-d"></span>';
        b.firstChild.textContent = o.name;
        b.lastChild.textContent = o.detail;
        b.addEventListener('mousedown', e => e.preventDefault());  // keep the box focused until the click lands
        b.addEventListener('click', () => pick(o));
        li.appendChild(b);
        list.appendChild(li);
      });
      if (!found.length) {
        const li = document.createElement('li');
        li.className = 'lg-none';
        li.textContent = 'Nothing matches.';
        list.appendChild(li);
      }
      at = -1;
      list.hidden = false;
    }

    async function search() {
      const text = q.value.trim();
      if (text.length === 1) { close(); return; }
      const mine = ++seq;
      try {
        const res = await fetch(root.dataset.options + '?q=' + encodeURIComponent(text),
                                {credentials: 'same-origin', headers: {Accept: 'application/json'}});
        const data = await res.json();
        if (mine === seq) render(data.options || []);
      } catch (e) {
        if (mine === seq) close();
      }
    }

    function field(kind, pk) {  // one hidden input per kind of place
      let input = root.querySelector('input[type=hidden][data-field="' + kind + '"]');
      if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = kind;
        input.dataset.field = kind;
        root.appendChild(input);
      }
      input.value = pk;
    }

    function addChip(o) {
      if (!chips) return;
      const old = chips.querySelector('[data-kind="' + o.kind + '"]');
      if (old) old.remove();
      const chip = document.createElement('span');
      chip.className = 'lg-chip';
      chip.dataset.kind = o.kind;
      chip.innerHTML = '<span class="lg-k"></span><b></b><button type="button" aria-label="Take it off">×</button>';
      chip.querySelector('.lg-k').textContent = LABEL[o.kind] + ' ';
      chip.querySelector('b').textContent = o.name;
      chip.querySelector('button').addEventListener('click', () => {
        const input = root.querySelector('input[type=hidden][data-field="' + o.kind + '"]');
        if (input) input.remove();
        chip.remove();
      });
      chips.appendChild(chip);
    }

    function pick(o) {
      field(o.kind, o.pk);
      q.value = '';
      close();
      if (root.hasAttribute('data-submit')) {
        const form = root.tagName === 'FORM' ? root : root.closest('form');
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
        return;
      }
      addChip(o);
      q.focus();
    }

    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 160); });
    q.addEventListener('focus', () => { if (!q.value) search(); });
    q.addEventListener('keydown', e => {
      const buttons = list.querySelectorAll('button');
      if (e.key === 'Escape') { close(); return; }
      if (list.hidden || !buttons.length) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); mark(Math.min(buttons.length - 1, at + 1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); mark(Math.max(0, at - 1)); }
      else if (e.key === 'Enter') { e.preventDefault(); buttons[Math.max(0, at)].click(); }
    });
    document.addEventListener('click', e => { if (!root.contains(e.target)) close(); });
  });
})();
