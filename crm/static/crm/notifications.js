// Notifications page: the × on a row marks just that alert read, without opening it (views.notification_dismiss).
// Without script the form still posts and the page comes back. With script the row leaves the Unread list at once
// (the All list keeps it, now read) and the bell's count follows the answer.
(function () {
  const list = document.querySelector('[data-notif-list]');
  if (!list) return;
  const unreadOnly = list.dataset.show === 'unread';
  const empty = list.querySelector('[data-notif-empty]');
  const say = list.querySelector('[data-notif-status]');
  const calm = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);

  // The bell is in this page's top bar, or in the page around it when this one is the split view's side.
  function bell(unread) {
    try { const a = window.top.acmiAlerts; if (a && a.setBadge) a.setBadge(unread); } catch (e) { /* not our window */ }
  }

  // The row to hand the keyboard to when this one goes: the next one that is staying, else the one before it.
  function neighbour(row) {
    const staying = r => r.matches('[data-notif-row]:not(.leaving)');
    for (let r = row.nextElementSibling; r; r = r.nextElementSibling) if (staying(r)) return r;
    for (let r = row.previousElementSibling; r; r = r.previousElementSibling) if (staying(r)) return r;
    return null;
  }

  function leave(row, unread) {
    const next = neighbour(row);
    row.remove();
    if (next) { (next.querySelector('.notif-x') || next.querySelector('.notif')).focus(); return; }
    if (list.querySelector('[data-notif-row]')) return;  // others are still fading out: the last one finishes the job
    if (unread > 0) { location.reload(); return; }  // the unread ones that are left are on another page: fetch them
    empty.hidden = false;
    const pager = list.querySelector('.pager');
    if (pager) pager.remove();
    empty.focus();
  }

  list.addEventListener('submit', async e => {
    const form = e.target.closest('[data-notif-dismiss]');
    if (!form) return;
    e.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = '1';
    const row = form.closest('[data-notif-row]');
    let answer = null;
    try {
      const r = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin',
                                          headers: {'X-Requested-With': 'fetch'}});
      answer = r.ok ? await r.json() : null;
    } catch (err) { answer = null; }
    if (!answer || !answer.ok) { form.submit(); return; }  // not understood (signed out, offline): the plain post sorts it out
    bell(answer.unread);
    if (say) say.textContent = 'Marked as read.';
    if (unreadOnly) {
      if (calm) { leave(row, answer.unread); return; }
      row.classList.add('leaving');
      setTimeout(() => leave(row, answer.unread), 180);
      return;
    }
    const link = row.querySelector('.notif');
    link.classList.remove('unread');  // the All list keeps the row, now read
    form.remove();
    link.focus();
  });
})();
