---
name: field-year-dashboard
description: Generate a single multi-panel dashboard image for one field and growing season. Four vertically stacked panels (NDVI, precipitation, temperature, cumulative GDD) each have a descriptive title and per-panel caption summarizing significant events. Panels share a common DOY axis with event annotations and maturity-by-FIPS crop context.
license: MIT
compatibility: Requires Python 3.10+, pandas, numpy, matplotlib. Optional rasterio for NDVI TIFF reading.
metadata:
  author: Boreal Bytes
  version: "1.0.0"
  category: visualization
  tags: dashboard, ndvi, weather, gdd, field, seasonal
---

# Field-Year Dashboard

## When to use

Use this when you need a single-field visual summary of one growing season showing NDVI dynamics, daily precipitation, temperature, and growing-degree-day accumulation together on an aligned time axis.

## Inputs

| Input | Source | Description |
|-------|--------|-------------|
| `field_id` | CLI or function arg | Unique field identifier (e.g., `OSM_1428284928`). The script discovers the field by scanning the runtime growers tree. |
| `year` | CLI or function arg | Target growing season (e.g., `2024`) |
| `DATA_PIPELINE_DATA_ROOT` | Env var | Absolute path to the runtime data root |

## Data sources (runtime tree)

| Panel | Source file | Notes |
|-------|-------------|-------|
| CDL crop | `{farm}/derived/tables/*_{year}_cdl.csv` | Dominant crop by pixel percentage |
| Weather | `{field}/weather/daily_weather.csv` | Columns: date, T2M, T2M_MAX, T2M_MIN, PRECTOTCORR |
| NDVI | `{field}/satellite/sentinel/manifest.json` + NDVI TIFFs, or CSV with `date` + `mean_ndvi` | Scans manifest, NDVI rasters, or CSVs |
| Crop strategy | `strategy/crop-strategy/resources/2026-usa-{crop}.md` | Thresholds extracted from skill resources |

## Output

A single PNG saved to `{field}/derived/reports/{year}_field_dashboard.png`.

## Dashboard layout

```
  Field {id} — {year} Growing Season                  ← suptitle
  ┌──────────────────────────────────────────────┐
  │ NDVI Dynamics                                  │ ← panel title
  │ Peak 0.89 (DOY 205) | Green-up DOY 140         │ ← per-panel caption
  │ ██ [bars + polynomial trend]                   │ NDVI events annotated
  ├──────────────────────────────────────────────┤
  │ Daily Precipitation                            │ ← panel title
  │ Total 580 mm | Dry spell DOY 208-220           │ ← per-panel caption
  │ ██ [bars + cumulative]                         │ Heavy rain / dry spell labels
  ├──────────────────────────────────────────────┤
  │ Air Temperature                                │ ← panel title
  │ Frost-free DOY 115-285 | 5 heat stress days    │ ← per-panel caption
  │ ██ [fill areas + thresholds]                   │ Heat stress, frost, cool periods
  ├──────────────────────────────────────────────┤
  │ Cumulative Growing Degree Days                 │ ← panel title
  │ GDD 1850 | V6→R1→R5→R6 | County RM 108         │ ← per-panel caption
  │ ██ [bars + cumulative]                         │ Growth stage markers
  └──────────────────────────────────────────────┘
  Day of Year (1–365)
```

## Crop thresholds used

Thresholds are derived from the strategy resource files under `strategy/crop-strategy/resources/`. Each crop defines:

- GDD base temperature and upper cap (Celsius)
- Heat-stress temperature threshold
- Frost-sensitivity threshold
- Growth stages with approximate GDD ranges

If a CDL crop is not recognized or CDL data is missing, generic fallback thresholds (GDD base 10°C, cap 30°C) are used.

## CLI usage

```bash
export DATA_PIPELINE_DATA_ROOT=/path/to/runtime

python src/field_year_dashboard.py \
  --field-id OSM_1428284928 \
  --year 2024
```

Optional flags:

```bash
  --data-root /custom/path   # Override DATA_PIPELINE_DATA_ROOT
  --skill-base /path/skills  # Path to my-farm-advisor skill root
  --output /custom/output.png # Override output path
```

## Python API

```python
from field_year_dashboard import generate_field_year_dashboard

path = generate_field_year_dashboard(
    field_id="OSM_1428284928",
    year=2024,
)
print(f"Dashboard saved: {path}")
```

## Event detection logic

| Event | Condition | Panel |
|-------|-----------|-------|
| NDVI decline | Consecutive acquisition Δ < -0.15 | NDVI |
| Rapid green-up | NDVI rise > 0.3 within ~15 days | NDVI |
| Peak NDVI | Seasonal maximum acquisition value | NDVI |
| Heavy rain | PRECTOTCORR > 25 mm | Precip |
| Dry spell | 10+ consecutive days < 1 mm | Precip |
| Heat stress | T2M_MAX > crop threshold | Temp |
| Cool period | 3+ days T2M_MAX < 20°C (May–Jul) | Temp |
| Spring/fall frost | T2M_MIN ≤ frost threshold | Temp |
| Growth stage | Cumulative GDD crosses stage threshold | GDD |

## Notes

- NDVI panel uses polynomial trend fitting (degree ≤ 3) when 3+ acquisitions exist.
- The shared x-axis spans the earliest to latest data point across all panels.
- If a data source is missing, the panel shows a placeholder message and remaining panels still render.
- Crop strategy resource files are read for reference only; the threshold values used in plotting are defined inline and should be updated when new resource years are added.
- Each panel has a descriptive title and a per-panel caption summarizing the most significant detected events for that metric.
- The GDD panel caption includes county-level maturity context (corn RM or soybean MG) loaded from maturity-by-FIPS shared parquet files when available.

## Per-panel captions

Each caption is composed from events detected for that panel:

| Panel | Caption content | Source |
|-------|----------------|--------|
| NDVI | Peak NDVI value and DOY, rapid green-up date, decline date | `_detect_ndvi_events()` |
| Precipitation | Total cumulative precipitation, dry spell DOY ranges, heavy rain event count | `_detect_precip_events()` + cumulative total |
| Temperature | Frost-free DOY range (spring/fall frost), heat stress day count, GDD base temperature | `_detect_temp_events()` |
| GDD | Total cumulative GDD, growth stage progression (→-separated), county RM/MG from maturity-by-FIPS, crop strategy reference filename | `_detect_gdd_events()` + maturity lookup |

## Maturity-by-FIPS integration

When a field boundary GeoJSON provides state and county FIPS codes, the dashboard loads the corresponding county-level corn relative maturity (RM) or soybean maturity group (MG) from shared pipeline parquet files:

- Corn: `{DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/corn_maturity/rm_by_fips_{year}.parquet`
- Soybean: `{DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/soybean_maturity/mg_by_fips_{year}.parquet`

The RM or MG value is displayed in the GDD panel caption. If the parquet files are unavailable (e.g., maturity pipeline not yet run, or missing pyarrow engine), this section is silently omitted.

## Crop strategy integration

- GDD thresholds (base temperature, upper cap, growth stages) are derived from `strategy/crop-strategy/resources/2026-usa-{crop}.md` and defined inline in `CROP_THRESHOLDS`.
- Growth stage labels in the GDD panel caption (e.g., `V6→R1→R5→R6` for corn) come directly from these crop-specific strategy resources.
- The resource filename appears as a reference in the GDD panel caption (e.g., `Ref: 2026-usa-corn.md`).
