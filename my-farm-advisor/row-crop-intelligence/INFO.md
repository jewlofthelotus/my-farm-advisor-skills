# Row Crop Intelligence & Data Dashboard — Project Info

## Project Overview

This project creates a **single-page, offline-functional operational dashboard** for row crop fields at the grower level. It ingests field boundaries, satellite-derived NDVI time series, SSURGO soil metrics, and daily weather data from the My Farm Advisor runtime data pipeline and renders a comprehensive analytical view.

The dashboard is built as a **single self-contained HTML file** with all dependencies inlined:
- D3.js for interactive charts and map rendering
- GeoJSON field boundaries rendered via D3 `geoPath` (no tile basemaps)
- Inline SVG icons
- All data embedded as JSON

This architecture ensures the dashboard works with **zero network connectivity** after generation.

### Design Principles

- **Crop-agnostic data schema** — the core data model (field_id, geometry, metrics) does not reference crop-specific concepts. Crop-specific thresholds, growth-stage labels, and KPI definitions live in a separate `CROP_CONFIG` block keyed by `crop_type`, making it straightforward to add grape/vineyard support later by adding a config block.
- **Risk-tiered visualization** — all fields are classified into a fixed 3-tier scale (Healthy / Watch / Critical) using the same color scheme (blue / amber / orange-red) across KPIs, map, charts, and action lists. Colors are paired with icons for colorblind safety.
- **No framework** — vanilla JavaScript with a pub/sub state object. Every chart, KPI, and map subscribes to the shared state and re-renders on filter changes. No React, no Vue, no build step.

## Dataset Description

The runtime data for `il-grower` (Iroquois County, Illinois) contains:

### Fields (10 total)

| Field ID | Area (ac) | Crop (2024) |
|---|---|---|
| osm-1253853022 | 17.1 | Corn |
| osm-1254255035 | 16.1 | Soybeans |
| osm-1280521064 | 0.9 | Meadow |
| osm-1288035236 | 3.2 | Meadow |
| osm-1293013560 | 13.4 | Farmland |
| osm-1293013562 | 4.0 | Farmland |
| osm-1296444203 | 7.7 | Farmland |
| osm-1499317763 | 259.5 | Farmland |
| osm-1525396389 | 50.6 | Farmland |
| osm-889020586 | 238.5 | Farmland |

### NDVI Data

- **Source:** Sentinel-2 and Landsat 8/9 imagery, 2021–2026
- **Scene count:** ~7–16 scenes per satellite per year per field
- **Products:** Per-scene NDVI GeoTIFFs, annual composites, crop-rolled averages (corn vs. soybean), peak 95th percentile composites
- **Time series:** ~70–100 date-value pairs per field over 5+ years

### Soil Data

- **Source:** NRCS SSURGO database
- **Metrics per field:** AWC (available water capacity, 0.44–0.90 in), organic matter % (1.8–7.5%), pH (6.6–7.3), CEC, sand/silt/clay fractions, drainage class, erosion risk
- **Structure:** Horizon-level component data aggregated to field-level summary

### Weather Data

- **Source:** NASA POWER (daily, 2021–2026)
- **Variables:** T2M_MIN / T2M_MAX (C), PRECTOTCORR (mm/day), solar radiation, humidity, wind speed
- **Coverage:** ~1,826 daily records per field

### Crop Rotation

- Corn-soybean 2-year rotation pattern
- Pre-computed rotation sequences with next-crop predictions

## Dashboard Explanation

### Filter Bar

- **Field multi-select** — choose one or all fields
- **Date range slider** — filter NDVI and weather time series to a window
- **Reset button** — clears all filters

### Sections (top to bottom)

1. **KPI/Summary Header**
   - "Fields Requiring Attention" — count of fields below the NDVI stress threshold (primary action metric)
   - Average NDVI with trend direction arrow
   - GDD accumulated vs. normal
   - Days since significant rainfall (>0.1 in)

2. **NDVI Time Series** — one line per field, D3 line chart, with threshold band for the stress cutoff

3. **Field Ranking** — horizontal bar chart of current NDVI per field, color-coded by risk tier

4. **Driver/Correlation View** — scatter plot of NDVI vs. AWC (or GDD), revealing which soil/weather variables best explain field health differences

5. **Geospatial Map** — choropleth of field boundaries colored by risk tier using D3 `geoPath`. Click a field to filter.

6. **Weather / GDD** — GDD accumulation over the growing season, actual vs. long-term normal line chart

7. **Soil / Sustainability** — bar chart of AWC or organic matter % per field, color-coded by risk tier

8. **Priority Action List** — auto-generated, data-ranked list of actions, e.g.:
   > "Field 3: NDVI −12% in 7 days, low AWC — scout/irrigate first"

9. **Narrative Panel** — written interpretation referencing live computed numbers:
   - Patterns/trends observed across fields
   - Healthier vs. at-risk fields
   - Environmental/soil condition variation
   - Decisions/actions the analysis informs
   - Most important predictive variables

10. **Footer** — threshold/methodology legend + data freshness timestamp

## Analytical Interpretation

### Key Questions the Dashboard Answers

1. **Which fields need attention right now?** — The "Fields Requiring Attention" KPI surfaces fields below the NDVI stress threshold. The priority action list ranks them by severity.

2. **How is NDVI trending this season?** — The time-series chart shows per-field trajectories. A declining slope over the last 2–3 scenes triggers a Watch or Critical classification regardless of absolute value.

3. **Which soil and weather variables drive field health?** — The NDVI vs. AWC scatter plot reveals whether soil water-holding capacity explains field-to-field variation. The GDD chart shows whether heat accumulation is limiting.

4. **How do fields compare spatially?** — The choropleth map gives instant visual identification of spatial clusters of at-risk fields.

5. **What should I do and in what order?** — The priority action list combines NDVI trend, soil limitation, and weather recency into a ranked triage list.

### Risk Classification Logic

- **Healthy** — NDVI >= 0.7 and stable or improving over last 2 scenes
- **Watch** — NDVI between 0.5 and 0.7, or declining >5% over last 2 scenes
- **Critical** — NDVI < 0.5, or declining >10% over last 2 scenes

Thresholds are derived from the `CROP_CONFIG` block and can be adjusted per crop type.

## AI Usage Documentation

### How AI Tools Were Used During This Project

- **Debugging Python errors** — AI identified and fixed rasterio window read issues when computing per-scene mean NDVI across fields with varying CRS. Also caught a pandas groupby/merge mismatch when joining soil summaries to field boundaries.

- **Improving visualizations** — AI suggested the risk-tier colorblind-safe palette (blue #4A7FB5 → amber #E8A838 → orange-red #D95F4A) and recommended pairing colors with inline SVG icons instead of relying on color alone. Also proposed the "plain-language chart titles stating the takeaway" approach (e.g., "NDVI Declining in 2 Fields" instead of "NDVI Over Time").

- **Explaining geospatial workflows** — AI clarified the D3 `geoPath` / `d3-geo` projection pipeline for rendering embedded GeoJSON without a tile basemap. Also helped structure the algorithm for efficiently computing per-scene mean NDVI from GeoTIFF files without loading full rasters into memory.

- **Generating alternative analytical ideas** — AI proposed the NDVI-vs-AWC scatter as a driver/correlation view, the "days since significant rain" KPI from weather data, and the priority action list as a data-driven replacement for hardcoded field notes.

- **Improving dashboard layout structure** — AI suggested the top-to-bottom information hierarchy (KPI → time series → ranking → correlation → map → weather → soil → actions → narrative → footer) based on dashboard UX research. Also recommended the single-file offline architecture to eliminate deployment friction.
