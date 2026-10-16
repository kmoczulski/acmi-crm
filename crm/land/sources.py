"""Public data sources for land search, and a small ArcGIS REST client.

Everything here is free and public:
- Parcels: TxGIO StratMap Land Parcels, one zip per county, refreshed by the state about once a year.
  The state's live parcel service allows map images and point lookups (identify) but not searching,
  so searchable parcels are a local copy of the county files (crm/land/parcels.py).
- Stored layers (crm/land/layers.py): used to measure every parcel's distance to power, rail, roads, bases.
- Live layers (crm/land/lookup.py and the map): flood, wetlands, water/sewer/electric providers, HUBZone, EPA.
"""
import time

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 ACMI-CRM"
)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT

TXGIO_API = "https://api.tnris.org/api/v1"
TX_PARCELS_MAPSERVER = (
    "https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer"
)
TX_CITY_LIMITS = (
    "https://feature.geographic.texas.gov/arcgis/rest/services/City_Boundaries/Texas_City_Boundaries/MapServer/0"
)
PUCT = "https://services6.arcgis.com/N6Lzvtb46cpxThhu/arcgis/rest/services"
FEMA_FLOOD = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
NWI_WETLANDS = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0"
EPA_CLEANUPS = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/Cleanups_in_my_Community_Sites/FeatureServer/0"
SBA_HUBZONE = "https://maps.certify.sba.gov/hubzone/map/search"

# Live provider territories, checked per parcel.
WATER_CCN = f"{PUCT}/Water_CCN_Service_Areas/FeatureServer/210"
SEWER_CCN = f"{PUCT}/Sewer_CCN_Service_Areas/FeatureServer/230"
WATER_LINES = f"{PUCT}/Water_CCN_Facility_Lines/FeatureServer/200"
SEWER_LINES = f"{PUCT}/Sewer_CCN_Facility_Lines/FeatureServer/220"
ELECTRIC_AREAS = {
    "Investor-owned utility": f"{PUCT}/IOU/FeatureServer/300",
    "Electric co-op": f"{PUCT}/COOP_DIST/FeatureServer/310",
    "Municipal utility": f"{PUCT}/MUNI/FeatureServer/320",
}

# Everywhere else: HIFLD electric retail service territories (EIA-based, national).
ELECTRIC_NATIONAL = ("https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services/"
                     "Electric_Retail_Service_Territories/FeatureServer/0")

# Stored layers: (url, where, name field, extra fields, simplify tolerance in degrees).
STORED_LAYERS = {
    "military": {
        "label": "Military installation",
        "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_Military_Bases/FeatureServer/0",
        "where": "1=1",
        "name": "siteName",
        "fields": ["featureDescription", "siteReportingComponent", "siteOperationalStatus"],
        "simplify": 0.0005,
    },
    "transmission": {
        "label": "Transmission line",
        "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0",
        "where": "1=1",  # every line in this layer is 100 kV or more
        "name": "OWNER",
        "fields": ["VOLTAGE", "VOLT_CLASS", "STATUS", "SUB_1", "SUB_2"],
        "simplify": 0.0003,
    },
    "substation": {
        "label": "Substation",
        "url": "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/Electric_Substations/FeatureServer/0",
        "where": "STATE IN ('TX', 'MD', 'VA', 'DC')",
        "name": "NAME",
        "fields": ["MAX_VOLTAG", "MIN_VOLTAG", "LINES", "CITY"],
        "simplify": 0,
    },
    "gas_pipeline": {
        "label": "Natural gas pipeline",
        "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0",
        "where": "1=1",
        "name": "Operator",
        "fields": ["TYPEPIPE", "Status"],
        "simplify": 0.0005,
    },
    "rail": {
        "label": "Rail line",
        "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_North_American_Rail_Network_Lines/FeatureServer/0",
        "where": "NET IN ('M', 'I', 'O', 'S', 'Y')",  # active main lines, industrial leads, sidings, yards
        "name": "RROWNER1",
        "fields": ["SUBDIV", "NET", "TRACKS"],
        "simplify": 0.0003,
    },
    "highway": {
        "label": "Major highway",
        "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_National_Highway_System/FeatureServer/0",
        "where": "1=1",
        "name": "SIGN1",
        "fields": ["LNAME", "FCLASS", "SIGNT1"],
        "simplify": 0.0005,
    },
    "city": {
        "label": "City limits",
        "url": TX_CITY_LIMITS,
        "where": "1=1",
        "name": "city_name",
        "fields": ["popest2025"],
        "simplify": 0.0002,
        "regions": ["texas_triangle"],
    },
    "town_md": {
        "label": "Town limits",
        "url": "https://services7.arcgis.com/3BMWkdyrt45RNCrq/arcgis/rest/services/Incorporated_Towns/FeatureServer/0",
        "where": "Inc_Muni <> 'Unincorporated'",
        "name": "Inc_Muni",
        "fields": [],
        "simplify": 0.0002,
        "regions": ["southern_md"],
    },
}
# Layers that mark city/town limits rather than something to measure distance to.
LIMIT_LAYERS = ("city", "town_md")

