"""Reading the team's Google Drive (the "1 ACMI Properties" shared drive): a company's tenancy folder and a project's
folder, the current version of each document in them, and a file's bytes for the data room.

Read-only, through a Google service account that Kellen adds as Viewer on that shared drive and nowhere else, so the
hub can't see anyone's My Drive or the other shared drives. Credentials: `ACMI_DRIVE_SA_FILE` (the service account's
JSON key, on the laptop) or `ACMI_DRIVE_ADC=true` (Cloud Run's own service account). Without either, Drive panels say
"Drive not connected" and nothing else changes.

Versions follow the team's habit: dated names ("ARA LOI - MEIH - 2026.03.23.pdf"), the newest kept beside older ones
or with them moved into an Archive folder. `current_documents` groups a folder's files by name without the date and
extension, picks the newest date (from the name, else Drive's modified time) and prefers the PDF of that version.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from django.conf import settings
from django.core.cache import cache

from .merge import core_name

API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
# Google's own formats are exported when downloaded.
EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}
SKIP_NAMES = ("desktop.ini",)
SKIP_EXT = (".lnk", ".tmp")
MAX_DEPTH = 3
LIST_CACHE = 300  # seconds a folder listing is kept

# Words in a file name that mark it as not for outsiders: sharing one asks twice.
INTERNAL_WORDS = re.compile(r"(?<![a-z])(internal|redline[sd]?|draft)(?![a-z])", re.I)
_DATE_PATTERNS = [
    (re.compile(r"(?<!\d)(20\d\d)[.\-_ ](\d{1,2})[.\-_ ](\d{1,2})(?!\d)"), "ymd"),  # 2026.03.23, 2026-03-23, 2025 07 03
    (re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)"), "ymd"),  # 20260910
    (re.compile(r"(?<!\d)(\d{1,2})[.\-_](\d{1,2})[.\-_](\d\d)(?!\d)"), "mdy"),  # 5_15_26, 10.11.24
]


class NotConnected(Exception):
    pass


def connected():
    return bool(getattr(settings, "DRIVE_SA_FILE", "") or getattr(settings, "DRIVE_ADC", False))


def folder_id_from(text):
    """A Drive folder link or a bare ID -> the folder ID, or ""."""
    text = (text or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", text) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text) else ""


def file_id_from(text):
    """A Drive or Docs file link -> the file ID, or "" (for a link that isn't a Drive file)."""
    m = re.search(r"(?:drive|docs)\.google\.com/.*?(?:/d/|[?&]id=)([A-Za-z0-9_-]{10,})", text or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def name_date(name):
    """The date written in a file name, or None."""
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(name):
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (2000 + c, a, b)
            try:
                return date(y, mo, d)
            except ValueError:
                continue
    return None


def base_name(name):
    """The document a file is a version of: the name without its dates, extension, copy marks and filler words."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    for pattern, _ in _DATE_PATTERNS:
        stem = pattern.sub(" ", stem)
    stem = re.sub(r"\(\d+\)", " ", stem)  # "(1)" copies
    stem = re.sub(r"\b(final|sent|signed|executed|clean|v\d+)\b", " ", stem, flags=re.I)
    return core_name(stem) or stem.lower()


def is_internal(name):
    return bool(INTERNAL_WORDS.search(name or ""))


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    modified: datetime = None
    folder: str = ""  # the sub-folder path inside the mapped folder ("LOI", "Design/Plans"), "" at the top
    web_link: str = ""

    @property
    def version_date(self):
        return name_date(self.name) or (self.modified.date() if self.modified else None)

    @property
    def key(self):
        return (self.folder, base_name(self.name))

    @property
    def internal(self):
        return is_internal(self.name)

    @property
    def is_pdf(self):
        return self.mime == "application/pdf" or self.name.lower().endswith(".pdf")


@dataclass
class Document:
    """One document in a folder: its current version and the older ones."""
    current: DriveFile
    older: list = field(default_factory=list)

    @property
    def folder(self):
        return self.current.folder


def current_documents(files):
    """Files -> one Document per (sub-folder, name without date), newest version first, PDF preferred on a tie."""
    groups = {}
    for f in files:
        groups.setdefault(f.key, []).append(f)
    docs = []
    for versions in groups.values():
        versions.sort(key=lambda f: (f.version_date or date.min, f.is_pdf, f.modified or datetime.min), reverse=True)
        docs.append(Document(versions[0], versions[1:]))
    docs.sort(key=lambda d: (d.folder.lower(), -(d.current.version_date or date.min).toordinal(), d.current.name.lower()))
    return docs


def newer_version(approved_name, approved_folder, docs):
    """The current file of the document `approved_name` is a version of, when it's a different file; else None."""
    key = (approved_folder, base_name(approved_name))
    for d in docs:
        if (d.folder, base_name(d.current.name)) == key:
            return d.current
    return None


# ---------------------------------------------------------------------------
# The Drive API (read-only)
# ---------------------------------------------------------------------------


def _user_session(email):
    """A session that reads Drive as this team member, with the Google token they signed in with (crm/gcal.py keeps it
    fresh). Needs the Drive read-only permission, which Google asks for at their next sign-in."""
    import requests
    from django.contrib.auth import get_user_model

    from . import gcal

    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
    token = gcal.token_for(user) if user else None
    if token is None:
        raise NotConnected(f"{email} has not signed in to the hub with Google yet")
    try:
        bearer = gcal.access_token(token)
    except gcal.CalendarError as exc:
        raise NotConnected(str(exc)) from exc
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {bearer}"
    return session


def _session(as_user=""):
    if as_user:
        return _user_session(as_user)
    if not connected():
        raise NotConnected
    from google.auth.transport.requests import AuthorizedSession  # loaded only when Drive is used

    if getattr(settings, "DRIVE_SA_FILE", ""):
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(settings.DRIVE_SA_FILE, scopes=[SCOPE])
    else:
        import google.auth

        creds, _ = google.auth.default(scopes=[SCOPE])
    return AuthorizedSession(creds)


class Client:
    """The few Drive calls the hub makes. Tests swap in a fake through `client()`."""

    def __init__(self, as_user=""):
        self.http = _session(as_user)

    def _get(self, path, **params):
        params.update(supportsAllDrives="true")
        r = self.http.get(f"{API}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def meta(self, file_id):
        return self._get(f"/files/{file_id}", fields="id,name,mimeType,modifiedTime,webViewLink,trashed")

    def children(self, folder_id):
        items, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)",
                      "includeItemsFromAllDrives": "true", "corpora": "allDrives"}
            if token:
                params["pageToken"] = token
            data = self._get("/files", **params)
            items += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return items

    def download(self, file_id, mime):
        """(response streaming the bytes, extension to add or "")."""
        if mime in EXPORTS:
            export_mime, ext = EXPORTS[mime]
            r = self.http.get(f"{API}/files/{file_id}/export", params={"mimeType": export_mime}, stream=True, timeout=60)
        else:
            ext = ""
            r = self.http.get(f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                              stream=True, timeout=60)
        r.raise_for_status()
        return r, ext


def client(as_user=""):
    return Client(as_user)


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _skip_folder(name):
    return "archive" in name.lower()


def _skip_file(name):
    low = name.lower()
    return low in SKIP_NAMES or low.endswith(SKIP_EXT) or name.startswith("~$")


def list_files(folder_id, refresh=False, as_user=""):
    """Every file under the folder (Archive folders skipped, three levels deep), cached for a few minutes. With
    `as_user` (an email) the folder is read with that person's own Google sign-in instead of the service account."""
    key = f"drive:list:{folder_id}:{as_user}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    c = client(as_user)
    files = []

    def walk(fid, path, depth):
        for item in c.children(fid):
            name = item.get("name", "")
            if item.get("mimeType") == FOLDER:
                if depth < MAX_DEPTH and not _skip_folder(name):
                    walk(item["id"], f"{path}/{name}" if path else name, depth + 1)
            elif not _skip_file(name):
                files.append(DriveFile(item["id"], name, item.get("mimeType", ""), _parse_time(item.get("modifiedTime")),
                                       path, item.get("webViewLink", "")))

    walk(folder_id, "", 1)
    cache.set(key, files, LIST_CACHE)
    return files


def subfolders(folder_id):
    """[(name, id)] of a folder's sub-folders (for suggesting a company's tenancy folder)."""
    key = f"drive:subfolders:{folder_id}"
    cached = cache.get(key)
    if cached is None:
        cached = [(i["name"], i["id"]) for i in client().children(folder_id) if i.get("mimeType") == FOLDER]
        cache.set(key, cached, LIST_CACHE)
    return cached


def suggest_folder(name):
    """The tenancy folder (under `DRIVE_TENANCY_FOLDER`) whose name matches a company's, as (name, id), or None."""
    root = getattr(settings, "DRIVE_TENANCY_FOLDER", "")
    want = core_name(name)
    if not (root and want and connected()):
        return None
    for folder_name, fid in subfolders(root):
        k = core_name(re.sub(r"\(.*?\)", " ", folder_name))  # "Castelion (Ranger)" -> castelion
        if k == want or core_name(folder_name) == want:
            return folder_name, fid
    return None
