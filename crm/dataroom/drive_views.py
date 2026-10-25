"""The Drive files panel on company and project pages (team only): the mapped folder's current documents, older
versions folded away, and, for Managers, "Share in data room". Sharing is the only way a Drive file reaches a room; a
name marked internal, redline or draft needs a second confirmation; a newer dated version is flagged with a swap.

The panel is fetched after the page loads (`drive_panel`), so a slow Drive never holds up the record page.
"""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import drive
from ..models import Company, DataRoom, DataRoomFile, Project
from ..permissions import is_manager, manager_required
from . import logic

RECORDS = {"company": Company, "project": Project}


def _record(kind, pk):
    model = RECORDS.get(kind)
    if model is None:
        raise Http404
    return get_object_or_404(model, pk=pk)


def stale_files(record, docs, shared_files):
    """[(approved file, newer current version)] for files approved from this record's folder that have a newer
    dated version now, whether the old one sits beside it or was moved to Archive."""
    out = []
    for f in shared_files:
        if not f.drive_name:
            continue
        if isinstance(record, Company) and (not f.opportunity_id or f.opportunity.company_id != record.pk):
            continue
        if isinstance(record, Project) and f.opportunity_id:
            continue
        newer = drive.newer_version(f.drive_name, f.drive_folder, docs)
        if newer is not None and newer.id != f.drive_id:
            out.append((f, newer))
    return out


def _next(request, fallback):
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next", "")
    ok = nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return nxt if ok else fallback


def _deals(room, record):
    """The room's deals a file from this record can go on: a company's investor-report deals; none for a project
    (its files go at the top of the room)."""
    if isinstance(record, Company):
        return list(logic.candidate_deals(room).filter(company=record))
    return []


def _listing(record, refresh=False):
    """(documents, error) for the record's folder."""
    if not record.drive_folder or not drive.connected():
        return [], ""
    try:
        return drive.current_documents(drive.list_files(record.drive_folder, refresh=refresh)), ""
    except drive.NotConnected:
        return [], ""
    except Exception:
        return [], "Drive didn't answer. Check the folder is on the 1 ACMI Properties shared drive, then Refresh."


def drive_panel(request, kind, pk):
    record = _record(kind, pk)
    docs, error = _listing(record, refresh=request.GET.get("refresh") == "1")
    rooms = list(DataRoom.objects.filter(active=True))
    shared = {}
    for f in DataRoomFile.objects.filter(room__in=rooms).exclude(drive_id="").select_related("room", "opportunity__company"):
        shared.setdefault(f.drive_id, []).append(f)
    in_folder = {d.current.id for d in docs} | {v.id for d in docs for v in d.older}
    for d in docs:
        d.shared = shared.get(d.current.id, [])
    stale = stale_files(record, docs, [f for fs in shared.values() for f in fs])
    groups = {}
    for d in docs:
        groups.setdefault(d.folder or "(top of the folder)", []).append(d)
    suggestion = None
    if kind == "company" and not record.drive_folder and drive.connected():
        try:
            suggestion = drive.suggest_folder(record.name)
        except Exception:
            suggestion = None
    room_deals = [(room, _deals(room, record)) for room in rooms]
    return render(request, "crm/partials/drive_panel.html", {
        "kind": kind, "record": record, "connected": drive.connected(), "groups": groups, "error": error,
        "stale": stale, "suggestion": suggestion, "room_deals": room_deals, "can_share": is_manager(request.user),
        "folder_url": f"https://drive.google.com/drive/folders/{record.drive_folder}" if record.drive_folder else "",
        "n_files": len(in_folder),
    })


@require_POST
def drive_folder_set(request, kind, pk):
    """Map (or clear) the record's Drive folder: a pasted folder link, or the suggested tenancy folder."""
    record = _record(kind, pk)
    if request.POST.get("clear"):
        record.drive_folder = ""
    else:
        fid = drive.folder_id_from(request.POST.get("folder", ""))
        if not fid:
            messages.error(request, "Paste the folder's Google Drive link (it has /folders/ in it).")
            return redirect(record)
        record.drive_folder = fid
    record.save(update_fields=["drive_folder"])
    messages.success(request, "Drive folder saved." if record.drive_folder else "Drive folder removed.")
    return redirect(record)


@require_POST
@manager_required
def drive_share(request, kind, pk):
    """Share one Drive file from the record's folder in a data room (the only way a Drive file gets there)."""
    record = _record(kind, pk)
    room = get_object_or_404(DataRoom, pk=request.POST.get("room"), active=True)
    docs, _ = _listing(record)
    files = {d.current.id: d.current for d in docs}
    f = files.get(request.POST.get("file", ""))
    if f is None:
        messages.error(request, "That file isn't the current version in this folder any more. Refresh the Drive panel.")
        return redirect(record)
    if f.internal and request.POST.get("confirm_internal") != "1":
        messages.error(request, f"“{f.name}” looks internal (internal, redline or draft in its name). Tick the box to "
                                f"confirm it's meant for {room.name}.")
        return redirect(record)
    deals = _deals(room, record)
    opp = None
    if isinstance(record, Company):
        opp = next((o for o in deals if str(o.pk) == request.POST.get("opportunity", "")), deals[0] if len(deals) == 1 else None)
        if opp is None:
            messages.error(request, f"Put one of {record.name}'s deals on the investor report (and pick it) first.")
            return redirect(record)
    if DataRoomFile.objects.filter(room=room, drive_id=f.id, opportunity=opp).exists():
        messages.info(request, f"“{f.name}” is already shared.")
        return redirect(record)
    DataRoomFile.objects.create(room=room, opportunity=opp, drive_id=f.id, drive_name=f.name, drive_folder=f.folder,
                                drive_mime=f.mime, title=f.name, approved_by=request.user)
    messages.success(request, f"“{f.name}” is shared in the {room.name} room" + (f" on {opp.company.name}." if opp else
                                                                                   " (top of the room)."))
    return redirect(record)


@require_POST
@manager_required
def drive_swap(request, pk):
    """Point an approved Drive file at the newest version of the same document, after a Manager saw it."""
    shared = get_object_or_404(DataRoomFile.objects.select_related("opportunity__company"), pk=pk)
    record = shared.opportunity.company if shared.opportunity_id else _project_for(shared)
    if record is None or not record.drive_folder:
        raise Http404
    docs, _ = _listing(record, refresh=True)
    newer = drive.newer_version(shared.drive_name, shared.drive_folder, docs)
    if newer is None or newer.id == shared.drive_id:
        messages.info(request, "Nothing newer to swap in.")
        return redirect(_next(request, record.get_absolute_url()))
    if newer.internal and request.POST.get("confirm_internal") != "1":
        messages.error(request, f"The newer version “{newer.name}” looks internal; tick the box to confirm.")
        return redirect(_next(request, record.get_absolute_url()))
    shared.drive_id, shared.drive_name, shared.drive_mime, shared.title = newer.id, newer.name, newer.mime, newer.name
    shared.approved_by, shared.approved_at = request.user, timezone.now()
    shared.save()
    messages.success(request, f"The room now shows “{newer.name}”.")
    return redirect(_next(request, record.get_absolute_url()))


def _project_for(shared):
    """A room-wide Drive file's project: the project whose mapped folder holds it."""
    for project in Project.objects.exclude(drive_folder=""):
        docs, _ = _listing(project)
        if any(shared.drive_id in {d.current.id, *(v.id for v in d.older)} for d in docs):
            return project
    return None
