# Row Crop Intelligence & Data Dashboard

## Purpose

Build a single-page, offline-functional operational dashboard for corn fields at the grower level. The dashboard ingests field boundaries, NDVI time series, soil metrics, and daily weather from the runtime data pipeline and renders a full analytical view with zero network dependencies.

## How to Run

### Prerequisites

- Python 3.10+ with `pandas`, `numpy`, `rasterio`, `geopandas` installed
- The runtime data pipeline installed and seeded (see `../data-pipeline/`)
- `DATA_PIPELINE_DATA_ROOT` environment variable pointing to the runtime root

### Generate the Dashboard

```bash
# Activate the runtime venv (if using the pipeline environment)
source /home/coder/my-farm-advisor-runtime/data-pipeline/.venv/bin/activate

# Run the generator for il-grower
python my-farm-advisor/row-crop-intelligence/scripts/generate_dashboard.py --grower il-grower

# Or specify a custom data root
python my-farm-advisor/row-crop-intelligence/scripts/generate_dashboard.py \
  --grower il-grower \
  --data-root /path/to/runtime
```

### Output

The generated dashboard HTML is written to:

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/derived/dashboards/row_crop_intelligence.html
```

Open the file directly in any modern browser. No server required.

## Where to Look in the Runtime Dataset

| Dashboard Section | Runtime Data Source |
|---|---|
| Field boundaries (map) | `growers/<g>/farms/<f>/boundary/field_boundaries.geojson` |
| Field boundaries (per-field) | `growers/<g>/farms/<f>/fields/<field>/boundary/field_boundary.geojson` |
| NDVI time series | `growers/<g>/farms/<f>/fields/<field>/satellite/{sentinel,landsat}/<year>/<scene>/*_ndvi.tif` (computed per-scene mean NDVI) |
| Soil AWC, OM% | `growers/<g>/farms/<f>/fields/<field>/soil/ssurgo_summary.csv` |
| Daily weather (temp, precip) | `growers/<g>/farms/<f>/fields/<field>/weather/daily_weather.csv` |
| Corn/soybean avg NDVI | `growers/<g>/farms/<f>/fields/<field>/derived/summaries/ndvi_card_summary.json` |
| Crop rotation | `growers/<g>/farms/<f>/derived/tables/*_crop_rotation.csv` |
| Farm-level weather | `growers/<g>/farms/<f>/derived/tables/*_weather_*.csv` |
| Farm-level soil | `growers/<g>/farms/<f>/derived/tables/*_ssurgo_summary.csv` |

## Dependencies

### Build-Time (Python)

| Package | Purpose |
|---|---|
| `pandas` | CSV/JSON data assembly |
| `numpy` | NDVI statistics, GDD calculation |
| `rasterio` | Read NDVI GeoTIFF scene files |
| `geopandas` | GeoJSON geometry handling |
| `json` (stdlib) | Data serialization |
| `csv` (stdlib) | Weather/soil CSV parsing |
| `datetime` (stdlib) | Date handling |

All are available in the pipeline venv at `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/`.

### Runtime (Browser)

**None.** The dashboard is a single self-contained HTML file with:
- D3.js (v7) minified and inlined
- All data embedded as JSON
- All icons as inline SVG
- System fonts only (no webfonts)
- Map rendered via D3 `geoPath` from embedded GeoJSON (no tile basemaps)

Open it offline with zero external requests.

## Crop-Type Configuration

Thresholds and definitions live in a `CROP_CONFIG` dictionary keyed by `crop_type`. Currently only `corn` is populated. Adding a `grape` config block later requires no logic changes — only new thresholds, growth-stage labels, and KPI definitions.
