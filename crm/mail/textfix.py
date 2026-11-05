"""Repair "mojibake" in email text: "weâ€™re" -> "we’re".

It happens when a message's text was written as UTF-8 but labelled (or re-encoded) as Windows-1252 / Latin-1 on its way,
so every curly quote, dash and accent comes out as two or three odd characters. The repair only touches runs that look
exactly like UTF-8 bytes read as Windows-1252 (a lead character Â..ô followed by the characters those continuation bytes
turn into) and only when the run decodes back to valid UTF-8; anything else, including normal accented text, is left alone."""
import re


def _cp1252_char(byte):
    try:
        return bytes([byte]).decode("cp1252")
    except UnicodeDecodeError:  # 0x81, 0x8d, 0x8f, 0x90, 0x9d have no Windows-1252 character
        return chr(byte)


_CONTINUATION = "".join(sorted({_cp1252_char(b) for b in range(0x80, 0xC0)}))
_RUN = re.compile("(?:[Â-ô][" + re.escape(_CONTINUATION) + "]+)+")
_BACK = {}


def _byte_of(ch):
    if ch not in _BACK:
        try:
            _BACK[ch] = ch.encode("cp1252")
        except UnicodeEncodeError:
            _BACK[ch] = ch.encode("latin-1")
    return _BACK[ch]


def _fix_run(match):
    run = match.group()
    try:
        return b"".join(_byte_of(c) for c in run).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return run


def repair(text):
    """The text with any UTF-8-read-as-Windows-1252 runs put right. Cheap when there is nothing to fix."""
    if not text or not any(c in text for c in "ÂÃâ"):
        return text
    return _RUN.sub(_fix_run, text)
