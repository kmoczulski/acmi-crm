"""A dummy client land search for demos: a made-up client, three made-up sites with full diligence reports, ranked and
shareable through the guest link. Everything is fictional and tagged DEMO so it can't be mistaken for a real deal.

    seed_demo_client_report            # dry run: says what it would make
    seed_demo_client_report --commit   # writes it (safe to re-run: rebuilds the same demo)
    seed_demo_client_report --remove --commit
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction
from shapely.geometry import box, mapping

from crm.land import assignments as A
from crm.models import Assignment, AssignmentSite, Company, LandCounty, LandSite, Parcel

NAME = "DEMO · Meridian Defense land search"
CLIENT = "Meridian Defense Systems (demo)"
FIPS = "99001"  # not a real county

SITES = [
    # name, parcel (key, x0, y0, size, acres, zoning, city), ratings, net deductions, ROM $, narrative
    {
        "name": "DEMO · Prairie Ridge",
        "parcels": [("DEMO-PR-1", -98.60, 34.60, 0.030, 612.0), ("DEMO-PR-2", -98.56, 34.60, 0.012, 214.0)],
        "zoning": "I-2 Heavy Industrial", "city": "Prairie Ridge",
        "cats": {
            "land": ("low", "Two owners, clean chain of title; no recorded liens. One pipeline easement crosses the north edge.",
                     "Order ALTA survey; ask title for easement endorsement.", "ACMI Land", False, True),
            "zoning": ("low", "Zoned I-2; explosives manufacturing is permitted by right with a site plan review.",
                       "Obtain zoning verification letter.", "ACMI Entitlements", False, True),
            "qd": ("moderate", "Governing HD 1.1 arc closes inside the parcel boundary on three of four cases; one rural road falls inside the PTRD arc.",
                   "Scope road relocation or easement with the county; rerun arcs after NEW update.", "SMS Energetics", True, False),
            "environmental": ("low", "Cropland and pasture for 60+ years; no RECs identified in the desktop screen.",
                              "Commission Phase I ESA after site selection.", "ACMI Environmental", False, False),
            "geotech": ("low", "Published soils are clay loam over weathered shale; gentle 2 to 4 percent slopes.",
                        "Confirmatory borings at building pads.", "Civil engineer", False, False),
            "flood": ("low", "No mapped FEMA floodplain on the developable area; one intermittent creek in the SE corner.",
                      "Hydrology concept with the civil package.", "Civil engineer", False, False),
            "electric": ("moderate", "Utility reports 115 kV line 1.8 miles away; capacity for the full program is not yet committed.",
                         "Submit load request and secure a capacity commitment before PSA execution.", "ACMI Utilities", True, True),
            "water": ("low", "12-inch main at the property line; wastewater by a county lift station 0.6 miles away.",
                      "Request will-serve letters.", "ACMI Utilities", False, False),
            "gas": ("low", "8-inch transmission line crosses the site; tap available.", "Binding estimate from the gas utility.", "ACMI Utilities", False, False),
            "rail": ("low", "Class III short line 0.9 miles south; extension under discussion.", "Carrier engineering consultation.", "ACMI Rail", False, False),
        },
        "net": {"wetlands": "6", "floodway": "18", "easements": "22", "qd": "0"},
        "rom": ["3,200,000", "9,500,000", "2,400,000", "4,100,000", "6,800,000", "2,900,000", "1,800,000", "0", "0", "3,000,000"],
        "summary": ("Prairie Ridge is the strongest of the three candidates. It offers about 826 gross acres with by-right industrial "
                    "zoning, utilities near the property line, and rail within a mile. Two moderate items remain: the QD arc "
                    "closure on one rural road, and the electric capacity commitment, which is schedule-critical."),
        "done": 21,
    },
    {
        "name": "DEMO · Cedar Flats",
        "parcels": [("DEMO-CF-1", -98.40, 34.55, 0.028, 540.0), ("DEMO-CF-2", -98.37, 34.55, 0.020, 291.0)],
        "zoning": "A-1 Agricultural (rezoning required)", "city": "Cedar Flats",
        "cats": {
            "land": ("moderate", "Three heirs share ownership of one parcel; a farm lease runs through 2028.",
                     "Confirm lease termination rights; obtain heirship affidavits.", "ACMI Land", False, False),
            "zoning": ("moderate", "Agricultural zoning. Rezoning to industrial requires a public hearing and county commission vote.",
                       "Pre-application meeting; schedule hearing.", "ACMI Entitlements", True, True),
            "qd": ("low", "Arcs close inside the parcel on all cases with margin; no receptors identified.", "Rerun after any program change.", "SMS Energetics", False, False),
            "environmental": ("low", "Row crops; one small stock pond. No RECs identified.", "Phase I ESA after site selection.", "ACMI Environmental", False, False),
            "geotech": ("moderate", "Shallow limestone expected in the north half; cut/fill and blasting may drive pad cost.", "Early borings in the north half.", "Civil engineer", False, False),
            "flood": ("low", "Clear of mapped floodplain.", "None.", "Civil engineer", False, False),
            "electric": ("low", "Substation 0.7 miles away with capacity; provider sees no issues.", "Capacity letter.", "ACMI Utilities", False, False),
            "water": ("moderate", "Water main is 2.5 miles away; extension required for process water.", "Cost-share discussion with the water district.", "ACMI Utilities", False, False),
            "gas": ("low", "Distribution main at the road frontage.", "Binding estimate.", "ACMI Utilities", False, False),
            "rail": ("moderate", "No rail within 6 miles; truck-only access without a major extension.", "Evaluate truck logistics against program needs.", "ACMI Rail", False, False),
        },
        "net": {"wetlands": "0", "floodway": "0", "easements": "15", "qd": "0"},
        "rom": ["3,000,000", "6,200,000", "2,100,000", "11,500,000", "0", "3,800,000", "1,900,000", "1,200,000", "0", "3,400,000"],
        "summary": ("Cedar Flats has good acreage and clean QD geometry, but needs rezoning, a water extension, and has no rail "
                    "service. Five moderate items, one of which (the rezoning hearing) sits on an externally imposed schedule."),
        "done": 14,
    },
    {
        "name": "DEMO · Sandstone Junction",
        "parcels": [("DEMO-SJ-1", -98.20, 34.70, 0.026, 480.0)],
        "zoning": "I-1 Light Industrial", "city": "Sandstone Junction",
        "cats": {
            "land": ("low", "Single owner; clean title.", "ALTA survey.", "ACMI Land", False, False),
            "zoning": ("moderate", "Light industrial; explosives manufacturing needs a special use permit.", "Pre-application meeting.", "ACMI Entitlements", True, True),
            "qd": ("elevated", "Residential subdivision of 40 homes sits inside the IBD arc for the governing case. Arc cannot close on site.",
                   "Buyout or program re-siting required before any commitment.", "SMS Energetics", True, True),
            "environmental": ("moderate", "Former landfill on the east boundary; groundwater monitoring wells present.", "Phase I with records review; consider Phase II.", "ACMI Environmental", False, False),
            "geotech": ("low", "Sandy loam; good bearing.", "Confirmatory borings.", "Civil engineer", False, False),
            "flood": ("moderate", "About 60 acres of mapped floodplain along the river corridor.", "Buffer and fill analysis.", "Civil engineer", False, False),
            "electric": ("low", "Existing 69 kV service at the boundary.", "Capacity letter.", "ACMI Utilities", False, False),
            "water": ("low", "Municipal water and sewer at the boundary.", "Will-serve letters.", "ACMI Utilities", False, False),
            "gas": ("low", "Main at the boundary.", "Binding estimate.", "ACMI Utilities", False, False),
            "rail": ("low", "Active Class I spur adjacent; siding rebuild needed.", "Carrier inspection.", "ACMI Rail", False, False),
        },
        "net": {"wetlands": "4", "floodway": "60", "easements": "10", "qd": "75"},
        "rom": ["2,400,000", "2,800,000", "1,600,000", "1,900,000", "5,200,000", "1,500,000", "2,300,000", "14,000,000", "1,000,000", "3,200,000"],
        "summary": ("Sandstone Junction has the best infrastructure but an Elevated QD finding: homes inside the IBD arc. This must be "
                    "resolved before any commitment, so the site ranks last."),
        "done": 9,
    },
]

UTIL_ROWS = [
    ("Electric", "Regional electric cooperative", "115 kV line, 1.8 mi", "Yes", "No", "Capacity commitment", "Submit load request"),
    ("Water", "County water district", "12-inch main at line", "Yes", "No", "Will-serve letter", "Request letter"),
    ("Wastewater", "County utility", "Lift station 0.6 mi", "Yes", "No", "Capacity for process flow", "Flow study"),
    ("Natural Gas", "Regional gas utility", "8-inch transmission", "Yes", "No", "Binding estimate", "Request estimate"),
    ("Fiber", "Regional carrier", "Along county road", "Yes", "No", "Redundant path", "Route review"),
    ("Rail", "Short line carrier", "Spur 0.9 mi", "Partial", "No", "Siding extension", "Carrier consultation"),
]
PERMIT_ROWS = [
    ("Special Use Permit (if required)", "County planning", "60 to 90 days", ""),
    ("Site plan approval", "County planning", "45 days", ""),
    ("Stormwater", "State environmental agency", "30 to 60 days", ""),
    ("Utility extension agreements", "Providers", "90 to 180 days", ""),
    ("Building permits", "County building", "30 days", ""),
]


def report_for(spec, ctx_assignment):
    cats = {}
    for key, (rating, finding, mit, owner, critical, confirmed) in spec["cats"].items():
        cats[key] = {"rating": rating, "finding": finding, "mitigation": mit, "owner": owner,
                     "confirmed": confirmed, "critical": critical}
    refs = [r[0] for r in A.CHECKLIST]
    checklist = {ref: {"status": "Complete" if i < spec["done"] else ("In Progress" if i < spec["done"] + 4 else "Not Started"),
                       "note": ""} for i, ref in enumerate(refs)}
    rom = [{"category": cat, "basis": "Demo ROM estimate", "amount": amt, "incentive": inc}
           for (cat, inc), amt in zip(A.ROM, spec["rom"])]
    return {
        "overview": {"opportunity_zone": "No", "rail": "See Rail category", "prior_use": "Agricultural (demo)",
                     "activity": "None observed (demo)", "zoning": spec["zoning"]},
        "summary": spec["summary"] + "\n\nThis is a fictional demonstration report; no part of it describes a real site.",
        "market": "Demo market overview: regional industrial vacancy is low and defense-related manufacturing demand is growing.",
        "sections": {
            "land": "Demo narrative on land, title and entitlements.",
            "environmental": "Demo narrative on environmental, geotechnical and hydrology findings.",
            "utilities": "Demo narrative on utility availability and gaps.",
            "transport": "Demo narrative on rail and highway access.",
            "permitting": "Demo permitting summary: approvals can run concurrently with site design.",
        },
        "categories": cats,
        "net": spec["net"],
        "utilities": [dict(zip(["utility", "provider", "infrastructure", "available", "committed", "gap", "action"], r)) for r in UTIL_ROWS],
        "permits": [dict(zip(["approval", "authority", "duration", "note"], r)) for r in PERMIT_ROWS],
        "rom": rom,
        "actions": [
            {"action": "Secure electric capacity commitment", "why": "Schedule-critical for pad-ready date", "owner": "ACMI Utilities", "deadline": "Before PSA execution"},
            {"action": "Order ALTA survey", "why": "Confirms acreage and easements", "owner": "ACMI Land", "deadline": "30 days"},
            {"action": "Rerun QD arcs after NEW update", "why": "Program change can move arcs", "owner": "SMS Energetics", "deadline": "On receipt"},
        ],
        "dates": [
            {"item": "Electric capacity commitment", "date": "2026-12-15", "consequence": "Delays pad-ready date", "owner": "ACMI Utilities", "critical": "Yes"},
            {"item": "Feasibility period ends", "date": "2027-02-01", "consequence": "Deposit goes hard", "owner": "ACMI Land", "critical": "Yes"},
        ],
        "checklist": checklist,
    }


class Command(BaseCommand):
    help = "Make (or --remove) a fictional client land search with three demo sites and reports. Dry run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true")
        parser.add_argument("--remove", action="store_true")

    def handle(self, *args, **opts):
        commit = opts["commit"]
        if opts["remove"]:
            self.stdout.write(f"{'Removing' if commit else 'Would remove'} the demo search, sites, parcels and client.")
            if commit:
                Assignment.objects.filter(name=NAME).delete()
                LandSite.objects.filter(name__startswith="DEMO · ").delete()
                Parcel.objects.filter(county__fips=FIPS).delete()
                LandCounty.objects.filter(fips=FIPS).delete()
                Company.objects.filter(name=CLIENT).delete()
            return
        self.stdout.write(f"{'Making' if commit else 'Would make'} {NAME!r} with {len(SITES)} sites.")
        if not commit:
            return
        with transaction.atomic():
            Assignment.objects.filter(name=NAME).delete()
            LandSite.objects.filter(name__startswith="DEMO · ").delete()
            Parcel.objects.filter(county__fips=FIPS).delete()
            county, _ = LandCounty.objects.get_or_create(fips=FIPS, defaults={"name": "Demo", "state": "OK", "region": "Demo"})
            company, _ = Company.objects.get_or_create(name=CLIENT)
            a = Assignment.objects.create(
                name=NAME, company=company, client=CLIENT, acreage_floor=600,
                program="Energetics manufacturing campus (fictional)", demand_driver="Fictional installation",
                guest_intro="This is a fictional demonstration report. Every site, owner, number and finding is invented.",
                share_token=A.new_token(), share_enabled=True)
            for spec in SITES:
                site = LandSite.objects.create(name=spec["name"], notes="Demo site: fictional.")
                for key, x0, y0, size, acres in spec["parcels"]:
                    geom = box(x0, y0, x0 + size, y0 + size)
                    p = Parcel.objects.create(
                        county=county, key=key, prop_id=key, owner_name="DEMO LANDOWNER LLC", acres=acres,
                        geometry=json.dumps(mapping(geom)), lat=y0 + size / 2, lon=x0 + size / 2,
                        min_lon=x0, min_lat=y0, max_lon=x0 + size, max_lat=y0 + size,
                        situs_address="Demo Road", situs_city=spec["city"], zoning=spec["zoning"])
                    site.parcels.add(p)
                AssignmentSite.objects.create(assignment=a, site=site, report=report_for(spec, a))
        self.stdout.write(f"Done. Open /land/assignments/{a.pk}/ ; client link: /g/{a.share_token}/")
