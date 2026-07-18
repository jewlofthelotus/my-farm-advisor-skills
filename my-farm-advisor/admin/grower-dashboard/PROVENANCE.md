# Provenance Record

## Origin

This skill was derived from the runtime generator script at:

- **Source:** `data-pipeline/src/scripts/generate_grower_dashboard.py`
- **Runtime path:** `my-farm-advisor-runtime/data-pipeline/src/scripts/generate_grower_dashboard.py`

The original script was written for a single hardcoded grower/farm (`ia-grower` / `ia-grower-iowa`). This skill refactors it into a reusable generator accepting `--grower-slug` and `--farm-slug` CLI arguments, using `lib.paths` from the data-pipeline for canonical path resolution.

## Skill Authorship

- **Author:** Superior Byte Works, LLC
- **Created:** 2025-07-17
- **License:** Apache-2.0

## Dependencies

- **Plotly.js 2.35.2** — downloaded and embedded at generation time from `https://cdn.plot.ly/plotly-2.35.2.min.js`
- **Leaflet 1.9.4** — downloaded and embedded at generation time from `https://unpkg.com/leaflet@1.9.4/dist/leaflet.js`
- **ESRI World Imagery** — tile server URL used at runtime (no API key required)
- **NASA POWER** — weather data source for the CSV inputs
