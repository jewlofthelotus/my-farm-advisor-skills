---
name: grower-dashboard
description: >
  Generate a self-contained offline HTML dashboard with a Leaflet satellite map
  (ESRI World Imagery) and Plotly GDD + precipitation charts for any grower/farm.
version: 1.0.0
author: Superior Byte Works, LLC
tags: [dashboard, visualization, leaflet, plotly, gdd, precipitation, satellite, offline]
---

# Workflow: Grower Dashboard

## Description

This workflow generates a single-file HTML dashboard for any grower/farm in the data pipeline. The dashboard combines:

- **Satellite basemap** (Leaflet + ESRI World Imagery, no token required)
- **Color-coded field boundaries** from `field_boundaries.geojson`
- **Field multi-select filter** — hides/shows fields across the map and both charts
- **Zoom-to-field** dropdown — centers and zooms the map to a selected field
- **Cumulative GDD chart** — base-10°C, accumulation starts from last frost date (DOY 112)
- **Precipitation chart** — dual-axis: daily bars + cumulative line, with frost date marker

All data (GeoJSON, 5 years of weather) is embedded in the HTML. Plotly.js and Leaflet.js are also embedded. The file is a fully self-contained 5–7 MB `.html` that works offline after the first tile load.

## Prerequisites

- A checkout of the runtime data pipeline with `DATA_PIPELINE_DATA_ROOT` set
- Required input files for the target farm:
  - `farms/<farm>/boundary/field_boundaries.geojson`
  - `farms/<farm>/derived/tables/<farm>_weather_2021_2025.csv`
- Internet on first run (to download Plotly.js and Leaflet.js to `/tmp/`)

## Quick Start

```bash
export DATA_PIPELINE_DATA_ROOT=/home/coder/my-farm-advisor-runtime

python3 admin/grower-dashboard/src/generate_dashboard.py \
  --grower-slug ia-grower --farm-slug ia-grower-iowa
```

The dashboard HTML is written to:
`$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers/ia-grower/farms/ia-grower-iowa/derived/dashboards/grower_dashboard.html`

Open it in any modern browser.

## CLI Reference

```
usage: generate_dashboard.py [-h] [--grower-slug GROWER_SLUG] [--farm-slug FARM_SLUG]
                             [--output OUTPUT] [--list-growers] [--validate-only]
                             [--skip-download]

Generate a self-contained offline grower dashboard with satellite map + GDD/precip charts.

options:
  --grower-slug GROWER_SLUG   Grower slug (e.g. ia-grower)
  --farm-slug FARM_SLUG       Farm slug (e.g. ia-grower-iowa)
  --output OUTPUT             Custom output path
  --list-growers              List available growers and farms
  --validate-only             Only check that input files exist
  --skip-download             Use cached JS/CSS assets only
```

### Commands

**Generate for a grower/farm:**
```bash
python3 admin/grower-dashboard/src/generate_dashboard.py \
  --grower-slug ia-grower --farm-slug ia-grower-iowa
```

**List available growers:**
```bash
python3 admin/grower-dashboard/src/generate_dashboard.py --list-growers
```

**Validate inputs without generating:**
```bash
python3 admin/grower-dashboard/src/generate_dashboard.py \
  --grower-slug ia-grower --farm-slug ia-grower-iowa --validate-only
```

**Custom output path:**
```bash
python3 admin/grower-dashboard/src/generate_dashboard.py \
  --grower-slug ia-grower --farm-slug ia-grower-iowa \
  --output /tmp/farm_dashboard.html
```

**Reuse cached assets (skip download):**
```bash
python3 admin/grower-dashboard/src/generate_dashboard.py \
  --grower-slug ia-grower --farm-slug ia-grower-iowa --skip-download
```

## Output

A single `grower_dashboard.html` file (5–7 MB) containing:

| Component | Library | Detail |
|---|---|---|
| Map | Leaflet 1.9.4 | ESRI satellite tiles, 10 field polygons, popup info |
| Basemap | ESRI World Imagery | Free, no token required |
| GDD chart | Plotly 2.35.2 | Lines per field, cumulative GDD from DOY 112 |
| Precip chart | Plotly 2.35.2 | Daily bars (left y) + cumulative lines (right y) |
| Field filter | HTML `<select multiple>` | Filters map + charts simultaneously |
| Year selector | HTML `<select>` | 2021–2025 |
| Reset | Button | Restores all fields, default year, full extent |

Default output: `$DATA_PIPELINE_DATA_ROOT/data-pipeline/growers/<grower>/farms/<farm>/derived/dashboards/grower_dashboard.html`

## Data Source

- **Weather:** NASA POWER, daily data 2021–2025, ingested by the data pipeline
- **Boundaries:** OpenStreetMap field boundaries, stored as `field_boundaries.geojson`
- **Last frost date:** Hardcoded DOY 112 (April 22), typical for Kossuth County, IA
- **GDD formula:** `max(0, (T2M_MIN + T2M_MAX) / 2 - 10.0°C)`, no upper cap

## Python API

```python
from admin.grower-dashboard.src.generate_dashboard import generate_html

html = generate_html(
    data_root="/path/to/runtime",
    grower_slug="ia-grower",
    farm_slug="ia-grower-iowa",
    plotly_js="...",      # Full plotly.min.js content
    leaflet_js="...",      # Full leaflet.js content
    leaflet_css="..."      # Full leaflet.css content (with fixed image URLs)
)
# html is the complete dashboard as a string
```

Helper functions:

| Function | Returns |
|---|---|
| `load_plotly_js(skip_download=False)` | Plotly.js source string |
| `load_leaflet_js(skip_download=False)` | Leaflet.js source string |
| `load_leaflet_css(skip_download=False)` | Leaflet CSS string (URLs fixed) |
| `build_weather_json(csv_rows)` | Structured dict by year/field |
| `compute_center_and_extent(geojson_fc)` | (center_lat, center_lon, min_lat, max_lat, min_lon, max_lon) |
| `discover_growers()` | List of {grower, farms} dicts |
| `validate_inputs(data_root, grower, farm)` | List of error strings (empty = OK) |

## Notes

- The dashboard is designed for **Kossuth County, IA** frost dates. For other regions, change `LAST_FROST_DOY` in the script.
- Field `osm-1465605263` (~43.42° lat) is ~10 km north of the other fields, causing a wider default map extent.
- The 5-year weather CSV is ~18K rows and ~1.4 MB as JSON — this dominates the HTML file size.
- ESRI World Imagery tiles are fetched at runtime from `server.arcgisonline.com`. No API key required.
