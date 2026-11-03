"""The email editor's plain-text format -> the email's HTML and plain-text bodies.

Write plain text; a blank line starts a new paragraph. Marks: **bold**, _italic_, [link text](https://...),
lines starting "- " are bullets, "1. " numbered. {greeting} {first_name} {company} {sector} pass through untouched."""
import html
import re

LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
ITALIC = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S)
BULLET = re.compile(r"^\s*[-*•]\s+")
NUMBER = re.compile(r"^\s*\d+[.)]\s+")


def _inline(text):
    links = []

    def stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00{len(links) - 1}\x00"

    out = html.escape(LINK.sub(stash, text), quote=False)
    out = BOLD.sub(r"<strong>\1</strong>", out)
    out = ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: '<a href="%s">%s</a>' % (
        html.escape(links[int(m.group(1))][1]), html.escape(links[int(m.group(1))][0], quote=False)), out)


def _blocks(src):
    return [b for b in re.split(r"\n\s*\n", (src or "").replace("\r\n", "\n").strip()) if b.strip()]


def to_html(src):
    parts = []
    for block in _blocks(src):
        lines = block.split("\n")
        if all(BULLET.match(x) for x in lines):
            parts.append("<ul>" + "".join(f"<li>{_inline(BULLET.sub('', x))}</li>" for x in lines) + "</ul>")
        elif all(NUMBER.match(x) for x in lines):
            parts.append("<ol>" + "".join(f"<li>{_inline(NUMBER.sub('', x))}</li>" for x in lines) + "</ol>")
        else:
            parts.append("<p>" + "<br>".join(_inline(x) for x in lines) + "</p>")
    return "".join(parts)


def to_plain(src):
    out = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", (src or "").replace("\r\n", "\n"))
    out = BOLD.sub(r"\1", out)
    return ITALIC.sub(r"\1", out).strip()


def from_html(markup):
    """Best-effort reverse of to_html for templates written as raw HTML, to start the editor from."""
    s = markup or ""
    s = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", s, flags=re.S | re.I)
    s = re.sub(r"</?(strong|b)\b[^>]*>", "**", s, flags=re.I)
    s = re.sub(r"</?(em|i)\b[^>]*>", "_", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|ul|ol|div)>", "\n\n", s, flags=re.I)
    s = re.sub(r"</li>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()
