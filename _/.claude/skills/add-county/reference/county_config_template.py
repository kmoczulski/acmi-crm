# Template for a COUNTIES entry in crm/land/counties.py. Copy the closest existing example and adapt:
#   Cumberland NC  statewide layer filtered by county          (_nc_county)
#   Hoke NC        the county's own service                    (HOKE_PARCELS / _hoke_fields)
#   Pinal AZ       own service + zoning + windows + NAP keys   (PINAL_* / _pinal_fields / _pinal_key)
#   Greene IN      statewide layer with no owner data          (IN_PARCELS / _in_fields) + load_owners
#
# Parcel fields a fields_fn must return (blank string or None when the source doesn't have it):
#   prop_id, geo_id, owner_name, care_of, mail_address, mail_city, mail_state, mail_zip,
#   situs_address, situs_city, situs_zip, legal_desc, legal_acres, state_land_use, local_land_use,
#   year_built, land_value, improvement_value, market_value, tax_year
# state_land_use is a Texas-style code the scorer understands: A residential, B multifamily, C vacant,
#   D farm/ranch, E rural, F1 commercial, F2 industrial, J utility, X tax-exempt, "" unknown.

COUNTIES["FIPS"] = {
    "fips": "FIPS", "name": "Name", "state": "ST", "bbox": (west, south, east, north),
    "aliases": ["name st", "name county st", "name, st"],          # never a bare name that collides with a Texas county
    "source": "Name County, ST tax parcels (who publishes it)", "release": "Name County live service",
    "parcels_url": "https://HOST/arcgis/rest/services/.../MapServer/N",
    "key_field": "PARCELID",            # also set "key_fn" if the key needs cleaning or has placeholders
    "owner_field": "OWNER",             # used for owner totals (owner_acres / owner_parcels)
    "count_fields": ["PARCELID", "OWNER", "ACRES"],               # fetched without geometry for the totals
    "acres_fn": lambda rec: rec.get("ACRES"),                     # None when unknown (geometry fills it in)
    "big_where_fn": lambda a: f"ACRES >= {a:.1f} OR ACRES IS NULL OR ACRES = 0",
    "fields_fn": _name_fields,
    "overlays": {},                      # zoning and planning layers stamped on every parcel (see PINAL_OVERLAYS)
    # Optional:
    # "base_where": "county = 'Name'",   # when the layer holds several counties
    # "count_where": "ACRES > 0",        # skip junk rows in the totals / when the county is huge
    # "full_windows": [(west, south, east, north)],   # load every parcel of any size inside these boxes
    # "digits_key": True,                # keep digits only in the key
}

# Zoning overlay entry (inside "overlays"):
#   "zoning": {"label": "Zoning (Name County)", "url": ".../MapServer/N", "fields": ["ZONING"], "value": "ZONING",
#              "value_labels": {"19": "CI-1"},            # when the layer stores numeric codes
#              "name_field": "DESCRIPTION", "authority": "Name County (unincorporated land only)", "clip": True},
#   "zoning_city": {..., "refines": "zoning", "when_value": "CITYNAME", "authority": "City of ..."},
# Then: score.ZONING_CODES["FIPS"] = {...}, zoning_ref.AUTHORITY_FILES[...] = "<json file>".
