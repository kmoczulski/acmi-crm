"""A site's landowner letters page (/land/sites/<id>/letters/): tick the owners, edit the letter, then download the
letters (PDF), Avery 5160 labels or a mail-house CSV, and log what went out (crm/land/letters.py)."""
from datetime import date

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..models import LandownerLetter, LandSite, LetterTemplate, OutreachSettings, Parcel
from ..permissions import is_manager, refuse
from . import briefs, letters


def _date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _int(raw, default=0):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else default


def _sender(request):
    """Who signs: what this person used last (kept in their session), else their name and email, with the outreach
    settings' title and phone when they are the outreach sender."""
    saved = request.session.get("letter_sender")
    if isinstance(saved, dict):
        return saved
    s, user = OutreachSettings.load(), request.user
    name = user.get_full_name() or s.sender_name
    same = name.strip().lower() == (s.sender_name or "").strip().lower()
    return {"sender": name, "sender_title": s.sender_title if same else "",
            "sender_phone": s.sender_phone if same else "", "sender_email": user.email or ""}


def _template(templates, raw):
    """The template picked, else the first built-in letter."""
    picked = next((t for t in templates if str(t.pk) == str(raw or "")), None)
    return picked or next((t for t in templates if t.key == "intro"), templates[0] if templates else None)


def _page(request, site, rows, templates, template, body, sender, day, picked, dears):
    history = list(site.letters.select_related("sent_by", "template"))
    pks = {pk for x in history for pk in (x.parcels or [])}
    ids = dict(Parcel.objects.filter(pk__in=pks).values_list("pk", "prop_id")) if pks else {}
    manager = is_manager(request.user)
    for x in history:
        x.parcel_ids = [ids.get(pk) or f"#{pk}" for pk in (x.parcels or [])]
        x.address = x.mailing.split("\n")[1:]
        x.can_delete = manager or x.sent_by_id == request.user.pk
    for r in rows:
        r["picked"] = r["mailable"] and r["key"] in picked
        r["dear"] = dears.get(r["key"]) or r["name"]
    return render(request, "crm/land_letters.html", {
        "site": site, "rows": rows, "templates": templates, "template": template, "body": body, "day": day,
        "sender_fields": [(key, label, sender.get(key, "")) for key, label, _ in letters.SENDER_FIELDS],
        "fields": letters.FIELDS, "methods": LandownerLetter.Method.choices, "today": timezone.localdate(),
        "history": history, "mailable": sum(1 for r in rows if r["mailable"]),
    })


