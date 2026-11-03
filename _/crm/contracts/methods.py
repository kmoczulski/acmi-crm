"""Built-in review templates (Notessa calls them methods): the points an abstract covers for each kind of agreement
ACMI signs. They're kept in step with this file by ensure_builtins(); to change one for a deal, copy it on the
templates page and edit the copy."""
from django.db import IntegrityError, transaction

from ..models import ContractTemplate

BUILTINS = [
    {
        "key": "psa", "name": "Land purchase (PSA) abstract", "kinds": ["psa", "option"],
        "description": "Price, deposits, feasibility, title, approvals, closing and exits for a land purchase.",
        "points": [
            ("Parties and property", "Buyer, seller, any guarantor; the land (address, tax parcels, acreage, "
                                     "exclusions); whether acreage is confirmed by survey."),
            ("Price", "Purchase price, any per-acre adjustment after the survey, credits."),
            ("Deposits", "Each deposit: amount, when due, escrow agent, when it goes hard (non-refundable), whether "
                         "it applies to the price."),
            ("Feasibility / due diligence", "Length, when it starts and ends, extensions and their cost, how the buyer "
                                            "terminates and what happens to the deposits."),
            ("Title and survey", "Objection deadlines, the seller's cure period and obligations, permitted "
                                 "exceptions, what happens if a defect isn't cured."),
            ("Approvals and entitlements", "Rezoning, site plan, subdivision, permits or utility commitments the deal "
                                           "depends on; deadlines; who applies and pays; seller cooperation."),
            ("Closing", "Closing date, outside date, extension options (notice, fees, whether they apply to the price), "
                        "conditions to close, deliverables."),
            ("Costs and prorations", "Transfer and recordation taxes, rollback or agricultural use taxes, title and "
                                     "escrow fees, prorations."),
            ("Seller's representations and covenants", "Key reps, how long they survive and any cap; what the "
                                                       "seller may not do before closing (new leases, liens, "
                                                       "timber, mining)."),
            ("Access and environmental", "Entry rights for testing, Phase I/II, insurance, restoration and indemnity "
                                         "for entry."),
            ("Assignment", "Can the buyer assign to an affiliate or project entity; consent needed."),
            ("Defaults and remedies", "Seller default (specific performance, damages, cap); buyer default "
                                      "(liquidated damages = deposits?)."),
            ("Termination rights", "Every way either side can end it, with the deadline and what happens to the "
                                   "deposits."),
            ("Brokers, 1031 and notices", "Commissions and who pays; 1031 exchange cooperation; notice addresses and "
                                          "methods."),
        ],
    },
    {
        "key": "loi", "name": "Letter of intent abstract", "kinds": ["loi"],
        "description": "Business terms, what is binding, exclusivity and what the definitive agreement must cover.",
        "points": [
            ("Parties and property or premises", "Who, and the land or space covered."),
            ("Business terms", "Price or rent, deposits, size, term, escalations, incentives."),
            ("Timing", "Due diligence, definitive agreement deadline, closing or delivery dates."),
            ("Conditions", "Approvals, financing, board or government approvals, other contingencies."),
            ("Binding provisions", "Which sections bind (confidentiality, exclusivity, costs, governing law) and "
                                   "which don't."),
            ("Exclusivity and expiration", "No-shop terms and length; when the LOI lapses if not signed or if no "
                                           "definitive agreement is signed."),
            ("Open items", "What the definitive agreement still has to settle."),
        ],
    },
    {
        "key": "lease", "name": "Lease abstract", "kinds": ["lease"],
        "description": "One page per lease: term, rent, delivery, options and termination rights.",
        "points": [
            ("Parties and guarantor", "Landlord, tenant, guarantor and guaranty limits."),
            ("Premises", "Building and land, square feet, parking, outdoor storage or yard, expansion space."),
            ("Term and key dates", "Commencement, rent commencement, expiration; how commencement is set."),
            ("Rent", "Base rent, escalations, free rent, additional rent (taxes, insurance, CAM / NNN), caps."),
            ("Security", "Deposit or letter of credit, burn-down."),
            ("Landlord work and delivery", "Build-to-suit scope, delivery deadline, late-delivery credits or "
                                           "termination, tenant improvement allowance."),
            ("Use", "Permitted use; hazardous or energetic materials, explosives arcs (QD), security clearance or "
                    "government requirements."),
            ("Options", "Renewal, expansion, ROFO / ROFR, purchase options: notice windows and terms."),
            ("Termination rights", "Early termination, casualty, condemnation, co-tenancy; notice and fees."),
            ("Assignment and subletting", "Consent standard, permitted transfers, recapture."),
            ("Maintenance, insurance and indemnity", "Who repairs what (roof, structure, systems), insurance "
                                                     "required, waivers."),
            ("Default and remedies", "Cure periods, landlord remedies, limits on landlord liability."),
            ("SNDA, estoppel and financing", "Obligations and response times."),
        ],
    },
    {
        "key": "pda", "name": "Development agreement abstract", "kinds": ["pda"],
        "description": "Obligations, incentives, clawbacks and deadlines in a public-private development agreement.",
        "points": [
            ("Parties and project", "The public body, the developer, the project and the land."),
            ("Developer obligations", "What we must build, invest or hire, and by when."),
            ("Public obligations", "Infrastructure, utilities, roads, permits or land the public side provides."),
            ("Incentives", "Tax abatements, PILOT, TIF, grants, fee waivers: amounts and conditions."),
            ("Performance tests and clawbacks", "Jobs, investment and completion tests; how they are measured; "
                                                "repayment if missed."),
            ("Approvals and vesting", "Entitlements granted or vested, and for how long."),
            ("Milestones and deadlines", "Every dated obligation in order."),
            ("Land conveyance", "Price, conditions, reversion or repurchase rights."),
            ("Assignment, default and term", "Transfers, cure periods, remedies, how long it lasts."),
        ],
    },
    {
        "key": "jv", "name": "JV / operating agreement abstract", "kinds": ["jv"],
        "description": "Capital, control, distributions and exits.",
        "points": [
            ("Members and interests", "Who holds what."),
            ("Capital contributions", "Initial and additional capital, deadlines, what happens if a member doesn't "
                                      "fund (dilution, member loans)."),
            ("Management and major decisions", "Who manages; decisions that need approval; deadlock."),
            ("Distributions", "Waterfall, preferred return, promote, timing."),
            ("Fees", "Development, asset management and other fees to members or affiliates."),
            ("Transfers", "Restrictions, ROFR / ROFO, tag-along, drag-along, buy-sell."),
            ("Removal and exit", "Removing the manager, sale rights, dissolution."),
            ("Reporting", "Budgets, financial statements, deadlines."),
        ],
    },
    {
        "key": "loan", "name": "Loan abstract", "kinds": ["loan"],
        "description": "Loan terms from the agreement and every amendment, with the covenant tests and their dates.",
        "points": [
            ("Parties and collateral", "Borrower, lender, guarantors, what secures it."),
            ("Amount and tranches", "Commitment, tranches, how much is drawn."),
            ("Interest", "Index, spread, floor, default rate, hedging requirements."),
            ("Term and extensions", "Maturity, extension options with conditions and fees."),
            ("Draws", "Conditions to each draw, retainage, budget reallocations."),
            ("Covenants and tests", "Financial covenants (DSCR, LTV, debt yield), completion deadlines, reporting; "
                                    "test dates."),
            ("Guaranties", "Completion, carve-out, payment; limits."),
            ("Reserves and prepayment", "Required reserves; prepayment terms and fees."),
            ("Events of default", "Triggers, cure periods, cross-defaults."),
        ],
    },
    {
        "key": "exits", "name": "Termination and exit rights", "kinds": [],
        "description": "Every way out for either side, with deadlines and consequences.",
        "points": [
            ("Our termination rights", "Each right ACMI has to end it: trigger, deadline, notice, cost, what happens "
                                       "to money paid."),
            ("Their termination rights", "The same for the other side."),
            ("Automatic termination", "Anything that ends it without notice (outside dates, failed conditions)."),
            ("After termination", "Refunds, surviving obligations, return of materials, recording releases."),
        ],
    },
    {
        "key": "risks", "name": "Risk review", "kinds": [],
        "description": "What's unusual, one-sided, unclear or missing, from ACMI's side of the table.",
        "points": [
            ("Off-market or one-sided terms", "Provisions unusual for this kind of deal or unfavorable to ACMI."),
            ("Ambiguities and conflicts", "Unclear language, and places where documents contradict each other."),
            ("Missing protections", "What a developer would expect to see that isn't there."),
            ("Deadlines at risk", "Dates that look hard to meet given how the deal is structured."),
            ("Questions for counsel", "What to raise with our lawyer."),
        ],
        "instructions": "Be direct and practical. Rank items by how much they could cost ACMI.",
    },
]


