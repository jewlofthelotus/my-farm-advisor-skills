# Row Crop Intelligence & Data Dashboard

## Purpose

Build a single-page, offline-functional operational dashboard for corn fields at the grower level. The dashboard ingests field boundaries, NDVI time series, soil metrics, and daily weather from the runtime data pipeline and renders a full analytical view with zero network dependencies.

## How to Run

### Prerequisites

- Python 3.10+ with `pandas`, `numpy`, `rasterio` installed (plus `shapely`, `scikit-learn`, `geopandas` for Management Zones)
- The runtime data pipeline installed and seeded (see `../data-pipeline/`)
- `DATA_PIPELINE_DATA_ROOT` environment variable pointing to the runtime root

### Generate the Dashboard

```bash
# Activate the runtime venv (if using the pipeline environment)
source /home/coder/my-farm-advisor-runtime/data-pipeline/.venv/bin/activate

# Run the generator for il-grower
python my-farm-advisor/field-management/row-crop-intelligence/scripts/generate_dashboard.py --grower il-grower

# Generate for a grape grower — crop type is auto-detected from the field CDL data
# (cdl_crop_code 69 / crop_name "grapes"), so no --crop-type flag is needed:
#   GDD base 50°F; Budbreak 59 / Bloom 298 / Fruit Set 473 / Veraison 1401 / Harvest 2053;
#   NDVI watch floor 0.45, stress floor 0.30
python my-farm-advisor/field-management/row-crop-intelligence/scripts/generate_dashboard.py \
  --grower fennville

# Or force a specific config with --crop-type (corn | grape)
python my-farm-advisor/field-management/row-crop-intelligence/scripts/generate_dashboard.py \
  --grower fennville \
  --crop-type grape

# Or specify a custom data root and/or output path
python my-farm-advisor/field-management/row-crop-intelligence/scripts/generate_dashboard.py \
  --grower il-grower \
  --data-root /path/to/runtime \
  --output /path/to/output.html
```

`--crop-type` selects the threshold/phenology block from `CROP_CONFIG` in the script. When omitted, it auto-detects from the grower's field boundaries (`cdl_crop_code`/`crop_name`): vineyard fields resolve to `grape`, otherwise `corn` (the default grower stays corn). The crop config block is designed for further extension — adding another crop requires only a new config dict, with no JS/Python logic changes.

### Output

The generated dashboard HTML is written to:

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/derived/dashboards/row_crop_intelligence.html
```

Open the file directly in any modern browser. No server required.

### Selecting a view

The dashboard defaults to the current growing season (actionable mode: Priority Actions, Fields Requiring Attention, live risk classification). Use the **Year** selector in the header to switch to a past season (reference mode: Season Recap & Notable Events, Peak NDVI, Season Stress Duration) for any year with crop-field data.

## Where to Look in the Runtime Dataset

| Dashboard Section | Runtime Data Source |
|---|---|
| Field boundaries (map) | `growers/<g>/farms/<f>/boundary/field_boundaries.geojson` |
| Field boundaries (per-field) | `growers/<g>/farms/<f>/fields/<field>/boundary/field_boundary.geojson` |
| NDVI time series | `growers/<g>/farms/<f>/fields/<field>/satellite/{sentinel,landsat}/<year>/<scene>/*_ndvi.tif` (computed per-scene mean NDVI) |
| Soil AWS, OM% | `growers/<g>/farms/<f>/fields/<field>/soil/ssurgo_summary.csv` |
| Daily weather (temp, precip) | `growers/<g>/farms/<f>/fields/<field>/weather/daily_weather.csv` |
| Corn/soybean avg NDVI | `growers/<g>/farms/<f>/fields/<field>/derived/summaries/ndvi_card_summary.json` |
| Crop rotation | `growers/<g>/farms/<f>/derived/tables/*_crop_rotation.csv` |
| Farm-level weather | `growers/<g>/farms/<f>/derived/tables/*_weather_*.csv` |
| Farm-level soil | `growers/<g>/farms/<f>/derived/tables/*_ssurgo_summary.csv` |

**Known data limitation:** NASA POWER weather data typically lags 1–2 months behind the current date. The dashboard handles this gracefully (a shaded "Weather gap" region on time-series charts, and GDD/rain KPIs that freeze at the last available reading rather than showing incorrect values) — but it means current-season weather-derived metrics may reflect conditions from several weeks prior, not today.

## Dependencies

### Build-Time (Python)

| Package | Purpose |
|---|---|
| `pandas` | CSV/JSON data assembly |
| `numpy` | NDVI statistics, GDD calculation |
| `rasterio` | Read NDVI GeoTIFF scene files |
| `shapely` | Dissolve + simplify management-zone polygons (optional; zones skipped if absent) |
| `scikit-learn` | K-means clustering for management zones (optional) |
| `geopandas` | Management-zone area in acres (optional; falls back without zones) |
| `json` (stdlib) | Data serialization |
| `csv` (stdlib) | Weather/soil CSV parsing |
| `datetime` (stdlib) | Date handling |

All are available in the pipeline venv at `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/`. The management-zone packages (`shapely`, `scikit-learn`, `geopandas`) are required only for Management Zones v1 — the generator degrades gracefully without them, generating the dashboard with everything except zone polygons.

### Runtime (Browser)

**None.** The dashboard is a single self-contained HTML file with:
- D3.js (v7) minified and inlined
- All data embedded as JSON
- All icons as inline SVG
- System fonts only (no webfonts)
- Map rendered via D3 `geoPath` from embedded GeoJSON (no tile basemaps)

Open it offline with zero external requests.

## Crop-Type Configuration

Thresholds and definitions live in a `CROP_CONFIG` dictionary keyed by `crop_type`. `corn` and `grape` are bundled; the default is auto-detected from field CDL data. Adding additional crop config blocks later requires no logic changes — only new thresholds, growth-stage labels, and KPI definitions.