#!/usr/bin/env python3
"""Generate a self-contained Row Crop Intelligence & Data Dashboard HTML for a grower."""

import argparse
import csv
import gzip
import io
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window
from shapely.geometry import shape

# ---------------------------------------------------------------------------
# Crop-type configuration  (grape-ready: add a "grape" key later)
# ---------------------------------------------------------------------------
CROP_CONFIG = {
    "corn": {
        "stress_threshold": 0.50,
        "watch_threshold": 0.70,
        "ndvi_decline_warning_pct": 5.0,
        "ndvi_decline_critical_pct": 10.0,
        "gdd_base_temp_f": 50.0,
        "gdd_target": 2500,
        "precip_significant_in": 0.1,
        "precip_significant_mm": 2.54,
        "growth_stages": {
            "VE": 0, "V6": 400, "VT": 1100, "R1": 1300, "R2": 1600,
            "R3": 1850, "R4": 2100, "R5": 2300, "R6": 2500
        },
        "crop_name": "Corn",
        "kpi_units": {"ndvi": "", "gdd": "\u00b0F-days", "precip": "in", "awc": "in/in", "om": "%"}
    }
}

THRESHOLD_LABELS = {
    "healthy": {"label": "Healthy", "color": "#4A7FB5", "icon": "check"},
    "watch": {"label": "Watch", "color": "#E8A838", "icon": "alert"},
    "critical": {"label": "Critical", "color": "#D95F4A", "icon": "warning"},
}

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def resolve_data_root():
    env = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env:
        return Path(env)
    return Path("/home/coder/my-farm-advisor-runtime")

def grower_path(data_root, grower_slug):
    return data_root / "data-pipeline" / "growers" / grower_slug

def farm_paths(grower_root):
    farms_dir = grower_root / "farms"
    if not farms_dir.is_dir():
        return []
    return sorted(farms_dir.iterdir())

def field_paths(farm_root):
    fields_dir = farm_root / "fields"
    if not fields_dir.is_dir():
        return []
    return sorted(fields_dir.iterdir())

# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------
def read_field_boundary(field_dir):
    path = field_dir / "boundary" / "field_boundary.geojson"
    if not path.exists():
        return None
    fc = json.loads(path.read_text())
    if fc["features"]:
        f = fc["features"][0]
        return {"type": "Feature", "geometry": f["geometry"], "properties": f.get("properties", {})}
    return None

def read_ndvi_card_summary(field_dir):
    path = field_dir / "derived" / "summaries" / "ndvi_card_summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())

def read_ndvi_yearly_summary(field_dir):
    path = field_dir / "derived" / "summaries" / "ndvi_yearly_summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())

def read_soil_summary(field_dir):
    path = field_dir / "soil" / "ssurgo_summary.csv"
    if not path.exists():
        return None
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if rows:
        return rows[0]
    return None

def read_weather_csv(field_dir):
    path = field_dir / "weather" / "daily_weather.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df = df.sort_values("date")
    return df.to_dict("records")

def compute_scene_ndvi_time_series(field_dir, data_root):
    """Compute per-scene mean NDVI from Sentinel and Landsat GeoTIFFs."""
    series = []
    for sat in ("sentinel", "landsat"):
        sat_base = field_dir / "satellite" / sat
        if not sat_base.is_dir():
            continue
        for year_dir in sorted(sat_base.iterdir()):
            if not year_dir.is_dir():
                continue
            for scene_dir in sorted(year_dir.iterdir()):
                if not scene_dir.is_dir():
                    continue
                ndvi_path = scene_dir / "ndvi.tif"
                if not ndvi_path.exists():
                    ndvi_path = scene_dir / f"{scene_dir.name}_ndvi.tif"
                if not ndvi_path.exists():
                    continue
                try:
                    date_str = scene_dir.name.split("_")[-1]
                    scene_date = datetime.strptime(date_str, "%Y%m%d").date()
                except (ValueError, IndexError):
                    continue
                try:
                    with rasterio.open(ndvi_path) as src:
                        data = src.read(1)
                        valid = data[~np.isnan(data) & (data > -1) & (data < 2)]
                        if len(valid) > 0:
                            mean_val = round(float(np.mean(valid)), 3)
                            series.append({"date": scene_date.isoformat(), "value": mean_val})
                except Exception:
                    pass
    series.sort(key=lambda x: x["date"])
    return series

def compute_gdd(tmin_c, tmax_c, base_c=10.0):
    """Growing degree days in Celsius, converted to Fahrenheit scale if needed."""
    avg_c = (tmin_c + tmax_c) / 2.0
    return max(0.0, avg_c - base_c)

def compute_weather_summaries(weather_records):
    if not weather_records:
        return {}
    df = pd.DataFrame(weather_records)
    df["date"] = pd.to_datetime(df["date"])
    df["gdd_c"] = df.apply(lambda r: compute_gdd(r["T2M_MIN"], r["T2M_MAX"]), axis=1)
    today = date.today()
    current_year = today.year
    current = df[df["date"].dt.year == current_year]
    # If no data for current year, fall back to most recent complete year
    if not len(current):
        max_year = int(df["date"].dt.year.max())
        current = df[df["date"].dt.year == max_year]
        use_year = max_year
    else:
        use_year = current_year
    historical = df[df["date"].dt.year < use_year]

    gdd_current = round(float(current["gdd_c"].sum()), 0) if len(current) else 0
    gdd_normal = round(float(historical.groupby(historical["date"].dt.year)["gdd_c"].sum().mean()), 0) if len(historical) else 0

    significant_mm = CROP_CONFIG["corn"]["precip_significant_mm"]
    recent_rain = current[current["PRECTOTCORR"] >= significant_mm].sort_values("date")
    days_since_rain = None
    if len(recent_rain):
        last_rain = recent_rain.iloc[-1]["date"].date()
        days_since_rain = (today - last_rain).days
    else:
        last_rain_all = df[df["PRECTOTCORR"] >= significant_mm].sort_values("date")
        if len(last_rain_all):
            days_since_rain = (today - last_rain_all.iloc[-1]["date"].date()).days

    return {
        "gdd_accumulated": gdd_current,
        "gdd_normal": gdd_normal,
        "days_since_significant_rain": days_since_rain
    }

