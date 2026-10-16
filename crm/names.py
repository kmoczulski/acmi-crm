"""Person names: cleaning, validation, and where to find a missing one."""
import re

ROLE_WORDS = {
    "sales", "info", "support", "team", "admin", "office", "contact", "hello", "noreply", "no-reply", "notifications",
    "notification", "billing", "accounts", "service", "services", "help", "marketing", "hr", "careers", "jobs",
    "mailer", "daemon", "postmaster", "inquiries", "inquiry", "orders", "reception", "general", "aog", "media",
    "press", "news", "newsletter", "webmaster", "customer", "customerservice", "operations", "procurement",
    "business", "development", "bd", "bizdev", "investor", "investors", "relations", "ir", "partnerships",
    "partners", "accounting", "purchasing", "contracts", "engineering", "quality", "legal", "finance", "events",
    "recruiting", "talent", "security", "it", "facilities", "government", "federal", "programs", "estimating",
    "development", "relations",
}
PLACEHOLDER_NAMES = {"tbd", "tba", "n/a", "na", "none", "unknown", "null", "-", "?", "test", "contact", "x"}
LOWER_PARTICLES = {"van", "von", "de", "der", "da", "di", "del", "la", "le", "du"}


def is_role_address(email):
    local = (email or "").split("@")[0].lower()
    return bool(local) and (local in ROLE_WORDS or re.split(r"[._+-]", local)[0] in ROLE_WORDS)


GENERIC_NAMES = {"mail", "email", "e-mail", "inbox", "hello", "hi", "team", "office", "info", "sales", "admin"}


def needs_name(person):
    name = (person.full_name or "").strip()
    lowered = name.lower()
    return (not name or "@" in name or lowered in PLACEHOLDER_NAMES or lowered in GENERIC_NAMES
            or (" " not in name and lowered in ROLE_WORDS))


def _cap(word):
    if word.lower() in LOWER_PARTICLES:
        return word.lower()
    # McCarter, O'Neil, Smith-Jones
    word = "-".join(p[:1].upper() + p[1:].lower() for p in word.split("-"))
    word = "'".join(p[:1].upper() + p[1:] for p in word.split("'"))
    if word.startswith("Mc") and len(word) > 2:
        word = "Mc" + word[2].upper() + word[3:]
    return word


def clean_name(raw, email=""):
    """A display name as (first, last, full), or None when it is not a person's name."""
    name = " ".join((raw or "").replace('"', "").replace("'", "'").split())
    name = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", name).strip()  # "Jane Doe (CBRE)" -> "Jane Doe"
    name = re.sub(r"\s+(via|on behalf of)\s+.*$", "", name, flags=re.I).strip()
    if "," in name:  # "Doe, Jane" -> "Jane Doe"; drop suffixes like ", PE"
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}" if first.strip() and len(first.strip()) > 3 else last.strip()
    if not name or "@" in name or any(ch.isdigit() for ch in name) or len(name) > 60:
        return None
    if name.lower() in PLACEHOLDER_NAMES or any(w.lower() in ("tbd", "tba") for w in name.split()):
        return None
    words = name.split()
    if any(w.lower().strip(".") in ROLE_WORDS for w in words):
        return None
    local = (email or "").split("@")[0].lower()
    if name.lower().replace(" ", "") == local.replace(".", "").replace("_", ""):
        # The display name is just the address again, unless it is clearly "First Last".
        if len(words) < 2:
            return None
    if name.isupper() or name.islower():
        words = [_cap(w) for w in words]
    full = " ".join(words)
    first, _, last = full.partition(" ")
    return first, last, full


def name_from_address(email):
    """jane.doe@x.com -> Jane Doe. Only for an unambiguous first.last (or first_last) local part."""
    local = (email or "").split("@")[0].lower()
    m = re.fullmatch(r"([a-z]{2,})[._]([a-z]{2,})", local)
    if not m or is_role_address(email) or m.group(1) in ROLE_WORDS or m.group(2) in ROLE_WORDS:
        return None
    first, last = _cap(m.group(1)), _cap(m.group(2))
    return first, last, f"{first} {last}"
