"""Incentive and funding programs a landowner or developer can use, with their map sources.

One registry drives the map's "Incentives & programs" layers (with a short what/how/link note for each)
and the per-parcel check that lists the programs a parcel falls in. Summaries are plain-English screening
notes, not tax or legal advice; each links to the program's official page.
"""
from .sources import SESSION, arcgis_query

ESRI = "https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services"
MD_ZONES = "https://mdgeodata.md.gov/imap/rest/services/BusinessEconomy/MD_IncentiveZones/FeatureServer"

PROGRAMS = [
    {
        "key": "oz", "label": "Opportunity Zones", "states": None, "color": "#2e7d32",
        "url": "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Opportunity_Zones/FeatureServer/13",
        "where": "1=1", "name": ["GEOID10"], "name_prefix": "Tract ", "points": 3,
        "source": "HUD (designated tracts)",
        "summary": "Federal tax incentive for investors: capital gains put into a Qualified Opportunity Fund that "
                   "invests in a designated low-income census tract are deferred, and gains on the fund investment "
                   "can be tax-free after a 10-year hold.",
        "how": "Own or develop the site through a Qualified Opportunity Fund (the building must be substantially "
               "improved) to bring in OZ equity. A 2025 federal law extended the program with a new round of "
               "designations from 2027: confirm current rules with tax counsel.",
        "link": "https://www.irs.gov/credits-deductions/businesses/opportunity-zones",
    },
    {
        "key": "oz2", "label": "Opportunity Zone 2.0 – eligible tracts", "states": None, "color": "#81c784",
        "url": f"{ESRI}/Opportunity_Zone_2_Eligible_Tracts/FeatureServer/0", "where": "1=1",
        "name": ["Geographic"], "points": 0, "source": "Esri, from CDFI Fund eligibility data",
        "summary": "Census tracts that meet the low-income test for the next round of Opportunity Zones. "
                   "Eligible is not designated: governors choose which eligible tracts become zones.",
        "how": "If a site is in an eligible tract, ask the state (via the county's economic development office) to "
               "nominate that tract so the site can take OZ investment in the new round.",
        "link": "https://www.cdfifund.gov/opportunity-zones",
    },
    {
        "key": "nmtc", "label": "New Markets Tax Credit tracts", "states": None, "color": "#6a1b9a",
        "url": f"{ESRI}/NMTC_Qualified_Tracts_2025/FeatureServer/124", "where": "NMTC_Qua_7 = 'Yes'",
        "name": ["NMTC_Quali"], "name_prefix": "Tract ", "points": 2, "source": "Esri, from CDFI Fund 2025 tracts",
        "summary": "A federal 39% tax credit (taken over 7 years) for investors in Community Development Entities "
                   "that finance businesses and real estate in qualifying low-income census tracts.",
        "how": "Partner with a CDE that holds an NMTC allocation to finance a tenant's facility here. The credit "
               "often covers roughly a fifth of the financed amount for industrial and commercial projects.",
        "link": "https://www.cdfifund.gov/programs-training/programs/new-markets-tax-credit",
    },
    {
        "key": "hubzone", "label": "HUBZones", "states": None, "color": "#0277bd",
        "url": f"{ESRI}/HUB_Zones/FeatureServer/1", "extra_urls": [f"{ESRI}/HUB_Zones/FeatureServer/0"],
        "where": "1=1", "name": [], "points": 0, "card": False, "source": "Esri, SBA 2023 HUBZone map",
        "summary": "SBA program: small businesses headquartered in a HUBZone, with 35% of staff living in one, can win "
                   "set-aside federal contracts and get a 10% price-evaluation preference.",
        "how": "A HUBZone site helps defense small businesses certify, which makes it a draw for those tenants. "
               "The parcel card's HUBZone check is live from SBA; the map shading is the 2023 SBA map.",
        "link": "https://www.sba.gov/federal-contracting/contracting-assistance-programs/hubzone-program",
    },
    {
        "key": "ftz", "label": "Foreign-Trade Zones", "states": None, "color": "#ef6c00", "geometry": "point",
        "url": f"{ESRI}/FTZ_2023/FeatureServer/0", "where": "1=1", "name": ["Zone", "Grantee"],
        "name_prefix": "FTZ ", "points": 1, "source": "Esri, from the FTZ Board",
        "summary": "U.S. Customs zones where imported parts can be stored and assembled with duties deferred, "
                   "reduced (paying the finished product's lower rate) or avoided when goods are re-exported.",
        "how": "A tenant that imports components can ask the zone's grantee to add our site as a usage-driven site "
               "under its service area (Alternative Site Framework). Points mark each zone's grantee; the parcel "
               "card names the zone whose service area covers the county.",
        "link": "https://www.trade.gov/about-ftzs",
    },
    {
        "key": "md_pfa", "label": "Priority Funding Areas (MD)", "states": ["MD"], "color": "#00897b",
        "url": "https://mdgeodata.md.gov/imap/rest/services/PlanningCadastre/MD_PriorityFundingAreas/FeatureServer/0",
        "where": "1=1", "name": ["RUVI_NAME"], "points": 0,
        "source": "Maryland Department of Planning",
        "summary": "Maryland's Smart Growth law sends most state growth-related spending (roads, water and sewer, "
                   "economic development financing) to Priority Funding Areas that counties designate.",
        "how": "Inside a PFA the site can use state infrastructure money and Maryland Commerce financing; outside "
               "one the state generally won't fund growth infrastructure. Already counted in the buy-box score.",
        "link": "https://planning.maryland.gov/Pages/OurProducts/pfaMap.aspx",
    },
    {
        "key": "md_ez", "label": "Enterprise Zones (MD)", "states": ["MD"], "color": "#c62828",
        "url": f"{MD_ZONES}/4", "extra_urls": [f"{MD_ZONES}/5"], "where": "1=1", "name": ["sitename"], "points": 2,
        "contact": ["contact_na", "contact_ph", "contact_em"],
        "source": "Maryland Department of Commerce",
        "summary": "Ten-year credit against local property tax on the increase in assessed value from new "
                   "construction (80% in years 1–5, stepping down to 30% by year 10), plus state income tax "
                   "credits for new jobs.",
        "how": "Building in a zone cuts property tax on the new improvements; the business applies through the "
               "county's Enterprise Zone administrator.",
        "link": "https://commerce.maryland.gov/financial-incentives/tax-incentives/enterprise-zone-tax-credit-ez",
    },
    {
        "key": "md_sc", "label": "Sustainable Communities (MD)", "states": ["MD"], "color": "#8d6e63",
        "url": f"{MD_ZONES}/6", "where": "1=1", "name": ["Name"], "points": 1,
        "source": "Maryland DHCD",
        "summary": "State designation (renewed every 5 years, always inside a PFA) that makes an area eligible for "
                   "DHCD revitalization funding such as Community Legacy, the Strategic Demolition Fund and façade "
                   "grants, and gives priority for other state programs.",
        "how": "Projects here can apply for DHCD money for site work, demolition and infrastructure.",
        "link": "https://dhcd.maryland.gov/neighborhood-revitalization/sustainable-communities",
    },
    {
        "key": "md_rise", "label": "RISE Zones (MD)", "states": ["MD"], "color": "#5e35b1",
        "url": f"{MD_ZONES}/11", "where": "1=1", "name": ["ZONE_NAME"], "points": 1,
        "contact": ["CONTACT_NAME", "CONTACT_PHONE", "CONTACT_EMAIL"],
        "source": "Maryland Department of Commerce",
        "summary": "Regional Institution Strategic Enterprise zones around Maryland universities and hospitals, "
                   "with property and income tax credits much like Enterprise Zones and priority for state help.",
        "how": "A site in a RISE zone can use those credits for research-oriented tenants tied to the anchor "
               "institution.",
        "link": "https://www.medco-corp.com/programs-and-grants/rise-zone-program",
    },
    {
        "key": "charles_dd", "label": "Development Districts (Charles Co.)", "states": ["MD"], "color": "#f9a825",
        "url": "https://services7.arcgis.com/3BMWkdyrt45RNCrq/arcgis/rest/services/DevelopmentDistrict_2016/FeatureServer/0",
        "where": "1=1", "name": ["Location"], "points": 0, "source": "Charles County (2016 Comprehensive Plan)",
        "summary": "The County's planned growth areas in its Comprehensive Plan, where it expects public water and "
                   "sewer and more intensive development.",
        "how": "Sites inside are the most realistic candidates for rezoning to employment uses and for utility "
               "extensions; outside, expect rural policies.",
        "link": "https://www.charlescountymd.gov/government/planning-and-growth-management-publications/plans-and-studies",
    },
    {
        "key": "tx_ez", "label": "Enterprise Zones (TX)", "states": ["TX"], "color": "#c62828",
        "url": "https://services6.arcgis.com/qBPMNP2zACwkFVPe/arcgis/rest/services/TEZ_2020/FeatureServer/0",
        "where": "1=1", "name": ["County", "TRT", "BG"], "points": 2,
        "source": "Office of the Texas Governor (2020 block groups)",
        "summary": "Texas refunds state sales and use tax on qualified spending for projects that a city or county "
                   "nominates and the Governor's office designates. Shaded block groups (20%+ poverty) are zones, "
                   "where a project qualifies by location rather than by its hiring mix.",
        "how": "Ask the city or county to nominate a tenant's project; the refund scales with the jobs created or "
               "retained and the capital investment.",
        "link": "https://gov.texas.gov/business/page/texas-enterprise-zone-program",
    },
]
BY_KEY = {p["key"]: p for p in PROGRAMS}


