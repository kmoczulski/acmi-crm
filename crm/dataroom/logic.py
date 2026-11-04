"""The capital partners' data room: which deals a room shows, the only fields a partner ever sees, invites and the
partner's account, sign-in throttling, and the room's log.

Everything a partner sees is built here from an allow-list (ROOM_COLUMNS, CARD_FIELDS). The room's pages never reuse
an internal page or template with parts hidden, so a field added to an internal page later can't reach a partner.
"""
import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from ..models import DataRoomDeal, DataRoomLoginAttempt, DataRoomView, Opportunity, OpportunityStage
from ..permissions import PARTNERS
from ..reports import BY_KEY, cell_value

INVITE_DAYS = 14
# Sign-in: this many wrong passwords for one account or one address within the window locks it for the window.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

# Report columns a room can show, in the report's order (the room's pipeline looks exactly like the investor report).
# Never: lead / source, owner, next step / notes, rent, the Lease / LOI link.
ROOM_COLUMNS = ["site", "deal_type", "stage", "tenant", "asset_type", "acres", "size", "cost", "psf", "roc", "win",
                "weighted", "target_close", "est_delivery"]
NUMERIC = {"acres", "size", "cost", "psf", "roc", "win", "weighted"}
COST_COLUMNS = {"cost", "psf", "roc"}
WIN_COLUMNS = {"win", "weighted"}
# Stages a partner sees as columns on the board. On hold and cold / lost deals never reach a room.
ROOM_STAGES = [s for s in OpportunityStage.values if s not in (OpportunityStage.ON_HOLD, OpportunityStage.LOST)]


def columns(room):
    keys = [k for k in ROOM_COLUMNS
            if (room.show_costs or k not in COST_COLUMNS) and (room.show_win or k not in WIN_COLUMNS)]
    return [BY_KEY[k] for k in keys]


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------


def candidate_deals(room):
    """Deals on the investor report at the room's campuses, open stages only: what the switches apply to."""
    qs = (Opportunity.objects.filter(investor_report=True, stage__in=ROOM_STAGES)
          .select_related("company", "campus"))
    campuses = list(room.campuses.all())
    if campuses:
        qs = qs.filter(campus__in=campuses)
    return qs


def visible_deals(room):
    """The deals the partner sees: candidates not switched off for this room."""
    off = DataRoomDeal.objects.filter(room=room, shown=False).values("opportunity_id")
    return candidate_deals(room).exclude(pk__in=off)


def deal_for(room, pk):
    return visible_deals(room).filter(pk=pk).first()


def set_shown(room, opp, shown, user):
    DataRoomDeal.objects.update_or_create(room=room, opportunity=opp, defaults={"shown": shown, "changed_by": user})


def room_cell(o, key):
    """A deal's cell as the room shows it (plain text; the tenant links inside the room)."""
    if key == "stage":
        return o.get_stage_display()
    value = cell_value(o, key)
    if value in (None, ""):
        return "TBD" if key == "cost" else ""
    if key == "roc":
        return f"{value:.1%}"
    if key in ("cost", "psf", "weighted"):
        return f"${value:,.0f}"
    if key == "win":
        return f"{value:.0%}"
    if key == "size":
        return f"{value:,}"
    if key == "acres":
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if key in ("target_close", "est_delivery"):
        return f"{value:%b} {value.year}"
    return str(value)


def sections(room, deals):
    """The pipeline in the investor report's sections (Signed agreements, Active discussions, Prospects), each with
    its deals' room rows and totals. Totals only add up columns the room shows."""
    from ..models import STAGE_ORDER
    from ..views import REPORT_SECTIONS

    keys = {c.key for c in columns(room)}
    deals = sorted(deals, key=lambda o: (-STAGE_ORDER[o.stage], o.site_label, -(o.value or 0), o.company.name))
    out = []
    for title, stages in REPORT_SECTIONS:
        opps = [o for o in deals if o.stage in stages]
        out.append({
            "title": title, "rows": opps, "room_rows": rows(room, opps),
            "sf": sum(o.sq_ft_need or 0 for o in opps) if "size" in keys else None,
            "cost": sum(o.value or 0 for o in opps) if "cost" in keys else None,
            "weighted": sum(o.weighted_value for o in opps) if "weighted" in keys and "cost" in keys else None,
        })
    return out


def sites(room, deals):
    """One row per development site, the totals of the room's deals there, as on the investor report. The Excel
    copy leaves out the campus's location and status notes (pipeline_workbook(partner=True))."""
    out = {}
    for o in deals:
        row = out.setdefault(o.site_label, {"site": o.site_label, "campus": o.campus, "tenants": 0, "sf": 0, "cost": 0,
                                            "weighted": 0, "acres": 0, "priced_sf": 0, "priced_cost": 0})
        row["tenants"] += 1
        row["sf"] += o.sq_ft_need or 0
        row["acres"] += o.acreage_need or 0
        if room.show_costs:
            row["cost"] += o.value or 0
            if room.show_win:
                row["weighted"] += o.weighted_value
            if o.value and o.sq_ft_need:  # $/sf only from deals that have both
                row["priced_sf"] += o.sq_ft_need
                row["priced_cost"] += o.value
    for row in out.values():
        row["psf"] = row["priced_cost"] / row["priced_sf"] if row["priced_sf"] else None
    return sorted(out.values(), key=lambda r: (r["campus"] is None, r["site"]))


