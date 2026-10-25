"""The data room's pages: a capital partner's sign-in, invite (making the account), the pipeline, a company card and
approved files; and the Manager's page that runs the room (members, deal switches, file approvals, the log).

A partner reaches only these pages (crm.middleware.PartnerOnlyMiddleware). Managers can open the partner's pages
with ?room=<id> to see exactly what the partner sees; their visits aren't logged.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import (
    Campus, DataRoom, DataRoomDeal, DataRoomFile, DataRoomMember, Document, Firm, Opportunity,
)
from ..permissions import is_manager, is_partner, manager_required
from . import logic

# Partners' passwords are checked harder than the default: 12 characters, not common, not like their name or email.
PARTNER_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


def _ip(request):
    return request.META.get("REMOTE_ADDR", "")


def _room(request):
    """(room, member) for the signed-in partner, or (room, None) for a Manager previewing with ?room=<id>."""
    user = request.user
    if is_partner(user):
        member = getattr(user, "data_room_member", None)
        if member is None or not member.active or not member.room.active:
            logout(request)
            raise Http404
        return member.room, member
    if is_manager(user):
        rid = request.GET.get("room", "")
        room = DataRoom.objects.filter(pk=rid).first() if rid.isdigit() else DataRoom.objects.first()
        if room is None:
            raise Http404
        return room, None
    raise Http404


def _preview_q(member, room):
    return "" if member is not None else f"?room={room.pk}"


# ---------------------------------------------------------------------------
# Partner pages
# ---------------------------------------------------------------------------


def room_login(request):
    if is_partner(request.user):
        return redirect("crm:room_home")
    error = ""
    username = request.POST.get("email", "").strip().lower()
    if request.method == "POST":
        ip = _ip(request)
        if logic.locked(username, ip):
            error = "Too many attempts. Wait 15 minutes, or ask ACMI for a new sign-in link."
        else:
            user = authenticate(request, username=username, password=request.POST.get("password", ""))
            if user is not None and is_partner(user):
                logic.cleared(username, ip)
                login(request, user, backend="crm.auth.PartnerBackend")
                logic.log(user.data_room_member.room, user.data_room_member, "sign-in")
                return redirect("crm:room_home")
            logic.failed(username, ip)
            error = "That email and password don't match an active data room account."
    return render(request, "crm/room/login.html", {"error": error, "email": username})


@require_POST
def room_logout(request):
    logout(request)
    return redirect("crm:room_login")


class InviteForm(forms.Form):
    first_name = forms.CharField(max_length=60)
    last_name = forms.CharField(max_length=60)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(label="Password again", widget=forms.PasswordInput, strip=False)

    def __init__(self, *args, member=None, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean(self):
        d = super().clean()
        p1, p2 = d.get("password1"), d.get("password2")
        if p1 and p1 != p2:
            raise ValidationError("The two passwords don't match.")
        if p1:
            from django.contrib.auth.models import User
            from django.contrib.auth.password_validation import get_password_validators

            probe = self.member.user or User(username=self.member.email, email=self.member.email,
                                              first_name=d.get("first_name", ""), last_name=d.get("last_name", ""))
            validate_password(p1, probe, password_validators=get_password_validators(PARTNER_PASSWORD_VALIDATORS))
        return d


def room_invite(request, token):
    member = logic.member_for_invite(token)
    if member is None:
        return render(request, "crm/room/invite_expired.html", status=404)
    if request.user.is_authenticated and not (is_partner(request.user) and request.user == member.user):
        logout(request)  # never attach a partner's account to whoever happens to be signed in
    first, _, last = member.name.partition(" ")
    form = InviteForm(request.POST or None, member=member,
                      initial={"first_name": (member.user.first_name if member.user else first),
                               "last_name": (member.user.last_name if member.user else last)})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        user = logic.accept_invite(member, d["first_name"], d["last_name"], d["password1"])
        login(request, user, backend="crm.auth.PartnerBackend")
        logic.log(member.room, member, "sign-in")
        return redirect("crm:room_home")
    return render(request, "crm/room/invite.html", {"form": form, "invitee": member, "room": member.room})


def room_home(request):
    room, member = _room(request)
    deals = list(logic.visible_deals(room).order_by("campus__code", "company__name"))
    view = request.GET.get("view", "table")
    stages = [{"label": label, "deals": [o for o in deals if o.stage == value]}
              for value, label in Opportunity._meta.get_field("stage").choices if value in logic.ROOM_STAGES]
    for s in stages:
        s["rows"] = logic.rows(room, s["deals"])
    logic.log(room, member, "home")
    return render(request, "crm/room/home.html", {
        "room": room, "member": member, "columns": logic.columns(room), "rows": logic.rows(room, deals),
        "stages": stages, "view": "board" if view == "board" else "table", "files": logic.files_for(room),
        "q": _preview_q(member, room), "preview": member is None, "numeric": logic.NUMERIC,
    })


def room_deal(request, pk):
    room, member = _room(request)
    o = logic.deal_for(room, pk)
    if o is None:
        raise Http404
    logic.log(room, member, "deal", opportunity=o)
    return render(request, "crm/room/deal.html", {
        "room": room, "member": member, "deal": o, "facts": logic.card(room, o), "files": logic.files_for(room, o),
        "q": _preview_q(member, room), "preview": member is None,
    })


def room_file(request, pk):
    room, member = _room(request)
    f = get_object_or_404(DataRoomFile.objects.select_related("document"), pk=pk, room=room)
    if f.opportunity_id and logic.deal_for(room, f.opportunity_id) is None:
        raise Http404  # the deal was switched off: its files go with it
    if f.source_drive_id:
        response = _from_drive(f)
        if response.status_code == 200:
            logic.log(room, member, "file", opportunity=f.opportunity, file=f)
        return response
    logic.log(room, member, "file", opportunity=f.opportunity, file=f)
    if f.link:
        return HttpResponseRedirect(f.link)
    stored = f.stored_file
    if not stored:
        raise Http404
    return FileResponse(stored.open("rb"), as_attachment=True, filename=stored.name.rsplit("/", 1)[-1])


def _newer_versions(files):
    """{shared file pk: newer DriveFile} for Drive files whose folder now has a newer dated version."""
    from .drive_views import _listing, _project_for, stale_files

    out = {}
    by_record = {}
    for f in files:
        if not f.drive_id or not f.drive_name:
            continue
        record = f.opportunity.company if f.opportunity_id else _project_for(f)
        if record is not None and record.drive_folder:
            by_record.setdefault((type(record), record.pk), (record, []))[1].append(f)
    for record, shared in by_record.values():
        docs, _ = _listing(record)
        out.update({f.pk: newer for f, newer in stale_files(record, docs, shared)})
    return out


def _from_drive(f):
    """Stream an approved Drive file to the partner: read from Drive now (so nothing is stored twice and the partner
    never sees a Drive address), Google Docs and Sheets exported as PDF / Excel."""
    from django.http import HttpResponse, StreamingHttpResponse

    from .. import drive

    try:
        c = drive.client()
        meta = c.meta(f.source_drive_id)
        if meta.get("trashed"):
            raise Http404
        upstream, ext = c.download(meta["id"], meta.get("mimeType", ""))
    except Http404:
        raise
    except drive.NotConnected:
        return HttpResponse("This document can't be opened right now. Please ask your ACMI contact.", status=503,
                            content_type="text/plain")
    except Exception:  # Drive refused or is down: never show its error to a partner
        return HttpResponse("This document can't be opened right now. Please try again later or ask your ACMI contact.",
                            status=502, content_type="text/plain")
    name = meta.get("name", f.title)
    if ext and not name.lower().endswith(ext):
        name += ext
    response = StreamingHttpResponse(upstream.iter_content(64 * 1024),
                                     content_type=upstream.headers.get("Content-Type", "application/octet-stream"))
    from django.utils.http import content_disposition_header

    response["Content-Disposition"] = content_disposition_header(True, name)
    return response


# ---------------------------------------------------------------------------
# The Manager's page
# ---------------------------------------------------------------------------


class RoomSettingsForm(forms.ModelForm):
    class Meta:
        model = DataRoom
        fields = ["name", "partner", "intro", "campuses", "show_costs", "show_win", "active"]
        widgets = {"intro": forms.Textarea(attrs={"rows": 3}), "campuses": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = Firm.objects.order_by("name")
        self.fields["campuses"].queryset = Campus.objects.all()


@manager_required
def data_rooms(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:120]
        if name:
            room = DataRoom.objects.create(name=name, created_by=request.user,
                                           partner=Firm.objects.filter(name__iexact=name).first())
            return redirect(room)
    rooms = DataRoom.objects.prefetch_related("members")
    if len(rooms) == 1 and not request.GET.get("all"):
        return redirect(rooms[0])
    return render(request, "crm/room/manage_list.html", {"rooms": rooms})


@manager_required
def data_room_manage(request, pk):
    room = get_object_or_404(DataRoom, pk=pk)
    new_link = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "settings":
            form = RoomSettingsForm(request.POST, instance=room)
            if form.is_valid():
                form.save()
                messages.success(request, "Room saved.")
            else:
                messages.error(request, "Fix the room settings: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
        elif action == "invite":
            email = request.POST.get("email", "").strip().lower()
            name = request.POST.get("name", "").strip()[:120]
            try:
                forms.EmailField().clean(email)
            except ValidationError:
                messages.error(request, "Enter the person's email address.")
                return redirect(room)
            existing = DataRoomMember.objects.filter(email=email).first()
            if existing and existing.room_id != room.pk:
                messages.error(request, f"{email} already belongs to the {existing.room.name} room.")
                return redirect(room)
            from ..permissions import team_users

            if team_users().filter(email__iexact=email).exists():
                messages.error(request, "That address belongs to someone on the ACMI team.")
                return redirect(room)
            member = existing or DataRoomMember.objects.create(room=room, email=email, name=name or email)
            new_link = request.build_absolute_uri(logic.issue_invite(member, request.user))
            messages.success(request, f"Sign-in link for {member.name} made. Copy it below and send it to them; it "
                                      f"works once, for {logic.INVITE_DAYS} days.")
        elif action in ("reinvite", "member_off", "member_on"):
            member = get_object_or_404(DataRoomMember, pk=request.POST.get("member"), room=room)
            if action == "reinvite":
                new_link = request.build_absolute_uri(logic.issue_invite(member, request.user))
                messages.success(request, f"New sign-in link for {member.name} (it sets a new password). Copy it below.")
            else:
                logic.switch_member(member, action == "member_on")
                messages.success(request, f"{member.name} {'can sign in again' if action == 'member_on' else 'can no longer sign in'}.")
        elif action == "deal":
            opp = get_object_or_404(logic.candidate_deals(room), pk=request.POST.get("opportunity"))
            logic.set_shown(room, opp, request.POST.get("shown") == "1", request.user)
        elif action == "approve_document":
            doc = get_object_or_404(Document, pk=request.POST.get("document"), opportunity__in=logic.candidate_deals(room))
            if not DataRoomFile.objects.filter(room=room, document=doc).exists():
                DataRoomFile.objects.create(room=room, document=doc, opportunity=doc.opportunity, title=doc.title,
                                            approved_by=request.user)
            messages.success(request, f"“{doc.title}” is shared in the room.")
        elif action == "upload":
            f = request.FILES.get("file")
            opp = logic.candidate_deals(room).filter(pk=request.POST.get("opportunity") or 0).first()
            if f:
                DataRoomFile.objects.create(room=room, file=f, opportunity=opp, approved_by=request.user,
                                            title=(request.POST.get("title", "").strip() or f.name)[:300])
                messages.success(request, "File shared in the room.")
        elif action == "unshare":
            DataRoomFile.objects.filter(room=room, pk=request.POST.get("file")).delete()
            messages.success(request, "File taken out of the room.")
        if not new_link:
            return redirect(room)
    switches = {s.opportunity_id: s.shown for s in DataRoomDeal.objects.filter(room=room)}
    deals = list(logic.candidate_deals(room).order_by("campus__code", "company__name"))
    for o in deals:
        o.room_shown = switches.get(o.pk, True)
    shared_docs = set(room.files.exclude(document=None).values_list("document_id", flat=True))
    docs = (Document.objects.filter(opportunity__in=deals).exclude(pk__in=shared_docs)
            .select_related("opportunity__company").order_by("opportunity__company__name", "title"))
    files = list(room.files.select_related("opportunity__company", "document", "approved_by"))
    newer = _newer_versions(files)
    for f in files:
        f.newer = newer.get(f.pk)
    return render(request, "crm/room/manage.html", {
        "room": room, "form": RoomSettingsForm(instance=room), "members": room.members.select_related("user"),
        "deals": deals, "docs": docs, "files": files,
        "views": logic.recent_views(room), "new_link": new_link, "invite_days": logic.INVITE_DAYS,
        "preview_url": reverse("crm:room_home") + f"?room={room.pk}",
    })
