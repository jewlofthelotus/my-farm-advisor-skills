# Grower Dashboard

A self-contained offline HTML dashboard for any farm, combining:

- **Left pane:** Leaflet map with ESRI World Imagery satellite basemap and color-coded field boundaries. Dropdown zooms to individual fields. Field multi-select filters across map + charts.
- **Right pane (top):** Plotly cumulative GDD time-series (base 10°C) starting from a hardcoded last frost date (DOY 112 / April 22 for Kossuth County, IA). Each field is a colored line.
- **Right pane (bottom):** Plotly dual-axis precipitation chart — daily bars (left y-axis) and cumulative sum (right y-axis). Frost date dashed line on both charts.

**Output:** A single 5–7 MB HTML file with Plotly.js, Leaflet.js, GeoJSON boundaries, and 5 years of weather data embedded inline. No external server, no runtime network calls beyond tile and image CDN loads on first open.

**Data source requirements:**
- Field boundaries: `field_boundaries.geojson` (EPSG:4326)
- Weather: farm-level `*_weather_2021_2025.csv` with columns `field_id, date, T2M_MIN, T2M_MAX, PRECTOTCORR`
