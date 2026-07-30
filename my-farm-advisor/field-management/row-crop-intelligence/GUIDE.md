---
name: row-crop-intelligence
description: Generate a single-page, offline-functional operational dashboard for corn fields at the grower level, integrating NDVI, soil, weather, and rotation data for current-season actionable and historical reference analysis.
license: MIT
compatibility: Requires Python 3.10+, pandas, numpy, rasterio
metadata:
  author: jewlofthelotus
  version: "1.0.0"
  category: field-management
  tags: [dashboard, ndvi, gdd, corn, soil, weather, risk]
---

# Row Crop Intelligence Dashboard Guide

## When to use

Use this when you need a multi-field, grower-level operational dashboard showing NDVI time series, soil metrics, weather data, and risk analysis together in a single offline HTML file. The dashboard supports both **current-season actionable mode** (Priority Actions, Fields Requiring Attention, live risk classification) and **historical reference mode** (Season Recap & Notable Events, Peak NDVI, Season Stress Duration). See [INFO.md](INFO.md#dashboard-explanation) for full context.

## Inputs

| Input | Source | Description |
|-------|--------|-------------|
| `--grower` | CLI arg | Grower slug (default: `il-grower`) |
| `DATA_PIPELINE_DATA_ROOT` | Env var or `--data-root` | Absolute path to runtime data |
| Field boundaries | Runtime tree | Per-field GeoJSON + farm-level collection |
| NDVI | Sentinel-2 / Landsat 8-9 | Per-scene mean NDVI computed from GeoTIFFs |
| Soil | NRCS SSURGO | AWS, OM%, pH, CEC, texture, drainage |
| Weather | NASA POWER | Daily min/max temperature, precipitation, solar radiation |
| Crop rotation | Pipeline tables | `predicted_next_crop` used for current-year crops |

See [README.md](README.md#where-to-look-in-the-runtime-dataset) for full data source paths.

## Output

A single self-contained HTML file at:

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/derived/dashboards/row_crop_intelligence.html
```

Zero network dependencies — D3.js is inlined, all data embedded as JSON, icons as inline SVG. Open directly in any browser.

## Dashboard sections

Ten sections top-to-bottom: KPI header → Priority Actions / Season Recap → Field Risk Map (D3 choropleth) → NDVI Time Series (with growth-stage annotations) → Field Ranking → NDVI-vs-AWS Scatter → GDD Accumulation → Soil Organic Matter → Narrative Panel → Footer. See [INFO.md](INFO.md#dashboard-explanation) for full section descriptions.

## CLI usage

```bash
export DATA_PIPELINE_DATA_ROOT=/path/to/runtime

python my-farm-advisor/field-management/row-crop-intelligence/scripts/generate_dashboard.py \
  --grower il-grower
```

Optional flags: `--data-root`, `--output`, `--d3-path`. See [README.md](README.md#generate-the-dashboard) for details.

## Risk classification

Growth-stage-gated logic based on accumulated GDD (base 50°F):

| Phase | GDD Range | Flagging |
|-------|-----------|----------|
| Establishing | < VE (120 GDD) | All flags suppressed (bare soil not diagnostic) |
| Building canopy | VE–R1 (120–1400 GDD) | Absolute NDVI floors + decline-based promotions |
| Early reproductive | R1–R4 (1400–2150 GDD) | Absolute floors only (gradual senescence expected) |
| Late reproductive | > R4 (2150+ GDD) | All flags suppressed (universal senescence) |

The Healthy (Watch) threshold scales from 30% of 0.7 at planting to the full 0.7 at VT, then stays flat. See [INFO.md](INFO.md#risk-classification-logic) for full details.

## Notes

- **Weather data lag:** NASA POWER's most recent 1–2 months are frequently unavailable. The dashboard marks this with a shaded gap region rather than silently extrapolating.
- **Shared scene footprints:** Fields in the same satellite scene share observation dates, producing identical season-level statistics for clustered fields. This is a genuine data property, not a bug.
- **Growth-stage model:** The Healthy threshold ramp to VT is a working geometric model, not a research-validated NDVI-by-growth-stage curve for specific hybrids.
- **Current-year crops:** The dashboard uses `predicted_next_crop` from rotation analysis for the current year, since USDA CDL data is not yet published mid-season.
