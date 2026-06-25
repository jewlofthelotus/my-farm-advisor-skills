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
- Satellite (default) and OpenStreetMap tile basemaps
- Zoom and pan controls

### Data overlay layers (toggle via layer control)

**SSURGO Soil Units** — colored soil polygon overlay from `ssurgo_soil_types.geojson`.
- Each soil component (`compname`) gets a distinct color
- Click any soil polygon for a popup with component name, drainage class, OM%, and pH
- Legend shows component-color mapping
- Data source: `<farm>/fields/<field_slug>/soil/ssurgo_soil_types.geojson`

**NDVI (mean)** — field choropleth colored by mean NDVI from the latest yearly composite.
- Fields with no NDVI data appear dashed/gray
- Click any field for a popup with the mean NDVI value
- Legend shows the NDVI color scale (red→yellow→green)
- Data source: `<farm>/fields/<field_slug>/derived/features/ndvi_year_*_composite.tif`

Both overlays are optional checkboxes. The legend updates dynamically to show the active layer's key.

## Data source

- **Field boundaries:** `<farm>/boundary/field_boundaries.geojson`
- **SSURGO:** `<farm>/fields/<field_slug>/soil/ssurgo_soil_types.geojson` (per-field)
- **NDVI:** `<farm>/fields/<field_slug>/derived/features/ndvi_year_<year>_composite.tif` (latest year used)

All data must already exist in the pipeline runtime. SSURGO and NDVI are silently skipped when the files are absent.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--grower-slug` | all growers | Generate for one grower |
| `--output-dir` | `<grower>/web-map/` | Custom output directory |
| `--no-ssurgo` | include | Skip SSURGO soil overlay |
| `--no-ndvi` | include | Skip NDVI overlay |
