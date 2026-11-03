"""Seed the Opportunities pipeline from the Fortress pipeline tracker and the Trello board.

Sources (both read-only):
- ACMI_Properties_Pipeline_Tracker.xlsx (Tenants sheet): one row per deal, with size,
  cost, site and stage. These rows are what capital partners saw, so they are marked
  "on investor report" and their stage wins.
- A JSON export of the Trello "Properties-Prospecting" board (cards grouped by list).
  Lists map to stages; card members become the deal team and the first one the owner.
  Cards in "New Leads", "Cold" and admin lists are not deals and are skipped.

Re-running updates the same opportunities (keyed by `source_key`) instead of adding
new ones, and never moves a deal backwards that someone has since advanced in the CRM.
"""
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import merge
from .models import (
    STAGE_ORDER, Activity, Campus, Company, CompanyStatus, Note, Opportunity, OpportunityStage, Priority,
    normalize_key, suppress_history,
)

User = get_user_model()

TRACKER_STAGE = {
    "prospect": OpportunityStage.PROSPECT,
    "qualified": OpportunityStage.QUALIFIED,
    "loi / pda neg.": OpportunityStage.LOI_NEGOTIATION,
    "loi signed": OpportunityStage.LOI_SIGNED,
    "pda signed": OpportunityStage.LOI_SIGNED,
    "loi / pda signed": OpportunityStage.LOI_SIGNED,
    "comitted": OpportunityStage.SIGNED,
    "committed": OpportunityStage.SIGNED,
    "signed lease": OpportunityStage.SIGNED,
    "signed dma": OpportunityStage.SIGNED,
    "on hold": OpportunityStage.ON_HOLD,
    "cold": OpportunityStage.LOST,
}

TRELLO_STAGE = {
    "LEASE / PDA SIGNED": OpportunityStage.SIGNED,
    "LEASE NEOGTIATIONS": OpportunityStage.LEASE_NEGOTIATION,
    "LEASE NEGOTIATIONS": OpportunityStage.LEASE_NEGOTIATION,
    "LOIs Executed": OpportunityStage.LOI_SIGNED,
    "LOI / PDA NEGOTIATIONS": OpportunityStage.LOI_NEGOTIATION,
    "ACTIVE DISCUSSIONS (at least 1 meeting)": OpportunityStage.QUALIFIED,
    "EARLY ENGAGEMENT": OpportunityStage.PROSPECT,
}

TRELLO_PRIORITY = {"High Value": Priority.HIGH, "Medium Value": Priority.MID, "Low Value": Priority.LOW}

# Trello member name -> CRM username. Paul and John get accounts created (no password;
# they sign in with Google once the app is hosted).
TEAM = {
    "Kellen Moczulski": ("kellen", "Kellen", "Moczulski"),
    "Simon Shewmaker": ("simon", "Simon", "Shewmaker"),
    "David DeNardo": ("dave", "Dave", "DeNardo"),
    "David Dowell": ("dowell", "David", "Dowell"),
    "Paul Kadzielski": ("paul", "Paul", "Kadzielski"),
    "John Burer": ("john", "John", "Burer"),
}
LEAD_NAMES = {"kellen": "kellen", "dave": "dave", "simon": "simon", "paul": "paul", "john": "john", "dowell": "dowell"}

# Names that differ between the tracker, Trello and the CRM.
ALIASES = {
    "ara (applied research associates)": "Applied Research Associates",
    "jhu": "Johns Hopkins University",
    "elbitamerica, inc.": "Elbit America / 26Defense",
    "fortus federal technologies": "Fortus Technologies",
    "castelion": "Castellion",
    "prometheus (kratos / rafael)": "Prometheus",
    "rmsl (rocky mountain scientific laboratory)": "Rocky Mountain Scientific Laboratory",
    "purdue (perc)": "Purdue University",
    "erg (eastern research group)": "Eastern Research Group",
    "physical sciences inc. (psi)": "Physical Sciences Inc.",
    "navy / hadrian": "Hadrian",
    "vts / voyager": "Voyager Technologies",
    "solugen & odin energetics": "Solugen",
    "stardust energetics / duygu group": "Stardust Energetics",
    "paligen / repkon": "Repkon",
    "hanwha defense usa, inc.": "Hanwha Defense USA",
    "hodgdon": "Hodgdon Powder",
    "electra": "Electra.aero",
    "lacamas labs": "Lacamas Laboratories",
    "materials resources llc": "MRL Materials Resources",
}
# Tracker rows that are a second, separate deal with a company that has its own Trello card
# (Ursa Major's tracker row is "an additional campus"; its Trello card is the executed LOI).
SEPARATE_FROM_TRELLO = {"ursa major"}
# Tracker company -> the CRM company it should land on (looked up after ALIASES).
COMPANY_LOOKUP_EXTRA = {
    "elbit america / 26defense": ["elbit america", "elbit systems of america", "elbit"],
    "johns hopkins university": ["johns hopkins university applied physics laboratory", "jhu apl", "johns hopkins"],
    "castellion": ["castelion"],
    "rocky mountain scientific laboratory": ["rmsl"],
}


