// Attachments on the compose page and the inline reply: every file you pick (or drop on the message) is kept, so
// picking again adds to the list instead of replacing it; the list shows each file with its size and a remove ×.
// The whole set goes back into the form's file input, so a plain submit and FormData(form) both send all of them.
(function () {
  const LIMIT = 25 * 1024 * 1024;  // Gmail's limit for what you attach (crm/mail/compose.py MAX_BYTES)
  const size = n => n < 1024 ? n + ' B' : n < 1048576 ? Math.round(n / 1024) + ' KB' : (n / 1048576).toFixed(1) + ' MB';

  function attach(input) {
    if (input.dataset.attachOn) return;
    input.dataset.attachOn = '1';
    const form = input.form;
    const list = document.createElement('ul');
    list.className = 'attach-list';
    list.setAttribute('aria-live', 'polite');
    input.closest('label').insertAdjacentElement('afterend', list);
    let files = [];

    function sync() {
      const dt = new DataTransfer();
      files.forEach(f => dt.items.add(f));
      input.files = dt.files;
      const total = files.reduce((n, f) => n + f.size, 0);
      list.replaceChildren(...files.map((f, i) => {
        const li = document.createElement('li');
        li.append('📎 ' + f.name + ' ');
        const s = document.createElement('span');
        s.className = 'muted';
        s.textContent = size(f.size);
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'btn-link';
        x.textContent = '×';
        x.title = x.ariaLabel = 'Remove ' + f.name;
        x.addEventListener('click', () => { files.splice(i, 1); sync(); });
        li.append(s, ' ', x);
        return li;
      }));
      if (files.length > 1 || total > LIMIT) {
        const li = document.createElement('li');
        li.className = total > LIMIT ? 'mail-error' : 'muted lc-small';
        li.textContent = `${files.length} files · ${size(total)}` + (total > LIMIT ? ' — over Gmail\'s 25 MB limit; remove some or share a Drive link' : '');
        list.append(li);
      }
      list.hidden = !files.length;
    }

    function add(picked) {
      for (const f of picked) {
        if (!files.some(g => g.name === f.name && g.size === f.size && g.lastModified === f.lastModified)) files.push(f);
      }
      sync();
    }

    input.addEventListener('change', () => add([...input.files]));
    // Drop files anywhere on the message.
    form.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); form.classList.add('attach-over'); } });
    form.addEventListener('dragleave', e => { if (!form.contains(e.relatedTarget)) form.classList.remove('attach-over'); });
    form.addEventListener('drop', e => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault();
      form.classList.remove('attach-over');
      add([...e.dataTransfer.files]);
    });
    // After a send or discard the inline reply is reused: start empty again.
    form.addEventListener('reset', () => { files = []; setTimeout(sync); });
    list.hidden = true;
  }

  const scan = () => document.querySelectorAll('.compose-attach input[type=file][name=files]').forEach(attach);
  scan();
  // The reading pane opens conversations in place (no page load), so watch for new reply boxes.
  new MutationObserver(scan).observe(document.body, {childList: true, subtree: true});
})();
