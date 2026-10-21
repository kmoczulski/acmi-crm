"""Redline between two documents (draft against draft, or a restated agreement against the original): word by word,
laid out with the newer document's line breaks, so a PDF that wraps lines differently doesn't show as changed.

Each change is numbered and carries who made it. Comparing two rounds that aren't next to each other, blame() walks
the rounds in between, so a change is credited to the round that made it, not just the newest one."""
import difflib
import html
import re
from bisect import bisect_right
from collections import Counter

MAX_WORDS = 200_000  # per side; past this the comparison would take too long for a page
CONTEXT = 40  # unchanged words kept visible on each side of a change


def split(text):
    """The words, and how many line breaks follow each one."""
    words, breaks, _ = tokens(text)
    return words, breaks


def tokens(text):
    """(words, line breaks after each, character offset of each)."""
    words, breaks, starts = [], [], []
    for m in re.finditer(r"\S+|\n", text or ""):
        if m.group(0) == "\n":
            if breaks:
                breaks[-1] += 1
        else:
            words.append(m.group(0))
            breaks.append(0)
            starts.append(m.start())
    return words, breaks, starts


def blame(texts):
    """For a run of versions oldest to newest: (origin, removed). origin[j] is the version that put word j of the
    newest text there (0 = it was in the first); removed[i] is the version that took out word i of the first."""
    cur = split(texts[0])[0]
    origin, first_idx, removed = [0] * len(cur), list(range(len(cur))), {}
    for v in range(1, len(texts)):
        nxt = split(texts[v])[0]
        new_origin, new_idx = [], []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, cur, nxt).get_opcodes():
            if tag == "equal":
                new_origin += origin[i1:i2]
                new_idx += first_idx[i1:i2]
                continue
            for i in range(i1, i2):
                if first_idx[i] is not None:
                    removed.setdefault(first_idx[i], v)
            new_origin += [v] * (j2 - j1)
            new_idx += [None] * (j2 - j1)
        cur, origin, first_idx = nxt, new_origin, new_idx
    return origin, removed


def redline(old, new, *, origin=None, removed=None, who=None, page_starts=None):
    """(html, stats): the newer text with deletions struck through and insertions underlined, each change in a
    numbered <span class="rl-chg"> carrying its author. Long unchanged stretches fold into a "… N unchanged words …"
    toggle; page_starts puts "Page n" rules where the newer document's pages begin.

    stats["items"] lists the changes: {"n", "deleted", "inserted", "start", "end" (where it sits in the newer text),
    "round" (the version that made it, from blame), "who", "cls"}. who(round, start, end) -> (label, css class)."""
    a, _, _ = tokens(old)
    b, breaks, starts = tokens(new)
    if len(a) > MAX_WORDS or len(b) > MAX_WORDS:
        return None, {"too_long": True}
    pages = list(page_starts or [])[1:]

    def page_rule(j):
        """A "Page n" rule before word j when a page starts between it and the word before."""
        if not pages or j == 0:
            return ""
        before, here = bisect_right(pages, starts[j - 1]), bisect_right(pages, starts[j])
        return "".join(f'<span class="rl-page" data-x>Page {p + 2}</span>' for p in range(before, here))

    def show(j1, j2):
        return "".join(page_rule(j) + html.escape(b[j] + ("\n" * min(breaks[j], 2) if breaks[j] else " "), quote=False)
                       for j in range(j1, j2))

    out, items = [], []
    stats = {"inserted": 0, "deleted": 0, "changes": 0, "items": items}
    ops = difflib.SequenceMatcher(None, a, b).get_opcodes()
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            size = j2 - j1
            head = 0 if n == 0 else min(CONTEXT, size)
            tail = 0 if n == len(ops) - 1 else min(CONTEXT, size - head)
            if size - head - tail > CONTEXT:
                out.append(f'{show(j1, j1 + head)}<details class="rl-same"><summary>… {size - head - tail:,} unchanged '
                           f'words …</summary>{show(j1 + head, j2 - tail)}</details>{show(j2 - tail, j2)}')
            else:
                out.append(show(j1, j2))
            continue
        stats["changes"] += 1
        start = starts[j1] if j1 < len(b) else len(new or "")
        end = starts[j2 - 1] + len(b[j2 - 1]) if j2 > j1 else start
        rounds = [origin[j] for j in range(j1, j2)] if origin else []
        rounds += [removed[i] for i in range(i1, i2) if removed and i in removed]
        rnd = Counter(r for r in rounds if r).most_common(1)[0][0] if any(rounds) else None
        label, cls = who(rnd, start, end) if who else ("", "")
        item = {"n": stats["changes"], "deleted": " ".join(a[i1:i2]), "inserted": " ".join(b[j1:j2]),
                "start": start, "end": end, "round": rnd, "who": label, "cls": cls}
        items.append(item)
        title = html.escape(label, quote=True)
        parts = [f'<span class="rl-chg {cls}" id="chg-{item["n"]}" data-n="{item["n"]}" data-o="{start}" '
                 f'title="{title}"><sup class="rl-n" data-x>{item["n"]}</sup>']
        if i2 > i1:
            parts.append(f"<del data-x>{html.escape(item['deleted'], quote=False)}</del> ")
            stats["deleted"] += i2 - i1
        if j2 > j1:
            added = show(j1, j2)
            parts.append(f"<ins>{added.rstrip()}</ins>")
            stats["inserted"] += j2 - j1
            parts.append("</span>" + added[len(added.rstrip()):])
        else:
            parts.append("</span>")
        out.append("".join(parts))
    return "".join(out).strip(), stats
