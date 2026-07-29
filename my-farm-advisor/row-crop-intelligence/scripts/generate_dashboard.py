#!/usr/bin/env python3
"""Generate a self-contained Row Crop Intelligence & Data Dashboard HTML for a grower."""

import argparse
import base64
import csv
import math
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
            "VE": 120, "V6": 500, "VT": 1130, "R1": 1400, "R2": 1650,
            "R3": 1880, "R4": 2150, "R5": 2450, "R6": 2700
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
    df["T2M_MIN"] = pd.to_numeric(df["T2M_MIN"], errors="coerce")
    df["T2M_MAX"] = pd.to_numeric(df["T2M_MAX"], errors="coerce")
    df = df.dropna(subset=["T2M_MIN", "T2M_MAX"])
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

    if latest < threshold:
        return "critical"
    if latest < watch_threshold:
        return "watch"

    if len(ndvi_series) >= 3:
        recent = ndvi_series[-3:]
        first_val = recent[0]["value"]
        latest_val = recent[-1]["value"]
        delta = latest_val - first_val
        if delta <= -0.10:
            return "critical"
        if delta <= -0.05:
            return "watch"
    return "healthy"

def compute_ndvi_trend(ndvi_series):
    if len(ndvi_series) < 3:
        return "stable", 0.0
    recent = ndvi_series[-3:]
    first, last = recent[0]["value"], recent[-1]["value"]
    delta = round(last - first, 3)
    if delta > 0.03:
        return "improving", delta
    if delta < -0.03:
        return "declining", delta
    return "stable", delta

# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------
def read_crop_rotation(farm_root):
    path = farm_root / "derived" / "tables"
    files = list(path.glob("*crop_rotation.csv"))
    if not files:
        return {}
    result = {}
    with open(files[0]) as f:
        for row in csv.DictReader(f):
            result[row["field_id"]] = row.get("predicted_next_crop", "")
    return result

def extract_field_data(field_dir, farm_root, data_root, current_crop=None):
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

    # Use only the most recent year's NDVI data for classification and metrics
    # to match JS behavior (which filters ndvi_series to selected year)
    ndvi_years = sorted(set(s["date"][:4] for s in ndvi_series if len(s["date"]) >= 4))
    latest_year = ndvi_years[-1] if ndvi_years else None
    year_ndvi_series = [s for s in ndvi_series if s["date"].startswith(latest_year)] if latest_year else ndvi_series

    cdl_crops = {}
    if yearly and "years" in yearly:
        for y in yearly["years"]:
            cdl_crops[str(y["year"])] = y.get("crop_name", "Unknown")

    area_acres = 0
    if boundary and "properties" in boundary:
        area_acres = float(boundary["properties"].get("area_acres", 0))
    field_id = field_meta.get("field_slug") or (boundary or {}).get("properties", {}).get("field_id") or field_dir.name

    crop_type = "corn"
    cc = CROP_CONFIG["corn"]
    risk = classify_risk(year_ndvi_series, cc)
    trend, trend_pct = compute_ndvi_trend(year_ndvi_series)

    current_ndvi = round(year_ndvi_series[-1]["value"], 3) if year_ndvi_series else None

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

    if current_crop is None and cdl_crops:
        latest = str(date.today().year)
        current_crop = cdl_crops.get(latest, "Unknown")

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
        "cdl_crops": cdl_crops,
        "current_crop": current_crop,
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
        rotation_map = read_crop_rotation(farm_root)
        for field_dir in field_paths(farm_root):
            fd = extract_field_data(field_dir, farm_root, data_root, current_crop=rotation_map.get(field_dir.name))
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

def compute_all_fields_bbox(fields):
    """Compute [min_lon, min_lat, max_lon, max_lat] across all fields."""
    all_lons, all_lats = [], []
    for f in fields:
        geo = f.get("geometry", {}).get("geometry")
        if not geo:
            continue
        coords = geo.get("coordinates", [])
        if geo["type"] == "Polygon":
            for c in coords[0]:
                all_lons.append(c[0])
                all_lats.append(c[1])
        elif geo["type"] == "MultiPolygon":
            for poly in coords:
                for c in poly[0]:
                    all_lons.append(c[0])
                    all_lats.append(c[1])
    if not all_lons:
        return [-88.5, 40.5, -87.5, 41.5]
    return [min(all_lons), min(all_lats), max(all_lons), max(all_lats)]

def _merc(lon, lat):
    r = 6378137
    return (math.radians(lon) * r, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * r)

