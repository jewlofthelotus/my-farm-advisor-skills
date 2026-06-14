# Grower Web-Map Workflow

## Description

Generate a lightweight, self-contained interactive web map for each existing grower in the data-pipeline runtime. The map shows all field polygon boundaries on an OpenStreetMap basemap with clickable popups and a zoom-to-field sidebar.

**Output:** `<grower-dir>/web-map/grower_map.html` — a single HTML file (no server required).

## Prerequisites

- `DATA_PIPELINE_DATA_ROOT` must point to the runtime data root
- Runtime virtualenv must be installed (geopandas, pandas)

## Usage

### Single grower

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/generate_grower_web_map.py --grower-slug il-grower
```

### All growers

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/generate_grower_web_map.py
```

### Custom output directory

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/generate_grower_web_map.py --grower-slug il-grower \
  --output-dir /tmp/my-maps
```

## Map features

- Field polygons with distinct colored outlines
- Click popup: grower, farm, field ID, crop, area (acres), county
- Sidebar field list with "Zoom to" links
- OpenStreetMap tile basemap (loads from internet)
- Zoom and pan controls

## Data source

Reads `<farm>/boundary/field_boundaries.geojson` from each farm under the grower directory.
