# Zoning reference file: `crm/land/zoning_ref/<jurisdiction>.json`

One file per jurisdiction. Loaded by `crm/land/zoning_ref.py`; shown on the parcel card and in the site briefs.

```json
{
  "jurisdiction": "City of Eloy, AZ",
  "code_name": "Eloy City Code, Chapter 21 Zoning",
  "edition": "American Legal online code, retrieved 2026-10",
  "code_url": "https://codelibrary.amlegal.com/codes/eloyaz/latest/eloy_az/",
  "retrieved": "2026-10-16",
  "caveats": ["Anything the reader should know: sources blocked, tables lost, codes not defined in the text."],
  "districts": [
    {
      "code": "I-2",
      "name": "General Industrial",
      "category": "industrial",
      "purpose": "Own-words paraphrase, 1-2 sentences.",
      "permitted_uses": ["Principal uses allowed by right, grouped and concise. Say 'not addressed' for data centers, explosives manufacturing, solar when the code is silent."],
      "conditional_uses": ["Special / conditional use permit items."],
      "standards": {"min_lot_size": "...", "max_height": "...", "setbacks": "...", "lot_coverage": "...", "other": "buffers, screening, parking"},
      "section": "21-2-5.2",
      "notes": "Gaps and judgment calls.",
      "source": "exact URL or file and page"
    }
  ]
}
```

## Rules
- `code` must match what the zoning layer stores (the first token before " / "). Variants (`IND-3p`, `I-2 / CR`,
  `R1-6 RUPD`) resolve to the base code automatically; one entry may cover several labels written `"A / B / C"`
  (for example the city names a county layer uses for land a city zones).
- `category`: industrial, commercial, residential, rural, planned, open-space, public, other.
- Unknown values are `null` (or `[]`). Never fill a gap with a guess; explain it in `notes`.
- If the text came from a third-party mirror, `edition` or `code_url` must contain "Zoneomics" or "mirror"; the
  reports then tag the district "check source".
- Register the file: `AUTHORITY_FILES["<overlay authority text>"] = "<file name without .json>"` in `zoning_ref.py`,
  or `COUNTY_FILES["<fips>"]` when the county's zoning layer has no authority text.
- Verify with `manage.py check_county <County_ST>`: "No definition in zoning_ref" should list nothing.
