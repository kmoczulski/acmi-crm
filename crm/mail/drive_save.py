"""Save a mail attachment into a company's mapped Drive folder, on the person's own Google permission.

The shared Drive service account is a Viewer on purpose (crm/drive.py). This uses the signed-in person's token,
asking for drive.file the first time, the same way Chat asks for its permissions. drive.file only lets the hub
touch files it created, so it cannot read the rest of anyone's Drive.
"""
import requests
from django.urls import reverse

from crm import gcal

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TIMEOUT = 60


class DriveSaveError(Exception):
    """Something the person can act on, in plain words."""


def connect_url(request, nxt):
    """Google's permission screen for drive.file, on top of what they already granted."""
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(
        request, process="connect", scope=DRIVE_FILE,
        auth_params="prompt=consent&include_granted_scopes=true", next=nxt,
    )


def has_scope(user, session=None):
    session = session or requests
    token = gcal.token_for(user)
    if token is None:
        return False
    try:
        access = gcal.access_token(token, session)
    except gcal.CalendarError:
        return False
    r = session.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": access}, timeout=30)
    if r.status_code != 200:
        return False
    return DRIVE_FILE in set((r.json().get("scope") or "").split())


def upload(user, *, name, mime, data, folder_id, session=None):
    """Put these bytes in the folder. Returns the new file's id and web link."""
    session = session or requests
    token = gcal.token_for(user)
    if token is None:
        raise DriveSaveError("Sign in with Google first.")
    try:
        access = gcal.access_token(token, session)
    except gcal.CalendarError as exc:
        raise DriveSaveError("Google didn't accept the hub's sign-in. Try again.") from exc
    meta = {"name": name[:200] or "attachment", "parents": [folder_id]}
    r = session.post(
        UPLOAD, params={"uploadType": "multipart", "supportsAllDrives": "true"},
        headers={"Authorization": f"Bearer {access}"},
        files={"metadata": ("", __import__("json").dumps(meta), "application/json; charset=UTF-8"),
               "file": (name, data, mime or "application/octet-stream")},
        timeout=TIMEOUT,
    )
    if r.status_code == 403:
        raise DriveSaveError("Google wouldn't let the hub save that file. Allow Drive access and try again.")
    if r.status_code >= 400:
        raise DriveSaveError("Drive didn't take the file. Try again.")
    body = r.json()
    return {"id": body.get("id", ""), "link": body.get("webViewLink", "")}
