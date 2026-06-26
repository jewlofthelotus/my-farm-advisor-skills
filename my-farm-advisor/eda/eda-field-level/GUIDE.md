---
name: eda-field-level
description: Field-level EDA comparing boundaries, CDL crop data, and weather across growers, farms, and individual fields.
version: 1.0.0
author: Boreal Bytes
tags: [eda, field-level, boundaries, cdl, weather, geospatial]
---

# Workflow: eda-field-level

## Description

Run a complete field-level exploratory analysis across the growers in your runtime data. This workflow produces static PNG maps, charts, and CSV tables comparing field boundaries, cropland data layer (CDL), and weather at multiple scales: within-field (time), within-grower, and across growers.

## When to Use This Workflow

- **After the data pipeline has been run** and farm assets (boundaries, CDL tables, weather) exist in `data-pipeline/growers/`
- **Before any report assembly** — the outputs here are building blocks for a later one-time report
- **When you need a static, reproducible view** of field characteristics, crop rotations, and weather comparisons

## Prerequisites

```bash
pip install pandas numpy matplotlib seaborn scipy geopandas
```

## Scripts

All scripts live in `src/` and expect `$DATA_PIPELINE_DATA_ROOT` to point at the runtime data root (e.g. `~/my-farm-advisor-runtime/data-pipeline`). They write to `$DATA_PIPELINE_DATA_ROOT/data-pipeline/eda/field-level/output/`.

| Script | Outputs | What it produces |
|---|---|---|
| `eda_geospatial_map.py` | M2 PNG | Cross-grower centroid field map with area-scaled markers on a state-outline basemap with water-body fill for the Great Lakes |
| `eda_field_boundaries.py` | V1c PNG | Cumulative area bars |
| `eda_field_cdl.py` | V3c PNG, V4a PNGs | Cross-grower crop composition flow, per-field rotation heatmap |
| `eda_field_weather.py` | V5a PNG, V6b PNG, A3c PNG + CSV | Seasonal violin plot, GDD comparison, precip/GDD anomaly vs crop-diversity scatter + correlation table |

## Quick Start

```bash
cd $DATA_PIPELINE_DATA_ROOT/data-pipeline/src/scripts/eda/field-level/src

# Process all discovered growers (default)
python eda_geospatial_map.py
python eda_field_boundaries.py
python eda_field_cdl.py
python eda_field_weather.py

# Process a subset of growers
python eda_geospatial_map.py --growers ia-grower,ne-grower
```

## Output Directory

All artifacts land in:

```
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/eda/field-level/output/
```

See `GUIDE.md` in each script directory for per-script details.

## Notes

- **Dynamic discovery** — scripts automatically scan `growers/` and process all available growers. Use `--growers slug1,slug2` to limit the set.
- **No soil analysis** is included in this workflow.
- **No report generation** is performed here; report assembly is a separate one-time step.
- All maps are **static PNGs only** — no interactive web maps.
