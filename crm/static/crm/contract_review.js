// Contract reader and redline: the sidebar tabs, commenting on selected words, and linking comment cards to the
// words they're pinned to. In the reader each page's text (.cr-text, data-ps = where the page starts) is the
// document's own text, so a selection is counted back to exact offsets; anything added on top carries data-x and is
// skipped. In a redline (approx) the server finds the quote instead, near the change before it (data-o).
window.ContractReview = (function () {
  function tab(name) {
    document.querySelectorAll('.cr-tabs .tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
    document.querySelectorAll('.cr-pane').forEach(p => { p.hidden = p.dataset.pane !== name; });
  }

  function textNodes(root) {
    return document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: t => t.parentElement.closest('[data-x]') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
  }

  // Character offset in the document text of a selection boundary, or null when it isn't inside a page.
  function offsetOf(node, off) {
    const el = node.nodeType === 3 ? node.parentElement : node;
    const page = el && el.closest('.cr-text');
    if (!page) return null;
    let probe = node, inText = node.nodeType === 3;
    if (!inText) {
      if (off < node.childNodes.length) {
        probe = node.childNodes[off];
      } else {
        const w = textNodes(node);
        let last = null;
        while (w.nextNode()) last = w.currentNode;
        if (!last) return null;
        probe = last; off = last.length; inText = true;
      }
    }
    let n = Number(page.dataset.ps);
    const w = textNodes(page);
    while (w.nextNode()) {
      const t = w.currentNode;
      if (t === probe) return n + (inText ? off : 0);
      const rel = probe.compareDocumentPosition(t);
      if (rel & Node.DOCUMENT_POSITION_CONTAINED_BY) return n;
      if ((rel & Node.DOCUMENT_POSITION_FOLLOWING) && !(rel & Node.DOCUMENT_POSITION_CONTAINS)) return n;
      n += t.length;
    }
    return n;
  }

  function flash(els) {
    els.forEach(e => { e.classList.remove('flash'); void e.offsetWidth; e.classList.add('flash'); });
  }
  function marksFor(id) {
    return [...document.querySelectorAll('.cm-mark')].filter(m => m.dataset.c.split(' ').includes(String(id)));
  }
  function goToMark(id) {
    const marks = marksFor(id);
    const change = document.querySelector(`.rl-chg[data-n="${id}"]`);
    const first = marks[0] || change;
    if (first) { first.scrollIntoView({block: 'center', behavior: 'smooth'}); flash(marks.length ? marks : [first]); }
  }
  function goToCard(id) {
    tab('comments');
    const card = document.getElementById('c-' + id);
    if (card) { card.scrollIntoView({block: 'nearest', behavior: 'smooth'}); flash([card]); }
  }

  function init(opts) {
    const doc = opts.doc;
    const approx = !!opts.approx;
    document.querySelectorAll('.cr-tabs .tab').forEach(t => t.addEventListener('click', () => tab(t.dataset.tab)));
    const form = document.getElementById('cm-new');
    const add = document.getElementById('cm-add');
    let pending = null;

    if (doc && form && add) {
      doc.addEventListener('mouseup', () => setTimeout(() => {
        const sel = window.getSelection();
        if (!sel.rangeCount || sel.isCollapsed || !doc.contains(sel.anchorNode)) { add.hidden = true; return; }
        const quote = sel.toString().trim();
        if (quote.length < 2) { add.hidden = true; return; }
        const range = sel.getRangeAt(0);
        let hint = 0;
        if (approx) {
          const marks = [...doc.querySelectorAll('[data-o]')].filter(
            m => m.compareDocumentPosition(range.startContainer) & Node.DOCUMENT_POSITION_FOLLOWING);
          if (marks.length) hint = marks[marks.length - 1].dataset.o;
        }
        pending = {quote: quote.slice(0, 2000), hint,
                   start: approx ? '' : offsetOf(range.startContainer, range.startOffset),
                   end: approx ? '' : offsetOf(range.endContainer, range.endOffset)};
        const r = range.getBoundingClientRect();
        add.style.top = (window.scrollY + r.top - 34) + 'px';
        add.style.left = (window.scrollX + r.left + r.width / 2 - 50) + 'px';
        add.hidden = false;
      }, 0));
      add.addEventListener('mousedown', e => e.preventDefault());
      add.addEventListener('click', () => {
        if (!pending) return;
        form.elements.start.value = pending.start ?? '';
        form.elements.end.value = pending.end ?? '';
        form.elements.quote.value = pending.quote;
        if (form.elements.hint) form.elements.hint.value = pending.hint;
        form.querySelector('.cm-pop-quote').textContent = '“' + pending.quote.slice(0, 200) + (pending.quote.length > 200 ? '…' : '') + '”';
        form.style.top = add.style.top;
        form.style.left = Math.max(8, Math.min(parseFloat(add.style.left) - 120,
                                               window.scrollX + document.documentElement.clientWidth - 340)) + 'px';
        form.hidden = false;
        add.hidden = true;
        form.querySelector('textarea').focus();
      });
      form.querySelector('[data-close]').addEventListener('click', () => { form.hidden = true; });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') { form.hidden = true; add.hidden = true; } });
    }

    document.addEventListener('click', e => {
      const mark = e.target.closest('.cm-mark');
      if (mark && !e.target.closest('a, button')) { goToCard(mark.dataset.c.split(' ')[0]); return; }
      const go = e.target.closest('[data-go]');
      if (go) goToMark(go.dataset.go);
      const chg = e.target.closest('[data-goto-change]');
      if (chg) goToMark(chg.dataset.gotoChange);
    });
    document.querySelectorAll('.cm-card[data-c]').forEach(card => {
      card.addEventListener('mouseenter', () => marksFor(card.dataset.c).forEach(m => m.classList.add('hover')));
      card.addEventListener('mouseleave', () => marksFor(card.dataset.c).forEach(m => m.classList.remove('hover')));
    });
    const m = location.hash.match(/^#c-(\d+)$/);
    if (m) setTimeout(() => { goToCard(m[1]); goToMark(m[1]); }, 50);
  }

  // Summaries: any paragraph, bullet or heading that rests on a citation opens it when clicked, not just the chip.
  function citable(root) {
    (root || document).querySelectorAll('.ct-a, .answer-body').forEach(box => {
      box.querySelectorAll('p, li, h4, td').forEach(block => {
        if (!block.querySelector('a.cite')) return;
        block.classList.add('citable');
        block.addEventListener('click', e => {
          if (e.target.closest('a, button') || window.getSelection().toString()) return;
          e.stopPropagation();
          const chips = block.querySelectorAll('a.cite');
          // The chip nearest after the click: the citation for the words clicked.
          let chip = chips[chips.length - 1];
          for (const c of chips) {
            if (c.compareDocumentPosition(e.target) & Node.DOCUMENT_POSITION_PRECEDING) { chip = c; break; }
          }
          chip.click();
        });
      });
    });
  }

  // The citation pop-up (on pages that don't have their own): the quoted language in place, with a link to it.
  function citePop() {
    const pop = document.createElement('div');
    pop.className = 'cite-pop';
    pop.hidden = true;
    pop.innerHTML = '<div class="cite-where"></div><blockquote class="cite-quote"></blockquote>' +
                    '<a class="cite-open btn btn-small" href="#">Open in the reader</a>';
    document.body.appendChild(pop);
    document.addEventListener('click', e => {
      const chip = e.target.closest('a.cite');
      if (!chip) { if (!e.target.closest('.cite-pop')) pop.hidden = true; return; }
      if (e.metaKey || e.ctrlKey) return;
      e.preventDefault();
      pop.querySelector('.cite-where').textContent = '[' + chip.dataset.n + '] ' + chip.dataset.where;
      pop.querySelector('.cite-quote').textContent = chip.dataset.quote || '(no quote)';
      pop.querySelector('.cite-open').href = chip.href;
      const r = chip.getBoundingClientRect();
      pop.hidden = false;
      pop.style.top = (window.scrollY + r.bottom + 6) + 'px';
      pop.style.left = Math.max(8, Math.min(window.scrollX + r.left - 20,
                                            window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8)) + 'px';
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') pop.hidden = true; });
  }

  return {tab, init, citable, citePop, offsetOf};
})();
