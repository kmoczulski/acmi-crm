"""Finding and merging duplicate companies (workbook name variants, subsidiaries, etc.)."""
import re
from collections import defaultdict

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from . import interactions
from .models import (
    Activity, AISummary, Company, DuplicateDismissal, ListEntry, Note, Opportunity, Person, Priority, Task,
    suppress_history,
)

SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co", "llc", "ltd", "limited", "lp", "plc", "gmbh",
    "group", "holdings", "technologies", "technology", "tech", "systems", "industries", "international", "usa",
    "us", "america", "americas", "federal", "solutions", "the",
)
PRIORITY_RANK = {Priority.HIGH: 3, Priority.MID: 2, Priority.LOW: 1}


def core_name(name):
    """'L3Harris Technologies, Inc.' -> 'l3harris'; used to spot name variants."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0] == "the":
        words.pop(0)
    return "".join(words)


def _dismissed():
    return {tuple(sorted(d.company_ids)) for d in DuplicateDismissal.objects.all()}


def suggestions(limit=200):
    """Groups of companies that look like the same organisation, biggest first."""
    rows = list(Company.objects.annotate(n_people=Count("people", distinct=True)).values(
        "pk", "name", "domains", "status", "n_people", "touch_count", "last_interaction_at",
    ))
    parent = {r["pk"]: r["pk"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    reasons = defaultdict(set)
    by_domain, by_core = defaultdict(list), defaultdict(list)
    for r in rows:
        for d in r["domains"] or []:
            by_domain[d.lower()].append(r["pk"])
        core = core_name(r["name"])
        if len(core) >= 3:
            by_core[core].append(r["pk"])
    for label, groups in (("same email domain", by_domain), ("same name", by_core)):
        for key, pks in groups.items():
            for other in pks[1:]:
                union(pks[0], other)
            if len(pks) > 1:
                for pk in pks:
                    reasons[pk].add(f"{label}: {key}")

    clusters = defaultdict(list)
    for r in rows:
        clusters[find(r["pk"])].append(r)
    dismissed = _dismissed()
    out = []
    for members in clusters.values():
        if len(members) < 2 or tuple(sorted(m["pk"] for m in members)) in dismissed:
            continue
        members.sort(key=lambda m: (-m["n_people"], -m["touch_count"], len(m["name"])))
        keep_core = core_name(members[0]["name"])
        for m in members:
            # Pre-tick only clear name variants; a shared domain alone may be a real subsidiary.
            m["suggest"] = core_name(m["name"]) == keep_core
        out.append({
            "companies": members,
            "keep": members[0]["pk"],
            "reasons": sorted({reason for m in members for reason in reasons[m["pk"]]})[:4],
            "key": ",".join(str(m["pk"]) for m in sorted(members, key=lambda m: m["pk"])),
        })
    out.sort(key=lambda g: -sum(m["n_people"] + m["touch_count"] for m in g["companies"]))
    return out[:limit]


def dismiss(company_ids, user=None):
    DuplicateDismissal.objects.get_or_create(
        key=",".join(str(i) for i in sorted(company_ids)),
        defaults={"company_ids": sorted(company_ids), "created_by": user},
    )


def _fill(keep, other, field):
    if not getattr(keep, field) and getattr(other, field):
        setattr(keep, field, getattr(other, field))


@transaction.atomic
def merge(keep, others, user=None):
    """Fold `others` into `keep`. Returns a dict of counts."""
    others = [o for o in others if o.pk != keep.pk]
    counts = defaultdict(int)
    token = suppress_history.set(True)
    try:
        for other in others:
            # Fields: fill blanks, union lists, keep the higher priority.
            for field in ("description", "sector", "hq_location", "website", "linkedin", "fit_score", "owner_id",
                          "notes", "campus_plan"):
                _fill(keep, other, field)
            for field in ("domains", "categories", "flags"):
                merged = list(getattr(keep, field) or [])
                merged += [v for v in getattr(other, field) or [] if v not in merged]
                setattr(keep, field, merged)
            if PRIORITY_RANK.get(other.priority, 0) > PRIORITY_RANK.get(keep.priority, 0):
                keep.priority = other.priority
            if other.notes and other.notes not in keep.notes:
                keep.notes = f"{keep.notes}\n\n[From {other.name}] {other.notes}".strip() if keep.notes else other.notes
            keep.custom_fields = {**(other.custom_fields or {}), **(keep.custom_fields or {})}
            keep.campus_interest.add(*other.campus_interest.all())

            counts["people"] += Person.objects.filter(company=other).update(company=keep)
            counts["opportunities"] += Opportunity.objects.filter(company=other).update(company=keep)
            counts["tasks"] += Task.objects.filter(company=other).update(company=keep)
            counts["notes"] += Note.objects.filter(company=other).update(company=keep)

            # Emails seen on both records: keep one copy, carrying over teammates and attachments.
            kept_ids = dict(Activity.objects.filter(company=keep).exclude(message_id="").values_list("message_id", "pk"))
            for act in Activity.objects.filter(company=other).exclude(message_id=""):
                twin = kept_ids.get(act.message_id)
                if twin:
                    target = Activity.objects.get(pk=twin)
                    target.team_members.add(*act.team_members.all())
                    act.attachments.exclude(filename__in=target.attachments.values("filename")).update(activity=target)
                    act.delete()
                    counts["duplicate emails dropped"] += 1
            counts["timeline events"] += Activity.objects.filter(company=other).update(company=keep)

            for entry in ListEntry.objects.filter(company=other):
                if ListEntry.objects.filter(list=entry.list, company=keep).exists():
                    entry.delete()
                else:
                    entry.company = keep
                    entry.save(update_fields=["company"])
            AISummary.objects.filter(company=other).delete()
            Activity.objects.create(
                kind=Activity.Kind.FIELD_CHANGE, occurred_at=timezone.now(), actor=user, company=keep,
                summary=f"Merged {other.name} into this company", is_background=True,
                source=Activity.Source.USER,
            )
            other.delete()
            counts["companies merged"] += 1
        keep.save()
    finally:
        suppress_history.reset(token)
    AISummary.objects.filter(company=keep).delete()  # it no longer covers the whole history
    interactions.recompute(company_ids=[keep.pk], person_ids=list(keep.people.values_list("pk", flat=True)))
    return dict(counts)
