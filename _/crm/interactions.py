"""Derives the computed interaction fields on Company and Person from Activity.

These replace the workbook's # Touches, Last Outreach, Days Since Touch and
Last Response columns (spec section 1).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from .models import Activity, Company, Person

FIELDS = [
    "first_interaction_at", "last_interaction_at", "next_interaction_at", "last_outreach_at",
    "last_reply_at", "touch_count", "last_response", "last_response_at",
    "strongest_connection_user_id", "connection_strength",
]
INBOUND = (Activity.Kind.REPLY, Activity.Kind.EMAIL_RECEIVED)
EMAIL_KINDS = (Activity.Kind.EMAIL_SENT, *INBOUND)
STRENGTH_RANK = {"": 0, "Weak": 1, "Medium": 2, "Strong": 3}
RECENT_DAYS = 90


def strength(sent, inbound, recent_inbound):
    """Strong: a real two-way conversation lately. Medium: they have written back. Weak: outbound only."""
    if recent_inbound >= 3:
        return "Strong"
    if inbound >= 1:
        return "Medium"
    return "Weak" if sent else ""


def _team_rows(fk, ids):
    """Per (record, teammate): emails sent, received, recent received, last email."""
    through = Activity.team_members.through.objects.filter(
        **{f"activity__{fk}__isnull": False}, activity__kind__in=EMAIL_KINDS
    )
    if ids is not None:
        through = through.filter(**{f"activity__{fk}__in": ids})
    recent = timezone.now() - timedelta(days=RECENT_DAYS)
    return through.values(f"activity__{fk}", "user_id").annotate(
        sent=Count("id", filter=Q(activity__kind=Activity.Kind.EMAIL_SENT)),
        inbound=Count("id", filter=Q(activity__kind__in=INBOUND)),
        recent_inbound=Count("id", filter=Q(activity__kind__in=INBOUND, activity__occurred_at__gte=recent)),
        last=Max("activity__occurred_at"),
    )


def _rank(row):
    return (STRENGTH_RANK[strength(row["sent"], row["inbound"], row["recent_inbound"])], row["inbound"],
            row["sent"], row["last"])


def team_connections(company=None, person=None):
    """Who on the team emails this company or person, strongest first, for the record page."""
    fk, obj = ("company", company) if company is not None else ("person", person)
    rows = sorted(_team_rows(fk, [obj.pk]), key=_rank, reverse=True)
    users = get_user_model().objects.in_bulk([r["user_id"] for r in rows])
    out = []
    for r in rows:
        contacts = []
        if fk == "company":
            contacts = list(
                Activity.objects.filter(company=obj, team_members=r["user_id"], kind__in=EMAIL_KINDS,
                                        person__isnull=False)
                .values("person_id", "person__full_name", "person__email")
                .annotate(n=Count("id")).order_by("-n")[:3]
            )
        out.append({
            "user": users.get(r["user_id"]),
            "sent": r["sent"],
            "inbound": r["inbound"],
            "last": r["last"],
            "strength": strength(r["sent"], r["inbound"], r["recent_inbound"]),
            "contacts": contacts,
        })
    return out


def _aggregate(fk, ids):
    now = timezone.now()
    qs = Activity.objects.filter(**{f"{fk}__isnull": False})
    if ids is not None:
        qs = qs.filter(**{f"{fk}__in": ids})
    past_interaction = Q(kind__in=Activity.INTERACTION_KINDS, occurred_at__lte=now)
    rows = qs.values(fk).annotate(
        first_interaction_at=Min("occurred_at", filter=past_interaction),
        last_interaction_at=Max("occurred_at", filter=past_interaction),
        next_interaction_at=Min("occurred_at", filter=Q(kind=Activity.Kind.MEETING, occurred_at__gt=now)),
        last_outreach_at=Max("occurred_at", filter=Q(kind=Activity.Kind.EMAIL_SENT)),
        last_reply_at=Max("occurred_at", filter=Q(kind__in=[Activity.Kind.REPLY, Activity.Kind.EMAIL_RECEIVED])),
        touch_count=Count("id", filter=Q(kind=Activity.Kind.EMAIL_SENT)),
    )
    result = {r[fk]: r for r in rows}

    # Latest response outcome: one pass over response activities, newest first.
    responses = qs.filter(kind__in=Activity.RESPONSE_KINDS).order_by(fk, "-occurred_at", "-id")
    seen = set()
    for key, outcome, at in responses.values_list(fk, "outcome", "occurred_at"):
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(key, {}).update(last_response=outcome, last_response_at=at)

    best = {}
    for row in _team_rows(fk, ids):
        key = row[f"activity__{fk}"]
        if key not in best or _rank(row) > _rank(best[key]):
            best[key] = row
    for key, row in best.items():
        result.setdefault(key, {}).update(
            strongest_connection_user_id=row["user_id"],
            connection_strength=strength(row["sent"], row["inbound"], row["recent_inbound"]),
        )
    return result


def _apply(model, fk, ids, batch_size=500):
    data = _aggregate(fk, ids)
    qs = model.objects.all() if ids is None else model.objects.filter(pk__in=ids)
    to_update = []
    for obj in qs.only("pk", *FIELDS).iterator(chunk_size=2000):
        row = data.get(obj.pk, {})
        changed = False
        for f in FIELDS:
            default = 0 if f == "touch_count" else ("" if f in ("last_response", "connection_strength") else None)
            value = row.get(f) if row.get(f) is not None else default
            if getattr(obj, f) != value:
                setattr(obj, f, value)
                changed = True
        if changed:
            to_update.append(obj)
    model.objects.bulk_update(to_update, [f.removesuffix("_id") for f in FIELDS], batch_size=batch_size)
    return len(to_update)


def recompute(company_ids=None, person_ids=None):
    """Recompute for the given records, or for everything when both are None."""
    everything = company_ids is None and person_ids is None
    n = 0
    if everything or company_ids:
        n += _apply(Company, "company", None if everything else list(company_ids))
    if everything or person_ids:
        n += _apply(Person, "person", None if everything else list(person_ids))
    return n


def recompute_for_activity(activity):
    recompute(
        company_ids=[activity.company_id] if activity.company_id else [],
        person_ids=[activity.person_id] if activity.person_id else [],
    )