# Regions the stored layers cover (lon/lat west, south, east, north).
REGIONS = {
    # Texas Triangle: DFW, Houston, Austin, San Antonio and the counties between them.
    "texas_triangle": (-99.6, 28.6, -94.4, 33.7),
    # Charles County MD plus ~40 miles around it (NSWC Indian Head, Dahlgren, Pax River, Andrews).
    "southern_md": (-77.8, 37.9, -76.2, 39.1),
}
TEXAS_TRIANGLE_BBOX = REGIONS["texas_triangle"]


def arcgis_query(url, *, where="1=1", fields=("*",), bbox=None, geometry=None, geometry_type=None,
                 distance=None, return_geometry=True, simplify=None, page_size=1000, max_records=None, timeout=90):
    """Run an ArcGIS REST query, following pages. Returns a list of GeoJSON-like features (WGS84)."""
    params = {
        "where": where,
        "outFields": ",".join(fields),
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": 4326,
        "f": "geojson",
    }
    if bbox:
        params.update(geometry=",".join(str(v) for v in bbox), geometryType="esriGeometryEnvelope",
                      inSR=4326, spatialRel="esriSpatialRelIntersects")
    if geometry is not None:
        params.update(geometry=geometry, geometryType=geometry_type, inSR=4326, spatialRel="esriSpatialRelIntersects")
    if distance:
        params.update(distance=distance, units="esriSRUnit_StatuteMile")
    if simplify:
        params["maxAllowableOffset"] = simplify
        params["geometryPrecision"] = 6
    features, offset = [], 0
    while True:
        page = dict(params, resultOffset=offset, resultRecordCount=page_size)
        data = _post(f"{url}/query", page, timeout)
        if "error" in data:
            raise RuntimeError(f"{url}: {data['error'].get('message')}")
        batch = data.get("features") or []
        features.extend(batch)
        if max_records and len(features) >= max_records:
            return features[:max_records]
        exceeded = data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit")
        if not batch or not exceeded:
            return features
        offset += len(batch)


def _post(url, data, timeout, tries=3):
    for attempt in range(tries):
        try:
            resp = SESSION.post(url, data=data, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def txgio_parcel_catalog():
    """{county name (lower): {"fips", "name", "url", "size", "release"}} for the newest statewide parcel release."""
    found = SESSION.get(f"{TXGIO_API}/collections_catalog/", params={"search": "Land Parcels"}, timeout=30).json()
    releases = [c for c in found.get("results", []) if c.get("name") == "Land Parcels"]
    latest = max(releases, key=lambda c: c.get("acquisition_date") or "")
    resources = SESSION.get(f"{TXGIO_API}/resources/", params={"collection_id": latest["collection_id"]},
                            timeout=30).json()["results"]
    catalog = {}
    for r in resources:
        if r.get("resource_type_abbreviation") != "LP":
            continue
        fips = r["resource"].rsplit("_", 2)[-2]  # ..._48209_lp.zip
        catalog[r["area_type_name"].lower()] = {
            "fips": fips, "name": r["area_type_name"], "url": r["resource"], "size": r.get("filesize") or 0,
            "release": f"TxGIO {latest.get('acquisition_date')}",
        }
    return catalog


def download(url, dest, chunk=1 << 20):
    """Stream a file to disk. TxGIO's CDN refuses requests without a browser user agent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(url, stream=True, timeout=120, headers={"Referer": "https://data.geographic.texas.gov/"}) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for block in resp.iter_content(chunk):
                fh.write(block)
    tmp.replace(dest)
    return dest


# Regions used to group counties on the map page.
COUNTY_REGIONS = {
    "Austin–San Antonio": ["Travis", "Williamson", "Hays", "Bastrop", "Caldwell", "Blanco", "Burnet", "Lee",
                           "Bexar", "Comal", "Guadalupe", "Wilson", "Medina", "Atascosa", "Kendall", "Gonzales"],
    "Dallas–Fort Worth": ["Collin", "Dallas", "Denton", "Ellis", "Hunt", "Kaufman", "Rockwall", "Johnson", "Parker",
                          "Tarrant", "Wise", "Hood", "Navarro", "Hill"],
    "Houston": ["Harris", "Fort Bend", "Montgomery", "Brazoria", "Galveston", "Liberty", "Waller", "Chambers",
                "Austin", "Colorado", "Fayette", "Washington", "Grimes", "Walker", "San Jacinto"],
    "Central Texas (I-35 / Brazos Valley)": ["McLennan", "Bell", "Coryell", "Falls", "Freestone", "Limestone", "Leon",
                                             "Robertson", "Brazos", "Burleson", "Milam", "Madison"],
    "Southern Maryland": ["Charles", "St. Mary's", "Calvert", "Prince George's"],
}


def region_for(county_name):
    return next((r for r, names in COUNTY_REGIONS.items() if county_name in names), "")
