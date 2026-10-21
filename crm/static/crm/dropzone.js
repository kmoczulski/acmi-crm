// Drag-and-drop upload for contracts: [data-dropzone] takes files dropped on it (or anywhere on the page while it's
// the only zone) or picked with its button, sends them one at a time, and lists what Claude made of each.
// data-url: where each file goes; data-mode "new": the first file starts a new contract (the response's url) and the
// rest are added to it.
(function () {
  const zone = document.querySelector('[data-dropzone]');
  if (!zone) return;
  const input = zone.querySelector('input[type=file]');
  const list = zone.querySelector('.dz-list');
  const token = (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';
  let busy = false;

  function row(name) {
    const li = document.createElement('li');
    li.className = 'dz-row working';
    li.innerHTML = '<span class="dz-name"></span><span class="dz-state">Reading…</span><div class="dz-what lc-small"></div>';
    li.querySelector('.dz-name').textContent = name;
    list.appendChild(li);
    return li;
  }

  function done(li, r) {
    li.classList.remove('working');
    if (!r.ok) {
      li.classList.add('failed');
      li.querySelector('.dz-state').textContent = r.error || 'Could not upload';
      return;
    }
    li.classList.add('ok');
    li.querySelector('.dz-state').textContent = '✓';
    const bits = [r.kind, r.title, r.dated, r.by && 'by ' + r.by, r.pages && r.pages + ' page' + (r.pages === 1 ? '' : 's')];
    let what = bits.filter(Boolean).join(' · ');
    if (r.filled && r.filled.length) what += ' — Claude filled in ' + r.filled.join(' and ') + '.';
    else if (r.note) what += ' — ' + r.note;
    if (r.scan) what += ' Scan with no text layer: Claude can read it, the page view can’t.';
    li.querySelector('.dz-what').textContent = what;
  }

  async function send(files) {
    if (busy || !files.length) return;
    busy = true;
    zone.classList.add('dz-busy');
    let url = zone.dataset.url, target = null;
    for (const file of files) {
      const li = row(file.name);
      const body = new FormData();
      body.append('file', file);
      if (zone.dataset.site) body.append('site', zone.dataset.site);
      let r;
      try {
        const res = await fetch(url, {method: 'POST', headers: {'X-CSRFToken': token}, body});
        r = await res.json().catch(() => ({ok: false, error: 'Server error (' + res.status + ')'}));
      } catch (e) {
        r = {ok: false, error: 'Upload failed: check the connection'};
      }
      done(li, r);
      if (r.ok && zone.dataset.mode === 'new' && !target) {
        target = r.url;
        url = r.url + 'drop/';  // the rest of the files go into the contract the first one started
      }
    }
    busy = false;
    zone.classList.remove('dz-busy');
    const ok = list.querySelectorAll('.dz-row.ok').length;
    if (!ok) return;
    const go = target || (location.pathname + '#documents');
    const next = document.createElement('a');
    next.className = 'btn btn-primary btn-small dz-next';
    next.href = go;
    next.textContent = target ? 'Open the contract' : 'Show them in the file';
    list.appendChild(next);
    // Give a moment to read what Claude found, then go there.
    setTimeout(() => { if (target) location.href = target; else { location.hash = 'documents'; location.reload(); } }, 2500);
  }

  zone.querySelector('[data-pick]').addEventListener('click', () => input.click());
  input.addEventListener('change', () => { send([...input.files]); input.value = ''; });
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  document.addEventListener('dragenter', e => { if (hasFiles(e)) { depth++; zone.classList.add('dz-over'); } });
  document.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; zone.classList.remove('dz-over'); } });
  document.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    zone.classList.remove('dz-over');
    send([...e.dataTransfer.files]);
  });
})();