def ensure_builtins():
    """Create or update the built-in templates from BUILTINS. Cheap to call: it only writes what changed."""
    existing = {t.key: t for t in ContractTemplate.objects.exclude(key="")}
    for spec in BUILTINS:
        fields = {
            "name": spec["name"], "description": spec.get("description", ""), "kinds": spec.get("kinds", []),
            "points": [{"label": label, "ask": ask} for label, ask in spec["points"]],
            "instructions": spec.get("instructions", ""),
        }
        current = existing.get(spec["key"])
        if current is None:
            try:
                with transaction.atomic():
                    ContractTemplate.objects.create(key=spec["key"], **fields)
            except IntegrityError:
                pass  # another request made it a moment ago
        elif any(getattr(current, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(current, k, v)
            current.save()


def for_kind(kind):
    """Templates that suit a contract kind (and the ones for any kind), suited ones first."""
    ensure_builtins()
    templates = list(ContractTemplate.objects.all())
    suited = [t for t in templates if kind in (t.kinds or [])]
    general = [t for t in templates if not t.kinds]
    rest = [t for t in templates if t.kinds and kind not in t.kinds]
    return suited + general + rest


def parse_points(raw):
    """Template points from the edit form: one per line, "Label: what to cover"."""
    points = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        if not line:
            continue
        label, _, ask = line.partition(":")
        points.append({"label": label.strip()[:120], "ask": ask.strip()[:600]})
    return points


def points_text(points):
    return "\n".join(f"{p['label']}: {p['ask']}" if p.get("ask") else p["label"] for p in points or [])
