"""Documents added by hand on a company or deal.

The default is a Google Drive link, so a file lives in one place only. Uploading a file
is also allowed: in development it is saved under MEDIA_ROOT/documents/. Once the app is
hosted on Google Cloud, `store_upload` should put the file in the team's shared drive
(Drive API, folder from ACMI_DRIVE_FOLDER_ID) and keep only the link; until those
credentials exist, uploads stay local.
"""
from django.conf import settings

from .mail.attachments import categorize


def drive_upload_enabled():
    return bool(getattr(settings, "ACMI_DRIVE_FOLDER_ID", ""))


def store_upload(document):
    """Hook for hosting: upload `document.file` to Drive, set `document.url`, drop the local copy."""
    if not drive_upload_enabled():
        return document
    raise NotImplementedError("Google Drive upload is set up when the app moves to Google Cloud (see docs/HANDOFF.md).")


def save_document(form, user, company=None, opportunity=None):
    doc = form.save(commit=False)
    doc.company = company or (opportunity.company if opportunity else None)
    doc.opportunity = opportunity
    doc.added_by = user
    if not doc.category:
        doc.category = categorize(doc.title or (doc.file.name if doc.file else ""))
    doc.save()
    return store_upload(doc)
