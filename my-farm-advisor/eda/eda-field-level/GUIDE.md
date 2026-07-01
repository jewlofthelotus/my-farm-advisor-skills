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
| `eda_geospatial_map.py` | `cross_grower_field_centroid_map.png` | Geospatial map of field centroids across all growers with area-scaled markers on state-outline basemap |
| `eda_field_boundaries.py` | `cross_grower_field_area_cumulative_stacked_bar.png`, `cross_grower_field_area_histogram.png`, `cross_grower_field_count_vs_acreage.png` | Cumulative field area bar chart, faceted field-size histograms, and field count vs total acreage paired bars by grower |
| `eda_field_cdl.py` | `cross_grower_cdl_crop_composition_flow.png`, `{grower}_cdl_crop_rotation_heatmap.png` (per grower), `{grower}_cdl_crop_composition_by_year.png` (per grower) | Cross-grower crop composition flow chart, per-grower field-level crop rotation heatmaps, and per-year CDL crop composition by grower |
| `eda_field_weather.py` | `cross_grower_weather_average_cumulative_annual_precip.png`, `{grower}_weather_mean_daily_precip_by_season.png` (per grower), `cross_grower_weather_temp_precip_dual_axis.png` | Average cumulative annual precipitation bar chart, per-grower daily precipitation faceted by year and colored by season, and precipitation + temperature dual-axis by grower |

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