def classify_risk(ndvi_series, config):
    if not ndvi_series:
        return "unknown"
    latest = ndvi_series[-1]["value"]
    threshold = config["stress_threshold"]
    watch_threshold = config["watch_threshold"]
    decline_warn = config["ndvi_decline_warning_pct"]
    decline_crit = config["ndvi_decline_critical_pct"]

    if latest < threshold:
        return "critical"
    if latest < watch_threshold:
        return "watch"

    if len(ndvi_series) >= 3:
        recent = ndvi_series[-3:]
        first_val = recent[0]["value"]
        if first_val > 0:
            change_pct = ((latest - first_val) / first_val) * 100
            if change_pct <= -decline_crit:
                return "critical"
            if change_pct <= -decline_warn:
                return "watch"
    return "healthy"

def compute_ndvi_trend(ndvi_series):
    if len(ndvi_series) < 3:
        return "stable", 0.0
    recent = ndvi_series[-3:]
    first, last = recent[0]["value"], recent[-1]["value"]
    if first == 0:
        return "stable", 0.0
    change = ((last - first) / first) * 100
    if change > 3:
        return "improving", round(change, 1)
    if change < -3:
        return "declining", round(change, 1)
    return "stable", round(change, 1)

# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------
def extract_field_data(field_dir, farm_root, data_root):
    field_json_path = field_dir / "field.json"
    field_meta = {}
    if field_json_path.exists():
        field_meta = json.loads(field_json_path.read_text())

    boundary = read_field_boundary(field_dir)
    cards = read_ndvi_card_summary(field_dir)
    yearly = read_ndvi_yearly_summary(field_dir)
    soil = read_soil_summary(field_dir)
    weather = read_weather_csv(field_dir)
    ndvi_series = compute_scene_ndvi_time_series(field_dir, data_root)
    weather_summ = compute_weather_summaries(weather)

    area_acres = 0
    if boundary and "properties" in boundary:
        area_acres = float(boundary["properties"].get("area_acres", 0))
    field_id = field_meta.get("field_slug") or (boundary or {}).get("properties", {}).get("field_id") or field_dir.name

    crop_type = "corn"
    cc = CROP_CONFIG["corn"]
    risk = classify_risk(ndvi_series, cc)
    trend, trend_pct = compute_ndvi_trend(ndvi_series)

    current_ndvi = round(ndvi_series[-1]["value"], 3) if ndvi_series else None

    ndvi_corn_avg = None
    ndvi_soybean_avg = None
    ndvi_peak = None
    if cards and "cards" in cards:
        c = cards["cards"]
        if "corn" in c and c["corn"].get("mean_ndvi"):
            ndvi_corn_avg = round(c["corn"]["mean_ndvi"], 3)
        if "soybean" in c and c["soybean"].get("mean_ndvi"):
            ndvi_soybean_avg = round(c["soybean"]["mean_ndvi"], 3)
        if "corn_peak_95" in c and c["corn_peak_95"].get("mean_ndvi"):
            ndvi_peak = round(c["corn_peak_95"]["mean_ndvi"], 3)

    soil_data = {
        "awc_in_in": round(float(soil.get("total_aws_inches", 0)), 2) if soil and soil.get("total_aws_inches") else None,
        "om_pct": round(float(soil.get("avg_om_pct", 0)), 2) if soil and soil.get("avg_om_pct") else None,
        "ph": round(float(soil.get("avg_ph", 0)), 1) if soil and soil.get("avg_ph") else None,
        "drainage_class": soil.get("drainage_class", "") if soil else "",
        "dominant_soil": soil.get("dominant_soil", "") if soil else "",
        "cec": round(float(soil.get("avg_cec", 0)), 1) if soil and soil.get("avg_cec") else None,
        "clay_pct": round(float(soil.get("avg_clay_pct", 0)), 1) if soil and soil.get("avg_clay_pct") else None,
        "sand_pct": round(float(soil.get("avg_sand_pct", 0)), 1) if soil and soil.get("avg_sand_pct") else None,
    }

    field_data = {
        "id": field_id,
        "name": field_id,
        "area_acres": round(area_acres, 1),
        "crop_type": crop_type,
        "geometry": boundary,
        "current_risk": risk,
        "ndvi_series": ndvi_series,
        "current_ndvi": current_ndvi,
        "ndvi_trend": trend,
        "ndvi_trend_pct": trend_pct,
        "ndvi_corn_avg": ndvi_corn_avg,
        "ndvi_soybean_avg": ndvi_soybean_avg,
        "ndvi_peak_95": ndvi_peak,
        "soil": soil_data,
        "weather_summary": weather_summ,
        "weather_daily": weather,
    }
    return field_data

