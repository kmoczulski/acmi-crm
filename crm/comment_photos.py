"""The picture on a comment (crm.views.comment_add): checked before it is stored, and shrunk when it is huge.

A phone photo is 3-8 MB and about 4000 px wide. The comment feed shows it at a fraction of that, so anything wider than
MAX_SIDE is scaled down once here instead of every page load carrying the full file. Pictures at or under that size are
stored exactly as sent. The stored name is made up here (never the uploaded one) and its extension follows what the
file really is, so the file view (crm.views.comment_photo) can always tell the browser the right type.
"""
import io
import uuid

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

MAX_BYTES = 15 * 1024 * 1024  # largest upload accepted (Cloud Run refuses requests over 32 MB)
MAX_PIXELS = 64_000_000       # width x height; decoding more than this could use too much of the server's memory
MAX_SIDE = 2400               # longer edge, in pixels, that a stored picture is scaled down to
FORMATS = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}  # Pillow's name -> extension
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
NOT_A_PICTURE = "That file isn't a picture the hub can use. Use a JPG, PNG, GIF or WebP."


class BadPhoto(ValueError):
    """The upload can't be used; the message is shown to the person who sent it."""


def prepare(upload):
    """The ContentFile to store for an uploaded picture, named for its real type. Raises BadPhoto."""
    if upload.size > MAX_BYTES:
        raise BadPhoto(f"That picture is {upload.size / 1048576:.1f} MB; the limit is {MAX_BYTES / 1048576:.0f} MB.")
    data = upload.read()
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt in FORMATS and img.width * img.height > MAX_PIXELS:
            raise BadPhoto("That picture has too many pixels (over 64 megapixels). Send a smaller copy.")
        img.load()  # decode all of it now, so a cut-off or booby-trapped file fails here and not on someone's screen
    except BadPhoto:
        raise
    except Exception:  # Pillow raises a handful of different errors for bad input, even a decompression bomb
        raise BadPhoto(NOT_A_PICTURE)
    if fmt not in FORMATS:
        raise BadPhoto(NOT_A_PICTURE)
    ext = FORMATS[fmt]
    name = f"{uuid.uuid4().hex}.{ext}"
    if ext == "gif" or getattr(img, "is_animated", False) or max(img.size) <= MAX_SIDE:
        return ContentFile(data, name=name)  # as sent (scaling would flatten an animation)
    img = ImageOps.exif_transpose(img)  # bake in the camera's rotation: the scaled copy has no EXIF to say so
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    if ext == "jpg":
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    elif ext == "png":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "WEBP", quality=85)
    return ContentFile(out.getvalue(), name=name)
