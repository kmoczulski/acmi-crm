"""Import from a spreadsheet: upload -> map the columns -> preview -> import -> results (and Undo for 7 days).

The file's cells sit in the session between the steps and are dropped once the import is done; the file itself is
never saved. Everyone on the team can import (partners never reach these pages: crm.middleware.PartnerOnlyMiddleware);
Undo deletes records, so it's Managers-only like every other delete.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm import choices
from crm.models import CompanyStatus, ImportBatch, List, ObjectType, Priority
from crm.permissions import manager_required, team_users

from . import logic

SESSION_KEY = "sheet_import"
SHOWN_ROWS = 500
FILTERS = [("", "All rows"), ("new", "New records"), ("matched", "Matches"), ("flagged", "Flagged"),
           ("skipped", "Skipped")]


def _pending(request):
    return request.session.get(SESSION_KEY)


def _sheet(pending):
    return pending["sheets"][pending.get("sheet", 0)]


def upload(request):
    if request.method == "POST":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Pick a file to import.")
            return redirect("crm:sheet_import")
        if f.size > logic.MAX_BYTES:
            messages.error(request, f"That file is {f.size / 1024 / 1024:.1f} MB. The limit is "
                                    f"{logic.MAX_BYTES // 1024 // 1024} MB: split it into smaller files.")
            return redirect("crm:sheet_import")
        try:
            sheets = logic.read_upload(f.name, f.read())
        except logic.SheetError as e:
            messages.error(request, str(e))
            return redirect("crm:sheet_import")
        first = next((i for i, s in enumerate(sheets) if s["rows"]), 0)
        request.session[SESSION_KEY] = {"file": f.name[:255], "sheets": sheets, "sheet": first,
                                        "mapping": logic.guess_mapping(sheets[first]["header"]), "options": {}}
        return redirect("crm:sheet_import_map")
    return render(request, "crm/sheet_import_upload.html", {
        "pending": _pending(request),
        "batches": ImportBatch.objects.select_related("created_by", "undone_by")[:15],
        "max_mb": logic.MAX_BYTES // 1024 // 1024, "max_rows": f"{logic.MAX_ROWS:,}",
    })


def _options_from(post):
    def list_spec(name):
        value = post.get(name, "")
        if value == "new":
            new = post.get(f"{name}_new", "").strip()
            return {"new": new} if new else None
        return int(value) if value.isdigit() else None

    return {
        "mode": post.get("mode") if post.get("mode") in ImportBatch.Mode.values else ImportBatch.Mode.FILL,
        "owner": post.get("owner", ""), "status": post.get("status", ""), "priority": post.get("priority", ""),
        "company_list": list_spec("company_list"), "person_list": list_spec("person_list"),
    }


def mapping(request):
    pending = _pending(request)
    if not pending:
        return redirect("crm:sheet_import")
    if request.GET.get("sheet", "").isdigit():
        i = int(request.GET["sheet"])
        if 0 <= i < len(pending["sheets"]) and i != pending.get("sheet"):
            pending["sheet"], pending["mapping"] = i, logic.guess_mapping(pending["sheets"][i]["header"])
            request.session.modified = True
        return redirect("crm:sheet_import_map")
    sheet = _sheet(pending)
    keys = {k for k, _, _ in logic.FIELDS}
    if request.method == "POST":
        chosen = [request.POST.get(f"col_{i}", "") for i in range(len(sheet["header"]))]
        chosen = [c if c in keys else "" for c in chosen]
        dupes = sorted({logic.FIELD_LABELS[c] for c in chosen if c and chosen.count(c) > 1})
        pending["mapping"], pending["options"] = chosen, {**pending.get("options", {}), **_options_from(request.POST)}
        request.session.modified = True
        if dupes:
            messages.error(request, f"Each field can come from one column only: {', '.join(dupes)}.")
        elif not ({"company_name", "company_domain", "email", "full_name", "first_name", "last_name"} & set(chosen)):
            messages.error(request, "Pick at least one column for the company name, a website, or the contact's "
                                    "name or email.")
        else:
            return redirect("crm:sheet_import_preview")
    samples = [[r[i] for r in sheet["rows"][:200] if i < len(r) and r[i]][:3] for i in range(len(sheet["header"]))]
    groups = {}
    for key, label, group in logic.FIELDS:
        groups.setdefault(group, []).append((key, label))
    columns = [{"i": i, "header": h, "samples": samples[i], "field": pending["mapping"][i]
                if i < len(pending["mapping"]) else ""} for i, h in enumerate(sheet["header"])]
    return render(request, "crm/sheet_import_map.html", {
        "pending": pending, "sheet": sheet, "columns": columns, "groups": groups.items(),
        "options": pending.get("options", {}), "modes": ImportBatch.Mode.choices,
        "users": team_users().order_by("first_name", "username"),
        "statuses": choices.offer(CompanyStatus), "priorities": choices.offer(Priority),
        "company_lists": List.objects.filter(object_type=ObjectType.COMPANY),
        "person_lists": List.objects.filter(object_type=ObjectType.PERSON),
    })


def _filtered(rows, show):
    def states(r):
        return {(x or {}).get("state") for x in (r["company"], r["person"])}

    if show == "new":
        return [r for r in rows if "new" in states(r) and not r["skipped"]]
    if show == "matched":
        return [r for r in rows if states(r) & {"matched", "updated"} and not r["skipped"]]
    if show == "flagged":
        return [r for r in rows if r["flags"] and not r["skipped"]]
    if show == "skipped":
        return [r for r in rows if r["skipped"]]
    return rows


def _list_label(spec):
    if isinstance(spec, dict):
        return f"{spec.get('new')} (new list)"
    lst = List.objects.filter(pk=spec).first() if spec else None
    return lst.name if lst else ""


def preview(request):
    pending = _pending(request)
    if not pending:
        return redirect("crm:sheet_import")
    sheet, options = _sheet(pending), dict(pending.get("options", {}))
    if request.method == "POST":
        options["use"] = {k[4:]: v for k, v in request.POST.items() if k.startswith("use:") and v.isdigit()}
        batch = logic.run(sheet, pending["mapping"], options, request.user, pending["file"])
        del request.session[SESSION_KEY]
        c = batch.counts
        messages.success(request, f"Imported {pending['file']}: {c['companies_new']} new compan"
                                  f"{'y' if c['companies_new'] == 1 else 'ies'}, {c['people_new']} new "
                                  f"contact{'' if c['people_new'] == 1 else 's'}.")
        return redirect(batch)
    plan = logic.build_plan(sheet["header"], sheet["rows"], pending["mapping"], options, sheet.get("numbers"))
    rows = [logic.result_row(r) for r in plan.rows]
    show = request.GET.get("show", "")
    shown = _filtered(rows, show)
    owner = team_users().filter(pk=options.get("owner")).first() if str(options.get("owner", "")).isdigit() else None
    return render(request, "crm/sheet_import_preview.html", {
        "pending": pending, "sheet": sheet, "counts": plan.counts, "rows": shown[:SHOWN_ROWS],
        "more": max(0, len(shown) - SHOWN_ROWS), "show": show, "filters": FILTERS,
        "close": [t for t in plan.companies if t.is_new and t.close],
        "mode_label": dict(ImportBatch.Mode.choices)[plan.mode], "owner": owner,
        "status_label": dict(choices.pairs(CompanyStatus)).get(options.get("status"), ""),
        "priority_label": dict(choices.pairs(Priority)).get(options.get("priority"), ""),
        "company_list": _list_label(options.get("company_list")), "person_list": _list_label(options.get("person_list")),
        "mapped": [(h, logic.FIELD_LABELS[k]) for h, k in zip(sheet["header"], pending["mapping"]) if k],
    })


@require_POST
def cancel(request):
    request.session.pop(SESSION_KEY, None)
    messages.success(request, "Import cancelled. Nothing was written.")
    return redirect("crm:sheet_import")


def result(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related("created_by", "undone_by"), pk=pk)
    show = request.GET.get("show", "")
    shown = _filtered(batch.results, show)
    return render(request, "crm/sheet_import_result.html", {
        "batch": batch, "counts": batch.counts, "rows": shown[:SHOWN_ROWS], "more": max(0, len(shown) - SHOWN_ROWS),
        "show": show, "filters": FILTERS, "can_undo": logic.can_undo(batch), "undo_until": logic.undo_until(batch),
        "touched": logic.touched_since(batch) if logic.can_undo(batch) else 0, "done": True,
    })


@require_POST
@manager_required
def undo(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    if not logic.can_undo(batch):
        messages.error(request, "This import can't be undone any more (it was undone already, or it's more than "
                                f"{logic.UNDO_DAYS} days old).")
        return redirect(batch)
    out = logic.undo(batch, request.user)
    messages.success(request, f"Undid the import of {batch.filename}: deleted {out['companies']} compan"
                              f"{'y' if out['companies'] == 1 else 'ies'} and {out['people']} contact"
                              f"{'' if out['people'] == 1 else 's'}, and put back the fields it filled on "
                              f"{out['reverted']} other record{'' if out['reverted'] == 1 else 's'}.")
    return redirect(reverse("crm:sheet_import_result", args=[batch.pk]))