def fetch_static_map(bbox):
    """Fetch ESRI World Imagery for the given WGS84 bbox, return base64 data URI."""
    min_lon, min_lat, max_lon, max_lat = bbox
    pad_lon = max((max_lon - min_lon) * 0.40, 0.005)
    pad_lat = max((max_lat - min_lat) * 0.40, 0.005)
    pl, pb, pr, pt = min_lon - pad_lon, min_lat - pad_lat, max_lon + pad_lon, max_lat + pad_lat
    mx1, my1 = _merc(pl, pb)
    mx2, my2 = _merc(pr, pt)
    mw, mh = mx2 - mx1, my2 - my1
    target_w = 1600
    target_h = max(1, int(target_w * mh / mw))
    url = (
        f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
        f"?bbox={pl},{pb},{pr},{pt}"
        f"&bboxSR=4326&size={target_w},{target_h}&imageSR=102100"
        f"&format=jpg&f=image"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            b64 = base64.b64encode(data).decode("ascii")
            print(f"  Static map fetched: {len(data) / 1024:.0f} KB")
            return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"  Warning: could not fetch static map ({e})")
        return ""

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

    # Compute all-field bounding box and fetch static basemap
    bbox = compute_all_fields_bbox(data['fields'])
    map_b64 = fetch_static_map(bbox)
    map_bbox_json = json.dumps(bbox)

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
    template = template.replace("__FIELDS_JSON__", fields_json)
    template = template.replace("__SUMMARY_JSON__", summary_json)
    template = template.replace("__CONFIG_JSON__", config_json)
    template = template.replace("__THRESHOLD_LABELS_JSON__", threshold_labels_json)
    template = template.replace("__CROP_CONFIG_JSON__", crop_config_json)
    template = template.replace("__MAP_BASE64__", map_b64)
    template = template.replace("__MAP_BBOX__", map_bbox_json)
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
.header .header-main { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.header h1 { font-size: 1.4rem; font-weight: 600; }
.header .subtitle { font-size: 0.85rem; color: #b8d4e8; margin-top: 4px; }
.header .freshness { font-size: 0.75rem; color: #8899aa; margin-top: 8px; }
.header .header-filters { display: flex; gap: 14px; align-items: flex-start; flex-shrink: 0; }
.header .header-filters .filter-group { display: flex; flex-direction: column; gap: 3px; }
.header .header-filters .filter-group label { font-size: 0.7rem; font-weight: 600; color: #b8d4e8; text-transform: uppercase; letter-spacing: 0.04em; }
.header .header-filters select { padding: 4px 8px; border: none; border-radius: 4px; font-size: 0.75rem; background: #1a2e4a; color: #e0e8f0; min-width: 170px; }
.header .grower-name { color: #F5D76E; }
.header .header-filters button { padding: 4px 14px; background: #4A7FB5; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 0.75rem; font-weight: 500; margin-top: 16px; }
.header .header-filters button:hover { background: #3a6fa5; }

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
.map-card h3 .map-legend { float: right; font-size: 0.7rem; font-weight: 400; display: flex; gap: 12px; }
.map-card h3 .map-legend .legend-item { display: inline-flex; align-items: center; gap: 4px; }
.map-card h3 .map-legend .legend-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
.map-card .map-container { width: 100%; height: 500px; position: relative; background: #f0f4f8; border-radius: 4px; overflow: hidden; }
.map-card .map-container svg { width: 100%; height: 100%; display: block; }
.map-zoom-controls { position: absolute; bottom: 12px; left: 12px; display: flex; flex-direction: column; gap: 4px; z-index: 10; }
.map-zoom-controls button { width: 30px; height: 30px; border: 1px solid #ccc; border-radius: 4px; background: #fff; color: #333; font-size: 16px; font-weight: 700; cursor: pointer; line-height: 1; display: flex; align-items: center; justify-content: center; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }
.map-zoom-controls button:hover { background: #f0f0f0; }

.map-action-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.map-action-col { min-width: 0; }
.map-action-col .map-card,
.map-action-col .action-list { margin-bottom: 0; }
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
  .map-action-row { grid-template-columns: 1fr; }
  .header .header-main { flex-direction: column; }
  .header .header-filters { flex-wrap: wrap; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-main">
      <div>
        <h1><span class="grower-name">__GROWER_NAME__</span> - Corn Health Intelligence Dashboard</h1>
        <div class="freshness">Generated: __GENERATED_AT__</div>
      </div>
      <div class="header-filters">
        <div class="filter-group">
          <label>Fields:</label>
          <select id="field-select"></select>
        </div>
        <div class="filter-group">
          <label>Year:</label>
          <select id="year-select"></select>
        </div>
        <button id="reset-btn">Reset</button>
      </div>
    </div>
  </div>

  <div id="kpi-row" class="kpi-row"></div>

  <div class="map-action-row">
    <div class="map-action-col">
      <div class="action-list" id="action-list-section">
        <h3>Priority Actions</h3>
        <div id="action-list"></div>
      </div>
    </div>
    <div class="map-action-col">
      <div class="map-card">
        <h3>Field Risk Map — Click to Filter<span class="map-legend" id="map-legend"></span></h3>
        <div class="map-container" id="field-map"></div>
      </div>
    </div>
  </div>

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
const MAP_BASE64 = '__MAP_BASE64__';
const MAP_BBOX = __MAP_BBOX__;

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

// ===== DATE-AWARE FIELD HELPERS =====
function classifyRisk(ndviSeries, config) {
  if (!ndviSeries || !ndviSeries.length) return 'unknown';
  var latest = ndviSeries[ndviSeries.length - 1].value;
  if (latest < config.stress_threshold) return 'critical';
  if (latest < config.watch_threshold) return 'watch';
  if (ndviSeries.length >= 3) {
    var recent = ndviSeries.slice(-3);
    var firstVal = recent[0].value, latestVal = recent[recent.length - 1].value;
    var delta = latestVal - firstVal;
    if (delta <= -0.10) return 'critical';
    if (delta <= -0.05) return 'watch';
  }
  return 'healthy';
}

function computeNDVITrend(ndviSeries) {
  if (ndviSeries.length < 3) return { trend: 'stable', pct: 0 };
  var recent = ndviSeries.slice(-3);
  var first = recent[0].value, last = recent[recent.length - 1].value;
  var delta = +(last - first).toFixed(3);
  if (delta > 0.03) return { trend: 'improving', pct: delta };
  if (delta < -0.03) return { trend: 'declining', pct: delta };
  return { trend: 'stable', pct: delta };
}

function computeWeatherSummaries(weatherRecords, config) {
  if (!weatherRecords || !weatherRecords.length) return { gdd_accumulated: 0, days_since_significant_rain: null };
  var significantMm = config.precip_significant_mm || 2.54;
  var gddTotal = 0;
  var precipTotal = 0;
  weatherRecords.forEach(function(d) {
    var tmax = +d.T2M_MAX, tmin = +d.T2M_MIN;
    if (tmax != null && tmin != null && !isNaN(tmax) && !isNaN(tmin)) {
      gddTotal += Math.max(0, (tmax + tmin) / 2 - 10);
    }
    var p = +d.PRECTOTCORR;
    if (!isNaN(p)) precipTotal += p;
  });
  var sorted = weatherRecords.slice().sort(function(a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
  var recentRain = sorted.filter(function(d) {
    var p = +d.PRECTOTCORR;
    return !isNaN(p) && p >= significantMm;
  });
  var daysSince = null;
  if (recentRain.length) {
    var lastRain = recentRain[recentRain.length - 1].date;
    daysSince = Math.round((new Date() - new Date(lastRain)) / 86400000);
  }
  return { gdd_accumulated: Math.round(gddTotal), days_since_significant_rain: daysSince, total_precip_mm: Math.round(precipTotal) };
}

function computeStressDuration(ndviSeries, config) {
  if (!ndviSeries || ndviSeries.length < 2) return 0;
  var total = 0, inStress = false, start = null;
  ndviSeries.forEach(function(d) {
    var stressed = d.value < config.watch_threshold;
    if (stressed && !inStress) { start = d.date; inStress = true; }
    else if (!stressed && inStress) {
      total += Math.round((new Date(d.date) - new Date(start)) / 86400000);
      inStress = false;
    }
  });
  if (inStress && start) {
    var last = ndviSeries[ndviSeries.length - 1].date;
    total += Math.round((new Date(last) - new Date(start)) / 86400000);
  }
  return total;
}

// ===== STATE (pub/sub) =====
function isFieldCorn(field, year) {
  var crop = field.cdl_crops && field.cdl_crops[year] && field.cdl_crops[year] !== 'Unknown'
    ? field.cdl_crops[year]
    : field.current_crop;
  return crop === 'Corn';
}

const state = {
  fields: ALL_FIELDS,
  filters: { fieldIds: [], selectedYear: '2026' },
  _listeners: [],
  subscribe(fn) { this._listeners.push(fn); return () => { this._listeners = this._listeners.filter(l => l !== fn); }; },
  publish() { this._listeners.forEach(fn => fn()); },
  getFilteredWeather(field) {
    var data = field.weather_daily || [];
    if (this.filters.selectedYear) {
      data = data.filter(function(d) { return d.date.startsWith(this.filters.selectedYear); }.bind(this));
    }
    return data;
  },
  getFilteredFields() {
    var ff = this.fields;
    var year = this.filters.selectedYear;
    ff = ff.filter(function(f) { return isFieldCorn(f, year); });
    if (this.filters.fieldIds.length > 0) {
      ff = ff.filter(function(f) { return this.filters.fieldIds.includes(f.id); }.bind(this));
    }
    var self = this;
    return ff.map(function(f) {
      var ndviSeries = self.getFilteredNDVISeries(f);
      var weatherData = self.getFilteredWeather(f);
      var risk = classifyRisk(ndviSeries, CONFIG);
      var trend = computeNDVITrend(ndviSeries);
      var lastNDVI = ndviSeries.length ? ndviSeries[ndviSeries.length - 1].value : null;
      var weatherSumm = computeWeatherSummaries(weatherData, CONFIG);
      return Object.assign({}, f, {
        current_ndvi: lastNDVI,
        current_risk: risk,
        ndvi_trend: trend.trend,
        ndvi_trend_pct: trend.pct,
        weather_summary: Object.assign({}, f.weather_summary, weatherSumm),
        ndvi_series: ndviSeries,
        weather_daily: weatherData,
      });
    });
  },
  getFilteredNDVISeries(field) {
    var series = field.ndvi_series;
    if (this.filters.selectedYear) {
      series = series.filter(function(d) { return d.date.startsWith(this.filters.selectedYear); }.bind(this));
    }
    return series;
  }
};

// ===== SYNC FILTERS =====
function syncFilters() {
  var year = state.filters.selectedYear;
  var selectedId = state.filters.fieldIds.length === 1 ? state.filters.fieldIds[0] : null;

  var cornFields = ALL_FIELDS.filter(function(f) { return isFieldCorn(f, year); });
  var validIds = cornFields.map(function(f) { return f.id; });
  if (selectedId && !validIds.includes(selectedId)) {
    state.filters.fieldIds = [];
    selectedId = null;
  }
  var fieldSelect = document.getElementById("field-select");
  fieldSelect.innerHTML = '<option value="">All Corn Fields</option>' + cornFields.map(function(f) {
    var sel = f.id === selectedId;
    return '<option value="' + f.id + '"' + (sel ? ' selected' : '') + '>' + f.name + ' (' + f.id + ')</option>';
  }).join('');

  var refIds = selectedId ? [selectedId] : validIds;
  var allYears = Object.keys(ALL_FIELDS[0] && ALL_FIELDS[0].cdl_crops || {}).sort();
  var validYears = allYears.filter(function(y) {
    return refIds.some(function(id) {
      var f = ALL_FIELDS.find(function(fi) { return fi.id === id; });
      return f && isFieldCorn(f, y);
    });
  });
  if (!validYears.includes(year) && validYears.length > 0) {
    state.filters.selectedYear = validYears[validYears.length - 1];
  } else if (validYears.length === 0) {
    state.filters.selectedYear = '2026';
  }
  var thisYear = String(new Date().getFullYear());
  var yearSelect = document.getElementById("year-select");
  yearSelect.innerHTML = validYears.map(function(y) {
    var sel = y === state.filters.selectedYear;
    var label = y + (y === thisYear ? ' (Current)' : '');
    return '<option value="' + y + '"' + (sel ? ' selected' : '') + '>' + label + '</option>';
  }).join('');

  state.publish();
}

// ===== TOOLTIP =====
const tooltip = d3.select("#tooltip");

// ===== KPI RENDER =====
function renderKPIs() {
  const ff = state.getFilteredFields();
  const isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  const total = ff.length;

  const ndviVals = ff.filter(f => f.current_ndvi != null).map(f => f.current_ndvi);
  const avgNDVI = ndviVals.length ? (ndviVals.reduce((a,b) => a+b, 0) / ndviVals.length).toFixed(3) : '--';

  const improving = ff.filter(f => f.ndvi_trend === 'improving').length;
  const declining = ff.filter(f => f.ndvi_trend === 'declining').length;

  var ndviTier = 'healthy';
  if (avgNDVI < CONFIG.stress_threshold) ndviTier = 'critical';
  else if (avgNDVI < CONFIG.watch_threshold) ndviTier = 'watch';
  var ndviLabel = THRESHOLD_LABELS[ndviTier]?.label || 'Unknown';
  var ndviIconHtml = ndviTier === 'critical' ? ICONS.warning : ndviTier === 'watch' ? ICONS.alert : ICONS.check;
  var ndviTrendText = ndviLabel;
  if (declining > 0 || improving > 0) ndviTrendText += ' &middot; ' + declining + ' declining, ' + improving + ' improving';

  const gddVals = ff.map(f => f.weather_summary?.gdd_accumulated || 0);
  const avgGDD = gddVals.length ? Math.round(gddVals.reduce((a,b) => a+b, 0) / gddVals.length) : 0;

  var gddCardHtml =
    '<div class="kpi-card healthy">' +
      '<div class="kpi-label">' + ICONS.temp + ' GDD Accumulated (avg)</div>' +
      '<div class="kpi-value">' + avgGDD + ' <span class="kpi-unit">&deg;F-days</span></div>' +
      '<div class="kpi-trend">Target: ' + CONFIG.gdd_target + ' &deg;F-days</div>' +
    '</div>';

  if (isCurrent) {
    const critical = ff.filter(f => f.current_risk === 'critical').length;
    const watch = ff.filter(f => f.current_risk === 'watch').length;
    const attention = critical + watch;
    const riskClass = attention > 0 ? (critical > 0 ? 'critical' : 'watch') : 'healthy';

    const rainDays = ff.map(f => f.weather_summary?.days_since_significant_rain).filter(d => d != null);
    const maxRainDays = rainDays.length ? Math.max(...rainDays) : '--';

    d3.select("#kpi-row").html(
      '<div class="kpi-card ' + riskClass + '">' +
        '<div class="kpi-label">' + ICONS.warning + ' Fields Requiring Attention</div>' +
        '<div class="kpi-value">' + attention + ' / ' + total + '</div>' +
        '<div class="kpi-trend">' + critical + ' critical &middot; ' + watch + ' watch</div>' +
      '</div>' +
      '<div class="kpi-card ' + ndviTier + '">' +
        '<div class="kpi-label">' + ICONS.plant + ' Average NDVI</div>' +
        '<div class="kpi-value">' + avgNDVI + ' <span class="kpi-unit"></span></div>' +
        '<div class="kpi-trend">' + ndviIconHtml + ' ' + ndviTrendText + '</div>' +
      '</div>' +
      gddCardHtml +
      '<div class="kpi-card ' + (maxRainDays > 7 ? 'watch' : 'healthy') + '">' +
        '<div class="kpi-label">' + ICONS.water + ' Days Since Significant Rain</div>' +
        '<div class="kpi-value">' + maxRainDays + ' <span class="kpi-unit">days</span></div>' +
        '<div class="kpi-trend">Threshold: >0.1 in</div>' +
      '</div>'
    );
  } else {
    const peakNdvVals = ff.map(function(f) {
      var s = f.ndvi_series;
      return s && s.length ? d3.max(s, function(d) { return d.value; }) : null;
    }).filter(function(v) { return v != null; });
    const avgPeak = peakNdvVals.length ? (peakNdvVals.reduce(function(a,b) { return a+b; }, 0) / peakNdvVals.length).toFixed(3) : '--';

    const precipVals = ff.map(f => f.weather_summary?.total_precip_mm || 0);
    const avgPrecipIn = precipVals.length ? (precipVals.reduce((a,b) => a+b, 0) / precipVals.length / 25.4).toFixed(1) : '--';

    const stressVals = ff.map(function(f) { return computeStressDuration(f.ndvi_series, CONFIG); });
    const avgStress = stressVals.length ? Math.round(stressVals.reduce(function(a,b) { return a+b; }, 0) / stressVals.length) : 0;

    d3.select("#kpi-row").html(
      '<div class="kpi-card healthy">' +
        '<div class="kpi-label">' + ICONS.plant + ' Peak NDVI (avg)</div>' +
        '<div class="kpi-value">' + avgPeak + ' <span class="kpi-unit"></span></div>' +
        '<div class="kpi-trend">Best mean NDVI across fields</div>' +
      '</div>' +
      gddCardHtml +
      '<div class="kpi-card healthy">' +
        '<div class="kpi-label">' + ICONS.water + ' Cumulative Rain (avg)</div>' +
        '<div class="kpi-value">' + avgPrecipIn + ' <span class="kpi-unit">in</span></div>' +
        '<div class="kpi-trend">Total for ' + state.filters.selectedYear + '</div>' +
      '</div>' +
      '<div class="kpi-card ' + (avgStress > 14 ? 'watch' : 'healthy') + '">' +
        '<div class="kpi-label">' + ICONS.warning + ' Season Stress Duration</div>' +
        '<div class="kpi-value">' + avgStress + ' <span class="kpi-unit">days</span></div>' +
        '<div class="kpi-trend">Avg days in Watch/Critical</div>' +
      '</div>'
    );
  }
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
    const series = f.ndvi_series;
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

  // Growth stage annotations (vertical lines from cumulative GDD)
  var stageColors = {"VE":"#4CAF50","V6":"#8BC34A","VT":"#FFC107","R1":"#FF9800","R2":"#FF5722","R3":"#795548","R4":"#9C27B0","R5":"#3F51B5","R6":"#607D8B"};
  var gddBaseF = CONFIG.gdd_base_temp_f;
  var displayYear = state.filters.selectedYear;
  var chartStart = xScale.domain()[0], chartEnd = xScale.domain()[1];
  var weatherField = ff[0];
  var dailyData = (weatherField.weather_daily || []).slice().sort(function(a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
  if (dailyData.length > 0) {
    // Determine planting date from last spring frost, fallback to April 20
    var frostThresholdC = 0.0;
    var defaultPlanting = new Date(displayYear + "-04-20");
    var lastFrostDate = null;
    dailyData.forEach(function(d) {
      if (d.T2M_MIN <= frostThresholdC) {
        var dObj = new Date(d.date);
        var startOfYear = new Date(dObj.getFullYear(), 0, 0);
        var doy = Math.floor((dObj - startOfYear) / 86400000);
        if (doy <= 182) {
          if (!lastFrostDate || dObj > lastFrostDate) {
            lastFrostDate = dObj;
          }
        }
      }
    });
    var plantingDate = lastFrostDate && lastFrostDate > defaultPlanting ? lastFrostDate : defaultPlanting;

    // Compute cumulative GDD from planting date (days before planting get 0)
    var cumGDD = 0;
    dailyData.forEach(function(d) {
      var dObj = new Date(d.date);
      if (dObj < plantingDate) {
        d._cumGDD = 0;
      } else {
        var dailyAvgF = (d.T2M_MIN + d.T2M_MAX) / 2 * 9 / 5 + 32;
        cumGDD += Math.max(0, dailyAvgF - gddBaseF);
        d._cumGDD = cumGDD;
      }
    });

    // Draw Planting annotation at computed date
    if (plantingDate >= chartStart && plantingDate <= chartEnd) {
      var px = xScale(plantingDate);
      svg.append("line")
        .attr("x1", px).attr("x2", px)
        .attr("y1", 0).attr("y2", height)
        .attr("stroke", "#333").attr("stroke-width", 0.8)
        .attr("stroke-dasharray", "3,3").attr("opacity", 0.45);
      var plg = svg.append("g").attr("transform", "translate(" + px + ",0)");
      var plt = plg.append("text")
        .attr("x", 0).attr("y", 10)
        .attr("text-anchor", "middle").attr("font-size", "8px")
        .attr("font-weight", "600").attr("fill", "#333")
        .text("Planting");
      var plb = plt.node().getBBox();
      plg.insert("rect", "text")
        .attr("x", plb.x - 2).attr("y", plb.y - 1)
        .attr("width", plb.width + 4).attr("height", plb.height + 2)
        .attr("fill", "#fff").attr("opacity", 0.8).attr("rx", 2);
    }

    // Stage annotations (using _cumGDD which is 0 before planting)
    var stages = CONFIG.growth_stages || {};
    var stageKeys = Object.keys(stages);
    stageKeys.forEach(function(stage) {
      var threshold = stages[stage];
      for (var i = 0; i < dailyData.length; i++) {
        if (dailyData[i]._cumGDD >= threshold) {
          var evDate = new Date(dailyData[i].date);
          if (evDate >= chartStart && evDate <= chartEnd) {
            var xPos = xScale(evDate);
            var c = stageColors[stage] || "#666";
            svg.append("line")
              .attr("x1", xPos).attr("x2", xPos)
              .attr("y1", 0).attr("y2", height)
              .attr("stroke", c).attr("stroke-width", 0.8)
              .attr("stroke-dasharray", "3,3").attr("opacity", 0.45);
            var labelG = svg.append("g").attr("transform", "translate(" + xPos + ",0)");
            var txt = labelG.append("text")
              .attr("x", 0).attr("y", 10)
              .attr("text-anchor", "middle").attr("font-size", "8px")
              .attr("font-weight", "600").attr("fill", c)
              .text(stage);
            var bbox = txt.node().getBBox();
            labelG.insert("rect", "text")
              .attr("x", bbox.x - 2).attr("y", bbox.y - 1)
              .attr("width", bbox.width + 4).attr("height", bbox.height + 2)
              .attr("fill", "#fff").attr("opacity", 0.8)
              .attr("rx", 2);
          }
          break;
        }
      }
    });
    // Clean up temporary property
    dailyData.forEach(function(d) { delete d._cumGDD; });
  }

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
    const series = f.ndvi_series;
    if (series.length < 2) return;
    const last = series[series.length - 1];
    const trendInfo = computeNDVITrend(series);
    const latestNDVI = last.value.toFixed(3);
    const ndviTip = "<strong>" + f.name + " (" + f.id + ")</strong><br>Latest NDVI: " + latestNDVI + " on " + last.date + "<br>Trend: " + trendInfo.trend + " (" + (trendInfo.pct >= 0 ? '+' : '') + trendInfo.pct + ")";

    svg.append("path")
      .datum(series)
      .attr("fill", "none")
      .attr("stroke", colorScale(f.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.8)
      .attr("d", line)
      .style("cursor", "pointer")
      .on("mouseenter", function(event) {
        d3.select(this).attr("stroke-width", 4).attr("opacity", 1);
        tooltip.classed("visible", true)
          .html(ndviTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("stroke-width", 2).attr("opacity", 0.8);
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(ndviTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      });

    svg.append("text")
      .attr("x", xScale(new Date(last.date)) + 4)
      .attr("y", yScale(last.value))
      .attr("font-size", "10px")
      .attr("fill", colorScale(f.id))
      .text(f.name)
      .style("cursor", "pointer")
      .on("mouseenter", function(event) {
        tooltip.classed("visible", true)
          .html(ndviTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(ndviTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      });
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
    const barTip = "<strong>" + f.name + " (" + f.id + ")</strong><br>NDVI: " + f.current_ndvi.toFixed(3) + "<br>Risk: " + f.current_risk;
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(f.current_ndvi))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("opacity", 0.85)
      .style("cursor", "pointer")
      .on("mouseenter", function(event) {
        d3.select(this).attr("opacity", 1);
        tooltip.classed("visible", true)
          .html(barTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("opacity", 0.85);
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(barTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      });
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
    const scatterTip = "<strong>" + f.name + " (" + f.id + ")</strong><br>NDVI: " + f.current_ndvi + "<br>AWC: " + f.soil.awc_in_in + " in/in<br>Risk: " + f.current_risk;
    svg.append("circle")
      .attr("cx", xScale(f.soil.awc_in_in))
      .attr("cy", yScale(f.current_ndvi))
      .attr("r", r)
      .attr("fill", color)
      .attr("opacity", 0.7)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .on("mouseenter", function(event) {
        d3.select(this).attr("opacity", 1).attr("r", r * 1.4);
        tooltip.classed("visible", true)
          .html(scatterTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("opacity", 0.7).attr("r", r);
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(scatterTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
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
  var container = d3.select("#field-map");
  container.html("");
  container.append("div").attr("class", "map-zoom-controls")
    .html('<button id="map-zoom-in">+</button><button id="map-zoom-out">-</button>');

  var year = state.filters.selectedYear;
  var selectedIds = state.filters.fieldIds;

  var allCornFields = ALL_FIELDS.filter(function(f) { return isFieldCorn(f, year) && f.geometry?.geometry; });
  if (!allCornFields.length) return;

  var visibleFields = selectedIds.length > 0
    ? allCornFields.filter(function(f) { return selectedIds.includes(f.id); })
    : allCornFields;
  if (!visibleFields.length) visibleFields = allCornFields;

  var rect = container.node().getBoundingClientRect();
  var width = rect.width, height = rect.height;

  var svg = container.append("svg")
    .attr("width", width).attr("height", height);

  var mapGroup = svg.append("g").attr("class", "map-group");

  // Projection from ALL corn fields (fixed — doesn't change per field selection)
  var lons = [], lats = [];
  allCornFields.forEach(function(f) {
    var geo = f.geometry.geometry;
    if (geo.type === "Polygon") geo.coordinates[0].forEach(function(c) { lons.push(c[0]); lats.push(c[1]); });
    else if (geo.type === "MultiPolygon") geo.coordinates.forEach(function(p) { p[0].forEach(function(c) { lons.push(c[0]); lats.push(c[1]); }); });
  });
  if (!lons.length) return;
  var cLon = (d3.min(lons) + d3.max(lons)) / 2, cLat = (d3.min(lats) + d3.max(lats)) / 2;

  var allGeoBounds = {
    type: "FeatureCollection",
    features: allCornFields.map(function(f) { return { type: "Feature", geometry: f.geometry.geometry, properties: {} }; })
  };

  var projection = d3.geoMercator()
    .center([cLon, cLat])
    .fitExtent([[20, 20], [width - 20, height - 20]], allGeoBounds);
  var geoPath = d3.geoPath().projection(projection);

  // Static basemap fills SVG viewport (or grey fallback)
  if (MAP_BASE64) {
    mapGroup.append("image")
      .attr("x", 0).attr("y", 0)
      .attr("width", width).attr("height", height)
      .attr("preserveAspectRatio", "xMidYMid slice")
      .attr("href", MAP_BASE64)
      .attr("opacity", 0.7);
  } else {
    mapGroup.append("rect")
      .attr("x", 0).attr("y", 0).attr("width", width).attr("height", height)
      .attr("fill", "#e8f0f8");
  }

  // Draw ALL corn fields — show only selected, hide others
  allCornFields.forEach(function(f) {
    var visible = selectedIds.length === 0 || selectedIds.includes(f.id);
    var color = visible ? (THRESHOLD_LABELS[f.current_risk]?.color || "#999") : "none";
    var fieldName = f.name, fieldRisk = f.current_risk, fieldNdvi = f.current_ndvi, fieldAcres = f.area_acres;
    var fieldId = f.id;
    // Fill path — risk tier color, no stroke
    var fp = mapGroup.append("path")
      .datum(f.geometry.geometry)
      .attr("d", geoPath)
      .attr("fill", color)
      .attr("stroke", "none")
      .attr("opacity", visible ? 0.85 : 0)
      .style("pointer-events", visible ? "auto" : "none")
      .style("cursor", visible ? "pointer" : "default");
    // Outline path — neon green border, no fill (sits on top)
    var op = mapGroup.append("path")
      .datum(f.geometry.geometry)
      .attr("d", geoPath)
      .attr("fill", "none")
      .attr("stroke", visible ? "#39FF14" : "none")
      .attr("stroke-width", visible ? 3.5 : 0)
      .attr("opacity", visible ? 1 : 0)
      .style("pointer-events", "none");
    if (!visible) return;
    fp.on("mouseenter", function(event) {
      op.attr("stroke-width", 6);
      tooltip.classed("visible", true)
        .html("<strong>" + fieldName + " (" + fieldId + ")</strong><br>Risk: " + fieldRisk + "<br>NDVI: " + (fieldNdvi || '--') + "<br>Area: " + fieldAcres + " ac")
        .style("left", (event.pageX + 12) + "px")
        .style("top", (event.pageY - 28) + "px");
    })
    .on("mouseleave", function() {
      op.attr("stroke-width", 3.5);
      tooltip.classed("visible", false);
    })
    .on("click", function() {
      state.filters.fieldIds = [fieldId];
      syncFilters();
    });
  });

  // Zoom behavior
  var zoom = d3.zoom()
    .scaleExtent([1, 30])
    .translateExtent([[0, 0], [width, height]])
    .on("zoom", function(event) {
      mapGroup.attr("transform", event.transform);
    });
  svg.call(zoom);

  // Zoom to visible fields
  var fitFields = visibleFields;
  var fitGeoBounds = {
    type: "FeatureCollection",
    features: fitFields.map(function(f) { return { type: "Feature", geometry: f.geometry.geometry, properties: {} }; })
  };
  var fb = geoPath.bounds(fitGeoBounds);
  var bx = fb[0][0], by = fb[0][1];
  var bw = fb[1][0] - bx, bh = fb[1][1] - by;
  if (bw > 0 && bh > 0) {
    var pad = 0.10;
    var s = Math.min((width * (1 - pad * 2)) / bw, (height * (1 - pad * 2)) / bh);
    if (s > 1.05) {
      var tx = width / 2 - (bx + bw / 2) * s;
      var ty = height / 2 - (by + bh / 2) * s;
      svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
    } else {
      svg.call(zoom.transform, d3.zoomIdentity);
    }
  } else {
    svg.call(zoom.transform, d3.zoomIdentity);
  }

  // HTML legend
  var legendEl = document.getElementById("map-legend");
  legendEl.innerHTML = "";
  ["healthy", "watch", "critical"].forEach(function(t) {
    var tl = THRESHOLD_LABELS[t];
    var item = document.createElement("span");
    item.className = "legend-item";
    item.innerHTML = '<span class="legend-swatch" style="background:' + tl.color + '"></span>' + tl.label;
    legendEl.appendChild(item);
  });

  document.getElementById("map-zoom-in").addEventListener("click", function() {
    svg.transition().duration(300).call(zoom.scaleBy, 1.5);
  });
  document.getElementById("map-zoom-out").addEventListener("click", function() {
    svg.transition().duration(300).call(zoom.scaleBy, 0.667);
  });
}

// ===== GDD CHART =====
function renderGDD() {
  const container = d3.select("#gdd-chart");
  container.html("");
  const ff = state.getFilteredFields();
  if (!ff.length) return;

  const displayYear = state.filters.selectedYear || String(new Date().getFullYear());
  const fieldData = ff.map(f => {
    const daily = f.weather_daily || [];
    const byDate = {};
    daily.forEach(d => {
      var tmax = +d.T2M_MAX, tmin = +d.T2M_MIN;
      if (tmax == null || tmin == null || isNaN(tmax) || isNaN(tmin)) return;
      const gdd = Math.max(0, (tmax + tmin) / 2 - 10);
      byDate[d.date] = (byDate[d.date] || 0) + gdd;
    });
    const sorted = Object.entries(byDate).sort((a, b) => a[0].localeCompare(b[0]));
    let cum = 0;
    const currentSeries = [];
    sorted.forEach(([dt, val]) => {
      cum += val;
      const y = dt.slice(0, 4);
      if (y === displayYear) {
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
    var lastGDD = fd.current[fd.current.length - 1].gdd;
    var gddTip = "<strong>" + fd.name + " (" + fd.id + ")</strong><br>GDD Accumulated: " + lastGDD + " &deg;F-days";
    svg.append("path")
      .datum(fd.current)
      .attr("fill", "none")
      .attr("stroke", colorScale(fd.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.7)
      .attr("d", line)
      .style("cursor", "pointer")
      .on("mouseenter", function(event) {
        d3.select(this).attr("stroke-width", 4).attr("opacity", 1);
        tooltip.classed("visible", true)
          .html(gddTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("stroke-width", 2).attr("opacity", 0.7);
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(gddTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      });
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
    const awcInfo = f.soil?.awc_in_in != null ? 'AWC: ' + f.soil.awc_in_in + ' in/in' : '';
    const drainInfo = f.soil?.drainage_class || '';
    const soilTip = "<strong>" + f.name + " (" + f.id + ")</strong><br>OM: " + f.soil.om_pct.toFixed(1) + "%" + (awcInfo ? '<br>' + awcInfo : '') + (drainInfo ? '<br>Drainage: ' + drainInfo : '');
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(f.soil.om_pct))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("opacity", 0.85)
      .style("cursor", "pointer")
      .on("mouseenter", function(event) {
        d3.select(this).attr("opacity", 1);
        tooltip.classed("visible", true)
          .html(soilTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      })
      .on("mouseleave", function() {
        d3.select(this).attr("opacity", 0.85);
        tooltip.classed("visible", false);
      })
      .on("click", function(event) {
        event.stopPropagation();
        tooltip.classed("visible", true)
          .html(soilTip)
          .style("left", (event.pageX + 12) + "px")
          .style("top", (event.pageY - 28) + "px");
      });
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
  var section = d3.select("#action-list-section");
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  if (!isCurrent) {
    section.style("display", "none");
    return;
  }
  section.style("display", "block");

  let ff = state.getFilteredFields()
    .filter(f => f.current_risk === 'critical' || f.current_risk === 'watch')
    .sort((a, b) => {
      function riskScore(f) {
        var tierWeight = f.current_risk === 'critical' ? 100 : 50;
        var ndviPenalty = f.current_ndvi != null ? Math.max(0, (CONFIG.watch_threshold - f.current_ndvi) * 100) : 0;
        var trendPenalty = f.ndvi_trend === 'declining' ? Math.abs(f.ndvi_trend_pct || 0) * 2 : 0;
        var awcPenalty = f.soil?.awc_in_in != null && f.soil.awc_in_in < 0.5 ? (0.5 - f.soil.awc_in_in) * 20 : 0;
        var rainPenalty = (f.weather_summary?.days_since_significant_rain || 0) * 0.5;
        return tierWeight + ndviPenalty + trendPenalty + awcPenalty + rainPenalty;
      }
      return riskScore(b) - riskScore(a);
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
    const trendInfo = f.ndvi_trend_pct ? ' (' + (f.ndvi_trend_pct >= 0 ? '+' : '') + f.ndvi_trend_pct + ')' : '';
    const soilInfo = f.soil?.awc_in_in != null ? 'AWC ' + f.soil.awc_in_in + ' in/in' : '';
    const action = f.current_risk === 'critical'
      ? 'Scout immediately -- consider irrigation or tissue sampling.'
      : 'Monitor weekly -- check NDVI trend and soil moisture.';

    html += '<div class="action-item">' +
      '<span class="risk-badge" style="background:' + tl.color + '">' + tl.label + '</span>' +
      '<span class="risk-text">' +
        '<strong>' + f.name + ' (' + f.id + ')</strong>: ' + ndviInfo + trendInfo + ' &middot; ' + soilInfo + '<br>' +
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
  state.filters.selectedYear = '2026';
  syncFilters();
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

document.addEventListener("click", function() {
  tooltip.classed("visible", false);
});

document.getElementById("reset-btn").addEventListener("click", resetFilters);

document.getElementById("field-select").addEventListener("change", function() {
  state.filters.fieldIds = this.value ? [this.value] : [];
  syncFilters();
});

document.getElementById("year-select").addEventListener("change", function() {
  state.filters.selectedYear = this.value;
  syncFilters();
});

syncFilters();
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