def _num(value):
    if value in (None, "", "-", "N/A", "TBD") or isinstance(value, str) and not re.fullmatch(r"[\d,.]+", value.strip()):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def _text(value):
    return " ".join(str(value).split()) if value not in (None, "-") else ""


def team_users(stdout=None):
    """Make sure every Trello member has a CRM user; returns {trello full name: User}."""
    users = {}
    for full, (username, first, last) in TEAM.items():
        user, created = User.objects.get_or_create(username=username, defaults={"first_name": first, "last_name": last})
        if created:
            user.set_unusable_password()
            user.save()
            if stdout:
                stdout.write(f"  created user {username} ({first} {last})")
        elif not user.last_name or user.first_name != first:
            user.first_name, user.last_name = first, last
            user.save(update_fields=["first_name", "last_name"])
        users[full] = user
    return users


class CompanyFinder:
    """Matches a deal's counterparty name to a CRM company (exact, alias, core name, parenthetical)."""

    def __init__(self):
        self.by_key = {}
        self.by_core = {}
        for pk, name, key in Company.objects.values_list("pk", "name", "name_key"):
            self.by_key[key] = pk
            self.by_core.setdefault(merge.core_name(name), pk)

    def find(self, raw):
        name = ALIASES.get(normalize_key(raw), raw)
        candidates = [name, raw, *COMPANY_LOOKUP_EXTRA.get(normalize_key(name), [])]
        inner = re.search(r"\(([^)]+)\)", raw)
        if inner:
            candidates += [raw[: inner.start()].strip(), inner.group(1)]
        candidates += [p.strip() for p in re.split(r"\s*/\s*|\s*&\s*", name) if p.strip()]
        for c in candidates:
            if pk := self.by_key.get(normalize_key(c)):
                return pk
        for c in candidates:
            core = merge.core_name(c)
            if len(core) >= 4 and (pk := self.by_core.get(core)):
                return pk
        return None

    def get_or_create(self, raw, created_list):
        pk = self.find(raw)
        if pk:
            return Company.objects.get(pk=pk)
        name = _text(ALIASES.get(normalize_key(raw), raw))
        company = Company.objects.create(name=name, status=CompanyStatus.ENGAGED)
        self.by_key[company.name_key] = company.pk
        self.by_core.setdefault(merge.core_name(name), company.pk)
        created_list.append(name)
        return company


