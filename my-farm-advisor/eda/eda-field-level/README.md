# eda-field-level

Field-level exploratory analysis of farm boundaries, CDL crop data, and NASA POWER weather across growers.

## Location

`my-farm-advisor/eda/eda-field-level/`

## What It Generates

17 static PNG charts and maps, 7 CSV data tables, and 2 aligned reports (HTML + DOCX):

- **Boundaries (3 PNGs + 2 CSVs + 1 CSV shared with geospatial):** cumulative field area stacked bar, field-size histogram, field count vs. acreage; per-field geometry metrics and per-grower acreage summary
- **CDL (3 PNGs + 2 CSVs):** cross-grower crop composition flow, per-grower rotation heatmaps, per-grower annual crop composition; full CDL composition and aggregated crop totals
- **Weather (3 PNGs + 2 CSVs):** cumulative annual precipitation, daily precipitation by season (per grower), temperature + precipitation dual-axis; annual precipitation and annual weather summary
- **Geospatial (1 PNG + 1 CSV):** cross-grower field centroid map with area-scaled markers; centroid coordinates with area
- **Reports (1 HTML + 1 DOCX):** aligned reports with category stories, plot descriptions, and CSV references

## Output Directory

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/field-level/output/
```

See [GUIDE.md](GUIDE.md) for script details, prerequisites, and usage.
