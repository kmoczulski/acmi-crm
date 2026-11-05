"""Load rounds from a team "LOI Tracker" workbook (the Lewis Place layout): a block headed "LOI INFO" with one column per
round (ACMI INITIAL OFFER, SELLER REDLINE, ACMI COUNTER) and one row per term; Phase II and Phase III blocks under the
first; the closing-extension rows after them.

Rows are matched by their wording, the workbook's own spelling slips included ("Extention", "Feasability", "Intitial",
"Redunable"), never by position, so a sheet with a row added or moved still reads. Cached values are read, not formulas.
Anything with numbers in it that isn't recognised is handed back in `skipped` so the person can check it.
"""
import io
import re
import zipfile

import openpyxl

from . import terms

MAX_UNPACKED = 100 * 1024 * 1024


class ImportProblem(Exception):
    pass


_TYPOS = (
    (r"extention|extenstion|extensions", "extension"), (r"feasability|feasilbity|feasbility", "feasibility"),
    (r"intitial|initail", "initial"), (r"dilligence|diligance", "diligence"), (r"redunable|refundible", "refundable"),
    (r"ancitipated|anticipted", "anticipated"), (r"commece|comence|commense", "commence"),
)


def _norm(text):
    s = re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()
    for pattern, word in _TYPOS:
        s = re.sub(pattern, word, s)
    return s


# (pattern on the normalized label, what it sets). "EXT_*" is a feasibility extension the first time a phase shows it
# and the closing extension the second time (the workbook lists the closing extension after the last phase).
_RULES = (
    (r"^date sent|^date received", "dated"),
    (r"^anticipated .*psa", "psa_signed"),
    (r"^subdivision", "subdivision"),
    (r"months to final zoning", "zoning_months"),
    (r"purchase size|^size ", "acres"),
    (r"period to commence feasibility", "start_days"),
    (r"^feasibility period( days)?$", "feasibility_days"),
    (r"feasibility period extension deposit", "feas_ext_deposit"),
    (r"non refundable deposit per extension", "feas_ext_deposit"),
    (r"deposit after expiration of feasibility|deposit after feasibility", "go_hard"),
    (r"^extension period", "EXT_DAYS"),
    (r"extension options available", "EXT_OPTIONS"),
    (r"extension options exercised", "EXT_USED"),
    (r"^price acre|^price per acre", "price_per_acre"),
    (r"^price$|^total price$", None),
    (r"^initial deposit", "initial_deposit"),
    (r"^non refundable portion|^refundable portion", "NONREF"),
    (r"^30 day deposit", "dep30"), (r"^60 day deposit", "dep60"), (r"^90 day deposit", "dep90"),
    (r"days to close following final zoning", "close_days:zoning"),
    (r"days to close post diligence|days to close following feasibility", "close_days:feasibility"),
    (r"^deposit amount", "close_ext_deposit"),
    (r"^outside date to close", "outside_months"),
)
_ROUND_KEYS = {"dated", "psa_signed", "subdivision", "zoning_months", "outside_months"}
_NONREF_OF = {"initial_deposit": "initial_nonref", "go_hard": "go_hard_nonref", "close_ext_deposit": "close_ext_nonref"}
_EXT = {"EXT_DAYS": ("feas_ext_days", "close_ext_days"), "EXT_OPTIONS": ("feas_ext_options", "close_ext_options"),
        "EXT_USED": ("feas_ext_used", "close_ext_used")}
_PHASE_ROW = re.compile(r"^phase (i{1,3}|iv|v|vi|[1-6])$")


def _label(header):
    text = re.sub(r"\bAcmi\b", "ACMI", str(header).strip().capitalize())
    return text[:120]


def _side(header):
    h = str(header).lower()
    return "them" if "seller" in h or "owner" in h else "us" if "acmi" in h else ""


def read_workbook(file):
    """{"rounds": [{"label", "sent_by", "dated", "terms"}], "skipped": [row labels]} from an uploaded workbook."""
    try:
        data = file.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:  # a workbook is a zip: don't unpack one that expands absurdly
            if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
                raise ImportProblem("That workbook is too large to read.")
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except ImportProblem:
        raise
    except Exception as exc:  # a damaged zip, a wrong file type, a password: say so plainly
        raise ImportProblem("That file couldn't be opened as an Excel workbook.") from exc
    for ws in wb.worksheets:
        found = _read_sheet(ws)
        if found:
            if not found["rounds"]:
                raise ImportProblem("The LOI INFO block has no filled-in rounds.")
            return found
    raise ImportProblem("Didn't find an \"LOI INFO\" block with a column per round (ACMI INITIAL OFFER, SELLER "
                        "REDLINE, ...). Add the round by hand instead.")