def _pdf_or_page(request, template, ctx, filename):
    """The page as a PDF download or, without Chrome on the server, the page itself to print."""
    try:
        data = briefs.html_to_pdf(render_to_string(template, {**ctx, "pdf": True, "logo_data": briefs.logo_data_uri()}))
    except briefs.PdfUnavailable as exc:
        return render(request, template, {**ctx, "note": f"Couldn't make the PDF ({exc}). Print this page and "
                                                          "choose Save as PDF."})
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def site_letters(request, pk):
    site = get_object_or_404(LandSite.objects.select_related("campus", "project"), pk=pk)
    if request.method == "GET" and request.GET.get("print"):
        return _reprint(request, site)
    if request.method == "POST" and request.POST.get("action") == "delete_letter":
        return _delete(request, site)
    letters.ensure_builtins()
    templates = list(LetterTemplate.objects.all())
    rows = letters.recipients(site)
    if request.method != "POST":
        template = _template(templates, request.GET.get("template"))
        new = {r["key"] for r in rows if not r["letters"]}  # owners not written to yet; all of them for a follow-up
        return _page(request, site, rows, templates, template, template.body if template else "", _sender(request),
                     timezone.localdate(), new or {r["key"] for r in rows}, {})

    action = request.POST.get("action") or "pdf"
    template = _template(templates, request.POST.get("template"))
    body = (request.POST.get("body") or "").replace("\r\n", "\n").strip()
    sender = {key: " ".join((request.POST.get(key) or "").split())[:n] for key, _, n in letters.SENDER_FIELDS}
    day = _date(request.POST.get("date")) or timezone.localdate()
    ticked = set(request.POST.getlist("to"))
    dears = {r["key"]: " ".join((request.POST.get("dear_" + r["key"]) or "").split())[:200] for r in rows}

    def again(error):  # back to the form, edits kept
        messages.error(request, error)
        return _page(request, site, rows, templates, template, body, sender, day, ticked, dears)

    if action == "save_template":
        name = " ".join((request.POST.get("template_name") or "").split())[:120]
        if not (name and body):
            return again("Write the letter and give the template a name first.")
        mine = LetterTemplate.objects.filter(key="", name__iexact=name).first()
        if mine:
            mine.body = body
            mine.save(update_fields=["body", "updated_at"])
        else:
            mine = LetterTemplate.objects.create(name=name, body=body, created_by=request.user)
        messages.success(request, f"Saved the template “{mine.name}”.")
        return redirect(reverse("crm:land_site_letters", args=[site.pk]) + f"?template={mine.pk}")
    if not body:
        return again("Write the letter first.")
    issues = letters.problems(body, sender)
    if issues:
        return again(" ".join(issues))
    picked = [r for r in rows if r["mailable"] and r["key"] in ticked]
    if not picked:
        return again("Tick at least one owner with a mailing address.")
    request.session["letter_sender"] = sender

    if action == "csv":
        resp = HttpResponse(letters.mail_csv(picked, dears), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{letters.export_name(site, "Mailing_List", "csv")}"'
        return resp
    if action == "labels":
        sheets = letters.label_sheets(picked, _int(request.POST.get("start"), 1))
        return _pdf_or_page(request, "crm/land_letter_labels.html", {"sheets": sheets, "title": f"Labels · {site.name}"},
                            letters.export_name(site, "Labels", "pdf"))
    if action == "sent":
        method = request.POST.get("method")
        if method not in LandownerLetter.Method.values:
            return again("Pick how the letters went out.")
        sent_on = _date(request.POST.get("sent_on")) or timezone.localdate()
        if sent_on > timezone.localdate():
            return again("The sent date can't be in the future.")
        made, skipped = letters.log_sent(site, picked, body, sender, day, dears, method, sent_on,
                                         (request.POST.get("notes") or "").strip(), template, request.user)
        if made:
            messages.success(request, f"Logged {made} letter{'s' if made != 1 else ''}; “Owner contacted” is dated on "
                             "those owners' parcels and the site (the first contact counts)."
                             + (f" {skipped} were already logged for that day." if skipped else ""))
        else:
            messages.info(request, "Those letters were already logged for that day.")
        return redirect("crm:land_site_letters", pk=site.pk)

    ctx = {"letters": letters.build(site, picked, body, sender, day, dears), "date": letters.long_date(day),
           "sender": sender, "title": f"Letters · {site.name}"}
    if action == "print":
        return render(request, "crm/land_letters_print.html", ctx)
    return _pdf_or_page(request, "crm/land_letters_print.html", ctx, letters.export_name(site, "Letters", "pdf"))


def _reprint(request, site):
    """One logged letter, as it went out."""
    letter = get_object_or_404(LandownerLetter, site=site, pk=_int(request.GET.get("print")))
    return render(request, "crm/land_letters_print.html", {
        "letters": [{"lines": letter.mailing.split("\n"), "text": letter.body or "(The letter's text wasn't kept.)"}],
        "date": letters.long_date(letter.sent_on), "title": f"Letter to {letter.owner_name}",
        "note": f"As logged: sent {letters.long_date(letter.sent_on)} ({letter.get_method_display().lower()})."})


def _delete(request, site):
    from .views import _log

    letter = get_object_or_404(LandownerLetter, site=site, pk=_int(request.POST.get("letter")))
    if not (is_manager(request.user) or letter.sent_by_id == request.user.pk):
        return refuse(request)
    _log(site, request.user, "edit",
         f"Took the {letter.sent_on:%b} {letter.sent_on.day} letter to {letter.owner_name} out of the letter log")
    letter.delete()
    messages.success(request, "Letter taken out of the log. Owner contacted dates stay as they are.")
    return redirect("crm:land_site_letters", pk=site.pk)
