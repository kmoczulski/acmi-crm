// LOI / PSA terms: the edit page asks the server what the terms as typed produce, and shows it beside the form.
// The server does all the arithmetic (crm/contracts/terms.py), so the preview can never disagree with the saved page.
(function () {
  const form = document.getElementById('terms-form');
  const box = document.getElementById('terms-preview');
  if (!form || !box || !form.dataset.preview) return;
  let timer = null;
  let latest = 0;

  function refresh() {
    const mine = ++latest;
    fetch(form.dataset.preview, {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
      .then(r => (r.ok ? r.text() : Promise.reject(r.status)))
      .then(html => { if (mine === latest) box.innerHTML = html; })
      .catch(() => { if (mine === latest) box.innerHTML = '<p class="muted">The preview isn\'t available right now. Saving still works.</p>'; });
  }

  function soon() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 350);
  }

  form.addEventListener('input', soon);
  form.addEventListener('change', soon);
  refresh();
})();