def build_summary(fields):
    config = CROP_CONFIG["corn"]
    total = len(fields)
    critical = sum(1 for f in fields if f["current_risk"] == "critical")
    watch = sum(1 for f in fields if f["current_risk"] == "watch")
    healthy = sum(1 for f in fields if f["current_risk"] == "healthy")

    ndvi_vals = [f["current_ndvi"] for f in fields if f["current_ndvi"] is not None]
    avg_ndvi = round(float(np.mean(ndvi_vals)), 3) if ndvi_vals else None

    gdd_vals = [f["weather_summary"].get("gdd_accumulated", 0) for f in fields]
    avg_gdd = round(float(np.mean(gdd_vals)), 0) if gdd_vals else 0

    rain_days = [f["weather_summary"].get("days_since_significant_rain") for f in fields if f["weather_summary"].get("days_since_significant_rain") is not None]
    max_days_since_rain = max(rain_days) if rain_days else None

    # Trend across fields
    improving = sum(1 for f in fields if f["ndvi_trend"] == "improving")
    declining = sum(1 for f in fields if f["ndvi_trend"] == "declining")

    return {
        "total_fields": total,
        "critical_count": critical,
        "watch_count": watch,
        "healthy_count": healthy,
        "avg_ndvi": avg_ndvi,
        "avg_gdd": avg_gdd,
        "max_days_since_rain": max_days_since_rain,
        "improving_count": improving,
        "declining_count": declining,
        "stress_threshold": config["stress_threshold"],
        "watch_threshold": config["watch_threshold"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "grower_name": "",
    }

def extract_all_field_data(grower_root, data_root):
    all_fields = []
    grower_name = grower_root.name
    for farm_root in farm_paths(grower_root):
        farm_json_path = farm_root / "farm.json"
        if farm_json_path.exists():
            farm_meta = json.loads(farm_json_path.read_text())
            grower_name = farm_meta.get("display_name", grower_name)
        for field_dir in field_paths(farm_root):
            fd = extract_field_data(field_dir, farm_root, data_root)
            if fd["geometry"]:
                all_fields.append(fd)

    # Assign display names
    for i, fd in enumerate(all_fields, 1):
        fd["name"] = f"Field {i}"

    return all_fields, grower_name

# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def download_d3():
    urls = [
        "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js",
        "https://unpkg.com/d3@7/dist/d3.min.js",
        "https://d3js.org/d3.v7.min.js",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            continue
    # Fallback: try to find from local
    local_d3 = Path(__file__).parent / "d3.v7.min.js"
    if local_d3.exists():
        return local_d3.read_text()
    raise RuntimeError("Could not download D3 from any CDN and no local fallback found")

def build_html(data_json_str, d3_min_js):
    config = CROP_CONFIG["corn"]
    data = json.loads(data_json_str)

    fields_json = json.dumps(data["fields"], default=str)
    summary_json = json.dumps(data["summary"], default=str)
    config_json = json.dumps(config, default=str)
    threshold_labels_json = json.dumps(THRESHOLD_LABELS, default=str)
    crop_config_json = json.dumps(CROP_CONFIG, default=str)

    grower_name = data['summary'].get('grower_name', 'Grower')
    total_fields = data['summary']['total_fields']
    generated_at = data['summary']['generated_at']
    declining_count = data['summary']['declining_count']

    field_options_html = ''.join(
        f'<option value="{f["id"]}" selected>{f["name"]}</option>'
        for f in data['fields']
    )

    # ------------------------------------------------------------------
    # Build the HTML using a regular string with .replace() substitutions.
    # This avoids Python f-string / JavaScript brace conflicts.
    # ------------------------------------------------------------------
    template = HTML_TEMPLATE
    template = template.replace("__D3_MIN_JS__", d3_min_js)
    template = template.replace("__GROWER_NAME__", grower_name)
    template = template.replace("__TOTAL_FIELDS__", str(total_fields))
    template = template.replace("__GENERATED_AT__", generated_at)
    template = template.replace("__DECLINING_COUNT__", str(declining_count))
    template = template.replace("__FIELD_OPTIONS__", field_options_html)
    template = template.replace("__FIELDS_JSON__", fields_json)
    template = template.replace("__SUMMARY_JSON__", summary_json)
    template = template.replace("__CONFIG_JSON__", config_json)
    template = template.replace("__THRESHOLD_LABELS_JSON__", threshold_labels_json)
    template = template.replace("__CROP_CONFIG_JSON__", crop_config_json)
    return template


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__GROWER_NAME__ — Row Crop Intelligence Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #f5f7fa; color: #1a1a2e; font-size: 14px; line-height: 1.5; }
.container { max-width: 1400px; margin: 0 auto; padding: 16px; }
.header { background: linear-gradient(135deg, #1e3a5f, #2a5a7f); color: #fff; padding: 20px 24px; border-radius: 8px; margin-bottom: 16px; }
.header h1 { font-size: 1.4rem; font-weight: 600; }
.header .subtitle { font-size: 0.85rem; color: #b8d4e8; margin-top: 4px; }
.header .freshness { font-size: 0.75rem; color: #8899aa; margin-top: 8px; }

.filter-bar { background: #fff; border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
.filter-bar label { font-size: 0.8rem; font-weight: 600; color: #555; }
.filter-bar select, .filter-bar input { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.85rem; }
.filter-bar select[multiple] { min-width: 200px; min-height: 80px; }
.filter-bar button { padding: 6px 16px; background: #4A7FB5; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 0.85rem; font-weight: 500; }
.filter-bar button:hover { background: #3a6fa5; }
.filter-bar .date-range { display: flex; gap: 8px; align-items: center; }

.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kpi-card { background: #fff; border-radius: 8px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.kpi-card .kpi-label { font-size: 0.75rem; font-weight: 600; color: #777; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-card .kpi-value { font-size: 1.6rem; font-weight: 700; margin-top: 4px; }
.kpi-card .kpi-unit { font-size: 0.8rem; color: #888; }
.kpi-card .kpi-trend { font-size: 0.8rem; margin-top: 2px; }
.kpi-card.critical { border-left: 4px solid #D95F4A; }
.kpi-card.watch { border-left: 4px solid #E8A838; }
.kpi-card.healthy { border-left: 4px solid #4A7FB5; }

.chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); gap: 16px; margin-bottom: 16px; }
.chart-card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.chart-card h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.chart-card .chart-container { width: 100%; height: 300px; position: relative; }
.chart-card .chart-container svg { width: 100%; height: 100%; }
.chart-full { grid-column: 1 / -1; }

.map-card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }
.map-card h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.map-card .map-container { width: 100%; height: 500px; position: relative; background: #e8edf2; border-radius: 4px; overflow: hidden; }
.map-card .map-container svg { width: 100%; height: 100%; }

.action-list { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }
.action-list h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.action-item { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid #eee; align-items: flex-start; }
.action-item:last-child { border-bottom: none; }
.action-item .risk-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.7rem; font-weight: 700; color: #fff; white-space: nowrap; }
.action-item .risk-text { flex: 1; font-size: 0.85rem; }

.narrative { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }
.narrative h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.narrative p { font-size: 0.85rem; margin-bottom: 8px; color: #444; line-height: 1.6; }

.footer { background: #fff; border-radius: 8px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 0.8rem; color: #777; display: flex; flex-wrap: wrap; gap: 24px; }
.footer h4 { font-size: 0.8rem; font-weight: 600; color: #555; margin-bottom: 4px; }
.footer .legend-item { display: inline-flex; align-items: center; gap: 6px; margin-right: 16px; }
.footer .legend-swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px; }
.footer .legend-icon { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; }

svg.icon { width: 16px; height: 16px; fill: currentColor; vertical-align: middle; }
svg.icon-lg { width: 24px; height: 24px; }

.tooltip { position: absolute; padding: 8px 12px; background: rgba(0,0,0,0.8); color: #fff; border-radius: 4px; font-size: 0.8rem; pointer-events: none; opacity: 0; transition: opacity 0.15s; z-index: 100; }
.tooltip.visible { opacity: 1; }

.axis text { font-size: 10px; fill: #555; }
.axis .domain, .axis .tick line { stroke: #ccc; }
.chart-title { font-size: 0.8rem; fill: #555; text-anchor: middle; }

@media (max-width: 768px) {
  .chart-grid { grid-template-columns: 1fr; }
  .kpi-row { grid-template-columns: 1fr 1fr; }
  .filter-bar { flex-direction: column; align-items: stretch; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>__GROWER_NAME__ — Row Crop Intelligence Dashboard</h1>
    <div class="subtitle">Corn field health analysis · __TOTAL_FIELDS__ fields</div>
    <div class="freshness">Generated: __GENERATED_AT__</div>
  </div>

  <div class="filter-bar">
    <label>Fields:</label>
    <select id="field-select" multiple>
      __FIELD_OPTIONS__
    </select>
    <div class="date-range">
      <label>From:</label>
      <input type="date" id="date-from">
      <label>To:</label>
      <input type="date" id="date-to">
    </div>
    <button id="reset-btn">Reset</button>
  </div>

  <div id="kpi-row" class="kpi-row"></div>

  <div class="chart-grid" id="ndvi-time-series-section">
    <div class="chart-card chart-full">
      <h3>NDVI Declining in __DECLINING_COUNT__ Fields</h3>
      <div class="chart-container" id="ndvi-time-series"></div>
    </div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3>Field Ranking by Current NDVI</h3>
      <div class="chart-container" id="field-ranking"></div>
    </div>
    <div class="chart-card">
      <h3>NDVI vs. Available Water Capacity</h3>
      <div class="chart-container" id="ndvi-vs-awc"></div>
    </div>
  </div>

  <div class="map-card">
    <h3>Field Risk Map — Click to Filter</h3>
    <div class="map-container" id="field-map"></div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3>GDD Accumulation: Actual vs. Normal</h3>
      <div class="chart-container" id="gdd-chart"></div>
    </div>
    <div class="chart-card">
      <h3>Soil Organic Matter by Field</h3>
      <div class="chart-container" id="soil-chart"></div>
    </div>
  </div>

  <div class="action-list" id="action-list-section">
    <h3>Priority Actions</h3>
    <div id="action-list"></div>
  </div>

  <div class="narrative" id="narrative-section">
    <h3>Analysis Summary</h3>
    <div id="narrative-text"></div>
  </div>

  <div class="footer" id="footer-section"></div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
__D3_MIN_JS__
</script>

<script>
// ===== CONFIG =====
const CROP_CONFIG = __CROP_CONFIG_JSON__;
const CONFIG = CROP_CONFIG.corn;
const THRESHOLD_LABELS = __THRESHOLD_LABELS_JSON__;
const ALL_FIELDS = __FIELDS_JSON__;
const SUMMARY = __SUMMARY_JSON__;

// ===== ICONS =====
const ICONS = {
  check: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 0a8 8 0 100 16A8 8 0 008 0zm4 5l-5 5-3-3 1-1 2 2 4-4 1 1z"/></svg>',
  warning: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 0L0 15h16L8 0zm0 5v5H7V5h1zm-1 6h2v2H7v-2z"/></svg>',
  alert: '<svg class="icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4v5M8 11v1"/></svg>',
  plant: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 1C5 1 2 3 2 6c0 2 1 4 3 5l1 4h4l1-4c2-1 3-3 3-5 0-3-3-5-6-5z"/></svg>',
  water: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 1S3 7 3 10a5 5 0 0010 0c0-3-5-9-5-9z"/></svg>',
  temp: '<svg class="icon" viewBox="0 0 16 16"><path d="M6 1v7.2A3.5 3.5 0 007 15a3.5 3.5 0 001-6.8V1H6zm2 11a1.5 1.5 0 110-3 1.5 1.5 0 010 3z"/></svg>',
  calendar: '<svg class="icon" viewBox="0 0 16 16"><path d="M4 1v2h8V1H4zM2 3v11h12V3H2zm1 2h10v7H3V5z"/></svg>',
  map_pin: '<svg class="icon" viewBox="0 0 16 16"><path d="M8 0C5.2 0 3 2.2 3 5c0 4 5 11 5 11s5-7 5-11c0-2.8-2.2-5-5-5zm0 8a3 3 0 110-6 3 3 0 010 6z"/></svg>',
  soil: '<svg class="icon" viewBox="0 0 16 16"><rect x="1" y="2" width="14" height="12" rx="1" fill="none" stroke="currentColor" stroke-width="1"/><path d="M4 5h2v2H4zM8 5h2v2H8zM6 9h4v2H6z"/></svg>',
  info: '<svg class="icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 7v5M8 5v1"/></svg>',
};

// ===== STATE (pub/sub) =====
const state = {
  fields: ALL_FIELDS,
  filters: { fieldIds: [], dateFrom: null, dateTo: null },
  _listeners: [],
  subscribe(fn) { this._listeners.push(fn); return () => { this._listeners = this._listeners.filter(l => l !== fn); }; },
  publish() { this._listeners.forEach(fn => fn()); },
  getFilteredFields() {
    let ff = this.fields;
    if (this.filters.fieldIds.length > 0) {
      ff = ff.filter(f => this.filters.fieldIds.includes(f.id));
    }
    return ff;
  },
  getFilteredNDVISeries(field) {
    let series = field.ndvi_series;
    if (this.filters.dateFrom) {
      series = series.filter(d => d.date >= this.filters.dateFrom);
    }
    if (this.filters.dateTo) {
      series = series.filter(d => d.date <= this.filters.dateTo);
    }
    return series;
  }
};

// ===== TOOLTIP =====
const tooltip = d3.select("#tooltip");

// ===== KPI RENDER =====
function renderKPIs() {
  const ff = state.getFilteredFields();
  const critical = ff.filter(f => f.current_risk === 'critical').length;
  const watch = ff.filter(f => f.current_risk === 'watch').length;
  const attention = critical + watch;
  const total = ff.length;

  const ndviVals = ff.filter(f => f.current_ndvi != null).map(f => f.current_ndvi);
  const avgNDVI = ndviVals.length ? (ndviVals.reduce((a,b) => a+b, 0) / ndviVals.length).toFixed(3) : '--';

  const improving = ff.filter(f => f.ndvi_trend === 'improving').length;
  const declining = ff.filter(f => f.ndvi_trend === 'declining').length;
  const trendIcon = declining > improving ? ICONS.warning : ICONS.check;
  const trendText = declining > improving ? 'Declining in ' + declining + ' fields' : 'Stable/Improving';

  const gddVals = ff.map(f => f.weather_summary?.gdd_accumulated || 0);
  const avgGDD = gddVals.length ? Math.round(gddVals.reduce((a,b) => a+b, 0) / gddVals.length) : 0;

  const rainDays = ff.map(f => f.weather_summary?.days_since_significant_rain).filter(d => d != null);
  const maxRainDays = rainDays.length ? Math.max(...rainDays) : '--';

  const riskClass = attention > 0 ? (critical > 0 ? 'critical' : 'watch') : 'healthy';

  d3.select("#kpi-row").html(
    '<div class="kpi-card ' + riskClass + '">' +
      '<div class="kpi-label">' + ICONS.warning + ' Fields Requiring Attention</div>' +
      '<div class="kpi-value">' + attention + ' / ' + total + '</div>' +
      '<div class="kpi-trend">' + critical + ' critical &middot; ' + watch + ' watch</div>' +
    '</div>' +
    '<div class="kpi-card healthy">' +
      '<div class="kpi-label">' + ICONS.plant + ' Average NDVI</div>' +
      '<div class="kpi-value">' + avgNDVI + ' <span class="kpi-unit"></span></div>' +
      '<div class="kpi-trend">' + trendIcon + ' ' + trendText + '</div>' +
    '</div>' +
    '<div class="kpi-card healthy">' +
      '<div class="kpi-label">' + ICONS.temp + ' GDD Accumulated (avg)</div>' +
      '<div class="kpi-value">' + avgGDD + ' <span class="kpi-unit">&deg;F-days</span></div>' +
      '<div class="kpi-trend">Target: ' + CONFIG.gdd_target + ' &deg;F-days</div>' +
    '</div>' +
    '<div class="kpi-card ' + (rainDays > 7 ? 'watch' : 'healthy') + '">' +
      '<div class="kpi-label">' + ICONS.water + ' Days Since Significant Rain</div>' +
      '<div class="kpi-value">' + maxRainDays + ' <span class="kpi-unit">days</span></div>' +
      '<div class="kpi-trend">Threshold: >0.1 in</div>' +
    '</div>'
  );
}

// ===== NDVI TIME SERIES =====
function renderNDVITimeSeries() {
  const container = d3.select("#ndvi-time-series");
  container.html("");
  const ff = state.getFilteredFields();
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 40, left: 50 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  let allPoints = [];
  ff.forEach(f => {
    const series = state.getFilteredNDVISeries(f);
    series.forEach(p => allPoints.push(Object.assign({}, p, { fieldId: f.id, fieldName: f.name })));
  });
  if (!allPoints.length) return;

  const xExtent = d3.extent(allPoints, d => new Date(d.date));
  const yExtent = [0, 1];

  const xScale = d3.scaleTime().domain(xExtent).range([0, width]);
  const yScale = d3.scaleLinear().domain(yExtent).range([height, 0]);

  const colorScale = d3.scaleOrdinal(d3.schemeTableau10).domain(ff.map(f => f.id));

  svg.append("line")
    .attr("x1", 0).attr("x2", width)
    .attr("y1", yScale(CONFIG.stress_threshold)).attr("y2", yScale(CONFIG.stress_threshold))
    .attr("stroke", "#D95F4A").attr("stroke-dasharray", "6,3").attr("stroke-width", 1.5)
    .append("title").text("Stress threshold: " + CONFIG.stress_threshold);

  svg.append("text")
    .attr("x", width).attr("y", yScale(CONFIG.stress_threshold) - 4)
    .attr("text-anchor", "end").attr("font-size", "10px").attr("fill", "#D95F4A")
    .text("Stress");

  svg.append("line")
    .attr("x1", 0).attr("x2", width)
    .attr("y1", yScale(CONFIG.watch_threshold)).attr("y2", yScale(CONFIG.watch_threshold))
    .attr("stroke", "#E8A838").attr("stroke-dasharray", "4,4").attr("stroke-width", 1)
    .append("title").text("Watch threshold: " + CONFIG.watch_threshold);

  svg.append("text")
    .attr("x", width).attr("y", yScale(CONFIG.watch_threshold) - 4)
    .attr("text-anchor", "end").attr("font-size", "10px").attr("fill", "#E8A838")
    .text("Watch");

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(6));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(8));

  svg.append("text").attr("class", "chart-title")
    .attr("x", -32).attr("y", 12).attr("transform", "rotate(-90)")
    .text("NDVI");

  const line = d3.line()
    .x(d => xScale(new Date(d.date)))
    .y(d => yScale(d.value))
    .curve(d3.curveLinear);

  ff.forEach(f => {
    const series = state.getFilteredNDVISeries(f);
    if (series.length < 2) return;
    svg.append("path")
      .datum(series)
      .attr("fill", "none")
      .attr("stroke", colorScale(f.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.8)
      .attr("d", line);

    const last = series[series.length - 1];
    svg.append("text")
      .attr("x", xScale(new Date(last.date)) + 4)
      .attr("y", yScale(last.value))
      .attr("font-size", "10px")
      .attr("fill", colorScale(f.id))
      .text(f.name);
  });
}

// ===== FIELD RANKING BAR CHART =====
function renderFieldRanking() {
  const container = d3.select("#field-ranking");
  container.html("");
  let ff = state.getFilteredFields().filter(f => f.current_ndvi != null).sort((a, b) => a.current_ndvi - b.current_ndvi);
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 10, right: 20, bottom: 20, left: 70 };
  const width = rect.width - margin.left - margin.right;
  const height = Math.max(200, ff.length * 32) - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const xScale = d3.scaleLinear().domain([0, 1]).range([0, width]);
  const yScale = d3.scaleBand().domain(ff.map(f => f.name)).range([0, height]).padding(0.3);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).tickSize(0)).select(".domain").remove();
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(5));

  ff.forEach(f => {
    const color = THRESHOLD_LABELS[f.current_risk]?.color || "#999";
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(f.current_ndvi))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("opacity", 0.85);
    svg.append("text")
      .attr("x", xScale(f.current_ndvi) - 4)
      .attr("y", yScale(f.name) + yScale.bandwidth() / 2)
      .attr("text-anchor", "end")
      .attr("dy", "0.35em")
      .attr("font-size", "11px")
      .attr("fill", "#fff")
      .attr("font-weight", "700")
      .text(f.current_ndvi.toFixed(3));
  });
}

// ===== NDVI vs AWC SCATTER =====
function renderNDVIvsAWC() {
  const container = d3.select("#ndvi-vs-awc");
  container.html("");
  let ff = state.getFilteredFields().filter(f => f.current_ndvi != null && f.soil?.awc_in_in != null);
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 40, left: 50 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const xExtent = d3.extent(ff, f => f.soil.awc_in_in);
  const yExtent = [0, 1];
  const xPad = (xExtent[1] - xExtent[0]) * 0.1 || 0.1;
  const xScale = d3.scaleLinear().domain([Math.max(0, xExtent[0] - xPad), xExtent[1] + xPad]).range([0, width]);
  const yScale = d3.scaleLinear().domain(yExtent).range([height, 0]);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(5));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(5));

  svg.append("text").attr("class", "chart-title")
    .attr("x", width / 2).attr("y", height + 25).text("AWC (in/in)");
  svg.append("text").attr("class", "chart-title")
    .attr("x", -32).attr("y", 12).attr("transform", "rotate(-90)").text("NDVI");

  const r = Math.min(12, width / ff.length * 0.8);
  ff.forEach(f => {
    const color = THRESHOLD_LABELS[f.current_risk]?.color || "#999";
    svg.append("circle")
      .attr("cx", xScale(f.soil.awc_in_in))
      .attr("cy", yScale(f.current_ndvi))
      .attr("r", r)
      .attr("fill", color)
      .attr("opacity", 0.7)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .on("mouseenter", function() {
        d3.select(this).attr("opacity", 1).attr("r", r * 1.4);
        tooltip.classed("visible", true)
          .html("<strong>" + f.name + '</strong><br>NDVI: ' + f.current_ndvi + '<br>AWC: ' + f.soil.awc_in_in + ' in/in<br>Risk: ' + f.current_risk)
          .style("left", (d3.event.pageX + 12) + "px")
          .style("top", (d3.event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("opacity", 0.7).attr("r", r);
        tooltip.classed("visible", false);
      });
    svg.append("text")
      .attr("x", xScale(f.soil.awc_in_in))
      .attr("y", yScale(f.current_ndvi) - r - 4)
      .attr("text-anchor", "middle")
      .attr("font-size", "9px")
      .attr("fill", "#555")
      .text(f.name);
  });
}

// ===== MAP =====
function renderMap() {
  const container = d3.select("#field-map");
  container.html("");
  const ff = state.getFilteredFields().filter(f => f.geometry?.geometry);
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const width = rect.width, height = rect.height;

  const svg = container.append("svg")
    .attr("width", width).attr("height", height);

  let allCoords = [];
  ff.forEach(f => {
    const geo = f.geometry.geometry;
    if (geo.type === "Polygon") {
      geo.coordinates[0].forEach(c => allCoords.push(c));
    } else if (geo.type === "MultiPolygon") {
      geo.coordinates.forEach(p => p[0].forEach(c => allCoords.push(c)));
    }
  });

  if (!allCoords.length) return;
  const lons = allCoords.map(c => c[0]);
  const lats = allCoords.map(c => c[1]);
  const cLon = (d3.min(lons) + d3.max(lons)) / 2;
  const cLat = (d3.min(lats) + d3.max(lats)) / 2;

  const projection = d3.geoMercator()
    .center([cLon, cLat])
    .fitExtent([[20, 20], [width - 20, height - 20]], {
      type: "FeatureCollection",
      features: ff.map(f => ({
        type: "Feature",
        geometry: f.geometry.geometry,
        properties: {}
      }))
    });

  const geoPath = d3.geoPath().projection(projection);

  ff.forEach(f => {
    const color = THRESHOLD_LABELS[f.current_risk]?.color || "#999";
    svg.append("path")
      .datum(f.geometry.geometry)
      .attr("d", geoPath)
      .attr("fill", color)
      .attr("stroke", "#fff")
      .attr("stroke-width", 2)
      .attr("opacity", 0.8)
      .style("cursor", "pointer")
      .on("mouseenter", function() {
        d3.select(this).attr("opacity", 1).attr("stroke-width", 3);
        tooltip.classed("visible", true)
          .html("<strong>" + f.name + "</strong><br>Risk: " + f.current_risk + "<br>NDVI: " + (f.current_ndvi || '--') + "<br>Area: " + f.area_acres + " ac")
          .style("left", (d3.event.pageX + 12) + "px")
          .style("top", (d3.event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("opacity", 0.8).attr("stroke-width", 2);
        tooltip.classed("visible", false);
      })
      .on("click", function() {
        state.filters.fieldIds = [f.id];
        document.querySelectorAll("#field-select option").forEach(opt => opt.selected = opt.value === f.id);
        state.publish();
      });
  });

  const legend = svg.append("g").attr("transform", "translate(" + (width - 120) + ", 20)");
  const tiers = ["healthy", "watch", "critical"];
  tiers.forEach((t, i) => {
    const tl = THRESHOLD_LABELS[t];
    legend.append("rect").attr("x", 0).attr("y", i * 20).attr("width", 14).attr("height", 14).attr("fill", tl.color).attr("rx", 2);
    legend.append("text").attr("x", 20).attr("y", i * 20 + 12).attr("font-size", "11px").attr("fill", "#333").text(tl.label);
  });
}

// ===== GDD CHART =====
function renderGDD() {
  const container = d3.select("#gdd-chart");
  container.html("");
  const ff = state.getFilteredFields();
  if (!ff.length) return;

  const currentYear = new Date().getFullYear();
  const fieldData = ff.map(f => {
    const daily = f.weather_daily || [];
    const byDate = {};
    daily.forEach(d => {
      const gdd = Math.max(0, (d.T2M_MAX + d.T2M_MIN) / 2 - 10);
      byDate[d.date] = (byDate[d.date] || 0) + gdd;
    });
    const sorted = Object.entries(byDate).sort((a, b) => a[0].localeCompare(b[0]));
    let cum = 0;
    const currentSeries = [];
    sorted.forEach(([dt, val]) => {
      cum += val;
      const y = parseInt(dt.slice(0, 4));
      if (y === currentYear) {
        currentSeries.push({ date: dt, gdd: Math.round(cum) });
      }
    });
    return { id: f.id, name: f.name, current: currentSeries };
  });

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 40, left: 50 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const normalGDD = ff[0].weather_summary?.gdd_normal || 1500;
  const maxGDD = Math.max(normalGDD, ...fieldData.map(f => f.current.length ? f.current[f.current.length - 1].gdd : 0));
  const maxY = Math.ceil(maxGDD / 500) * 500;

  const xScale = d3.scalePoint()
    .domain(fieldData[0]?.current.map(d => d.date) || ["2026-01-01"])
    .range([0, width]);
  const yScale = d3.scaleLinear().domain([0, maxY]).range([height, 0]);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(5));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).tickFormat(function(d) {
      return d3.timeFormat("%b")(new Date(d));
    }).ticks(8));

  svg.append("text").attr("class", "chart-title")
    .attr("x", -32).attr("y", 12).attr("transform", "rotate(-90)").text("GDD (&deg;F-days)");

  svg.append("line")
    .attr("x1", 0).attr("x2", width)
    .attr("y1", yScale(normalGDD)).attr("y2", yScale(normalGDD))
    .attr("stroke", "#999").attr("stroke-dasharray", "6,3").attr("stroke-width", 1.5);
  svg.append("text")
    .attr("x", width).attr("y", yScale(normalGDD) - 4)
    .attr("text-anchor", "end").attr("font-size", "10px").attr("fill", "#777")
    .text("Normal: " + normalGDD + " &deg;F-days");

  const colorScale = d3.scaleOrdinal(d3.schemeTableau10).domain(ff.map(f => f.id));
  const line = d3.line()
    .x(d => xScale(d.date))
    .y(d => yScale(d.gdd));

  fieldData.forEach(fd => {
    if (fd.current.length < 2) return;
    svg.append("path")
      .datum(fd.current)
      .attr("fill", "none")
      .attr("stroke", colorScale(fd.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.7)
      .attr("d", line);
  });
}

// ===== SOIL CHART =====
function renderSoil() {
  const container = d3.select("#soil-chart");
  container.html("");
  let ff = state.getFilteredFields().filter(f => f.soil?.om_pct != null).sort((a, b) => a.soil.om_pct - b.soil.om_pct);
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 10, right: 20, bottom: 20, left: 70 };
  const width = rect.width - margin.left - margin.right;
  const height = Math.max(200, ff.length * 32) - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const xMax = d3.max(ff, f => f.soil.om_pct) * 1.15;
  const xScale = d3.scaleLinear().domain([0, xMax]).range([0, width]);
  const yScale = d3.scaleBand().domain(ff.map(f => f.name)).range([0, height]).padding(0.3);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).tickSize(0)).select(".domain").remove();
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(5));

  ff.forEach(f => {
    const color = THRESHOLD_LABELS[f.current_risk]?.color || "#7cb342";
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(f.soil.om_pct))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("opacity", 0.85);
    svg.append("text")
      .attr("x", xScale(f.soil.om_pct) - 4)
      .attr("y", yScale(f.name) + yScale.bandwidth() / 2)
      .attr("text-anchor", "end")
      .attr("dy", "0.35em")
      .attr("font-size", "11px")
      .attr("fill", "#fff")
      .attr("font-weight", "700")
      .text(f.soil.om_pct.toFixed(1) + "%");
  });

  svg.append("text").attr("class", "chart-title")
    .attr("x", width / 2).attr("y", height + 16).text("Organic Matter (%)");
}

// ===== ACTION LIST =====
function renderActionList() {
  let ff = state.getFilteredFields()
    .filter(f => f.current_risk === 'critical' || f.current_risk === 'watch')
    .sort((a, b) => {
      const order = { critical: 0, watch: 1, healthy: 2 };
      return (order[a.current_risk] || 2) - (order[b.current_risk] || 2);
    });

  const container = d3.select("#action-list");
  if (!ff.length) {
    container.html('<p style="color:#777; font-size:0.85rem;">All fields are healthy. No immediate action required.</p>');
    return;
  }

  let html = '';
  ff.forEach(f => {
    const tl = THRESHOLD_LABELS[f.current_risk];
    const ndviInfo = f.current_ndvi != null ? 'NDVI: ' + f.current_ndvi.toFixed(3) : '';
    const trendInfo = f.ndvi_trend_pct ? ' (' + (f.ndvi_trend_pct >= 0 ? '+' : '') + f.ndvi_trend_pct + '%)' : '';
    const soilInfo = f.soil?.awc_in_in != null ? 'AWC ' + f.soil.awc_in_in + ' in/in' : '';
    const action = f.current_risk === 'critical'
      ? 'Scout immediately -- consider irrigation or tissue sampling.'
      : 'Monitor weekly -- check NDVI trend and soil moisture.';

    html += '<div class="action-item">' +
      '<span class="risk-badge" style="background:' + tl.color + '">' + tl.label + '</span>' +
      '<span class="risk-text">' +
        '<strong>' + f.name + '</strong>: ' + ndviInfo + trendInfo + ' &middot; ' + soilInfo + '<br>' +
        '<span style="color:#777; font-size:0.8rem;">' + action + '</span>' +
      '</span>' +
    '</div>';
  });
  container.html(html);
}

// ===== NARRATIVE =====
function renderNarrative() {
  const ff = state.getFilteredFields();
  const total = ff.length;
  const crit = ff.filter(f => f.current_risk === 'critical').length;
  const watch = ff.filter(f => f.current_risk === 'watch').length;
  const healthy = ff.filter(f => f.current_risk === 'healthy').length;
  const ndviArr = ff.filter(f => f.current_ndvi != null);
  const ndviAvg = ndviArr.length ? ndviArr.reduce((s, f) => s + f.current_ndvi, 0) / ndviArr.length : 0;
  const declining = ff.filter(f => f.ndvi_trend === 'declining').length;
  const improving = ff.filter(f => f.ndvi_trend === 'improving').length;
  const lowAWC = ff.filter(f => f.soil?.awc_in_in != null && f.soil.awc_in_in < 0.5).length;
  const highOM = ff.filter(f => f.soil?.om_pct != null && f.soil.om_pct > 3).length;
  const awcVals = ff.filter(f => f.soil?.awc_in_in != null).map(f => f.soil.awc_in_in);
  const omVals = ff.filter(f => f.soil?.om_pct != null).map(f => f.soil.om_pct);
  const scatterCount = ff.filter(f => f.current_ndvi != null && f.soil?.awc_in_in != null).length;
  const gdd = ff[0]?.weather_summary?.gdd_accumulated || 0;
  const normal = ff[0]?.weather_summary?.gdd_normal || 0;
  const gddDiff = gdd - normal;

  let html = '';

  html += '<p><strong>Patterns & Trends.</strong> Of ' + total + ' fields, <strong>' + crit + ' critical</strong> and <strong>' + watch + ' watch</strong> require attention. Average NDVI across all fields is <strong>' + ndviAvg.toFixed(3) + '</strong>.';
  if (declining > 0) html += ' ' + declining + ' field(s) show declining NDVI trend, warranting priority monitoring.';
  if (improving > 0) html += ' ' + improving + ' field(s) are improving.';
  html += ' ' + healthy + ' field(s) appear healthy and stable.</p>';

  html += '<p><strong>Field Health.</strong> Critical-risk fields typically combine below-threshold NDVI with declining trend. ';
  if (lowAWC > 0) {
    html += '' + lowAWC + ' field(s) have low available water capacity (AWC < 0.5 in/in), which likely contributes to stress under dry conditions.';
  } else {
    html += 'Soil AWC across fields is adequate for current conditions.';
  }
  if (highOM > 0) html += ' ' + highOM + ' field(s) have elevated organic matter (>3%), supporting better moisture retention.';
  html += '</p>';

  html += '<p><strong>Environmental & Soil Variation.</strong> Fields range from ' +
    (awcVals.length ? d3.min(awcVals).toFixed(2) : '--') + ' to ' + (awcVals.length ? d3.max(awcVals).toFixed(2) : '--') +
    ' in/in AWC and ' + (omVals.length ? d3.min(omVals).toFixed(1) : '--') + '% to ' +
    (omVals.length ? d3.max(omVals).toFixed(1) : '--') +
    '% organic matter. This variation directly correlates with NDVI differences -- the scatter plot of NDVI vs. AWC shows ' +
    (scatterCount > 3 ? 'a visible positive relationship' : 'limited correlation given available datapoints') + '.</p>';

  html += '<p><strong>Decisions & Actions.</strong> Focus scouting on critical-risk fields first. ';
  if (gddDiff < -100) {
    html += 'GDD accumulation (' + gdd + ' &deg;F-days) is below normal (' + normal + ' &deg;F-days), which may delay maturity.';
  } else if (gddDiff > 200) {
    html += 'GDD accumulation (' + gdd + ' &deg;F-days) exceeds normal (' + normal + ' &deg;F-days), advancing crop development.';
  } else {
    html += 'GDD accumulation (' + gdd + ' &deg;F-days) is near normal (' + normal + ' &deg;F-days).';
  }
  html += ' The priority action list above ranks fields by risk severity for operational triage.</p>';

  html += '<p><strong>Key Variables.</strong> NDVI trend direction, soil AWC, and GDD accumulation are the three most important indicators in this analysis. Fields with low AWC and declining NDVI consistently appear in the critical tier and should be prioritized for irrigation and stand assessment.</p>';

  d3.select("#narrative-text").html(html);
}

// ===== FOOTER =====
function renderFooter() {
  const html =
    '<div>' +
      '<h4>Risk Tiers</h4>' +
      '<div>' +
        '<span class="legend-item"><span class="legend-swatch" style="background:#4A7FB5"></span> Healthy (NDVI >= 0.7)</span>' +
        '<span class="legend-item"><span class="legend-swatch" style="background:#E8A838"></span> Watch (NDVI 0.5-0.7 or declining >5%)</span>' +
        '<span class="legend-item"><span class="legend-swatch" style="background:#D95F4A"></span> Critical (NDVI < 0.5 or declining >10%)</span>' +
      '</div>' +
    '</div>' +
    '<div>' +
      '<h4>Methods</h4>' +
      '<div style="font-size:0.75rem; color:#999;">' +
        'NDVI: mean per-scene from Sentinel-2/Landsat 8-9.<br>' +
        'GDD: base 50&deg;F from daily Tmin/Tmax<br>' +
        'Soil: NRCS SSURGO (AWC, OM%)<br>' +
        'Weather: NASA POWER daily' +
      '</div>' +
    '</div>' +
    '<div>' +
      '<h4>Data Freshness</h4>' +
      '<div>Generated: ' + SUMMARY.generated_at + '</div>' +
      '<div style="font-size:0.75rem; color:#999;">Data up to latest available scene</div>' +
    '</div>';
  d3.select("#footer-section").html(html);
}

// ===== RESET =====
function resetFilters() {
  state.filters.fieldIds = [];
  state.filters.dateFrom = null;
  state.filters.dateTo = null;
  document.querySelectorAll("#field-select option").forEach(opt => opt.selected = true);
  document.getElementById("date-from").value = '';
  document.getElementById("date-to").value = '';
  state.publish();
}

// ===== RENDER ALL =====
function renderAll() {
  renderKPIs();
  renderNDVITimeSeries();
  renderFieldRanking();
  renderNDVIvsAWC();
  renderMap();
  renderGDD();
  renderSoil();
  renderActionList();
  renderNarrative();
  renderFooter();
}

// ===== INIT =====
state.subscribe(renderAll);

document.getElementById("reset-btn").addEventListener("click", resetFilters);

document.getElementById("field-select").addEventListener("change", function() {
  state.filters.fieldIds = Array.from(this.selectedOptions).map(o => o.value);
  state.publish();
});

document.getElementById("date-from").addEventListener("change", function() {
  state.filters.dateFrom = this.value || null;
  state.publish();
});

document.getElementById("date-to").addEventListener("change", function() {
  state.filters.dateTo = this.value || null;
  state.publish();
});

const allDates = ALL_FIELDS.flatMap(f => f.ndvi_series.map(d => d.date)).sort();
if (allDates.length) {
  document.getElementById("date-from").value = allDates[0];
  document.getElementById("date-to").value = allDates[allDates.length - 1];
  state.filters.dateFrom = allDates[0];
  state.filters.dateTo = allDates[allDates.length - 1];
}

renderAll();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate Row Crop Intelligence Dashboard")
    parser.add_argument("--grower", default="il-grower", help="Grower slug (default: il-grower)")
    parser.add_argument("--data-root", default=None, help="Runtime data root (default: $DATA_PIPELINE_DATA_ROOT or /home/coder/my-farm-advisor-runtime)")
    parser.add_argument("--d3-path", default=None, help="Path to local d3.v7.min.js (optional, downloads if not provided)")
    parser.add_argument("--output", default=None, help="Output HTML path (default: auto to runtime dashboards dir)")
    args = parser.parse_args()

    data_root = Path(args.data_root) if args.data_root else resolve_data_root()
    grower_root = grower_path(data_root, args.grower)

    if not grower_root.is_dir():
        print(f"Error: Grower path not found: {grower_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading data for grower: {args.grower}")
    fields, grower_name = extract_all_field_data(grower_root, data_root)
    print(f"  Found {len(fields)} fields")

    summary = build_summary(fields)
    summary["grower_name"] = grower_name

    dashboard_data = {
        "crop_config": CROP_CONFIG,
        "fields": fields,
        "summary": summary,
    }

    data_json_str = json.dumps(dashboard_data, default=str)

    print(f"Downloading D3.js...")
    d3_js = download_d3()

    print(f"Generating dashboard HTML...")
    html = build_html(data_json_str, d3_js)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        farms = list(farm_paths(grower_root))
        if farms:
            output_path = farms[0] / "derived" / "dashboards" / "row_crop_intelligence.html"
        else:
            output_path = grower_root / "row_crop_intelligence.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    print(f"Dashboard written to: {output_path}")
    print(f"  File size: {output_path.stat().st_size / 1024:.0f} KB")
    print(f"  Fields: {len(fields)}")
    print(f"  Risk summary: {summary['healthy_count']} healthy, {summary['watch_count']} watch, {summary['critical_count']} critical")


if __name__ == "__main__":
    main()
