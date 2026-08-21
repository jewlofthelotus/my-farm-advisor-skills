---
name: field-year-dashboard
description: Generate a single multi-panel dashboard image for one field and growing season. Four vertically stacked panels (NDVI, precipitation, temperature, cumulative GDD) each with a descriptive title. Panels share a common DOY axis with event annotations and maturity-by-FIPS crop context.
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
| `field_id` | CLI or function arg | Unique field identifier (e.g., `OSM_1428284928`). The script discovers the field by scanning the runtime growers tree. Use `grower_slug`/`--grower-slug` when field ids collide across growers (common for CDL-derived grape/fruit parcels). |
| `year` | CLI or function arg | Target growing season (e.g., `2024`) |
| `DATA_PIPELINE_DATA_ROOT` | Env var | Absolute path to the runtime data root |

## Data sources (runtime tree)

| Panel | Source file | Notes |
|-------|-------------|-------|
| CDL crop | `{farm}/derived/tables/*_{year}_cdl.csv` | Dominant crop by pixel percentage. Falls back to the most recent available year's table when the requested year's archive is missing (CDL lags; e.g. current-year runs use last year's classification). |
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
  │ ██ [bars + polynomial trend]                   │ NDVI events annotated
  ├──────────────────────────────────────────────┤
  │ Daily Precipitation                            │ ← panel title
  │ ██ [bars + cumulative]                         │ Heavy rain / dry spell labels
  ├──────────────────────────────────────────────┤
  │ Air Temperature                                │ ← panel title
  │ ██ [fill areas + thresholds]                   │ Heat stress, frost, cool periods
  ├──────────────────────────────────────────────┤
  │ Cumulative Growing Degree Days                 │ ← panel title
  │ ██ [bars + cumulative]                         │ Growth stage markers
  └──────────────────────────────────────────────┘
  Day of Year (1–365)
```

## Crop thresholds used

Thresholds are derived from the strategy resource files under `strategy/crop-strategy/resources/`. Each crop defines:

- GDD base temperature and upper cap (Fahrenheit)
- Heat-stress temperature threshold
- Frost-sensitivity threshold
- Growth stages with approximate GDD ranges

If a CDL crop is not recognized or CDL data is missing, generic fallback thresholds (GDD base 50°F, cap 86°F) are used.

Crops with defined thresholds: Corn, Soybeans, Cotton, Winter Wheat, Sorghum, and Grapes (GDD base 50°F with Budbreak/Bloom/Fruit Set/Veraison/Harvest stage GDD targets). Grape GDD targets align with the 50°F-base targets used by the row-crop-intelligence dashboards.

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
  --grower-slug my-grower    # Scope field resolution to one grower
```

## Python API

```python
from field_year_dashboard import generate_field_year_dashboard

path = generate_field_year_dashboard(
    field_id="OSM_1428284928",
    year=2024,
    grower_slug="my-grower",  # optional; disambiguates colliding field ids
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
| Cool period | 3+ days T2M_MAX < 68°F (May–Jul) | Temp |
| Spring/fall frost | T2M_MIN ≤ frost threshold | Temp |
| Growth stage | Cumulative GDD crosses stage threshold | GDD |

## Notes

- NDVI panel uses polynomial trend fitting (degree ≤ 3) when 3+ acquisitions exist.
- The shared x-axis spans the earliest to latest data point across all panels.
- If a data source is missing, the panel shows a placeholder message and remaining panels still render.
- Weather rows with missing temperature/precipitation values (e.g., trailing NaN-padded days past the NASA POWER archive end in current-year files) are dropped before plotting so partial-season years render cleanly. When a ≥2-day NaN span exists in the raw year frame, the three weather panels (precip, temperature, GDD) get a grey `Weather gap` band shading the missing DOY range (same convention as the row-crop-intelligence dashboards).
- A `Sources` footer is drawn at the bottom of every dashboard. A `Generated: <iso timestamp>` line sits above the list with a blank line between; the list covers the actual data sources (Sentinel-2 L2A NDVI, NASA POWER weather, GDD method, CDL crop). When a weather gap is present, the footer adds a caveat explaining the grey band and the NASA POWER archive lag.
- Crop strategy resource files are read for reference only; the threshold values used in plotting are defined inline and should be updated when new resource years are added.
- The GDD panel includes county-level maturity context (corn RM or soybean MG) loaded from maturity-by-FIPS shared parquet files when available.

## Maturity-by-FIPS integration

When a field boundary GeoJSON provides state and county FIPS codes, the dashboard loads the corresponding county-level corn relative maturity (RM) or soybean maturity group (MG) from shared pipeline parquet files:

- Corn: `{DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/corn_maturity/rm_by_fips_{year}.parquet`
- Soybean: `{DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/soybean_maturity/mg_by_fips_{year}.parquet`

The RM or MG value is displayed in the GDD panel. If the parquet files are unavailable (e.g., maturity pipeline not yet run, or missing pyarrow engine), this section is silently omitted.

## Crop strategy integration

- GDD thresholds (base temperature, upper cap, growth stages) are derived from `strategy/crop-strategy/resources/2026-usa-{crop}.md` and defined inline in `CROP_THRESHOLDS`.
- Growth stage labels in the GDD panel (e.g., `V6→R1→R5→R6` for corn) come directly from these crop-specific strategy resources.
