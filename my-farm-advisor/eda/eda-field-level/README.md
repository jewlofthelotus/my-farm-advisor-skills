# eda-field-level

Field-level exploratory analysis of farm boundaries, CDL crop data, and NASA POWER weather across growers.

## Location

`my-farm-advisor/eda/eda-field-level/`

## What It Generates

17 static PNG charts and maps:

- **Boundaries (3):** cumulative field area stacked bar, field-size histogram, field count vs. acreage
- **CDL (7):** cross-grower crop composition flow, per-grower rotation heatmaps, per-grower annual crop composition
- **Weather (6):** cumulative annual precipitation, daily precipitation by season (per grower), temperature + precipitation dual-axis
- **Geospatial (1):** cross-grower field centroid map with area-scaled markers

## Output Directory

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/field-level/output/
```

See [GUIDE.md](GUIDE.md) for script details, prerequisites, and usage.
