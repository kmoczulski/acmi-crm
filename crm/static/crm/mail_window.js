// A conversation in the mail dock's little box (mail_window.html): size each message to its text, offer Show images
// when the server held some back, and open Reply / Reply all / Forward in their own boxes on the page around this one.
(function () {
  const dock = () => { try { return window.top !== window ? window.top.acmiDock : null; } catch (e) { return null; } };

  function fit(frame) {
    try {
      const doc = frame.contentDocument;
      if (!doc || !doc.body) return;
      frame.style.height = '0px';
      frame.style.height = Math.max(40, doc.body.scrollHeight + 12) + 'px';
      const offer = frame.parentElement.querySelector('.mail-images');
      if (offer) offer.hidden = !doc.querySelectorAll('img[data-blocked]').length;
    } catch (e) { /* not loaded yet */ }
  }
  document.querySelectorAll('[data-mail-frame]').forEach(frame => {
    frame.addEventListener('load', () => {
      fit(frame);
      setTimeout(() => fit(frame), 400);
      try { frame.contentDocument.addEventListener('toggle', () => fit(frame), true); } catch (e) { /* not ours */ }
    });
    const details = frame.closest('details');
    if (details) details.addEventListener('toggle', () => fit(frame));
  });
  document.querySelectorAll('[data-show-images]').forEach(btn => btn.addEventListener('click', () => {
    const frame = btn.closest('details').querySelector('[data-mail-frame]');
    frame.src = frame.src.split('?')[0] + '?images=1';
    btn.closest('.mail-images').hidden = true;
  }));

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-dock-reply]');
    const d = dock();
    if (!b || !d) return;  // without a dock around it the link opens the full compose page
    e.preventDefault();
    d.compose({reply: b.dataset.dockReply, mode: b.dataset.mode});
  });

  const d = dock(), id = document.body.dataset.window;
  if (d && id) d.title(id, document.body.dataset.windowTitle);
})();
