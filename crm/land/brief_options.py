"""What a site brief prints: sections can be switched off and free-text sections added, per site (LandSite.brief_options).
Whole-report pages (the comparison rows, the zoning pages, the notes page) are switched with ?hide= on the report URL."""

SECTIONS = [  # (key, label, page)
    ("maps", "Aerial and context maps", "Page 1"),
    ("ownership", "Ownership, zoning and assessed value", "Page 1"),
    ("flags", "Flags", "Page 1"),
    ("power", "Power, roads and utilities", "Page 1"),
    ("verify", "Still to verify", "Page 1"),
    ("notes", "Source and broker notes", "Page 1"),
    ("parcels", "Parcel table", "Page 2"),
    ("flyer", "Listing flyer facts, brokers, market", "Page 2"),
    ("claims", "Flyer claims against the app's data", "Page 2"),
]
COVER_ROWS = [  # rows of the comparison table
    ("where", "Where"), ("acres", "Acres"), ("zoning", "Zoning"), ("power", "Transmission / substation"),
    ("roads", "Highway / rail"), ("electric", "Electric provider(s)"), ("water", "Water / sewer"),
    ("flood", "FEMA floodplain"), ("score", "Buy-box score"), ("flags", "Flags"), ("concerns", "Biggest concerns"),
]
PAGES = [("cover", "Comparison page"), ("zoning", "Zoning districts pages"), ("definitions", "Notes & definitions page")]
SECTION_KEYS = {k for k, _, _ in SECTIONS}
MAX_EXTRAS = 8


def hidden(site):
    """Sections left out. A site nobody has laid out yet and that has no listing facts (raw land, not marketed) starts
    without the flyer sections; saving the layout page makes the choice explicit."""
    opts = site.brief_options or {}
    if "hide" not in opts:
        return {"flyer", "claims"} if not any((site.listing or {}).values()) and not site.flyer else set()
    return {k for k in opts["hide"] if k in SECTION_KEYS}


def extras(site):
    return [e for e in (site.brief_options or {}).get("extras", []) if e.get("title") or e.get("body")]


def page_hidden(request):
    """?hide=cover,zoning,row_flood ... for the whole report."""
    return {x for raw in request.GET.getlist("hide") for x in raw.split(",") if x.strip()}


def parse(post):
    """The customize form: unticked sections are hidden; extra sections come as extra_title_N / extra_body_N."""
    shown = set(post.getlist("show"))
    out = {"hide": [k for k, _, _ in SECTIONS if k not in shown], "extras": []}
    for i in range(MAX_EXTRAS):
        title, body = post.get(f"extra_title_{i}", "").strip(), post.get(f"extra_body_{i}", "").strip()
        if (title or body) and not post.get(f"extra_remove_{i}"):
            out["extras"].append({"title": title[:120], "body": body[:4000]})
    return out


def cover_pages(built, per_page=3):
    """The comparison table split into pages of at most per_page sites, as evenly as it goes (5 sites: 3 + 2)."""
    if not built:
        return []
    n = -(-len(built) // per_page)
    size = -(-len(built) // n)
    return [built[i:i + size] for i in range(0, len(built), size)]