def rows(room, deals):
    cols = columns(room)
    return [{"deal": o, "url": reverse("crm:room_deal", args=[o.pk]),
             "cells": [(c, room_cell(o, c.key)) for c in cols]} for o in deals]


def card(room, o):
    """The company card for one deal: (label, value) pairs, each from the allow-list, blanks left out."""
    company = o.company
    facts = [
        ("Company", company.name),
        ("Website", _website(company.website)),
        ("Sector", company.sector),
        ("Campus / project", o.site_label),
        ("Stage", o.get_stage_display()),
        ("Deal type", o.get_deal_type_display()),
        ("Asset type", o.get_asset_type_display()),
        ("Acres", room_cell(o, "acres")),
        ("Size (sf)", room_cell(o, "size")),
    ]
    if room.show_costs:
        facts += [("Project cost", room_cell(o, "cost")), ("$ PSF", room_cell(o, "psf")), ("ROC", room_cell(o, "roc"))]
    if room.show_win:
        facts += [("Win %", room_cell(o, "win"))]
    facts += [("Target close", room_cell(o, "target_close")), ("Est. delivery", room_cell(o, "est_delivery"))]
    return [(label, value) for label, value in facts if value not in (None, "")]


def _website(url):
    """Only a plain web address reaches the page as a link."""
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url if url.lower().startswith(("http://", "https://")) and " " not in url else ""


def files_for(room, o=None):
    """The room's approved files: the room-wide ones, or one deal's."""
    qs = room.files.select_related("document")
    return qs.filter(opportunity=o) if o is not None else qs.filter(opportunity=None)


# ---------------------------------------------------------------------------
# Invites and accounts
# ---------------------------------------------------------------------------


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_invite(member, by):
    """A fresh sign-in link for the member (making the account, or setting a new password). Only its hash is kept;
    the link is shown once, to the Manager, to send."""
    token = secrets.token_urlsafe(32)
    member.invite_hash = _hash(token)
    member.invite_expires = timezone.now() + timedelta(days=INVITE_DAYS)
    member.invited_by = by
    member.invited_at = timezone.now()
    member.save(update_fields=["invite_hash", "invite_expires", "invited_by", "invited_at"])
    return reverse("crm:room_invite", args=[token])


def member_for_invite(token):
    from ..models import DataRoomMember

    if not token or len(token) < 20:
        return None
    m = DataRoomMember.objects.select_related("room", "user").filter(invite_hash=_hash(token)).first()
    if m is None or not m.active or not m.room.active or not m.invite_expires or m.invite_expires < timezone.now():
        return None
    return m


@transaction.atomic
def accept_invite(member, first_name, last_name, password):
    """Make (or update) the member's account with the password they chose; the link stops working."""
    User = get_user_model()
    user = member.user
    if user is None:
        user = User.objects.create_user(username=member.email.lower(), email=member.email.lower(),
                                        first_name=first_name, last_name=last_name)
        user.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        member.user = user
    else:
        user.first_name, user.last_name = first_name, last_name
    user.is_active = True
    user.set_password(password)
    user.save()
    member.invite_hash = ""
    member.invite_expires = None
    member.accepted_at = member.accepted_at or timezone.now()
    member.save(update_fields=["user", "invite_hash", "invite_expires", "accepted_at"])
    return user


def switch_member(member, active):
    member.active = active
    member.save(update_fields=["active"])
    if member.user_id:
        member.user.is_active = active
        member.user.save(update_fields=["is_active"])


# ---------------------------------------------------------------------------
# Sign-in throttling and the log
# ---------------------------------------------------------------------------


def _recent():
    return DataRoomLoginAttempt.objects.filter(at__gte=timezone.now() - timedelta(seconds=FAILURE_WINDOW))


def _who(username, ip):
    """The (account, address) pair an attempt is counted under; blanks count under nothing."""
    return (username or "").strip().lower()[:254], (ip or "").strip()[:45]


def locked(username, ip):
    """MAX_FAILURES wrong passwords within the window, for this account or from this address. Counted in the database
    (DataRoomLoginAttempt), so every server process and instance sees the same count."""
    username, ip = _who(username, ip)
    recent = _recent()
    return ((bool(username) and recent.filter(username=username).count() >= MAX_FAILURES)
            or (bool(ip) and recent.filter(ip=ip).count() >= MAX_FAILURES))


def failed(username, ip):
    username, ip = _who(username, ip)
    DataRoomLoginAttempt.objects.create(username=username, ip=ip)
    # Old rows are only clutter once they're outside the window; drop them as we go.
    DataRoomLoginAttempt.objects.filter(at__lt=timezone.now() - timedelta(seconds=FAILURE_WINDOW)).delete()


def cleared(username, ip):
    """A right password clears the account's and the address's strikes."""
    username, ip = _who(username, ip)
    q = Q()
    if username:
        q |= Q(username=username)
    if ip:
        q |= Q(ip=ip)
    if q:
        DataRoomLoginAttempt.objects.filter(q).delete()


def log(room, member, page, opportunity=None, file=None):
    """Record a partner's visit; Managers previewing the room (member None) aren't logged."""
    if member is None:
        return
    DataRoomView.objects.create(room=room, member=member, page=page, opportunity=opportunity, file=file)
    member.last_seen = timezone.now()
    member.save(update_fields=["last_seen"])


def recent_views(room, limit=100):
    return (DataRoomView.objects.filter(room=room).select_related("member", "opportunity__company", "file")
            .filter(Q(member__isnull=False))[:limit])