def _read_sheet(ws):
    head = next(((c.row, c.column) for row in ws.iter_rows() for c in row
                 if isinstance(c.value, str) and _norm(c.value) == "loi info"), None)
    if not head:
        return None
    hr, hc = head
    cols = [(c.column, c.value) for c in ws[hr] if c.column > hc and isinstance(c.value, str) and c.value.strip()]
    if not cols:
        return None
    raw = {col: {"round": {}, "phases": {}} for col, _ in cols}
    phase, seen, last_deposit, skipped = 0, set(), None, []

    def put(col, key, value, at=None):
        idx = phase if at is None else at
        if value in (None, "") or (isinstance(value, str) and value.strip().lower() in terms.BLANKS):
            return  # "N/A" or an empty cell never wipes a value an earlier row with the same meaning gave
        bucket = raw[col]["round"] if key in _ROUND_KEYS else raw[col]["phases"].setdefault(idx, {})
        if not bucket.get(key):  # the first row that gives a real value wins; a later 0 doesn't undo it
            bucket[key] = value

    for r in range(hr + 1, ws.max_row + 1):
        label = ws.cell(r, hc).value
        if not isinstance(label, str) or not label.strip():
            continue
        n = _norm(label)
        if n.startswith("loi tracker"):
            break
        values = {col: ws.cell(r, col).value for col, _ in cols}
        has_value = any(v not in (None, "") for v in values.values())
        m = _PHASE_ROW.match(n)
        if m and not has_value:
            phase = {"i": 0, "ii": 1, "iii": 2, "iv": 3, "v": 4, "vi": 5}.get(m.group(1)) \
                if not m.group(1).isdigit() else int(m.group(1)) - 1
            seen, last_deposit = set(), None
            continue
        target = next((t for pattern, t in _RULES if re.search(pattern, n)), "?")
        if target is None:
            continue
        if target == "?":
            if has_value:
                skipped.append(label.strip())
            continue
        at = None
        if target in _EXT:
            first, second = _EXT[target]
            target, at = (second, 0) if first in seen else (first, None)
            seen.add(first)
        elif target == "NONREF":
            if last_deposit is None:
                skipped.append(label.strip())
                continue
            target = _NONREF_OF[last_deposit]
            at = 0 if target == "close_ext_nonref" else None
        elif target == "close_ext_deposit":
            at = 0
        elif target.startswith("close_days:"):
            target, basis = target.split(":")
            for col, _ in cols:
                if values[col] not in (None, "") and terms.num(values[col]) is not None:
                    put(col, "close_basis", basis)
        if target in _NONREF_OF:
            last_deposit = target
        elif target == "close_ext_deposit":
            last_deposit = "close_ext_deposit"
        for col, _ in cols:
            put(col, target, values[col], at)
    return _build(cols, raw, skipped)


def _build(cols, raw, skipped):
    rounds = []
    for col, header in cols:
        got = raw[col]
        rd = {k: terms.coerce(terms.ROUND_BY_KEY[k], v) for k, v in got["round"].items() if k in terms.ROUND_BY_KEY}
        dated = terms.as_date(got["round"].get("dated"))
        psa = terms.as_date(got["round"].get("psa_signed"))
        if dated and psa and psa >= dated:
            rd["psa_days"] = (psa - dated).days
        phases = []
        for idx in sorted(got["phases"]):
            fields = got["phases"][idx]
            phase = {k: terms.coerce(terms.PHASE_BY_KEY[k], v) for k, v in fields.items() if k in terms.PHASE_BY_KEY}
            while len(phases) < idx:
                phases.append({})
            phases.append(phase)
        t = terms.dump({**{f.key: None for f in terms.ROUND_FIELDS}, **rd, "phases": [
            {**{f.key: None for f in terms.PHASE_FIELDS}, **p} for p in phases]})
        if not dated and not t.get("phases"):
            continue
        rounds.append({"label": _label(header), "sent_by": _side(header), "dated": dated, "terms": t})
    return {"rounds": rounds, "skipped": list(dict.fromkeys(skipped))}