def read_tracker(path):
    """Tenant rows from the tracker's Tenants sheet: [{section, site, deal_type, stage, ...}]."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Tenants"]
    rows, section = [], ""
    for r in ws.iter_rows(values_only=True):
        first = r[0]
        if isinstance(first, str) and first.strip() in ("Signed Agreements", "Active Discussions", "Prospects"):
            section = first.strip()
            continue
        if not section or not r[3] or str(r[3]).startswith("Tenant /"):
            continue
        rows.append({
            "section": section,
            "site": _text(r[0]),
            "deal_type": _text(r[1]),
            "stage": _text(r[2]),
            "tenant": _text(r[3]),
            "asset_type": _text(r[4]),
            "acres": _num(r[5]),
            "sf": _num(r[6]),
            "sf_text": _text(r[6]) if _num(r[6]) is None else "",
            "cost": _num(r[7]),
            "lead": _text(r[9]),
            "target_close": r[10].date() if isinstance(r[10], datetime) else None,
            "delivery": r[11].date() if isinstance(r[11], datetime) else None,
            "notes": _text(r[12]),
        })
    return rows


def trello_stage(card):
    """The "LEASE / PDA SIGNED" list mixes signed leases with executed PDAs; the label tells them apart."""
    stage = TRELLO_STAGE[card["list"]["name"]]
    if stage == OpportunityStage.SIGNED and any(l["name"] == "PDA Executed" for l in card["labels"]):
        return OpportunityStage.LOI_SIGNED
    return stage


def read_trello(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    cards = data["cards"]["nodes"] if isinstance(data, dict) else data
    return [c for c in cards if c["list"]["name"] in TRELLO_STAGE]


def _campus_and_site(site, campuses):
    code = site.strip().upper()
    if code in campuses:
        return campuses[code], ""
    return None, site.strip() or "TBD"


@transaction.atomic
def run(tracker_path, trello_path, stdout, dry_run=False):
    token = suppress_history.set(True)
    try:
        return _run(tracker_path, trello_path, stdout, dry_run)
    finally:
        suppress_history.reset(token)


def _run(tracker_path, trello_path, stdout, dry_run):
    users = team_users(stdout)
    by_username = {u.username: u for u in users.values()}
    campuses = {c.code.upper(): c for c in Campus.objects.all()}
    finder = CompanyFinder()
    created_companies, report = [], {"created": 0, "updated": 0, "differences": []}

    tracker = read_tracker(tracker_path) if tracker_path else []
    cards = read_trello(trello_path) if trello_path else []
    card_by_company = {}
    for card in cards:
        company = finder.get_or_create(card["name"], created_companies)
        card_by_company.setdefault(company.pk, card)

    def upsert(key, company, fields, team=(), note=""):
        opp = Opportunity.objects.filter(source_key=key).first()
        if opp is None:
            opp = Opportunity(source_key=key, company=company, **fields)
            opp.save()
            report["created"] += 1
            Activity.objects.create(
                kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), company=company, opportunity=opp,
                summary=f"Opportunity imported: {opp.name}", source=Activity.Source.IMPORT, is_background=True,
            )
        else:
            new_stage = fields.pop("stage")
            if STAGE_ORDER.get(new_stage, 0) > STAGE_ORDER.get(opp.stage, 0):
                opp.stage = new_stage
            for k, v in fields.items():
                if v not in (None, "") and getattr(opp, k) in (None, ""):
                    setattr(opp, k, v)
            opp.save()
            report["updated"] += 1
        if team:
            opp.team.add(*team)
        if note and not opp.notes_set.filter(body=note).exists():
            Note.objects.create(body=note, company=company, opportunity=opp)
        return opp

    used_cards = set()
    seen_tracker_companies = {}
    for row in tracker:
        company = finder.get_or_create(row["tenant"], created_companies)
        n = seen_tracker_companies[company.pk] = seen_tracker_companies.get(company.pk, 0) + 1
        campus, site = _campus_and_site(row["site"], campuses)
        stage = TRACKER_STAGE.get(row["stage"].lower(), OpportunityStage.PROSPECT)
        card = card_by_company.get(company.pk) if n == 1 and normalize_key(company.name) not in SEPARATE_FROM_TRELLO else None
        team, owner = [], by_username.get(LEAD_NAMES.get(row["lead"].lower(), ""))
        if card:
            used_cards.add(card["id"])
            team = [users[m["fullName"]] for m in card["members"] if m["fullName"] in users]
            owner = owner or (team[0] if team else None)
            card_stage = trello_stage(card)
            if card_stage != stage:
                # Trello is updated more often than the tracker (last saved 2026-09-28): take the
                # further-along stage of the two, and list the difference for review.
                later = card_stage if STAGE_ORDER[card_stage] > STAGE_ORDER[stage] else stage
                report["differences"].append(
                    f"{company.name}: tracker {row['stage']}, Trello {card['list']['name']} -> {later.label}"
                )
                stage = later
        label = site if not campus else campus.code
        fields = {
            "name": f"{company.name} · {label}" + (f" {row['deal_type'].lower()}" if row["deal_type"] else ""),
            "campus": campus, "site": site, "stage": stage, "owner": owner,
            "deal_type": row["deal_type"] if row["deal_type"] in dict(Opportunity._meta.get_field("deal_type").choices) else "",
            "asset_type": row["asset_type"] if row["asset_type"] in dict(Opportunity._meta.get_field("asset_type").choices) else "",
            "acreage_need": row["acres"], "sq_ft_need": int(row["sf"]) if row["sf"] else None,
            "timeline": f"Size: {row['sf_text']} sf" if row["sf_text"] not in ("", "TBD") else "",
            "value": row["cost"], "lead_source": row["lead"], "target_close": row["target_close"],
            "est_delivery": row["delivery"], "next_step": row["notes"][:300], "investor_report": True,
            "trello_url": card["url"] if card else "",
            "priority": next((TRELLO_PRIORITY[l["name"]] for l in card["labels"] if l["name"] in TRELLO_PRIORITY), "") if card else "",
        }
        desc = _card_note(card) if card else ""
        upsert(f"tracker:{company.name_key}:{n}", company, fields, team, desc)

    for card in cards:
        if card["id"] in used_cards:
            continue
        company = finder.get_or_create(card["name"], created_companies)
        if card_by_company.get(company.pk, {}).get("id") != card["id"]:
            continue  # two cards for one company: the first one wins
        labels = {l["name"] for l in card["labels"]}
        camp = [c for c in ("MEIH", "NSIH") if c in labels]
        team = [users[m["fullName"]] for m in card["members"] if m["fullName"] in users]
        fields = {
            "name": company.name + (f" · {camp[0]}" if len(camp) == 1 else ""),
            "campus": campuses.get(camp[0]) if len(camp) == 1 else None,
            "site": "" if len(camp) == 1 else ("NSIH or MEIH" if len(camp) == 2 else ""),
            "stage": trello_stage(card),
            "owner": team[0] if team else None,
            "trello_url": card["url"],
            "priority": next((TRELLO_PRIORITY[l] for l in labels if l in TRELLO_PRIORITY), ""),
            "next_step_due": _date(card.get("due")) if not card.get("dueComplete") else None,
        }
        upsert(f"trello:{card['id'].rsplit('/', 1)[-1]}", company, fields, team, _card_note(card))

    _owner_from_connections()
    _advance_companies()
    report["owners_filled"] = _fill_company_owners()
    report["companies_created"] = created_companies
    if dry_run:
        transaction.set_rollback(True)
    return report


def _date(value):
    if not value:
        return None
    return timezone.localtime(datetime.fromisoformat(value.replace("Z", "+00:00"))).date()


def _card_note(card):
    desc = (card.get("desc") or "").replace("‌", "").replace("**", "").strip()
    if not desc:
        return ""
    return f"From Trello ({card['list']['name']}), {card['url']}\n\n{desc}"


def _advance_companies():
    """Company stage follows its best deal: signed -> Tenant, otherwise open deal -> Opportunity."""
    early = [CompanyStatus.PROSPECT, CompanyStatus.CONTACTED, CompanyStatus.ENGAGED, CompanyStatus.MEETING]
    signed = Opportunity.objects.filter(stage=OpportunityStage.SIGNED).values("company")
    Company.objects.filter(pk__in=signed).exclude(status=CompanyStatus.TENANT).update(status=CompanyStatus.TENANT)
    open_ = Opportunity.objects.filter(stage__in=[
        OpportunityStage.QUALIFIED, OpportunityStage.LOI_NEGOTIATION, OpportunityStage.LOI_SIGNED,
        OpportunityStage.LEASE_NEGOTIATION,
    ]).values("company")
    Company.objects.filter(pk__in=open_, status__in=early).update(status=CompanyStatus.OPPORTUNITY)


def _owner_from_connections():
    """Trello lists members in join order, not by who leads. Prefer the teammate with the
    strongest email relationship to the company, when they are on the deal team."""
    for opp in Opportunity.objects.exclude(source_key=None).select_related("company").prefetch_related("team"):
        best = opp.company.strongest_connection_user_id
        if best and best != opp.owner_id and best in {u.pk for u in opp.team.all()}:
            opp.owner_id = best
            opp.save(update_fields=["owner"])


def _fill_company_owners():
    n = 0
    for opp in Opportunity.objects.filter(owner__isnull=False, company__owner__isnull=True).select_related("company"):
        if opp.company.owner_id is None:
            opp.company.owner_id = opp.owner_id
            opp.company.save(update_fields=["owner"])
            n += 1
    return n
