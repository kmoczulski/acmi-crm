"""Redline between two documents (draft against draft, or a restated agreement against the original): word by word,
laid out with the newer document's line breaks, so a PDF that wraps lines differently doesn't show as changed."""
import difflib
import html
import re

MAX_WORDS = 200_000  # per side; past this the comparison would take too long for a page
CONTEXT = 40  # unchanged words kept visible on each side of a change


def split(text):
    """The words, and how many line breaks follow each one."""
    words, breaks = [], []
    for m in re.finditer(r"\S+|\n", text or ""):
        if m.group(0) == "\n":
            if breaks:
                breaks[-1] += 1
        else:
            words.append(m.group(0))
            breaks.append(0)
    return words, breaks


def redline(old, new):
    """(html, stats): the newer text with deletions struck through and insertions underlined. Long unchanged
    stretches fold into a "… N unchanged words …" toggle."""
    a, _ = split(old)
    b, breaks = split(new)
    if len(a) > MAX_WORDS or len(b) > MAX_WORDS:
        return None, {"too_long": True}

    def show(j1, j2):
        return html.escape("".join(b[j] + ("\n" * min(breaks[j], 2) if breaks[j] else " ") for j in range(j1, j2)),
                           quote=False)

    out, stats = [], {"inserted": 0, "deleted": 0, "changes": 0}
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
        if i2 > i1:
            out.append(f"<del>{html.escape(' '.join(a[i1:i2]), quote=False)}</del> ")
            stats["deleted"] += i2 - i1
        if j2 > j1:
            added = show(j1, j2)
            out.append(f"<ins>{added.rstrip()}</ins>{added[len(added.rstrip()):]}")
            stats["inserted"] += j2 - j1
    return "".join(out).strip(), stats