def for_state(state):
    return [p for p in PROGRAMS if not p["states"] or state in p["states"]]


def public(programs=PROGRAMS):
    """What the map needs (no internal query details beyond the layer URLs)."""
    keep = ("key", "label", "color", "url", "extra_urls", "where", "name", "name_prefix", "summary", "how",
            "link", "source", "geometry", "states", "contact")
    return [{k: p.get(k) for k in keep} for p in programs]


def _name(p, props):
    parts = [str(props.get(f)).strip() for f in p.get("name") or [] if props.get(f) not in (None, "", " ")]
    return (p.get("name_prefix", "") + " · ".join(parts)) if parts else ""


def check(geom, county_name, state):
    """Programs whose areas contain the parcel's representative point, plus the FTZ covering the county."""
    import json

    from shapely.geometry import Point

    from .layers import METERS_PER_MILE, to_albers

    pt = geom.representative_point()
    point = json.dumps({"x": pt.x, "y": pt.y, "spatialReference": {"wkid": 4326}})
    hits, errors = [], []
    for p in for_state(state):
        if p.get("card") is False:
            continue
        try:
            if p.get("geometry") == "point":
                hit = _ftz(p, pt, county_name, state, to_albers, Point, METERS_PER_MILE)
                if hit:
                    hits.append(hit)
                continue
            for url in [p["url"], *(p.get("extra_urls") or [])]:
                fields = [*(p.get("name") or []), *(p.get("contact") or [])] or ["*"]
                feats = arcgis_query(url, where=p["where"], fields=fields, geometry=point,
                                     geometry_type="esriGeometryPoint", return_geometry=False, max_records=3)
                if feats:
                    props = feats[0].get("properties") or {}
                    contact = " · ".join(str(props[f]).strip() for f in p.get("contact") or []
                                         if props.get(f) not in (None, "", " "))
                    hits.append({"key": p["key"], "label": p["label"], "area": _name(p, props), "contact": contact,
                                 "summary": p["summary"], "how": p["how"], "link": p["link"], "points": p["points"]})
                    break
        except Exception as exc:  # a slow program server shouldn't hide the others
            errors.append(f"{p['label']}: {str(exc)[:80]}")
    return {"programs": hits, "errors": errors}


def _ftz(p, pt, county_name, state, to_albers, Point, meters_per_mile):
    zones = arcgis_query(p["url"], where=f"State = '{state}'", fields=["Zone", "Grantee", "Location", "Service_Ar"])
    best = None
    here = to_albers(Point(pt.x, pt.y))
    for z in zones:
        props = z.get("properties") or {}
        area = (props.get("Service_Ar") or "") + " " + (props.get("Location") or "")
        covers = bool(county_name) and county_name.lower() in area.lower()
        coords = (z.get("geometry") or {}).get("coordinates")
        miles = here.distance(to_albers(Point(*coords[:2]))) / meters_per_mile if coords else 9999
        rank = (0 if covers else 1, miles)
        if best is None or rank < best[0]:
            best = (rank, props, covers, miles)
    if not best or (not best[2] and best[3] > 60):
        return None
    _, props, covers, miles = best
    where = (f"service area covers {county_name} County" if covers else f"grantee {miles:.0f} mi away")
    return {"key": "ftz", "label": p["label"],
            "area": f"FTZ {props.get('Zone')} · {props.get('Grantee')} ({where})",
            "summary": p["summary"], "how": p["how"], "link": p["link"], "points": p["points"] if covers else 0}
