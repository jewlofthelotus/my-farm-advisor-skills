#!/usr/bin/env python3
"""Generate a self-contained Row Crop Intelligence & Data Dashboard HTML for a grower."""

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize, shapes

# Management-zone deps are optional: if absent, zone computation is skipped and
# fields simply render without zones (graceful degradation).
try:
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    from sklearn.cluster import KMeans
    _ZONES_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without sklearn/shapely
    _ZONES_AVAILABLE = False

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
        "stage_descriptions": {
            "VE": "Early Vegetative", "V6": "Vegetative", "VT": "Tasseling",
            "R1": "Silking", "R2": "Blister", "R3": "Milk",
            "R4": "Dough", "R5": "Dent", "R6": "Maturing"
        },
        "phase_anchors": {
            "establishing_end": "VE",
            "building_end": "R1",
            "reproductive_early_end": "R4",
            "full_canopy": "VT"
        },
        "crop_name": "Corn",
        "match_all": False,
        "sensitive_stages": ["VT", "R1", "R2"],
        "kpi_units": {"ndvi": "", "gdd": "\u00b0F-days", "precip": "in", "awc": "in", "om": "%"}
    },
    "grape": {
        "stress_threshold": 0.30,
        "watch_threshold": 0.45,
        "ndvi_decline_warning_pct": 5.0,
        "ndvi_decline_critical_pct": 10.0,
        "gdd_base_temp_f": 50.0,
        "gdd_target": 2053,
        "precip_significant_in": 0.1,
        "precip_significant_mm": 2.54,
        "growth_stages": {
            "Budbreak": 59, "Bloom": 298, "Fruit Set": 473,
            "Veraison": 1401, "Harvest": 2053
        },
        "stage_descriptions": {
            "Budbreak": "Dormancy Break", "Bloom": "Flowering",
            "Fruit Set": "Berry Set", "Veraison": "Ripening", "Harvest": "Maturity"
        },
        "phase_anchors": {
            "establishing_end": "Budbreak",
            "building_end": "Bloom",
            "reproductive_early_end": "Veraison",
            "full_canopy": "Bloom"
        },
        "crop_name": "Grapes",
        "match_all": True,
        "sensitive_stages": ["Bloom", "Fruit Set", "Veraison"],
        "stage_colors": {
            "Budbreak": "#8BC34A", "Bloom": "#FFC107",
            "Fruit Set": "#FF9800", "Veraison": "#9C27B0", "Harvest": "#607D8B"
        },
        "kpi_units": {"ndvi": "", "gdd": "\u00b0F-days", "precip": "in", "awc": "in", "om": "%"}
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

def detect_crop_type(grower_root):
    """Infer the default crop config from the grower's field boundaries.

    Uses CDL crop metadata stamped on each field boundary (cdl_crop_code / crop_name).
    Returns the name of a CROP_CONFIG key ("grape" for vineyard fields, else "corn"),
    falling back to "corn" when no boundaries are available.
    """
    grape_count = 0
    total = 0
    for farm_root in farm_paths(grower_root):
        for field_dir in field_paths(farm_root):
            boundary = read_field_boundary(field_dir)
            if not boundary:
                continue
            total += 1
            props = boundary.get("properties", {})
            code = props.get("cdl_crop_code")
            name = str(props.get("crop_name") or "").lower()
            if code == 69 or "grape" in name:
                grape_count += 1
    if total and grape_count / total >= 0.5:
        return "grape"
    return "corn"

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

_WEATHER_ROUND_COLS = {
    "T2M": 1, "T2M_MAX": 1, "T2M_MIN": 1, "PRECTOTCORR": 1,
    "RH2M": 1, "WS10M": 1, "ALLSKY_SFC_SW_DWN": 2,
}


def _grid_key_from_latlon(lat: float, lon: float) -> str:
    """Mirror of nasa_power.assign_power_grid rounding for backward compat.

    Called only when weather CSVs lack a grid_key column (pre-migration data).
    Once all CSVs are regenerated after the pipeline change this can be removed.
    """
    grid_lat = round(lat / 0.5) * 0.5
    grid_lon = round(lon / 0.625) * 0.625
    return f"{grid_lat:.3f}:{grid_lon:.3f}"


def read_weather_records(field_dir):
    path = field_dir / "weather" / "daily_weather.csv"
    if not path.exists():
        return [], None
    df = pd.read_csv(path)
    df = df.sort_values("date")
    today = date.today()
    df = df[pd.to_datetime(df["date"]).dt.date <= today]
    for col, decimals in _WEATHER_ROUND_COLS.items():
        if col in df.columns:
            df[col] = df[col].round(decimals)
    if "grid_key" in df.columns and not df["grid_key"].isna().all():
        grid_key = str(df["grid_key"].iloc[0])
    elif "lat" in df.columns and "lon" in df.columns:
        grid_key = _grid_key_from_latlon(
            float(df["lat"].iloc[0]), float(df["lon"].iloc[0])
        )
    else:
        grid_key = None
    return df.to_dict("records"), grid_key

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
                        valid = data[_valid_ndvi_mask(data)]
                        if len(valid) > 0:
                            mean_val = round(float(np.mean(valid)), 3)
                            series.append({"date": scene_date.isoformat(), "value": mean_val})
                except Exception:
                    pass
    series.sort(key=lambda x: x["date"])
    return series

def _valid_ndvi_mask(arr):
    """Boolean mask of usable NDVI pixels (not nodata/NaN, within valid NDVI range)."""
    return ~np.isnan(arr) & (arr > -1) & (arr < 2)

def _collect_field_scenes(field_dir, year):
    """Return [(scene_date, satellite, ndvi_path)] for one field/year."""
    scenes = []
    for sat in ("sentinel", "landsat"):
        year_dir = field_dir / "satellite" / sat / str(year)
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
            scenes.append((scene_date, sat, ndvi_path))
    return scenes

def _rasterize_field_mask(geom, out_shape, transform):
    mask = rasterize(
        [(geom, 1)],
        out_shape=out_shape,
        transform=transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)
    return mask

def _scene_in_field_stats(ndvi_path, geom):
    """Valid in-field NDVI fraction and masked mean for a scene. None if unreadable."""
    try:
        with rasterio.open(ndvi_path) as src:
            data = src.read(1)
            mask = _rasterize_field_mask(geom, data.shape, src.transform)
            in_field = data[mask]
            if len(in_field) == 0:
                return None, None
            valid = in_field[_valid_ndvi_mask(in_field)]
            frac = len(valid) / len(in_field)
            mean = float(np.mean(valid)) if len(valid) else None
            return frac, mean
    except Exception:
        return None, None

def _select_scene_for_zones(field_dir, year, geom, current_year):
    """Pick the first scene passing the quality gate.

    Pre-sorted by the dashboard convention: most-recent scene first for the current
    year, peak-NDVI scene first for past years. Returns (raster, transform, mask,
    mean_ndvi) for the chosen scene, or None when no candidate clears the gate.
    """
    candidates = _collect_field_scenes(field_dir, year)
    if not candidates:
        return None

    keyed = []
    for scene_date, sat, path in candidates:
        frac, mean = _scene_in_field_stats(path, geom)
        if frac is None or mean is None:
            continue
        keyed.append((frac, mean, scene_date, path))

    if year == str(current_year):
        keyed.sort(key=lambda k: k[2], reverse=True)  # most-recent scene first
    else:
        keyed.sort(key=lambda k: k[1], reverse=True)  # peak-NDVI scene first

    for frac, mean, scene_date, path in keyed:
        if mean is None or frac < 0.90:
            continue
        with rasterio.open(path) as src:
            data = src.read(1)
            mask = _rasterize_field_mask(geom, data.shape, src.transform)
            if int(mask.sum()) == 0:
                continue
            return data, src.transform, mask, mean
    return None

def _cluster_ndvi_zones(data, transform, mask):
    """K-means (k=3) on valid in-field NDVI, then dissolve + simplify.

    Returns zone list sorted low->high by mean NDVI, or None when clustering would
    be degenerate (too few pixels / too few distinct values).
    """
    in_field = data[mask]
    valid = in_field[_valid_ndvi_mask(in_field)]
    if len(valid) < 25 or len(np.unique(valid)) < 3:
        return None

    km = KMeans(n_clusters=3, n_init=10, random_state=0).fit(valid.reshape(-1, 1))

    # Assign cluster labels to their raster cells
    label_raster = np.full(data.shape, -1, dtype="int32")
    valid_pos = np.flatnonzero(mask & _valid_ndvi_mask(data))
    labels = km.predict(valid.reshape(-1, 1))
    np.put(label_raster, valid_pos, labels)

    polys_by_label = {i: [] for i in range(3)}
    for polygon, value in shapes(label_raster, mask=(label_raster >= 0), transform=transform):
        if value >= 0:
            if isinstance(polygon, dict):
                polygon = shape(polygon)
            polys_by_label[int(value)].append(polygon)

    # Dissolve + simplify per cluster; order by mean NDVI ascending.
    # Tolerance ~1.5 pixels smooths raster stair-stepping without over-rounding shapes.
    tol = abs(transform.a) * 1.5
    zones = []
    for lab in range(3):
        if not polys_by_label[lab]:
            continue
        merged = unary_union([p for p in polys_by_label[lab] if p and not p.is_empty])
        if merged.is_empty:
            continue
        merged = merged.buffer(0)
        merged = merged.simplify(tol, preserve_topology=True)
        # Drop sub-0.1-acre slivers so zone outlines read clean rather than speckled
        if merged.geom_type == "MultiPolygon":
            parts = [p for p in merged.geoms if _geom_area_acres(p) >= 0.1]
            if len(parts) < len(list(merged.geoms)):
                merged = unary_union(parts) if parts else None
                if merged is None or merged.is_empty:
                    continue
        mean_ndvi = float(np.mean(valid[labels == lab])) if len(labels[labels == lab]) else None
        zones.append({"label": lab, "mean_ndvi": mean_ndvi, "geom": merged})

    zones.sort(key=lambda z: z["mean_ndvi"] or -1)
    label_names = {0: "low", 1: "medium", 2: "high"}
    result = []
    for i, z in enumerate(zones):
        result.append({
            "label": label_names[i],
            "area_acres": round(_geom_area_acres(z["geom"]), 2),
            "mean_ndvi": round(z["mean_ndvi"], 3) if z["mean_ndvi"] is not None else None,
            "geometry": _round_coords(mapping(z["geom"])),
        })
    return result

def _round_coords(obj, ndigits=6):
    """Recursively round GeoJSON coordinates (~0.1 m at 6 dp) to keep the dashboard lean."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, (list, tuple)):
        return [_round_coords(x, ndigits) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_coords(v, ndigits) for k, v in obj.items()}
    return obj

def _geom_area_acres(geom):
    """Area in acres via US Contiguous Albers Equal Area (EPSG:5070)."""
    try:
        import geopandas as gpd
        if geom.geom_type == "GeometryCollection":
            geom = geom.buffer(0)
        return float(gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:5070").area[0]) / 4046.8564
    except Exception:
        return 0.0

def _normalize_zone_areas(zones, target_acres):
    """Scale zone areas so they sum to the field's known acreage."""
    if not zones or not target_acres:
        return zones
    total = sum(z["area_acres"] for z in zones)
    if total <= 0:
        return zones
    scale = target_acres / total
    for z in zones:
        z["area_acres"] = round(z["area_acres"] * scale, 2)
    return zones

def compute_field_zones(field_dir, geom, years, current_year, field_acres):
    """Per-year management zones for a field. Missing years have no entry."""
    if not _ZONES_AVAILABLE:
        return {}
    zones = {}
    for year in years:
        selected = _select_scene_for_zones(field_dir, year, geom, current_year)
        if selected is None:
            continue
        data, transform, mask, _mean = selected
        # TODO: nearest-valid-pixel interpolation for small gaps could go here
        year_zones = _cluster_ndvi_zones(data, transform, mask)
        if year_zones:
            zones[str(year)] = _normalize_zone_areas(year_zones, field_acres)
    return zones

def compute_gdd(tmin_c, tmax_c):
    """Growing degree days in Fahrenheit (base 50°F)."""
    avg_c = (tmin_c + tmax_c) / 2.0
    avg_f = avg_c * 9 / 5 + 32
    return max(0.0, avg_f - 50.0)

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

def _ordered_stages(config):
    """Return [(stage_name, gdd_threshold)] sorted ascending by GDD from config."""
    stages = config.get("growth_stages", {})
    if stages:
        return sorted(stages.items(), key=lambda item: item[1])
    return [("VE", 120), ("V6", 500), ("VT", 1130), ("R1", 1400),
            ("R2", 1650), ("R3", 1880), ("R4", 2150), ("R5", 2450), ("R6", 2700)]


def _anchor_gdd(config, anchor_key, fallback_name, fallback_gdd):
    """Resolve a phase-anchor stage's GDD threshold from config, with fallback."""
    anchors = config.get("phase_anchors", {})
    stages = config.get("growth_stages", {})
    name = anchors.get(anchor_key, fallback_name)
    return stages.get(name, stages.get(fallback_name, fallback_gdd))


def compute_growth_phase(weather_records, config):
    """Determine current growth phase from accumulated GDD (post-planting, base 50°F).

    Returns a dict:
      phase         – one of "establishing" | "building" | "reproductive_early" | "reproductive_late"
      cum_gdd_f     – cumulative GDD in °F-days from planting to the latest weather record
      stage_label   – adjacent-stage bracket, e.g. "V6–VT"
      stage_description – human-readable phase description, e.g. "Vegetative"
    """
    stages = config.get("growth_stages", {})
    descriptions = config.get("stage_descriptions", {})
    # Ordered stage list with GDD thresholds (config-driven)
    ordered = _ordered_stages(config)
    # Reproductive sub-window boundaries
    R1_GDD = _anchor_gdd(config, "building_end", "R1", 1400)
    R4_GDD = _anchor_gdd(config, "reproductive_early_end", "R4", 2150)

    if not weather_records:
        return {
            "phase": "building",
            "cum_gdd_f": 0.0,
            "stage_label": "Unknown",
            "stage_description": "Unknown",
        }

    df = pd.DataFrame(weather_records)
    df["date"] = pd.to_datetime(df["date"])
    df["T2M_MIN"] = pd.to_numeric(df["T2M_MIN"], errors="coerce")
    df["T2M_MAX"] = pd.to_numeric(df["T2M_MAX"], errors="coerce")
    df = df.dropna(subset=["T2M_MIN", "T2M_MAX"]).sort_values("date").reset_index(drop=True)

    today = date.today()
    use_year = today.year
    year_df = df[df["date"].dt.year == use_year]
    if year_df.empty:
        max_year = int(df["date"].dt.year.max())
        year_df = df[df["date"].dt.year == max_year]
        use_year = max_year

    # Determine planting date: last spring frost (T2M_MIN <= 0°C) before July 1,
    # defaulting to April 20 if no frost found.
    default_planting = pd.Timestamp(f"{use_year}-04-20")
    frost_rows = year_df[(year_df["T2M_MIN"] <= 0.0) &
                         (year_df["date"].dt.dayofyear <= 182)]
    if not frost_rows.empty:
        last_frost = frost_rows["date"].max()
        planting_date = last_frost if last_frost > default_planting else default_planting
    else:
        planting_date = default_planting

    # Accumulate GDD in °F from planting date (base 50°F)
    post_planting = year_df[year_df["date"] >= planting_date]
    base_f = config.get("gdd_base_temp_f", 50.0)
    cum_gdd_f = 0.0
    for _, row in post_planting.iterrows():
        avg_f = (row["T2M_MIN"] + row["T2M_MAX"]) / 2.0 * 9 / 5 + 32
        cum_gdd_f += max(0.0, avg_f - base_f)

    # Determine phase
    VE_GDD = _anchor_gdd(config, "establishing_end", "VE", 120)
    if cum_gdd_f < VE_GDD:
        phase = "establishing"
    elif cum_gdd_f < R1_GDD:
        phase = "building"
    elif cum_gdd_f < R4_GDD:
        phase = "reproductive_early"
    else:
        phase = "reproductive_late"

    # Stage label: find the two adjacent thresholds that bracket cum_gdd_f
    first_name = ordered[0][0]
    last_name = ordered[-1][0]
    stage_label = f"Pre-{first_name}"
    stage_description = descriptions.get(first_name, "Emergence")
    for i, (sname, sthresh) in enumerate(ordered):
        if cum_gdd_f < sthresh:
            if i == 0:
                stage_label = f"Planting–{sname}"
                stage_description = descriptions.get(sname, "Emergence")
            else:
                prev_name = ordered[i - 1][0]
                stage_label = f"{prev_name}–{sname}"
                stage_description = descriptions.get(prev_name, _stage_desc(prev_name))
            break
    else:
        # Past last stage
        stage_label = f"{last_name}+"
        stage_description = descriptions.get(last_name, "Maturing")

    return {
        "phase": phase,
        "cum_gdd_f": round(cum_gdd_f, 1),
        "stage_label": stage_label,
        "stage_description": stage_description,
    }


def _stage_desc(stage_name):
    """Human-readable description for the opening stage of a bracket."""
    return {
        "VE": "Early Vegetative",
        "V6": "Vegetative",
        "VT": "Tasseling",
        "R1": "Silking",
        "R2": "Blister",
        "R3": "Milk",
        "R4": "Dough",
        "R5": "Dent",
        "R6": "Maturing",
    }.get(stage_name, stage_name)


def healthy_threshold_for_stage(accumulated_gdd, config):
    """Scale the Healthy (watch_threshold) by growth stage progress toward full canopy."""
    full_canopy_gdd = _anchor_gdd(config, "full_canopy", "VT", 1130)
    full_threshold = config["watch_threshold"]
    if accumulated_gdd >= full_canopy_gdd:
        return full_threshold
    progress = accumulated_gdd / full_canopy_gdd if full_canopy_gdd else 1.0
    return full_threshold * max(progress, 0.3)


def classify_risk(ndvi_series, config, phase="building", accumulated_gdd=None):
    """Classify field NDVI risk, gated by growth phase.

    Phase behavior:
      establishing      – suppress all flags (bare soil NDVI is not diagnostic)
      building          – full flagging: absolute floors + decline-based promotions
      reproductive_early – absolute floors only (0.50/0.70); decline flags suppressed
      reproductive_late  – suppress all flags (universal senescence; non-diagnostic)
    """
    if not ndvi_series:
        return "unknown"

    # Phases where NDVI is not diagnostic at all
    if phase in ("establishing", "reproductive_late"):
        return "healthy"

    latest = ndvi_series[-1]["value"]
    threshold = config["stress_threshold"]

    # Absolute NDVI floor — active in building and reproductive_early
    if latest < threshold:
        return "critical"
    healthy_threshold = healthy_threshold_for_stage(accumulated_gdd or 0, config)
    if latest < healthy_threshold:
        return "watch"

    # Decline-based promotions — only in building phase
    if phase == "building" and len(ndvi_series) >= 3:
        recent = ndvi_series[-3:]
        first_val = recent[0]["value"]
        latest_val = recent[-1]["value"]
        delta = latest_val - first_val
        if delta <= -0.10:
            return "critical"
        if delta <= -0.05:
            return "watch"

    return "healthy"


def compute_ndvi_trend(ndvi_series, phase="building"):
    """Compute NDVI trend direction, gated by growth phase.

    Returns (trend_label, delta) where trend_label is one of:
      "improving" | "stable" | "declining" | "expected_decline"

    "expected_decline" means the crop is declining, but it is biologically
    normal for the current phase — not a stress signal.
    """
    if len(ndvi_series) < 3:
        return "stable", 0.0

    recent = ndvi_series[-3:]
    first, last = recent[0]["value"], recent[-1]["value"]
    delta = round(last - first, 3)

    # In establishing phase, trend is meaningless
    if phase == "establishing":
        return "stable", 0.0

    if delta > 0.03:
        return "improving", delta

    if delta < -0.03:
        # In reproductive phases, declining NDVI is expected — label it as such
        if phase in ("reproductive_early", "reproductive_late"):
            return "expected_decline", delta
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

def extract_field_data(field_dir, farm_root, data_root, current_crop=None, weather_cache=None, crop_type="corn"):
    field_json_path = field_dir / "field.json"
    field_meta = {}
    if field_json_path.exists():
        field_meta = json.loads(field_json_path.read_text())

    boundary = read_field_boundary(field_dir)
    cards = read_ndvi_card_summary(field_dir)
    yearly = read_ndvi_yearly_summary(field_dir)
    soil = read_soil_summary(field_dir)
    weather, grid_key = read_weather_records(field_dir)
    if weather_cache is not None and grid_key and grid_key not in weather_cache:
        weather_cache[grid_key] = weather
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

    cc = CROP_CONFIG.get(crop_type, CROP_CONFIG["corn"])

    # Compute growth phase from weather records (base-50°F GDD from planting date)
    growth_info = compute_growth_phase(weather, cc)
    phase = growth_info["phase"]

    risk = classify_risk(year_ndvi_series, cc, phase=phase, accumulated_gdd=growth_info["cum_gdd_f"])
    trend, trend_pct = compute_ndvi_trend(year_ndvi_series, phase=phase)

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

    # Management zones (v1): per-year k-means zones from a quality-gated scene.
    # Years with no passing scene are omitted; JS falls back to the solid boundary.
    zones = {}
    if boundary and boundary.get("geometry"):
        scene_years = sorted(set(s["date"][:4] for s in ndvi_series if len(s["date"]) >= 4))
        zones = compute_field_zones(field_dir, boundary["geometry"], scene_years, date.today().year, area_acres)

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
        "weather_series_id": grid_key or field_id,
        "cdl_crops": cdl_crops,
        "current_crop": current_crop,
        "zones": zones,
        # Growth-stage context (used by JS as initial seed; JS recomputes on filter changes)
        "current_phase": phase,
        "current_stage_label": growth_info["stage_label"],
        "stage_description": growth_info["stage_description"],
        "cum_gdd_f": growth_info["cum_gdd_f"],
    }
    return field_data

def build_summary(fields, crop_type="corn"):
    config = CROP_CONFIG.get(crop_type, CROP_CONFIG["corn"])
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

def _weather_to_columnar(records):
    """Convert array-of-objects weather records to columnar format.

    Before: [{"date": "2021-01-01", "T2M": -1.6, "T2M_MAX": 0.6, ...}, ...]
    After:  {"dates": ["2021-01-01", ...], "T2M": [-1.6, ...], "T2M_MAX": [0.6, ...]}
    """
    if not records:
        return {"dates": []}
    columnar: dict[str, list] = {"dates": []}
    sample = records[0]
    for k in sample:
        if k in ("field_id", "lat", "lon", "grid_key"):
            continue
        if k == "date":
            columnar["dates"] = [r["date"] for r in records]
        else:
            columnar[k] = [r.get(k) for r in records]
    return columnar


def extract_all_field_data(grower_root, data_root, crop_type="corn"):
    all_fields = []
    grower_name = grower_root.name
    weather_cache: dict[str, list[dict]] = {}

    for farm_root in farm_paths(grower_root):
        farm_json_path = farm_root / "farm.json"
        if farm_json_path.exists():
            farm_meta = json.loads(farm_json_path.read_text())
            grower_name = farm_meta.get("display_name", grower_name)
        rotation_map = read_crop_rotation(farm_root)
        for field_dir in field_paths(farm_root):
            fd = extract_field_data(
                field_dir, farm_root, data_root,
                current_crop=rotation_map.get(field_dir.name),
                weather_cache=weather_cache,
                crop_type=crop_type,
            )
            if fd["geometry"]:
                all_fields.append(fd)

    # Assign display names
    for i, fd in enumerate(all_fields, 1):
        fd["name"] = f"Field {i}"

    # Deduplicated columnar weather series keyed by grid_key
    weather_series = {
        gk: _weather_to_columnar(records)
        for gk, records in weather_cache.items()
    }

    return all_fields, grower_name, weather_series

# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def download_d3():
    local_custom = Path(__file__).parent / "d3-custom.min.js"
    if local_custom.exists():
        print(f"  Using local custom bundle ({local_custom.stat().st_size // 1024} KB)")
        return local_custom.read_text(encoding="utf-8")
    local_full = Path(__file__).parent / "d3.v7.min.js"
    if local_full.exists():
        print(f"  Using local full bundle ({local_full.stat().st_size // 1024} KB)")
        return local_full.read_text(encoding="utf-8")
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
    raise RuntimeError("Could not download D3 from any CDN and no local fallback found")



def build_html(data_json_str, d3_min_js, weather_series=None, crop_type="corn"):
    config = CROP_CONFIG.get(crop_type, CROP_CONFIG["corn"])
    data = json.loads(data_json_str)

    fields_json = json.dumps(data["fields"], default=str)
    config_json = json.dumps(config, default=str)
    threshold_labels_json = json.dumps(THRESHOLD_LABELS, default=str)
    crop_config_json = json.dumps(CROP_CONFIG, default=str)
    weather_series_json = json.dumps(weather_series or {}, default=str)

    grower_name = data['summary'].get('grower_name', 'Grower')
    total_fields = data['summary']['total_fields']
    generated_at = data['summary']['generated_at']

    template = HTML_TEMPLATE
    template = template.replace("__D3_MIN_JS__", d3_min_js)
    template = template.replace("__GROWER_NAME__", grower_name)
    template = template.replace("__TOTAL_FIELDS__", str(total_fields))
    template = template.replace("__GENERATED_AT__", generated_at)
    template = template.replace("__FIELDS_JSON__", fields_json)
    template = template.replace("__CONFIG_JSON__", config_json)
    template = template.replace("__THRESHOLD_LABELS_JSON__", threshold_labels_json)
    template = template.replace("__CROP_CONFIG_JSON__", crop_config_json)
    template = template.replace("__CROP_TYPE__", crop_type)
    template = template.replace("__CROP_NAME__", config.get("crop_name", "Crop"))
    template = template.replace("__WEATHER_SERIES_JSON__", weather_series_json)
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
.header .freshness { font-size: 0.75rem; color: #8899aa; }
.source-lag { font-size: 0.7rem; color: #99aabb; font-style: italic; }
.header-subrow { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.header .header-filters { display: flex; gap: 14px; align-items: flex-start; flex-shrink: 0; }
.header .header-filters .filter-group { display: flex; flex-direction: column; gap: 3px; }
.header .header-filters .filter-group label { font-size: 0.7rem; font-weight: 600; color: #b8d4e8; text-transform: uppercase; letter-spacing: 0.04em; }
.header .header-filters select { padding: 4px 8px; border: none; border-radius: 4px; font-size: 0.75rem; background: #1a2e4a; color: #e0e8f0; min-width: 130px; }
#field-filter-indicator { font-size: 0.75rem; white-space: nowrap; line-height: 1.5; }
#field-filter-indicator.no-filter { background: none; border: none; padding: 0; color: rgba(255,255,255,0.85); font-weight: 500; cursor: default; }
#field-filter-indicator.has-filter { display: inline-flex; align-items: center; gap: 6px; background: rgba(245, 215, 110, 0.15); border: 1px solid rgba(245, 215, 110, 0.4); border-radius: 999px; padding: 0 5px 0 10px; color: #fff; cursor: default; }
#field-filter-indicator .clear-field-filter { cursor: pointer; opacity: 0.7; padding: 2px 4px; border-radius: 50%; }
#field-filter-indicator .clear-field-filter:hover { opacity: 1; background: rgba(255,255,255,0.15); }
.header .grower-name { color: #F5D76E; }
.header-legend-toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; font-size: 0.8rem; color: #b8d4e8; text-decoration: none; user-select: none; }
.header-legend-toggle:hover { color: #fff; }
.header-legend-toggle .chevron { display: inline-block; transition: transform 0.25s; font-size: 0.7rem; }
.header-legend-toggle .chevron.open { transform: rotate(90deg); }
.header-legend-content { max-height: 0; overflow: hidden; transition: max-height 0.3s ease-in-out, padding 0.3s ease-in-out; padding: 0 0; }
.header-legend-content.open { max-height: 340px; padding: 10px 0 4px 0; }
.header-legend-body { display: flex; gap: 40px; border-top: 1px solid rgba(255,255,255,0.15); padding-top: 10px; }
.header-legend-body > div { flex: 1; }
.header-legend-body h4 { font-size: 0.72rem; font-weight: 600; color: #8899aa; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 5px; }
.header-legend-body .legend-column .legend-item { display: flex; align-items: center; gap: 6px; font-size: 0.78rem; color: #c8d8e8; margin-bottom: 3px; white-space: nowrap; }
.header-legend-body .legend-column .legend-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.header-legend-body .sources-column { font-size: 0.78rem; color: #c8d8e8; }
.header-legend-body .sources-column div { margin-bottom: 2px; }
.header-legend-body .sources-column .method-label { color: #8899aa; }

.header-legend-hint { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.15); font-size: 0.85rem; color: rgba(255,255,255,0.75); }

.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kpi-card { background: #fff; border-radius: 8px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.kpi-card .kpi-label { font-size: 0.75rem; font-weight: 600; color: #777; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-card .kpi-value { font-size: 1.6rem; font-weight: 700; margin-top: 4px; }
.kpi-card .kpi-unit { font-size: 0.8rem; color: #888; }
.kpi-card .kpi-trend { font-size: 0.8rem; margin-top: 2px; }
.kpi-card.critical { border-left: 4px solid #D95F4A; }
.kpi-card.watch { border-left: 4px solid #E8A838; }
.kpi-card.healthy { border-left: 4px solid #4A7FB5; }
.kpi-card.headline.critical { background: #fef0ef; }
.kpi-card.headline.watch { background: #fef8ed; }
.kpi-card.headline.healthy { background: #f0f5fa; }

.chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); gap: 16px; margin-bottom: 16px; }
.chart-card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.chart-card h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.chart-card .chart-container { width: 100%; height: 300px; position: relative; }
.chart-card .chart-container svg { width: 100%; height: 100%; }
.chart-full { grid-column: 1 / -1; }

.map-card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }
.map-card h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.map-card h3 .map-legend, .chart-card h3 .map-legend { float: right; font-size: 0.7rem; font-weight: 400; display: flex; gap: 12px; }
.map-card h3 .map-legend .legend-item, .chart-card h3 .map-legend .legend-item { display: inline-flex; align-items: center; gap: 4px; }
.map-card h3 .map-legend .legend-swatch, .chart-card h3 .map-legend .legend-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
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
.action-item .risk-badge { display: inline-block; flex: 0 0 115px; text-align: center; padding: 2px 8px; border-radius: 3px; font-size: 0.7rem; font-weight: 700; color: #fff; white-space: nowrap; }
.action-item .risk-text { flex: 1; font-size: 0.85rem; }

.narrative { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }
.narrative h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 10px; color: #333; }
.narrative p { font-size: 0.85rem; margin-bottom: 8px; color: #444; line-height: 1.6; }

.footer { background: linear-gradient(135deg, #1e3a5f, #2a5a7f); color: #c8d8e8; font-size: 0.8rem; border-radius: 8px; padding: 14px 24px; display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.footer a { color: #F5D76E; text-decoration: none; }
.footer a:hover { text-decoration: underline; }
.footer .footer-disclaimer { font-size: 0.72rem; color: #8899aa; text-align: center; max-width: 500px; }

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
  .header .header-subrow { flex-direction: column; align-items: flex-start; gap: 8px; }
  .header-legend-body { flex-direction: column; gap: 20px; }
  .header-legend-content.open { max-height: 340px; }
  .footer { flex-direction: column; text-align: center; gap: 8px; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-main">
      <div>
        <h1><span class="grower-name">__GROWER_NAME__</span> - __CROP_NAME__ Health Intelligence Dashboard</h1>
      </div>
      <div class="header-filters">
        <div class="filter-group">
          <label>Selected Field(s):</label>
          <div id="field-filter-indicator">All __CROP_NAME__ Fields</div>
        </div>
        <div class="filter-group">
          <label>Year:</label>
          <select id="year-select"></select>
        </div>
      </div>
    </div>
    <div class="header-subrow">
      <div class="freshness">Generated: __GENERATED_AT__</div>
      <a class="header-legend-toggle" onclick="toggleLegend()">
        Dashboard Legend <span class="chevron" id="legend-chevron">&#9654;</span>
      </a>
    </div>
    <div class="header-legend-content" id="legend-content">
      <div class="header-legend-body">
        <div class="legend-column">
          <h4 id="legend-tiers-title">Risk Tiers</h4>
          <div id="legend-risk-tiers"></div>
        </div>
        <div class="sources-column">
          <h4>Sources</h4>
          <div><span class="method-label">NDVI:</span> mean per-scene from Sentinel-2/Landsat 8-9 <span class="source-lag">(dashed lines &equals; &ge;30-day scene gap)</span></div>
          <div><span class="method-label">GDD:</span> base 50&deg;F from daily Tmin/Tmax</div>
          <div><span class="method-label">Soil:</span> NRCS SSURGO (AWS, OM%)</div>
          <div><span class="method-label">Weather:</span> NASA POWER daily <span class="source-lag">(~2mo lag)</span></div>
        </div>
      </div>
      <div class="header-legend-hint"><em><b>Click any data point on a chart for details.</b> On the map, clicking a field filters the whole dashboard instead.</em></div>
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
      <h3 id="ndvi-declining-title">NDVI<span class="map-legend" id="ndvi-legend"></span></h3>
      <div class="chart-container" id="ndvi-time-series"></div>
    </div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3 id="field-ranking-title">Field Ranking by Current NDVI</h3>
      <div class="chart-container" id="field-ranking"></div>
    </div>
    <div class="chart-card">
      <h3 id="scatter-title">NDVI vs. Available Water Storage</h3>
      <div class="chart-container" id="ndvi-vs-awc"></div>
    </div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <h3>GDD Accumulation: Actual vs. Target<span class="map-legend" id="gdd-legend"></span></h3>
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
const CONFIG = CROP_CONFIG.__CROP_TYPE__;
const THRESHOLD_LABELS = __THRESHOLD_LABELS_JSON__;
const ZONE_COLORS = { low: "#C96A2B", medium: "#E3C04A", high: "#4E9A6A" };
const ZONE_LABELS = { low: "Low NDVI", medium: "Medium NDVI", high: "High NDVI" };
// Reference mode relabels the same three palette colors by season-stress tier
const SEASON_TIER_LABELS = { critical: "High Stress", watch: "Moderate Stress", healthy: "Strong Season" };
const ALL_FIELDS = __FIELDS_JSON__;
const WEATHER_SERIES = __WEATHER_SERIES_JSON__;

// ===== GEOMETRY NORMALIZATION =====
// CDL-derived boundaries are wound counterclockwise per RFC 7946, but the bundled
// d3 build (geoMercator + geoPath only) renders CCW rings as the globe-covering
// complement, collapsing every field to the same full-viewport square. Re-orient
// exterior rings to CW (negative shoelace) and holes to CCW so d3 draws the small
// polygon interior. Uses a translated shoelace to avoid float cancellation on tiny
// (~1 acre) parcels.
function normalizeFieldGeometry(geom) {
  if (!geom) return geom;
  function ringArea(ring) {
    var n = ring.length - 1;
    if (n < 3) return 0;
    var cx = 0, cy = 0;
    for (var i = 0; i < n; i++) { cx += ring[i][0]; cy += ring[i][1]; }
    cx /= n; cy /= n;
    var a = 0;
    for (var i = 0; i < n; i++) {
      var p = ring[i], q = ring[(i + 1) % n];
      a += (p[0] - cx) * (q[1] - cy) - (q[0] - cx) * (p[1] - cy);
    }
    return a;
  }
  function fixRing(ring, wantCCW) {
    var isCCW = ringArea(ring) > 0;
    return (isCCW !== wantCCW) ? ring.slice().reverse() : ring;
  }
  function fixPolygon(coordinates) {
    return coordinates.map(function(ring, idx) { return fixRing(ring, idx > 0); });
  }
  if (geom.type === 'Polygon') return { type: 'Polygon', coordinates: fixPolygon(geom.coordinates) };
  if (geom.type === 'MultiPolygon') return { type: 'MultiPolygon', coordinates: geom.coordinates.map(fixPolygon) };
  return geom;
}
ALL_FIELDS.forEach(function(f) {
  if (f.geometry && f.geometry.geometry) f.geometry.geometry = normalizeFieldGeometry(f.geometry.geometry);
});

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

/**
 * phaseAndStageFromGDD
 * Maps an accumulated GDD value (base 50°F) to a growth phase and a human-readable
 * stage label. Shared by per-field classification and the grower-wide average.
 *
 * Phase enum:
 *   "establishing"       – 0 → VE (120 GDD): bare soil / emergence, NDVI not diagnostic
 *   "building"           – VE → R1 (120–1400 GDD): canopy building, full flagging active
 *   "reproductive_early" – R1 → R4 (1400–2150 GDD): absolute NDVI floors only, decline suppressed
 *   "reproductive_late"  – R4+ (2150+ GDD): universal senescence, all flagging suppressed
 */
function phaseAndStageFromGDD(cumGDD, config) {
  var stages = config.growth_stages || {};
  var anchors = config.phase_anchors || {};
  var descriptions = config.stage_descriptions || {};
  function anchorGDD(key, fallbackName, fallbackGDD) {
    var name = anchors[key] || fallbackName;
    return stages[name] != null ? stages[name] : (stages[fallbackName] != null ? stages[fallbackName] : fallbackGDD);
  }
  var VE_GDD = anchorGDD('establishing_end', 'VE', 120);
  var R1_GDD = anchorGDD('building_end', 'R1', 1400);
  var R4_GDD = anchorGDD('reproductive_early_end', 'R4', 2150);

  var orderedStages = Object.keys(stages).length
    ? Object.keys(stages).map(function(k) { return [k, stages[k]]; }).sort(function(a, b) { return a[1] - b[1]; })
    : [
        ['VE', 120], ['V6', 500], ['VT', 1130], ['R1', 1400],
        ['R2', 1650], ['R3', 1880], ['R4', 2150], ['R5', 2450], ['R6', 2700],
      ];
  var defaultDescMap = {
    VE: 'Early Vegetative', V6: 'Vegetative', VT: 'Tasseling',
    R1: 'Silking', R2: 'Blister', R3: 'Milk',
    R4: 'Dough',  R5: 'Dent',    R6: 'Maturing',
  };
  var stageDescMap = Object.assign({}, defaultDescMap, descriptions);

  // --- Determine phase ---
  var phase;
  if (cumGDD < VE_GDD)       phase = 'establishing';
  else if (cumGDD < R1_GDD)  phase = 'building';
  else if (cumGDD < R4_GDD)  phase = 'reproductive_early';
  else                        phase = 'reproductive_late';

  // --- Stage label: adjacent bracket ---
  var firstName = orderedStages[0][0];
  var lastName = orderedStages[orderedStages.length - 1][0];
  var stageLabel = 'Planting\u2013' + firstName;
  var stageDescription = stageDescMap[firstName] || 'Emergence';
  for (var k = 0; k < orderedStages.length; k++) {
    if (cumGDD < orderedStages[k][1]) {
      if (k === 0) {
        stageLabel = 'Planting\u2013' + orderedStages[0][0];
        stageDescription = stageDescMap[orderedStages[0][0]] || 'Emergence';
      } else {
        var prevName = orderedStages[k - 1][0];
        var curName  = orderedStages[k][0];
        stageLabel = prevName + '\u2013' + curName;
        stageDescription = stageDescMap[prevName] || prevName;
      }
      break;
    }
    if (k === orderedStages.length - 1) {
      stageLabel = lastName + '+';
      stageDescription = stageDescMap[lastName] || 'Maturing';
    }
  }

  return { phase: phase, stageLabel: stageLabel, stageDescription: stageDescription };
}

// Representative field for grower-wide chart annotations: the weather grid shared by
// the most filtered fields (ties fall back to field order). Collapses to the single
// selected field when a field filter is active.
function representativeField(ff) {
  if (!ff.length) return null;
  var counts = {};
  ff.forEach(function(f) {
    var g = f.weather_series_id;
    counts[g] = (counts[g] || 0) + 1;
  });
  var best = ff[0], bestCount = -1;
  ff.forEach(function(f) {
    if (counts[f.weather_series_id] > bestCount) {
      best = f;
      bestCount = counts[f.weather_series_id];
    }
  });
  return best;
}

// Grower-wide growth-stage aggregate: average each field's own accumulated GDD, then
// derive phase/label from the average. Used only for whole-dashboard display values
// (KPI header, chart title); per-field classification always uses the field's own phase.
function aggregatePhaseInfo(ff) {
  var gddSum = 0, n = 0;
  ff.forEach(function(f) {
    if (f.cum_gdd_f != null) { gddSum += f.cum_gdd_f; n++; }
  });
  if (!n) return { phase: 'building', stageLabel: '', stageDescription: '' };
  var avgGDD = Math.round(gddSum / n * 10) / 10;
  return phaseAndStageFromGDD(avgGDD, CONFIG);
}

/**
 * Compute accumulated GDD (base 50°F) and current growth stage for a field's own
 * weather series. Fields span multiple weather grid cells, so callers must pass the
 * field's own weather data — never another field's series.
 */
function computeCurrentGDDFromWeather(weatherData, displayYear, config) {
  var gddBaseF = config.gdd_base_temp_f || 50.0;

  // Guard: empty weather
  if (!weatherData || !weatherData.dates || !weatherData.dates.length) {
    return { phase: 'building', cumGDD: 0, stageLabel: 'Unknown', stageDescription: 'Unknown', plantingDate: null };
  }

  // --- Determine planting date (last spring frost before July 1, else Apr 20) ---
  var plantingDate = getPlantingDate(weatherData, displayYear);

  // --- Accumulate GDD in °F from planting date ---
  var cumGDD = 0;
  for (var j = 0; j < weatherData.dates.length; j++) {
    var dj = new Date(weatherData.dates[j]);
    if (dj < plantingDate) continue;
    var tmx = +weatherData.T2M_MAX[j], tmn = +weatherData.T2M_MIN[j];
    if (!isNaN(tmx) && !isNaN(tmn)) {
      var avgF = (tmn + tmx) / 2 * 9 / 5 + 32;
      cumGDD += Math.max(0, avgF - gddBaseF);
    }
  }
  cumGDD = Math.round(cumGDD * 10) / 10;

  // --- Determine phase and stage label from accumulated GDD ---
  var ps = phaseAndStageFromGDD(cumGDD, config);

  return { phase: ps.phase, cumGDD: cumGDD, stageLabel: ps.stageLabel, stageDescription: ps.stageDescription, plantingDate: plantingDate };
}

/**
 * classifyRisk — growth-stage-aware NDVI risk tier.
 *
 * phase behavior:
 *   establishing      → always 'healthy' (bare soil, not diagnostic)
 *   building          → full flagging: absolute floors + decline-based promotions
 *   reproductive_early → absolute floors only (0.50 / 0.70); decline flags suppressed
 *   reproductive_late  → always 'healthy' (universal senescence, not diagnostic)
 */
function healthyThresholdForStage(accumulatedGDD, config) {
  var stages = config.growth_stages;
  var anchors = config.phase_anchors || {};
  var fullCanopy = anchors.full_canopy || 'VT';
  var fullGDD = stages[fullCanopy] != null ? stages[fullCanopy] : (stages.VT != null ? stages.VT : 1130);
  var fullThreshold = config.watch_threshold;
  if (accumulatedGDD >= fullGDD) {
    return fullThreshold;
  }
  var progress = fullGDD > 0 ? accumulatedGDD / fullGDD : 1;
  return fullThreshold * Math.max(progress, 0.3);
}

function classifyRisk(ndviSeries, config, phase, accumulatedGDD) {
  if (!ndviSeries || !ndviSeries.length) return 'unknown';
  phase = phase || 'building';
  accumulatedGDD = accumulatedGDD || 0;

  // Phases where NDVI number is not diagnostic
  if (phase === 'establishing' || phase === 'reproductive_late') return 'healthy';

  var latest = ndviSeries[ndviSeries.length - 1].value;

  // Absolute NDVI floor — active in building and reproductive_early
  if (latest < config.stress_threshold) return 'critical';
  var healthyThreshold = healthyThresholdForStage(accumulatedGDD, config);
  if (latest < healthyThreshold)  return 'watch';

  // Decline-based promotions — building phase only
  if (phase === 'building' && ndviSeries.length >= 3) {
    var recent = ndviSeries.slice(-3);
    var firstVal = recent[0].value, latestVal = recent[recent.length - 1].value;
    var delta = latestVal - firstVal;
    if (delta <= -0.10) return 'critical';
    if (delta <= -0.05) return 'watch';
  }
  return 'healthy';
}

/**
 * computeNDVITrend — growth-stage-aware trend direction.
 *
 * Returns { trend, pct } where trend is one of:
 *   'improving' | 'stable' | 'declining' | 'expected_decline'
 *
 * 'expected_decline' = crop is declining but it is biologically normal for
 * the current phase (reproductive). Consumers that check === 'declining'
 * will NOT count expected_decline as a stress signal.
 */
function computeNDVITrend(ndviSeries, phase) {
  phase = phase || 'building';
  if (ndviSeries.length < 3) return { trend: 'stable', pct: 0 };
  var recent = ndviSeries.slice(-3);
  var first = recent[0].value, last = recent[recent.length - 1].value;
  var delta = +(last - first).toFixed(3);

  // Establishing: too noisy, report stable
  if (phase === 'establishing') return { trend: 'stable', pct: 0 };

  if (delta > 0.03) return { trend: 'improving', pct: delta };

  if (delta < -0.03) {
    // Reproductive phases: decline is expected — use distinct label
    if (phase === 'reproductive_early' || phase === 'reproductive_late') {
      return { trend: 'expected_decline', pct: delta };
    }
    return { trend: 'declining', pct: delta };
  }
  return { trend: 'stable', pct: delta };
}

function computeWeatherSummaries(weatherRecords, config) {
  if (!weatherRecords || !weatherRecords.dates || !weatherRecords.dates.length) return { gdd_accumulated: 0, days_since_significant_rain: null };
  var displayYear = weatherRecords.dates[0].slice(0, 4);
  var plantingDate = getPlantingDate(weatherRecords, displayYear);
  var significantMm = config.precip_significant_mm || 2.54;
  var gddTotal = 0;
  var precipTotal = 0;
  var lastRainIdx = -1;
  for (var i = 0; i < weatherRecords.dates.length; i++) {
    if (plantingDate && new Date(weatherRecords.dates[i]) < plantingDate) continue;
    var tmax = +weatherRecords.T2M_MAX[i], tmin = +weatherRecords.T2M_MIN[i];
    if (tmax != null && tmin != null && !isNaN(tmax) && !isNaN(tmin)) {
      gddTotal += Math.max(0, ((tmax + tmin) / 2 * 9 / 5 + 32) - (config.gdd_base_temp_f || 50));
    }
    var p = +weatherRecords.PRECTOTCORR[i];
    if (!isNaN(p)) {
      precipTotal += p;
      if (p >= significantMm) lastRainIdx = i;
    }
  }
  var daysSince = null;
  if (lastRainIdx >= 0) {
    var lastRainDate = new Date(weatherRecords.dates[lastRainIdx]);
    var now = new Date();
    var utcNow = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    var utcRain = Date.UTC(lastRainDate.getFullYear(), lastRainDate.getMonth(), lastRainDate.getDate());
    daysSince = Math.floor((utcNow - utcRain) / 86400000);
  }
  return { gdd_accumulated: Math.round(gddTotal), days_since_significant_rain: daysSince, total_precip_mm: Math.round(precipTotal) };
}

function getPlantingDate(weatherData, displayYear) {
  if (!weatherData || !weatherData.dates || !weatherData.dates.length) return null;
  var frostThresholdC = 0.0;
  var defaultPlanting = new Date(displayYear + '-04-20');
  var lastFrostDate = null;
  for (var i = 0; i < weatherData.dates.length; i++) {
    var tminVal = +weatherData.T2M_MIN[i];
    if (!isNaN(tminVal) && tminVal <= frostThresholdC) {
      var dObj = new Date(weatherData.dates[i]);
      var doy  = Math.floor((dObj - new Date(dObj.getFullYear(), 0, 0)) / 86400000);
      if (doy <= 182) {
        if (!lastFrostDate || dObj > lastFrostDate) lastFrostDate = dObj;
      }
    }
  }
  return (lastFrostDate && lastFrostDate > defaultPlanting) ? lastFrostDate : defaultPlanting;
}

function getChartDateExtent(ff, year) {
  var isCurrent = year === String(new Date().getFullYear());
  var allDates = [];
  ff.forEach(function(f) {
    (f.ndvi_series || []).forEach(function(p) { allDates.push(new Date(p.date)); });
  });
  if (!allDates.length) {
    return [new Date(year + '-01-01'), isCurrent ? new Date() : new Date(year + '-12-31')];
  }
  var minD = new Date(Math.min.apply(null, allDates));
  var maxD = new Date(Math.max.apply(null, allDates));
  minD.setDate(minD.getDate() - 7);
  maxD.setDate(maxD.getDate() + 7);
  var upperBound = isCurrent ? new Date() : new Date(year + '-12-31');
  if (maxD > upperBound) maxD = upperBound;
  return [minD, maxD];
}

function buildDailyGDDLookup(weatherData, config) {
  var lookup = {};
  if (!weatherData || !weatherData.dates || !weatherData.dates.length) return lookup;
  var displayYear = weatherData.dates[0].slice(0, 4);
  var plantingDate = getPlantingDate(weatherData, displayYear);
  var gddBaseF = config.gdd_base_temp_f || 50.0;
  var cumGDD = 0;
  for (var j = 0; j < weatherData.dates.length; j++) {
    var dj = new Date(weatherData.dates[j]);
    if (plantingDate && dj < plantingDate) {
      lookup[weatherData.dates[j]] = 0;
      continue;
    }
    var tmx = +weatherData.T2M_MAX[j], tmn = +weatherData.T2M_MIN[j];
    if (!isNaN(tmx) && !isNaN(tmn)) {
      var avgF = (tmn + tmx) / 2 * 9 / 5 + 32;
      cumGDD += Math.max(0, avgF - gddBaseF);
    }
    lookup[weatherData.dates[j]] = Math.round(cumGDD * 10) / 10;
  }
  return lookup;
}

// Season-stress tier — single source of truth for reference-mode map colors and
// Season Recap badges. Thresholds match the existing badge logic (>30% High,
// >=10% Moderate, else Strong).
function seasonStressTier(stressDays, seasonLengthDays) {
  var pct = seasonLengthDays > 0 ? stressDays / seasonLengthDays : 0;
  if (pct > 0.30) return 'critical';
  if (pct >= 0.10) return 'watch';
  return 'healthy';
}

// Which tier colors a field: classifyRisk in actionable mode, season-stress tier in
// reference mode. Used by the map, Field Ranking, scatter, and Soil OM chart so a
// field's color agrees everywhere on the page.
function fieldTierKey(field, isCurrent) {
  if (isCurrent) return field.current_risk || 'healthy';
  if (field.season_stress_days != null) return seasonStressTier(field.season_stress_days, field.season_length_days);
  return 'healthy';
}

function tierTooltipLine(field, isCurrent) {
  var tk = fieldTierKey(field, isCurrent);
  return isCurrent
    ? 'Risk: ' + (THRESHOLD_LABELS[tk]?.label || tk)
    : 'Season: ' + (SEASON_TIER_LABELS[tk] || tk);
}

// Per-field timeseries line colors — deliberately free of blue/amber/red so lines
// can't be confused with tier colors or the Watch/Stress reference lines.
const FIELD_LINE_COLORS = [
  '#7C3AED', // violet
  '#0D9488', // teal
  '#DB2777', // magenta
  '#65A30D', // olive green
  '#78716C', // warm gray
  '#4C1D95', // deep indigo
  '#059669', // jade green
  '#F472B6', // rose pink
  '#2DD4BF', // seafoam
  '#44403C', // charcoal
];

function computeStressDuration(ndviSeries, config, dailyGDDLookup) {
  if (!ndviSeries || ndviSeries.length < 2) return 0;
  var stages = config.growth_stages;
  var anchors = config.phase_anchors || {};
  function anchorGDD(key, fallbackName, fallbackGDD) {
    var name = anchors[key] || fallbackName;
    return stages[name] != null ? stages[name] : (stages[fallbackName] != null ? stages[fallbackName] : fallbackGDD);
  }
  var VE_GDD = anchorGDD('establishing_end', 'VE', 120);
  var R4_GDD = anchorGDD('reproductive_early_end', 'R4', 2150);
  var total = 0, inStress = false, start = null;
  dailyGDDLookup = dailyGDDLookup || {};

  function daysBetween(a, b) {
    return Math.round((new Date(b) - new Date(a)) / 86400000);
  }

  // Phase gating mirrors classifyRisk(): establishing and reproductive_late
  // are not diagnostic, so they neither start nor accumulate stress days.
  function phaseForGDD(gdd) {
    if (gdd == null) return 'building'; // fallback if a date has no matching weather
    if (gdd < VE_GDD) return 'establishing';
    if (gdd < R4_GDD) return 'active'; // building or reproductive_early — both counted
    return 'reproductive_late';
  }

  function closeOutIfOpen(endDate) {
    if (inStress) {
      total += daysBetween(start, endDate);
      inStress = false;
    }
  }

  ndviSeries.forEach(function(d) {
    var gdd = dailyGDDLookup[d.date] != null ? dailyGDDLookup[d.date] : null;
    var phase = phaseForGDD(gdd);

    if (phase !== 'active') {
      closeOutIfOpen(d.date); // suppressed phase — close any open window, don't count this day
      return;
    }

    var threshold = healthyThresholdForStage(gdd, config); // scaled pre-VT, flat 0.7 from VT on
    var stressed = d.value < threshold;
    if (stressed && !inStress) { start = d.date; inStress = true; }
    else if (!stressed && inStress) { closeOutIfOpen(d.date); }
  });

  closeOutIfOpen(ndviSeries[ndviSeries.length - 1].date);
  return total;
}

function computeDateStageMap(weatherData, config) {
  if (!weatherData || !weatherData.dates || !weatherData.dates.length) return null;
  var stages = config.growth_stages || {};
  var descriptions = config.stage_descriptions || {};
  var gddBaseF = config.gdd_base_temp_f || 50.0;
  var orderedStages = Object.keys(stages).length
    ? Object.keys(stages).map(function(k) { return [k, stages[k]]; }).sort(function(a, b) { return a[1] - b[1]; })
    : [
        ['VE', 120], ['V6', 500], ['VT', 1130], ['R1', 1400],
        ['R2', 1650], ['R3', 1880], ['R4', 2150], ['R5', 2450], ['R6', 2700],
      ];
  var defaultDescMap = {
    VE: 'Early Vegetative', V6: 'Vegetative', VT: 'Tasseling',
    R1: 'Silking', R2: 'Blister', R3: 'Milk',
    R4: 'Dough',  R5: 'Dent',    R6: 'Maturing',
  };
  var stageDescMap = Object.assign({}, defaultDescMap, descriptions);
  var displayYear = weatherData.dates[0].slice(0, 4);
  var plantingDate = getPlantingDate(weatherData, displayYear);
  if (!plantingDate) return null;

  var dateStageMap = [];
  var cumGDD = 0;
  var firstName = orderedStages[0][0];
  var lastName = orderedStages[orderedStages.length - 1][0];
  for (var j = 0; j < weatherData.dates.length; j++) {
    var dj = new Date(weatherData.dates[j]);
    if (dj < plantingDate) continue;
    var tmx = +weatherData.T2M_MAX[j], tmn = +weatherData.T2M_MIN[j];
    if (!isNaN(tmx) && !isNaN(tmn)) {
      var avgF = (tmn + tmx) / 2 * 9 / 5 + 32;
      cumGDD += Math.max(0, avgF - gddBaseF);
    }
    cumGDD = Math.round(cumGDD * 10) / 10;

    var stageLabel = 'Pre-' + firstName, stageDescription = stageDescMap[firstName] || 'Emergence';
    for (var k = 0; k < orderedStages.length; k++) {
      if (cumGDD < orderedStages[k][1]) {
        if (k > 0) {
          stageLabel = orderedStages[k - 1][0] + '-' + orderedStages[k][0];
          stageDescription = stageDescMap[orderedStages[k - 1][0]] || orderedStages[k - 1][0];
        }
        break;
      }
      if (k === orderedStages.length - 1) {
        stageLabel = lastName + '+';
        stageDescription = stageDescMap[lastName] || 'Maturing';
      }
    }
    dateStageMap.push({ date: weatherData.dates[j], stageLabel: stageLabel, stageDescription: stageDescription });
  }
  return { dateStageMap: dateStageMap, plantingDate: plantingDate };
}

function countConsecutiveDryDays(weatherData, startDate, endDate, thresholdMm) {
  if (!weatherData || !weatherData.dates) return 0;
  var start = new Date(startDate), end = new Date(endDate);
  var maxDry = 0, curDry = 0;
  for (var i = 0; i < weatherData.dates.length; i++) {
    var d = new Date(weatherData.dates[i]);
    if (d >= start && d <= end) {
      var p = +weatherData.PRECTOTCORR[i];
      if (isNaN(p) || p < thresholdMm) {
        curDry++;
        if (curDry > maxDry) maxDry = curDry;
      } else {
        curDry = 0;
      }
    }
  }
  return maxDry;
}

function hasTemperatureExtreme(weatherData, startDate, endDate) {
  if (!weatherData || !weatherData.dates) return false;
  var start = new Date(startDate), end = new Date(endDate);
  for (var i = 0; i < weatherData.dates.length; i++) {
    var d = new Date(weatherData.dates[i]);
    if (d >= start && d <= end) {
      var tmax = +weatherData.T2M_MAX[i];
      var tmin = +weatherData.T2M_MIN[i];
      if ((!isNaN(tmax) && tmax > 35) || (!isNaN(tmin) && tmin < 0)) return true;
    }
  }
  return false;
}

function getStageLabelForDates(dateStageMap, startDate, endDate) {
  if (!dateStageMap) return null;
  var start = new Date(startDate), end = new Date(endDate);
  var seen = [];
  for (var i = 0; i < dateStageMap.length; i++) {
    var d = new Date(dateStageMap[i].date);
    if (d >= start && d <= end) {
      if (seen.indexOf(dateStageMap[i].stageLabel) === -1) {
        seen.push(dateStageMap[i].stageLabel);
      }
    }
  }
  if (seen.length === 0) return null;
  if (seen.length === 1) return seen[0];
  return seen[0] + '\u2013' + seen[seen.length - 1];
}

function formatDateShort(dateStr) {
  var d = new Date(dateStr);
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
}

function detectNotableEvent(ndviSeries, weatherData, config, dateStageMap) {
  if (!ndviSeries || ndviSeries.length < 3 || !weatherData) return null;
  var significantMm = config.precip_significant_mm || 2.54;
  var stages = config.growth_stages || {};
  var vtR2StartGDD = stages.VT || 1130;
  var vtR2EndGDD = stages.R2 || 1650;

  var stageMapEntry = null;
  if (!dateStageMap) {
    var result = computeDateStageMap(weatherData, config);
    if (result) stageMapEntry = result.dateStageMap;
  } else {
    stageMapEntry = dateStageMap;
  }

  for (var i = 1; i < ndviSeries.length; i++) {
    var drop = ndviSeries[i - 1].value - ndviSeries[i].value;
    if (drop > 0.15) {
      var dropStart = ndviSeries[i - 1].date;
      var dropEnd = ndviSeries[i].date;
      var dryDays = countConsecutiveDryDays(weatherData, dropStart, dropEnd, significantMm);

      // Stage-sensitive threshold: the most drought-sensitive window
      var useThreshold = 14;
      var sensitiveStages = config.sensitive_stages || ['VT', 'R1', 'R2'];
      if (stageMapEntry) {
        var stageLabel = getStageLabelForDates(stageMapEntry, dropStart, dropEnd);
        if (stageLabel) {
          for (var s = 0; s < sensitiveStages.length; s++) {
            if (stageLabel.indexOf(sensitiveStages[s]) !== -1) {
              useThreshold = 10;
              break;
            }
          }
        }
      }

      if (dryDays >= useThreshold) {
        var stageText = '';
        if (stageMapEntry) {
          var sl = getStageLabelForDates(stageMapEntry, dropStart, dropEnd);
          if (sl) {
            for (var si = 0; si < stageMapEntry.length; si++) {
              if (stageMapEntry[si].stageLabel === sl) {
                stageText = ' during ' + sl + ' (' + stageMapEntry[si].stageDescription.toLowerCase() + ')';
                break;
              }
            }
          }
        }
        return 'Sharp NDVI drop ' + formatDateShort(dropStart) + '\u2013' + formatDateShort(dropEnd) + ', coinciding with a ' + dryDays + '-day dry spell' + stageText + '.';
      }

      if (hasTemperatureExtreme(weatherData, dropStart, dropEnd)) {
        var stageText2 = '';
        if (stageMapEntry) {
          var sl2 = getStageLabelForDates(stageMapEntry, dropStart, dropEnd);
          if (sl2) {
            stageText2 = ' during ' + sl2;
          }
        }
        return 'Sharp NDVI drop ' + formatDateShort(dropStart) + '\u2013' + formatDateShort(dropEnd) + ', coinciding with a temperature extreme' + stageText2 + '.';
      }
    }
  }
  return null;
}

// ===== STATE (pub/sub) =====
function isFieldCrop(field, year) {
  if (CONFIG.match_all) return true;
  var crop = field.cdl_crops && field.cdl_crops[year] && field.cdl_crops[year] !== 'Unknown'
    ? field.cdl_crops[year]
    : field.current_crop;
  return crop === CONFIG.crop_name;
}

const state = {
  fields: ALL_FIELDS,
  filters: { fieldIds: [], selectedYear: '2026' },
  _listeners: [],
  subscribe(fn) { this._listeners.push(fn); return () => { this._listeners = this._listeners.filter(l => l !== fn); }; },
  publish() { this._listeners.forEach(fn => fn()); },
  getFilteredWeather(field) {
    var data = WEATHER_SERIES[field.weather_series_id];
    if (!data || !data.dates) return {dates: [], T2M: [], T2M_MAX: [], T2M_MIN: [], PRECTOTCORR: [], ALLSKY_SFC_SW_DWN: [], RH2M: [], WS10M: []};
    if (this.filters.selectedYear) {
      var year = this.filters.selectedYear;
      var indices = [];
      for (var i = 0; i < data.dates.length; i++) {
        if (data.dates[i].startsWith(year)) indices.push(i);
      }
      var result = {};
      for (var key in data) {
        if (data.hasOwnProperty && data.hasOwnProperty(key)) {
          result[key] = indices.map(function(idx) { return data[key][idx]; });
        } else {
          result[key] = indices.map(function(idx) { return data[key][idx]; });
        }
      }
      return result;
    }
    return data;
  },
  getFilteredFields() {
    var ff = this.fields;
    var year = this.filters.selectedYear;
    ff = ff.filter(function(f) { return isFieldCrop(f, year); });
    if (this.filters.fieldIds.length > 0) {
      ff = ff.filter(function(f) { return this.filters.fieldIds.includes(f.id); }.bind(this));
    }
    var self = this;

    return ff.map(function(f) {
      var ndviSeries = self.getFilteredNDVISeries(f);
      var weatherData = self.getFilteredWeather(f);
      // Growth phase from the field's OWN weather grid (fields span multiple grid cells,
      // so a single shared phase misclassifies fields outside the first field's grid).
      var fieldPhaseInfo = computeCurrentGDDFromWeather(weatherData, year, CONFIG);
      var risk = classifyRisk(ndviSeries, CONFIG, fieldPhaseInfo.phase, fieldPhaseInfo.cumGDD);
      var trend = computeNDVITrend(ndviSeries, fieldPhaseInfo.phase);
      var lastNDVI = ndviSeries.length ? ndviSeries[ndviSeries.length - 1].value : null;
      var peakNDVI = ndviSeries.length ? d3.max(ndviSeries, function(d) { return d.value; }) : null;
      var weatherSumm = computeWeatherSummaries(weatherData, CONFIG);

      // Reference-mode season stress (phase-gated, scaled threshold) — computed once
      // here so the map, Season Recap badges, KPI, and scatter all read one value.
      var seasonStressDays = null;
      var seasonLengthDays = null;
      if (year !== String(new Date().getFullYear()) && ndviSeries.length >= 2) {
        seasonStressDays = computeStressDuration(ndviSeries, CONFIG, buildDailyGDDLookup(weatherData, CONFIG));
        seasonLengthDays = Math.round((new Date(ndviSeries[ndviSeries.length - 1].date) - new Date(ndviSeries[0].date)) / 86400000) || 90;
      }

      return Object.assign({}, f, {
        current_ndvi: lastNDVI,
        display_ndvi: year === String(new Date().getFullYear()) ? lastNDVI : peakNDVI,
        current_risk: risk,
        ndvi_trend: trend.trend,
        ndvi_trend_pct: trend.pct,
        weather_summary: Object.assign({}, f.weather_summary, weatherSumm),
        ndvi_series: ndviSeries,
        // Growth-stage context for KPI display and action list
        current_phase: fieldPhaseInfo.phase,
        current_stage_label: fieldPhaseInfo.stageLabel,
        stage_description: fieldPhaseInfo.stageDescription,
        cum_gdd_f: fieldPhaseInfo.cumGDD,
        // Reference-mode season stress (null in actionable mode)
        season_stress_days: seasonStressDays,
        season_length_days: seasonLengthDays,
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

  var cropFields = ALL_FIELDS.filter(function(f) { return isFieldCrop(f, year); });
  var validIds = cropFields.map(function(f) { return f.id; });
  if (selectedId && !validIds.includes(selectedId)) {
    state.filters.fieldIds = [];
    selectedId = null;
  }

  // Year dropdown: always full range, never dependent on field selection
  var allYears = Object.keys(ALL_FIELDS[0] && ALL_FIELDS[0].cdl_crops || {}).sort();
  var thisYear = String(new Date().getFullYear());
  var yearSelect = document.getElementById("year-select");
  yearSelect.innerHTML = allYears.map(function(y) {
    var sel = y === year;
    var label = y + (y === thisYear ? ' (Current)' : '');
    return '<option value="' + y + '"' + (sel ? ' selected' : '') + '>' + label + '</option>';
  }).join('');

  // Field filter indicator (read-only, no dropdown)
  var indicator = document.getElementById("field-filter-indicator");
  indicator.classList.toggle("has-filter", !!selectedId);
  indicator.classList.toggle("no-filter", !selectedId);
  if (selectedId) {
    var f = cropFields.find(function(fi) { return fi.id === selectedId; });
    indicator.innerHTML = (f ? f.name : 'Field') + ' <span class="clear-field-filter" title="Clear field filter">\u2715</span>';
  } else {
    indicator.textContent = 'All ' + CONFIG.crop_name + ' Fields';
  }

  state.publish();
}

// ===== TOOLTIP =====
const tooltip = d3.select("#tooltip");

function showTooltip(html, pageX, pageY) {
  tooltip.classed("visible", true).html(html);
  var rect = tooltip.node().getBoundingClientRect();
  var tw = rect.width, th = rect.height;
  var left = pageX + 12;
  var top = pageY - 28;
  if (left + tw > window.innerWidth - 10) left = pageX - tw - 12;
  if (top < 10) top = pageY + 12;
  tooltip.style("left", left + "px").style("top", top + "px");
}

function hideTooltip() {
  tooltip.classed("visible", false);
}

// ===== KPI RENDER =====
function renderKPIs() {
  const ff = state.getFilteredFields();
  const isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  const total = ff.length;

  const ndviVals = ff.filter(f => f.current_ndvi != null).map(f => f.current_ndvi);
  const avgNDVI = ndviVals.length ? (ndviVals.reduce((a,b) => a+b, 0) / ndviVals.length).toFixed(3) : '--';

  const improving = ff.filter(f => f.ndvi_trend === 'improving').length;
  // Use stage-aware 'declining' count (ndvi_trend === 'declining') — consistent with
  // the chart title and free of the raw-negative-delta false positives that the old
  // anyDeclining variable introduced. 'expected_decline' fields are NOT counted here.
  const declining = ff.filter(f => f.ndvi_trend === 'declining').length;

  // Aggregate growth-stage context — grower-wide average of each field's own GDD
  var aggPhase = aggregatePhaseInfo(ff);
  var currentPhase = aggPhase.phase;
  var stageLabel   = aggPhase.stageLabel;
  var stageDesc    = aggPhase.stageDescription;

  // NDVI tier: also gate by phase — in establishing/reproductive_late the avg NDVI
  // number is not diagnostic, so don't colour-code the KPI card by it.
  var ndviTier = 'healthy';
  if (currentPhase !== 'establishing' && currentPhase !== 'reproductive_late') {
    if (avgNDVI < CONFIG.stress_threshold) ndviTier = 'critical';
    else if (avgNDVI < CONFIG.watch_threshold) ndviTier = 'watch';
  }
  var ndviLabel = THRESHOLD_LABELS[ndviTier]?.label || 'Unknown';
  var ndviIconHtml = ndviTier === 'critical' ? ICONS.warning : ndviTier === 'watch' ? ICONS.alert : ICONS.check;
  var ndviTrendText = ndviLabel;
  if (declining > 0 || improving > 0) ndviTrendText += ' &middot; ' + declining + ' declining, ' + improving + ' improving';

  const gddVals = ff.map(f => f.weather_summary?.gdd_accumulated || 0);
  const avgGDD = gddVals.length ? Math.round(gddVals.reduce((a,b) => a+b, 0) / gddVals.length) : 0;

  var repField = representativeField(ff);
  var normalGDD = (repField && repField.weather_summary?.gdd_normal) || 0;
  var gddTrendLine = 'Target: ' + CONFIG.gdd_target + ' (maturity) &middot; Annual Avg: ' + normalGDD;
  var gddCardHtml =
    '<div class="kpi-card healthy">' +
      '<div class="kpi-label">' + ICONS.temp + ' GDD Accumulated (avg)</div>' +
      '<div class="kpi-value">' + avgGDD + ' <span class="kpi-unit">&deg;F-days</span></div>' +
      '<div class="kpi-trend">' + gddTrendLine + '</div>' +
    '</div>';

  if (isCurrent) {
    const critical = ff.filter(f => f.current_risk === 'critical').length;
    const watch = ff.filter(f => f.current_risk === 'watch').length;
    const attention = critical + watch;
    const riskClass = attention > 0 ? (critical > 0 ? 'critical' : 'watch') : 'healthy';

    const rainDays = ff.map(f => f.weather_summary?.days_since_significant_rain).filter(d => d != null);
    const maxRainDays = rainDays.length ? Math.max(...rainDays) : '--';

    d3.select("#kpi-row").html(
      '<div class="kpi-card headline ' + riskClass + '">' +
        '<div class="kpi-label">' + ICONS.warning + ' Fields Requiring Attention</div>' +
        '<div class="kpi-value">' + attention + ' / ' + total + ' <span class="kpi-unit">' + critical + ' critical &middot; ' + watch + ' watch</span></div>' +
        (stageLabel && stageDesc ? '<div class="kpi-trend">Growth Stage: ' + stageLabel + ' &middot; ' + stageDesc + '</div>' : '<div class="kpi-trend"></div>') +
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

    const stressVals = ff.map(function(f) { return f.season_stress_days || 0; });
    const avgStress = stressVals.length ? Math.round(stressVals.reduce(function(a,b) { return a+b; }, 0) / stressVals.length) : 0;

    d3.select("#kpi-row").html(
      '<div class="kpi-card headline ' + (avgStress > 14 ? 'watch' : 'healthy') + '">' +
        '<div class="kpi-label">' + ICONS.warning + ' Season Stress Duration (avg)</div>' +
        '<div class="kpi-value">' + avgStress + ' <span class="kpi-unit">days</span></div>' +
        '<div class="kpi-trend">Avg days below NDVI threshold (VE-R4)</div>' +
      '</div>' +
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
      '</div>'
    );
  }
}

function getWeatherGap(dailyData, chartStart, chartEnd) {
  var firstNaN = null, lastNaN = null;
  for (var i = 0; i < dailyData.dates.length; i++) {
    var d = new Date(dailyData.dates[i]);
    if (d < chartStart || d > chartEnd) continue;
    if (isNaN(+dailyData.T2M_MIN[i]) || isNaN(+dailyData.T2M_MAX[i])) {
      if (!firstNaN) firstNaN = d;
      lastNaN = d;
    }
  }
  if (firstNaN && lastNaN && (lastNaN - firstNaN) >= 2 * 86400000) {
    return { start: firstNaN, end: lastNaN };
  }
  return null;
}

// ===== NDVI TIME SERIES =====
function renderNDVITimeSeries() {
  const container = d3.select("#ndvi-time-series");
  container.html("");
  const ff = state.getFilteredFields();

  // Stage-aware chart title (grower-wide average stage)
  var aggPhase = aggregatePhaseInfo(ff);
  var currentPhase = aggPhase.phase;
  var stageLabel   = aggPhase.stageLabel;
  var ndviLegendSpan = '<span class="map-legend" id="ndvi-legend"></span>';
  var chartTitle;
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  if (!isCurrent) {
    chartTitle = 'NDVI Trajectory \u2014 ' + state.filters.selectedYear + ' Season' + ndviLegendSpan;
  } else if (currentPhase === 'establishing') {
    chartTitle = 'NDVI &middot; Emergence Phase (pre-canopy, flagging suppressed)' + ndviLegendSpan;
  } else if (currentPhase === 'building') {
    var decliningCount = ff.filter(function(f) { return f.ndvi_trend === 'declining'; }).length;
    chartTitle = 'NDVI Declining in ' + decliningCount + ' ' + (decliningCount === 1 ? 'Field' : 'Fields') + ndviLegendSpan;
  } else if (currentPhase === 'reproductive_early') {
    var belowFloor = ff.filter(function(f) { return f.current_risk === 'critical' || f.current_risk === 'watch'; }).length;
    chartTitle = 'NDVI &middot; Early Grain Fill (' + (stageLabel || 'R1\u2013R4') + ') &middot; ' + belowFloor + ' field' + (belowFloor !== 1 ? 's' : '') + ' below threshold' + ndviLegendSpan;
  } else {
    // reproductive_late
    chartTitle = 'NDVI &middot; Natural Senescence (' + (stageLabel || 'R4+') + ', flagging suppressed)' + ndviLegendSpan;
  }
  d3.select("#ndvi-declining-title").html(chartTitle);

  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 50, left: 60 };
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

  var lastNdviDate = d3.max(allPoints, function(p) { return new Date(p.date); });

  const year = state.filters.selectedYear;
  const xExtent = getChartDateExtent(ff, year);
  const yExtent = [0, 1];

  const xScale = d3.scaleTime().domain(xExtent).range([0, width]);
  const yScale = d3.scaleLinear().domain(yExtent).range([height, 0]);

  const colorScale = d3.scaleOrdinal(FIELD_LINE_COLORS).domain(ff.map(f => f.id));

  svg.append("line")
    .attr("x1", 0).attr("x2", width)
    .attr("y1", yScale(CONFIG.stress_threshold)).attr("y2", yScale(CONFIG.stress_threshold))
    .attr("stroke", "#D95F4A").attr("stroke-dasharray", "2,2").attr("stroke-width", 1.5)
    .append("title").text("Stress threshold: " + CONFIG.stress_threshold);

  svg.append("text")
    .attr("x", width).attr("y", yScale(CONFIG.stress_threshold) - 4)
    .attr("text-anchor", "end").attr("font-size", "10px").attr("fill", "#D95F4A")
    .text("Stress");



  // Growth stage annotations (vertical lines from cumulative GDD)
  var stageColors = Object.assign({"VE":"#4CAF50","V6":"#8BC34A","VT":"#FFC107","R1":"#FF9800","R2":"#FF5722","R3":"#795548","R4":"#9C27B0","R5":"#3F51B5","R6":"#607D8B"}, CONFIG.stage_colors || {});
  var gddBaseF = CONFIG.gdd_base_temp_f;
  var displayYear = state.filters.selectedYear;
  var chartStart = xScale.domain()[0], chartEnd = xScale.domain()[1];
  var weatherField = representativeField(ff);
  var dailyData = state.getFilteredWeather(weatherField);
  if (dailyData.dates.length > 0) {
    // Determine planting date from last spring frost, fallback to April 20
    var frostThresholdC = 0.0;
    var defaultPlanting = new Date(displayYear + "-04-20");
    var lastFrostDate = null;
    for (var i = 0; i < dailyData.dates.length; i++) {
      if (dailyData.T2M_MIN[i] <= frostThresholdC) {
        var dObj = new Date(dailyData.dates[i]);
        var startOfYear = new Date(dObj.getFullYear(), 0, 0);
        var doy = Math.floor((dObj - startOfYear) / 86400000);
        if (doy <= 182) {
          if (!lastFrostDate || dObj > lastFrostDate) {
            lastFrostDate = dObj;
          }
        }
      }
    }
    var plantingDate = lastFrostDate && lastFrostDate > defaultPlanting ? lastFrostDate : defaultPlanting;

    // Compute cumulative GDD from planting date (days before planting get 0)
    var cumGDD = 0;
    var cumGDDArr = [];
    for (var i = 0; i < dailyData.dates.length; i++) {
      var dObj = new Date(dailyData.dates[i]);
      if (dObj < plantingDate) {
        cumGDDArr.push(0);
      } else {
        var tmin = +dailyData.T2M_MIN[i], tmax = +dailyData.T2M_MAX[i];
        if (isNaN(tmin) || isNaN(tmax)) {
          cumGDDArr.push(cumGDD);
        } else {
          var avgF = (tmin + tmax) / 2 * 9 / 5 + 32;
          cumGDD += Math.max(0, avgF - gddBaseF);
          cumGDDArr.push(cumGDD);
        }
      }
    }

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

    // Stage annotations
    var stages = CONFIG.growth_stages || {};
    var stageKeys = Object.keys(stages);
    stageKeys.forEach(function(stage) {
      var threshold = stages[stage];
      for (var i = 0; i < cumGDDArr.length; i++) {
        if (cumGDDArr[i] >= threshold) {
          var evDate = new Date(dailyData.dates[i]);
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

    // Watch threshold path (growth-stage-adjusted)
    if (lastNdviDate) {
      var watchData = [];
      for (var i = 0; i < dailyData.dates.length; i++) {
        var dObj = new Date(dailyData.dates[i]);
        if (dObj < chartStart) continue;
        if (dObj > lastNdviDate) break;
        watchData.push({ date: dObj, value: healthyThresholdForStage(cumGDDArr[i], CONFIG) });
      }
      if (watchData.length > 0) {
        var watchLine = d3.line()
          .x(function(d) { return xScale(d.date); })
          .y(function(d) { return yScale(d.value); });
        svg.append("path")
          .datum(watchData)
          .attr("fill", "none")
          .attr("stroke", "#E8A838")
          .attr("stroke-dasharray", "2,2")
          .attr("stroke-width", 1)
          .attr("d", watchLine)
          .append("title").text("Watch threshold (growth-stage-adjusted)");
        var lastWatch = watchData[watchData.length - 1];
        svg.append("text")
          .attr("x", xScale(lastWatch.date))
          .attr("y", yScale(lastWatch.value) - 4)
          .attr("text-anchor", "end")
          .attr("font-size", "10px")
          .attr("fill", "#E8A838")
            .text("Watch");
      }
    }
  }

  var wgap = getWeatherGap(dailyData, chartStart, chartEnd);
  if (wgap) {
    var gx1 = xScale(wgap.start), gx2 = xScale(wgap.end);
    svg.append("rect")
      .attr("x", gx1).attr("y", 0)
      .attr("width", gx2 - gx1).attr("height", height)
      .attr("fill", "#888").attr("opacity", 0.12)
      .attr("pointer-events", "none");
    svg.append("text")
      .attr("x", (gx1 + gx2) / 2).attr("y", 10)
      .attr("text-anchor", "middle").attr("font-size", "8px")
      .attr("font-weight", "600").attr("fill", "#888")
      .text("Weather gap");
  }

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(6));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(8));

  svg.append("text").attr("class", "chart-title")
    .attr("x", -(height / 2)).attr("y", -(margin.left - 14))
    .attr("transform", "rotate(-90)").attr("text-anchor", "middle")
    .text("NDVI");

  const line = d3.line()
    .x(function(d) { return d ? xScale(new Date(d.date)) : null; })
    .y(function(d) { return d ? yScale(d.value) : null; })
    .defined(function(d) { return d != null; })
    .curve(d3.curveLinear);

  var GAP_THRESHOLD_DAYS = 30;

  ff.forEach(function(f, i) {
    const series = f.ndvi_series;
    if (series.length < 2) return;

    // Split series into solid segments separated by nulls at large gaps
    var solidData = [];
    var gapSegments = [];
    for (var pi = 0; pi < series.length; pi++) {
      solidData.push(series[pi]);
      if (pi < series.length - 1) {
        var gap = (new Date(series[pi + 1].date) - new Date(series[pi].date)) / 86400000;
        if (gap >= GAP_THRESHOLD_DAYS) {
          gapSegments.push({ a: series[pi], b: series[pi + 1] });
          solidData.push(null);
        }
      }
    }

    const last = series[series.length - 1];
    const trendInfo = computeNDVITrend(series);
    const latestNDVI = last.value.toFixed(3);
    const ndviTip = "<strong>" + f.name + "</strong><br>Latest NDVI: " + latestNDVI + " on " + last.date + "<br>Trend: " + trendInfo.trend + " (" + (trendInfo.pct >= 0 ? '+' : '') + trendInfo.pct + ")";

    svg.append("path")
      .datum(solidData)
      .attr("data-field-id", f.id)
      .attr("data-default-sw", 2)
      .attr("data-default-op", 0.8)
      .attr("fill", "none")
      .attr("stroke", colorScale(f.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.8)
      .attr("d", line)
      .style("cursor", "pointer")
      .on("click", function(event) {
        event.stopPropagation();
        svg.selectAll("path[data-field-id]").each(function() {
          var p = d3.select(this);
          p.attr("stroke-width", p.attr("data-default-sw")).attr("opacity", p.attr("data-default-op"));
        });
        d3.select(this).attr("stroke-width", 4).attr("opacity", 1);
        showTooltip(ndviTip, event.pageX, event.pageY);
      });

    // Dotted segments for large gaps
    gapSegments.forEach(function(seg) {
      svg.append("line")
        .attr("x1", xScale(new Date(seg.a.date)))
        .attr("y1", yScale(seg.a.value))
        .attr("x2", xScale(new Date(seg.b.date)))
        .attr("y2", yScale(seg.b.value))
        .attr("stroke", colorScale(f.id))
        .attr("stroke-width", 2)
        .attr("stroke-dasharray", "4,4")
        .attr("opacity", 0.5);
    });

    // HTML legend (like map legend)
    if (i === 0) {
      var legendEl = document.getElementById("ndvi-legend");
      legendEl.innerHTML = "";
      ff.forEach(function(fi) {
        var lastPt = fi.ndvi_series.length ? fi.ndvi_series[fi.ndvi_series.length - 1] : null;
        var trendInfo = lastPt ? computeNDVITrend(fi.ndvi_series) : null;
        var tipHtml = lastPt && trendInfo
          ? "<strong>" + fi.name + "</strong><br>Latest NDVI: " + lastPt.value.toFixed(3) + " on " + lastPt.date + "<br>Trend: " + trendInfo.trend + " (" + (trendInfo.pct >= 0 ? '+' : '') + trendInfo.pct + ")"
          : "<strong>" + fi.name + "</strong><br>No NDVI data";
        var item = document.createElement("span");
        item.className = "legend-item";
        item.style.cursor = "pointer";
        item.innerHTML = '<span class="legend-swatch" style="background:' + colorScale(fi.id) + '"></span>' + fi.name;
        (function(fid, fSeries) {
          item.addEventListener("click", function(event) {
            event.stopPropagation();
            svg.selectAll("path[data-field-id]").each(function() {
              var p = d3.select(this);
              p.attr("stroke-width", p.attr("data-default-sw")).attr("opacity", p.attr("data-default-op"));
            });
            svg.select('path[data-field-id="' + fid + '"]').attr("stroke-width", 4).attr("opacity", 1);
            var svgNode = document.querySelector("#ndvi-time-series svg");
            var svgRect = svgNode.getBoundingClientRect();
            var pt = fSeries[fSeries.length - 1];
            var tipX = svgRect.left + window.scrollX + margin.left + xScale(new Date(pt.date));
            var tipY = svgRect.top + window.scrollY + margin.top + yScale(pt.value);
            showTooltip(tipHtml, tipX, tipY);
          });
        })(fi.id, fi.ndvi_series);
        legendEl.appendChild(item);
      });
      // Gap annotation
      var gapItem = document.createElement("span");
      gapItem.className = "legend-item";
      gapItem.style.marginTop = "6px";
      gapItem.innerHTML = '<svg width="14" height="4" viewBox="0 0 14 4"><line x1="0" y1="2" x2="14" y2="2" stroke="#999" stroke-width="2" stroke-dasharray="4,4" opacity="0.5"/></svg> \u226530-day data gap';
      legendEl.appendChild(gapItem);
    }
  });
}

// ===== FIELD RANKING BAR CHART =====
function renderFieldRanking() {
  const container = d3.select("#field-ranking");
  container.html("");
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  d3.select("#field-ranking-title").text(isCurrent ? 'Field Ranking by Current NDVI' : 'Field Ranking by Peak NDVI');
  let ff = state.getFilteredFields();
  if (isCurrent) {
    ff = ff.filter(f => f.current_ndvi != null).sort((a, b) => a.current_ndvi - b.current_ndvi);
  } else {
    ff = ff.filter(f => f.ndvi_series && f.ndvi_series.length)
      .map(function(f) {
        var peak = d3.max(f.ndvi_series, function(d) { return d.value; });
        return Object.assign({}, f, { peak_ndvi: peak });
      })
      .filter(function(f) { return f.peak_ndvi != null; })
      .sort((a, b) => b.peak_ndvi - a.peak_ndvi);
  }
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
    var ndviVal = isCurrent ? f.current_ndvi : f.peak_ndvi;
    const color = THRESHOLD_LABELS[fieldTierKey(f, isCurrent)]?.color || "#999";
    const barTip = "<strong>" + f.name + "</strong><br>NDVI: " + ndviVal.toFixed(3) + "<br>" + tierTooltipLine(f, isCurrent);
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(ndviVal))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("data-default-op", 0.85)
      .attr("opacity", 0.85)
      .style("cursor", "pointer")
      .on("click", function(event) {
        event.stopPropagation();
        svg.selectAll("rect").each(function() {
          d3.select(this).attr("opacity", d3.select(this).attr("data-default-op"));
        });
        d3.select(this).attr("opacity", 1);
        showTooltip(barTip, event.pageX, event.pageY);
      });
    svg.append("text")
      .attr("x", xScale(ndviVal) - 4)
      .attr("y", yScale(f.name) + yScale.bandwidth() / 2)
      .attr("text-anchor", "end")
      .attr("dy", "0.35em")
      .attr("font-size", "11px")
      .attr("fill", "#fff")
      .attr("font-weight", "700")
      .text(ndviVal.toFixed(3));
  });
}

// ===== NDVI vs AWC SCATTER =====
function renderNDVIvsAWC() {
  const container = d3.select("#ndvi-vs-awc");
  container.html("");
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  let ff = state.getFilteredFields();
  if (isCurrent) {
    ff = ff.filter(f => f.current_ndvi != null && f.soil?.awc_in_in != null);
  } else {
    ff = ff.filter(f => f.ndvi_series && f.ndvi_series.length >= 2 && f.soil?.awc_in_in != null);
  }
  if (!ff.length) return;

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 60, left: 60 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  // Pre-compute stress days for reference mode
  var stressDaysByField = {};
  if (!isCurrent) {
    ff.forEach(function(f) {
      stressDaysByField[f.id] = f.season_stress_days || 0;
    });
    var stressVals = ff.map(function(f) { return stressDaysByField[f.id]; });
    var maxStress = d3.max(stressVals) || 0;
  }

  const xExtent = d3.extent(ff, f => f.soil.awc_in_in);
  var yExtent = isCurrent ? [0, 1] : [0, Math.max(maxStress * 1.15, 10)];
  const xPad = (xExtent[1] - xExtent[0]) * 0.1 || 0.1;
  const xScale = d3.scaleLinear().domain([Math.max(0, xExtent[0] - xPad), xExtent[1] + xPad]).range([0, width]);
  const yScale = d3.scaleLinear().domain(yExtent).range([height, 0]);

  d3.select("#scatter-title").text(isCurrent ? "NDVI vs. Available Water Storage" : "Season Stress Duration vs. Available Water Storage");

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(5));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(5));

  svg.append("text").attr("class", "chart-title")
    .attr("x", width / 2).attr("y", height + 32).text("AWS (in)");
  svg.append("text").attr("class", "chart-title")
    .attr("x", -(height / 2)).attr("y", -(margin.left - 14))
    .attr("transform", "rotate(-90)").attr("text-anchor", "middle")
    .text(isCurrent ? "NDVI" : "Stress days");

  const r = Math.min(12, width / ff.length * 0.8);
  ff.forEach(f => {
    var yVal = isCurrent ? f.current_ndvi : stressDaysByField[f.id];
    const color = THRESHOLD_LABELS[fieldTierKey(f, isCurrent)]?.color || "#999";
    var scatterTip;
    if (isCurrent) {
      scatterTip = "<strong>" + f.name + "</strong><br>NDVI: " + f.current_ndvi + "<br>AWS: " + f.soil.awc_in_in + " in<br>" + tierTooltipLine(f, isCurrent);
    } else {
      scatterTip = "<strong>" + f.name + "</strong><br>Stress: " + yVal + " days<br>AWS: " + f.soil.awc_in_in + " in<br>" + tierTooltipLine(f, isCurrent);
    }
    svg.append("circle")
      .attr("cx", xScale(f.soil.awc_in_in))
      .attr("cy", yScale(yVal))
      .attr("r", r)
      .attr("fill", color)
      .attr("data-default-op", 0.7)
      .attr("data-default-r", r)
      .attr("opacity", 0.7)
      .attr("stroke", "#fff")
      .attr("stroke-width", 1.5)
      .style("cursor", "pointer")
      .on("click", function(event) {
        event.stopPropagation();
        svg.selectAll("circle").each(function() {
          var c = d3.select(this);
          c.attr("opacity", c.attr("data-default-op")).attr("r", c.attr("data-default-r"));
        });
        d3.select(this).attr("opacity", 1).attr("r", r * 1.4);
        showTooltip(scatterTip, event.pageX, event.pageY);
      });
    svg.append("text")
      .attr("x", xScale(f.soil.awc_in_in))
      .attr("y", yScale(yVal) - r - 4)
      .attr("text-anchor", "middle")
      .attr("font-size", "9px")
      .attr("fill", "#555")
      .text(f.name);
  });
}

// ===== MAP =====
function fieldHasRenderedZones(fieldId) {
  var year = state.filters.selectedYear;
  var f = ALL_FIELDS.find(function(fi) { return fi.id === fieldId; });
  return !!(f && isFieldCrop(f, year) && f.zones && f.zones[year] && f.zones[year].length);
}

function renderMapLegend() {
  var selectedIds = state.filters.fieldIds;
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  var showingZones = selectedIds.length === 1 && fieldHasRenderedZones(selectedIds[0]);
  var legendEl = document.getElementById("map-legend");
  legendEl.innerHTML = "";
  if (showingZones) {
    ["low", "medium", "high"].forEach(function(t) {
      var item = document.createElement("span");
      item.className = "legend-item";
      item.innerHTML = '<span class="legend-swatch" style="background:' + ZONE_COLORS[t] + '"></span>' + ZONE_LABELS[t];
      legendEl.appendChild(item);
    });
  } else {
    ["healthy", "watch", "critical"].forEach(function(t) {
      var tl = THRESHOLD_LABELS[t];
      var label = isCurrent ? tl.label : SEASON_TIER_LABELS[t];
      var item = document.createElement("span");
      item.className = "legend-item";
      item.innerHTML = '<span class="legend-swatch" style="background:' + tl.color + '"></span>' + label;
      legendEl.appendChild(item);
    });
  }
}

function renderMap() {
  var container = d3.select("#field-map");
  container.html("");
  container.append("div").attr("class", "map-zoom-controls")
    .html('<button id="map-zoom-in">+</button><button id="map-zoom-out">-</button>');

  var year = state.filters.selectedYear;
  var selectedIds = state.filters.fieldIds;
  var isCurrent = year === String(new Date().getFullYear());

  var allCropFields = ALL_FIELDS.filter(function(f) { return isFieldCrop(f, year) && f.geometry?.geometry; });
  if (!allCropFields.length) return;

  // Use dynamically-computed field data (risk, NDVI) for the selected year
  var filteredFields = state.getFilteredFields();
  var ffMap = {};
  filteredFields.forEach(function(ff) { ffMap[ff.id] = ff; });

  var visibleFields = selectedIds.length > 0
    ? allCropFields.filter(function(f) { return selectedIds.includes(f.id); })
    : allCropFields;
  if (!visibleFields.length) visibleFields = allCropFields;

  var rect = container.node().getBoundingClientRect();
  var width = rect.width, height = rect.height;

  var svg = container.append("svg")
    .attr("width", width).attr("height", height);

  var mapGroup = svg.append("g").attr("class", "map-group");

  // Projection source follows the filter: single selected field zooms to its boundary,
  // otherwise the full grower view. Defensive fallback keeps fitExtent non-empty.
  var geoSource = selectedIds.length === 1
    ? allCropFields.filter(function(f) { return f.id === selectedIds[0]; })
    : allCropFields;
  if (!geoSource.length) geoSource = allCropFields;

  var geoCollection = {
    type: "FeatureCollection",
    features: geoSource.map(function(f) {
      return { type: "Feature", geometry: f.geometry.geometry, properties: {} };
    })
  };

  // Generous padding for the single-field zoom so the boundary doesn't touch edges
  var pad = Math.min(width, height) * (selectedIds.length === 1 ? 0.20 : 0.12);
  var projection = d3.geoMercator()
    .fitExtent([[pad, pad], [width - pad, height - pad]], geoCollection);
  var geoPath = d3.geoPath().projection(projection);

  // Marker radius scales with field area (sqrt: visual area ∝ acres), clamped.
  var minR = 5, maxR = 22;
  var aMin = Infinity, aMax = -Infinity;
  allCropFields.forEach(function(f) {
    var a = f.area_acres;
    if (a < aMin) aMin = a;
    if (a > aMax) aMax = a;
  });
  function markerRadius(acres) {
    if (aMax === aMin) return (minR + maxR) / 2;
    var t = Math.sqrt(Math.max(0, acres - aMin)) / Math.sqrt(aMax - aMin);
    return minR + t * (maxR - minR);
  }

  // Zone polygons replace the risk fill only when zoomed to a single field with zones

  // Draw fields — use markers for tiny polygons, true polygons otherwise
  allCropFields.forEach(function(f) {
    var visible = selectedIds.length === 0 || selectedIds.includes(f.id);
    var dynamic = ffMap[f.id];
    var field = dynamic || f;
    var fieldNdvi = dynamic ? dynamic.display_ndvi : f.current_ndvi;
    // Mode-aware tier: actionable uses classifyRisk, reference uses season-stress tier
    var tierKey = fieldTierKey(field, isCurrent);
    var fillColor = visible ? (THRESHOLD_LABELS[tierKey]?.color || "#999") : "#e0e0e0";
    var fieldName = f.name, fieldAcres = f.area_acres;
    var fieldId = f.id;
    var centroid = geoPath.centroid(f.geometry.geometry);

    // Check rendered polygon size
    var pathBounds = geoPath.bounds(f.geometry.geometry);
    var pw = pathBounds[1][0] - pathBounds[0][0];
    var ph = pathBounds[1][1] - pathBounds[0][1];
    var useMarker = (pw < 15 || ph < 15) && selectedIds.length !== 1;

    var fp;
    if (useMarker) {
      fp = mapGroup.append("circle")
        .attr("cx", centroid[0]).attr("cy", centroid[1])
        .attr("r", markerRadius(fieldAcres))
        .attr("fill", fillColor)
        .attr("stroke", visible ? "#fff" : "none")
        .attr("stroke-width", visible ? 2 : 0)
        .attr("opacity", visible ? 0.9 : 0.3)
        .style("cursor", visible ? "pointer" : "default");
    } else {
      var fieldZones = (selectedIds.length === 1 && f.zones && f.zones[year]) ? f.zones[year] : null;
      var hasZones = !!fieldZones && fieldZones.length > 0;
      var riskColor = THRESHOLD_LABELS[tierKey]?.color || "#fff";
      // Zone fills render beneath the boundary path so the risk-tier stroke stays on top
      if (hasZones) {
        fieldZones.forEach(function(z) {
          var zoneGeom = z.geometry && z.geometry.type ? normalizeFieldGeometry(z.geometry) : z.geometry;
          var zp = mapGroup.append("path")
            .datum(zoneGeom)
            .attr("d", geoPath)
            .attr("fill", ZONE_COLORS[z.label] || "#999")
            .attr("stroke", "#fff")
            .attr("stroke-width", 0.5)
            .attr("opacity", 0.95);
          zp.on("mouseenter", function(event) {
            showTooltip(
              '<strong>' + fieldName + '</strong><br>' + (ZONE_LABELS[z.label] || 'NDVI zone') +
              '<br>Area: ' + (z.area_acres != null ? z.area_acres.toFixed(2) : '--') + ' acres' +
              '<br>Mean NDVI: ' + (z.mean_ndvi != null ? z.mean_ndvi.toFixed(3) : '--'),
              event.pageX, event.pageY
            );
          }).on("mouseleave", hideTooltip);
          zp.on("click", function() {
            state.filters.fieldIds = [];
            syncFilters();
          });
        });
      }
      fp = mapGroup.append("path")
        .datum(f.geometry.geometry)
        .attr("d", geoPath)
        .attr("fill", hasZones ? "none" : fillColor)
        .attr("stroke", visible ? (hasZones ? riskColor : "#fff") : "none")
        .attr("stroke-width", visible ? (hasZones ? 2.5 : 1.5) : 0)
        .attr("opacity", visible ? 0.9 : 0.3)
        .style("cursor", visible ? "pointer" : "default");
    }

    if (!visible) return;

    // Hover tooltip — name, NDVI, and risk tier (replaces permanent on-map labels)
    fp.on("mouseenter", function(event) {
      showTooltip(
        '<strong>' + fieldName + '</strong><br>NDVI: ' + (fieldNdvi != null ? fieldNdvi.toFixed(2) : '--') +
        '<br>Size: ' + (fieldAcres != null ? fieldAcres.toFixed(1) : '--') + ' acres' +
        '<br>' + tierTooltipLine(field, isCurrent),
        event.pageX, event.pageY
      );
    }).on("mouseleave", hideTooltip);

    fp.on("click", function() {
      if (state.filters.fieldIds.length === 1 && state.filters.fieldIds[0] === fieldId) {
        state.filters.fieldIds = []; // clicking the already-selected field clears it
      } else {
        state.filters.fieldIds = [fieldId];
      }
      syncFilters();
    });
  });

  // Zoom behavior
  var zoom = d3.zoom()
    .scaleExtent([1, 30])
    .on("zoom", function(event) {
      mapGroup.attr("transform", event.transform);
    });
  svg.call(zoom);

  // Legend reflects what actually rendered: zone legend when zoomed to a field with
  // zones, risk legend otherwise
  renderMapLegend();

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

  const fieldData = ff.map(f => {
    const daily = state.getFilteredWeather(f);
    var displayYear = state.filters.selectedYear;
    var plantingDate = getPlantingDate(daily, displayYear);
    const byDate = {};
    for (var i = 0; i < daily.dates.length; i++) {
      if (plantingDate && new Date(daily.dates[i]) < plantingDate) continue;
      var tmax = +daily.T2M_MAX[i], tmin = +daily.T2M_MIN[i];
      if (tmax == null || tmin == null || isNaN(tmax) || isNaN(tmin)) continue;
      const gdd = Math.max(0, ((tmax + tmin) / 2 * 9 / 5 + 32) - (CONFIG.gdd_base_temp_f || 50));
      byDate[daily.dates[i]] = (byDate[daily.dates[i]] || 0) + gdd;
    }
    const sorted = Object.entries(byDate).sort((a, b) => a[0].localeCompare(b[0]));
    let cum = 0;
    const currentSeries = [];
    sorted.forEach(([dt, val]) => {
      cum += val;
      currentSeries.push({ date: dt, gdd: Math.round(cum) });
    });
    return { id: f.id, name: f.name, current: currentSeries };
  });

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 20, right: 20, bottom: 50, left: 60 };
  const width = rect.width - margin.left - margin.right;
  const height = rect.height - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const targetGDD = CONFIG.gdd_target;
  const repField = representativeField(ff);
  const normalGDD = (repField && repField.weather_summary?.gdd_normal) || 1500;
  const maxGDD = Math.max(targetGDD, normalGDD, ...fieldData.map(f => f.current.length ? f.current[f.current.length - 1].gdd : 0));
  const maxY = Math.ceil(maxGDD / 500) * 500;

  const year = state.filters.selectedYear;
  const xDomain = getChartDateExtent(ff, year);
  const xScale = d3.scaleTime().domain(xDomain).range([0, width]);
  const yScale = d3.scaleLinear().domain([0, maxY]).range([height, 0]);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).ticks(5));
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(d3.timeMonth).tickFormat(d3.timeFormat("%b")));

  svg.append("text").attr("class", "chart-title")
    .attr("x", -(height / 2)).attr("y", -(margin.left - 14))
    .attr("transform", "rotate(-90)").attr("text-anchor", "middle")
    .text("GDD (\u00b0F-days)");

  svg.append("line")
    .attr("x1", 0).attr("x2", width)
    .attr("y1", yScale(targetGDD)).attr("y2", yScale(targetGDD))
    .attr("stroke", "#e67e22").attr("stroke-dasharray", "6,3").attr("stroke-width", 1.5);
  svg.append("text")
    .attr("x", width).attr("y", yScale(targetGDD) - 4)
    .attr("text-anchor", "end").attr("font-size", "10px").attr("fill", "#e67e22")
    .text("Target: " + targetGDD + " \u00b0F-days (maturity)");

  var gddColorScale = d3.scaleOrdinal(FIELD_LINE_COLORS).domain(ff.map(f => f.id));
  const line = d3.line()
    .x(d => xScale(new Date(d.date)))
    .y(d => yScale(d.gdd));

  fieldData.forEach(fd => {
    if (fd.current.length < 2) return;
    var lastGDD = fd.current[fd.current.length - 1].gdd;
    var gddTip = "<strong>" + fd.name + "</strong><br>GDD Accumulated: " + lastGDD + " &deg;F-days";
    svg.append("path")
      .datum(fd.current)
      .attr("fill", "none")
      .attr("data-field-id", fd.id)
      .attr("data-default-sw", 2)
      .attr("data-default-op", 0.7)
      .attr("stroke", gddColorScale(fd.id))
      .attr("stroke-width", 2)
      .attr("opacity", 0.7)
      .attr("d", line)
      .style("cursor", "pointer")
      .on("click", function(event) {
        event.stopPropagation();
        svg.selectAll("path").each(function() {
          var p = d3.select(this);
          p.attr("stroke-width", p.attr("data-default-sw")).attr("opacity", p.attr("data-default-op"));
        });
        d3.select(this).attr("stroke-width", 4).attr("opacity", 1);
        showTooltip(gddTip, event.pageX, event.pageY);
      });
  });

  // HTML legend (like map legend)
  var gddLegendEl = document.getElementById("gdd-legend");
  gddLegendEl.innerHTML = "";
  fieldData.forEach(function(fd) {
    var lastPt = fd.current.length ? fd.current[fd.current.length - 1] : null;
    var tipHtml = lastPt
      ? "<strong>" + fd.name + "</strong><br>GDD Accumulated: " + lastPt.gdd + " &deg;F-days"
      : "<strong>" + fd.name + "</strong><br>No GDD data";
    var item = document.createElement("span");
    item.className = "legend-item";
    item.style.cursor = "pointer";
    item.innerHTML = '<span class="legend-swatch" style="background:' + gddColorScale(fd.id) + '"></span>' + fd.name;
    (function(fid, fSeries) {
      item.addEventListener("click", function(event) {
        event.stopPropagation();
        svg.selectAll("path[data-field-id]").each(function() {
          var p = d3.select(this);
          p.attr("stroke-width", p.attr("data-default-sw")).attr("opacity", p.attr("data-default-op"));
        });
        svg.select('path[data-field-id="' + fid + '"]').attr("stroke-width", 4).attr("opacity", 1);
        var svgNode = document.querySelector("#gdd-chart svg");
        var svgRect = svgNode.getBoundingClientRect();
        var pt = fSeries[fSeries.length - 1];
        var tipX = svgRect.left + window.scrollX + margin.left + xScale(new Date(pt.date));
        var tipY = svgRect.top + window.scrollY + margin.top + yScale(pt.gdd);
        showTooltip(tipHtml, tipX, tipY);
      });
    })(fd.id, fd.current);
    gddLegendEl.appendChild(item);
  });

  // Growth stage annotations — identical to NDVI chart: dashed verticals at the
  // calendar date each stage threshold (base-50°F GDD from planting) was crossed.
  var stageColors = Object.assign({"VE":"#4CAF50","V6":"#8BC34A","VT":"#FFC107","R1":"#FF9800",
                     "R2":"#FF5722","R3":"#795548","R4":"#9C27B0","R5":"#3F51B5","R6":"#607D8B"}, CONFIG.stage_colors || {});
  var gddBaseF    = CONFIG.gdd_base_temp_f;
  var displayYear = state.filters.selectedYear;
  var chartStart  = xScale.domain()[0], chartEnd = xScale.domain()[1];
  var dailyData   = state.getFilteredWeather(representativeField(ff));
  if (dailyData.dates.length > 0) {
    // Planting date: last spring frost (T2M_MIN <= 0°C, DOY <= 182), fallback Apr 20
    var plantingDate = getPlantingDate(dailyData, displayYear);
    if (plantingDate) {

    // Accumulate GDD in °F-days (base 50°F) from planting — matches growth_stages thresholds
    var cumGDD    = 0;
    var cumGDDArr = [];
    for (var i = 0; i < dailyData.dates.length; i++) {
      var dObj = new Date(dailyData.dates[i]);
      if (dObj < plantingDate) {
        cumGDDArr.push(0);
      } else {
        var tmin = +dailyData.T2M_MIN[i], tmax = +dailyData.T2M_MAX[i];
        if (isNaN(tmin) || isNaN(tmax)) {
          cumGDDArr.push(cumGDD);
        } else {
          var avgF = (tmin + tmax) / 2 * 9 / 5 + 32;
          cumGDD += Math.max(0, avgF - gddBaseF);
          cumGDDArr.push(cumGDD);
        }
      }
    }

    // Planting annotation
    if (plantingDate >= chartStart && plantingDate <= chartEnd) {
      var px  = xScale(plantingDate);
      svg.append("line")
        .attr("x1", px).attr("x2", px).attr("y1", 0).attr("y2", height)
        .attr("stroke", "#333").attr("stroke-width", 0.8)
        .attr("stroke-dasharray", "3,3").attr("opacity", 0.45);
      var plg = svg.append("g").attr("transform", "translate(" + px + ",0)");
      var plt = plg.append("text")
        .attr("x", 0).attr("y", 10).attr("text-anchor", "middle")
        .attr("font-size", "8px").attr("font-weight", "600").attr("fill", "#333")
        .text("Planting");
      var plb = plt.node().getBBox();
      plg.insert("rect", "text")
        .attr("x", plb.x - 2).attr("y", plb.y - 1)
        .attr("width", plb.width + 4).attr("height", plb.height + 2)
        .attr("fill", "#fff").attr("opacity", 0.8).attr("rx", 2);
    }

    // Stage annotations
    var stages    = CONFIG.growth_stages || {};
    var stageKeys = Object.keys(stages);
    stageKeys.forEach(function(stage) {
      var threshold = stages[stage];
      for (var i = 0; i < cumGDDArr.length; i++) {
        if (cumGDDArr[i] >= threshold) {
          var evDate = new Date(dailyData.dates[i]);
          if (evDate >= chartStart && evDate <= chartEnd) {
            var xPos = xScale(evDate);
            var c    = stageColors[stage] || "#666";
            svg.append("line")
              .attr("x1", xPos).attr("x2", xPos).attr("y1", 0).attr("y2", height)
              .attr("stroke", c).attr("stroke-width", 0.8)
              .attr("stroke-dasharray", "3,3").attr("opacity", 0.45);
            var labelG = svg.append("g").attr("transform", "translate(" + xPos + ",0)");
            var txt = labelG.append("text")
              .attr("x", 0).attr("y", 10).attr("text-anchor", "middle")
              .attr("font-size", "8px").attr("font-weight", "600").attr("fill", c)
              .text(stage);
            var bbox = txt.node().getBBox();
            labelG.insert("rect", "text")
              .attr("x", bbox.x - 2).attr("y", bbox.y - 1)
              .attr("width", bbox.width + 4).attr("height", bbox.height + 2)
              .attr("fill", "#fff").attr("opacity", 0.8).attr("rx", 2);
          }
          break;
        }
      }
    });
  }

  var wgap = getWeatherGap(dailyData, chartStart, chartEnd);
  if (wgap) {
    var gx1 = xScale(wgap.start), gx2 = xScale(wgap.end);
    svg.append("rect")
      .attr("x", gx1).attr("y", 0)
      .attr("width", gx2 - gx1).attr("height", height)
      .attr("fill", "#888").attr("opacity", 0.12)
      .attr("pointer-events", "none");
    svg.append("text")
      .attr("x", (gx1 + gx2) / 2).attr("y", 10)
      .attr("text-anchor", "middle").attr("font-size", "8px")
      .attr("font-weight", "600").attr("fill", "#888")
      .text("Weather gap");
  }
  }
}

// ===== SOIL CHART =====
function renderSoil() {
  const container = d3.select("#soil-chart");
  container.html("");
  let ff = state.getFilteredFields().filter(f => f.soil?.om_pct != null).sort((a, b) => b.soil.om_pct - a.soil.om_pct);
  if (!ff.length) return;
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());

  const rect = container.node().getBoundingClientRect();
  const margin = { top: 10, right: 20, bottom: 20, left: 70 };
  const width = rect.width - margin.left - margin.right;
  const baseHeight = Math.max(200, ff.length * 32);
  const height = baseHeight - margin.top - margin.bottom;

  const svg = container.append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", baseHeight + 22)
    .append("g")
    .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

  const xMax = d3.max(ff, f => f.soil.om_pct) * 1.15;
  const xScale = d3.scaleLinear().domain([0, xMax]).range([0, width]);
  const yScale = d3.scaleBand().domain(ff.map(f => f.name)).range([0, height]).padding(0.3);

  svg.append("g").attr("class", "axis").call(d3.axisLeft(yScale).tickSize(0)).select(".domain").remove();
  svg.append("g").attr("class", "axis").attr("transform", "translate(0," + height + ")")
    .call(d3.axisBottom(xScale).ticks(5));

  ff.forEach(f => {
    const color = THRESHOLD_LABELS[fieldTierKey(f, isCurrent)]?.color || "#7cb342";
    const awcInfo = f.soil?.awc_in_in != null ? 'AWS: ' + f.soil.awc_in_in + ' in' : '';
    const drainInfo = f.soil?.drainage_class || '';
    const soilTip = "<strong>" + f.name + "</strong><br>OM: " + f.soil.om_pct.toFixed(1) + "%" + (awcInfo ? '<br>' + awcInfo : '') + (drainInfo ? '<br>Drainage: ' + drainInfo : '');
    svg.append("rect")
      .attr("x", 0)
      .attr("y", yScale(f.name))
      .attr("width", xScale(f.soil.om_pct))
      .attr("height", yScale.bandwidth())
      .attr("fill", color)
      .attr("rx", 3)
      .attr("data-default-op", 0.85)
      .attr("opacity", 0.85)
      .style("cursor", "pointer")
      .on("click", function(event) {
        event.stopPropagation();
        svg.selectAll("rect").each(function() {
          d3.select(this).attr("opacity", d3.select(this).attr("data-default-op"));
        });
        d3.select(this).attr("opacity", 1);
        showTooltip(soilTip, event.pageX, event.pageY);
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
    .attr("x", width / 2)    .attr("y", height + 27).text("Organic Matter (%)");
}

// ===== ACTION LIST =====
function renderActionList() {
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  if (isCurrent) {
    renderPriorityActions();
  } else {
    renderSeasonRecap();
  }
}

function renderPriorityActions() {
  var section = d3.select("#action-list-section");
  section.style("display", "block");
  d3.select("#action-list-section h3").text("Priority Actions");

  let ff = state.getFilteredFields()
    .filter(f => f.current_risk === 'critical' || f.current_risk === 'watch')
    .sort((a, b) => {
      function riskScore(f) {
        var tierWeight = f.current_risk === 'critical' ? 100 : 50;
        var ndviPenalty = f.current_ndvi != null ? Math.max(0, (CONFIG.watch_threshold - f.current_ndvi) * 100) : 0;
        var trendPenalty = f.ndvi_trend === 'declining' ? Math.abs(f.ndvi_trend_pct || 0) * 2 : 0;
        var awcPenalty = f.soil?.awc_in_in != null && f.soil.awc_in_in < 1.0 ? (1.0 - f.soil.awc_in_in) * 10 : 0;
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
    const soilInfo = f.soil?.awc_in_in != null ? 'AWS ' + f.soil.awc_in_in + ' in' : '';
    var action = '';
    if (f.current_risk === 'critical') {
      action = 'Scout immediately.';
      if (f.ndvi_trend === 'declining') action += ' Declining trend warrants priority.';
      if (f.soil?.awc_in_in != null && f.soil.awc_in_in < 1.0) action += ' Low AWS increases drought risk.';
      if (f.weather_summary?.days_since_significant_rain > 10) action += ' Extended dry period.';
      action += ' Consider irrigation or tissue sampling.';
    } else {
      action = 'Monitor weekly.';
      if (f.soil?.awc_in_in != null && f.soil.awc_in_in < 1.0) action += ' Low AWS raises drought sensitivity.';
      if (f.weather_summary?.days_since_significant_rain > 10) action += ' Extended dry period.';
      action += ' Check soil moisture and NDVI trend next week.';
    }

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

function renderSeasonRecap() {
  var section = d3.select("#action-list-section");
  section.style("display", "block");
  d3.select("#action-list-section h3").text("Season Recap & Notable Events");

  var ff = state.getFilteredFields();
  if (!ff.length) {
    d3.select("#action-list").html('<p style="color:#777; font-size:0.85rem;">No field data available for this season.</p>');
    return;
  }

  // Pre-compute stage map per unique weather series
  var stageMapCache = {};
  ff.forEach(function(f) {
    if (!stageMapCache[f.weather_series_id]) {
      var wd = state.getFilteredWeather(f);
      var result = computeDateStageMap(wd, CONFIG);
      stageMapCache[f.weather_series_id] = result ? result.dateStageMap : null;
    }
  });

  ff.sort(function(a, b) {
    var stressA = a.season_stress_days || 0;
    var stressB = b.season_stress_days || 0;
    if (stressB !== stressA) return stressB - stressA;
    return (b.current_ndvi || 0) - (a.current_ndvi || 0);
  });

  var html = '';
  ff.forEach(function(f) {
    var series = f.ndvi_series;
    if (!series || series.length < 2) return;

    var stressDays = f.season_stress_days || 0;
    var totalSeasonDays = f.season_length_days || 90;
    var tier = seasonStressTier(stressDays, totalSeasonDays);
    var badgeLabel = SEASON_TIER_LABELS[tier];
    var badgeColor = THRESHOLD_LABELS[tier].color;

    var peakNDVI = d3.max(series, function(d) { return d.value; });
    var peakDate = '';
    if (peakNDVI != null) {
      for (var pi = 0; pi < series.length; pi++) {
        if (series[pi].value === peakNDVI) {
          var pd = new Date(series[pi].date);
          peakDate = pd.toLocaleString('en-US', { month: 'long' });
          break;
        }
      }
    }

    var weatherData = state.getFilteredWeather(f);
    var eventLine = detectNotableEvent(series, weatherData, CONFIG, stageMapCache[f.weather_series_id]);

    html += '<div class="action-item">' +
      '<span class="risk-badge" style="background:' + badgeColor + '">' + badgeLabel + '</span>' +
      '<span class="risk-text">' +
        '<strong>' + f.name + '</strong><br>' +
        '<span style="color:#777; font-size:0.8rem;">Reached peak NDVI of ' + (peakNDVI != null ? peakNDVI.toFixed(2) : '--') + (peakDate ? ' in ' + peakDate : '') + ' &middot; ' + stressDays + ' days below threshold this season</span>' +
        (eventLine ? '<br><span style="color:#777; font-size:0.8rem;">' + eventLine + '</span>' : '') +
      '</span>' +
    '</div>';
  });
  d3.select("#action-list").html(html || '<p style="color:#777; font-size:0.85rem;">No field data available for this season.</p>');
}

// ===== NARRATIVE =====
function renderNarrative() {
  const ff = state.getFilteredFields();
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  const total = ff.length;
  const crit = ff.filter(f => f.current_risk === 'critical').length;
  const watch = ff.filter(f => f.current_risk === 'watch').length;
  const healthy = ff.filter(f => f.current_risk === 'healthy').length;
  const ndviArr = ff.filter(f => f.display_ndvi != null);
  const ndviAvg = ndviArr.length ? ndviArr.reduce((s, f) => s + f.display_ndvi, 0) / ndviArr.length : 0;
  const declining = ff.filter(f => f.ndvi_trend === 'declining').length;
  const improving = ff.filter(f => f.ndvi_trend === 'improving').length;
  const aboveAvg = ff.filter(f => f.display_ndvi != null && f.ndvi_corn_avg != null && f.display_ndvi > f.ndvi_corn_avg).length;
  const belowAvg = ff.filter(f => f.display_ndvi != null && f.ndvi_corn_avg != null && f.display_ndvi < f.ndvi_corn_avg).length;
  const lowAWC = ff.filter(f => f.soil?.awc_in_in != null && f.soil.awc_in_in < 1.0).length;
  const highOM = ff.filter(f => f.soil?.om_pct != null && f.soil.om_pct > 3).length;
  const awcVals = ff.filter(f => f.soil?.awc_in_in != null).map(f => f.soil.awc_in_in);
  const omVals = ff.filter(f => f.soil?.om_pct != null).map(f => f.soil.om_pct);
  const scatterCount = ff.filter(f => f.display_ndvi != null && f.soil?.awc_in_in != null).length;
  const gddVals = ff.map(f => f.weather_summary?.gdd_accumulated || 0);
  const gdd = gddVals.length ? Math.round(gddVals.reduce((a,b) => a+b, 0) / gddVals.length) : 0;
  const repField = representativeField(ff);
  const normal = (repField && repField.weather_summary?.gdd_normal) || 0;
  const gddDiff = gdd - normal;

  // Reference-mode correlation for scatter plot description
  var refStressVals = [], refAwsVals = [], refScatterCount = 0;
  function pearsonCorrelation(xs, ys) {
    var n = xs.length, sx = 0, sy = 0, sxy = 0, sx2 = 0, sy2 = 0;
    if (n < 3) return 0;
    for (var i = 0; i < n; i++) { sx += xs[i]; sy += ys[i]; sxy += xs[i] * ys[i]; sx2 += xs[i] * xs[i]; sy2 += ys[i] * ys[i]; }
    var num = n * sxy - sx * sy, den = Math.sqrt((n * sx2 - sx * sx) * (n * sy2 - sy * sy));
    return den === 0 ? 0 : num / den;
  }
  if (!isCurrent) {
    ff.forEach(function(f) {
      if (f.ndvi_series && f.ndvi_series.length >= 2 && f.soil?.awc_in_in != null) {
        refStressVals.push(f.season_stress_days || 0);
        refAwsVals.push(f.soil.awc_in_in);
      }
    });
    refScatterCount = refStressVals.length;
  }
  var relationshipText;
  if (isCurrent) {
    relationshipText = scatterCount > 3 ? 'a visible positive relationship' : 'limited correlation given available datapoints';
  } else if (refScatterCount > 3) {
    var r = pearsonCorrelation(refAwsVals, refStressVals);
    if (r < -0.15) relationshipText = 'a visible inverse relationship';
    else if (r > 0.15) relationshipText = 'a visible positive relationship';
    else relationshipText = 'limited correlation given available datapoints';
  } else {
    relationshipText = 'limited correlation given available datapoints';
  }

  let html = '';

  html += '<p><strong>Patterns & Trends.</strong> Of ' + total + ' fields, ' + (isCurrent ? '<strong>' + crit + ' critical</strong> and <strong>' + watch + ' watch</strong> require attention.' : '<strong>' + crit + ' finished the season in Critical status</strong> and <strong>' + watch + ' in Watch</strong>.') + ' Average ' + (isCurrent ? '' : 'peak ') + 'NDVI across all fields is <strong>' + ndviAvg.toFixed(3) + '</strong>.';
  if (isCurrent) {
    if (declining > 0) html += ' ' + declining + ' field(s) show declining NDVI trend, warranting priority monitoring.';
    if (improving > 0) html += ' ' + improving + ' field(s) are improving.';
  } else {
    if (aboveAvg > 0 || belowAvg > 0) html += ' ' + aboveAvg + ' field(s) ended the season above the historical average NDVI and ' + belowAvg + ' below.';
  }
  html += ' ' + healthy + ' field(s) appear healthy and stable.</p>';

  if (crit > 0) {
    html += '<p><strong>Field Health.</strong> Critical-risk fields typically combine below-threshold NDVI with declining trend. ';
    if (lowAWC > 0) {
      html += '' + lowAWC + ' field(s) have low available water storage (AWS < 1.0 in)' + (isCurrent ? ', which likely contributes to stress under dry conditions.' : ', which likely contributed to stress during dry stretches this season.');
    } else {
      html += 'Soil AWS across fields is adequate for current conditions.';
    }
    if (highOM > 0) html += ' ' + highOM + ' field(s) have elevated organic matter (>3%), supporting better moisture retention.';
    html += '</p>';
  } else if (watch > 0) {
    html += '<p><strong>Field Health.</strong> No fields are currently in Critical status. ' + watch + ' field(s) are in Watch' + (isCurrent ? ' and warrant continued monitoring, particularly those with low available water storage.' : ' and finished the season with moderate stress duration and lower available water storage -- a starting point for next-season irrigation or drainage planning.');
    if (lowAWC > 0) {
      html += ' ' + lowAWC + ' field(s) have low available water storage (AWS < 1.0 in).';
    } else {
      html += ' Soil AWS across fields is adequate for current conditions.';
    }
    if (highOM > 0) html += ' ' + highOM + ' field(s) have elevated organic matter (>3%), supporting better moisture retention.';
    html += '</p>';
  } else {
    html += '<p><strong>Field Health.</strong> All ' + total + ' fields are Healthy. No fields require immediate attention based on NDVI thresholds.';
    if (lowAWC > 0) html += ' ' + lowAWC + ' field(s) have low available water storage (AWS < 1.0 in)' + (isCurrent ? ', which may warrant attention under dry conditions.' : ', which likely contributed to stress during dry stretches this season.');
    html += '</p>';
  }

  html += '<p><strong>Environmental & Soil Variation.</strong> Fields range from ' +
    (awcVals.length ? d3.min(awcVals).toFixed(2) : '--') + ' to ' + (awcVals.length ? d3.max(awcVals).toFixed(2) : '--') +
    ' in AWS and ' + (omVals.length ? d3.min(omVals).toFixed(1) : '--') + '% to ' +
    (omVals.length ? d3.max(omVals).toFixed(1) : '--') +
    '% organic matter. This variation directly correlates with NDVI differences -- the scatter plot of ' + (isCurrent ? 'NDVI' : 'Season Stress Duration') + ' vs. AWS shows ' + relationshipText + '.</p>';

  if (crit > 0) {
    html += '<p><strong>Decisions & Actions.</strong> ' + (isCurrent ? 'Focus scouting on critical-risk fields first.' : 'Critical-risk fields ended the season with below-threshold NDVI and declining trends -- review these fields first for post-season stand assessment and drainage planning.') + ' ';
  } else {
    html += '<p><strong>Decisions & Actions.</strong> ' + (isCurrent ? 'No fields require immediate intervention. Continue routine monitoring of Watch-tier fields, particularly for soil moisture and NDVI trend.' : 'No fields required immediate intervention this season. Watch-tier fields combined moderate stress duration with lower available water storage -- a starting point for next-season irrigation or drainage planning.') + ' ';
  }
  if (isCurrent) {
    var pctOfTarget = gdd > 0 ? Math.round(gdd / CONFIG.gdd_target * 100) : 0;
    html += 'GDD accumulation (' + gdd + ' &deg;F-days) is ' + pctOfTarget + '% of the way to the maturity target (' + CONFIG.gdd_target + ' &deg;F-days).';
  } else {
    if (gddDiff < -100) {
      html += 'GDD accumulation (' + gdd + ' &deg;F-days) is below normal (' + normal + ' &deg;F-days), which likely delayed maturity.';
    } else if (gddDiff > 200) {
      html += 'GDD accumulation (' + gdd + ' &deg;F-days) exceeds normal (' + normal + ' &deg;F-days), advancing crop development.';
    } else {
      html += 'GDD accumulation (' + gdd + ' &deg;F-days) is near normal (' + normal + ' &deg;F-days).';
    }
  }
  if (isCurrent) {
    html += ' The priority action list above ranks fields by risk severity for operational triage.</p>';
  } else {
    html += ' The Season Recap panel above ranks fields by stress duration for the season.</p>';
  }

  if (crit > 0) {
    html += '<p><strong>Key Variables.</strong> NDVI trend direction, soil AWC, and GDD accumulation are the three most important indicators in this analysis. Fields with low AWC and declining NDVI consistently ' + (isCurrent ? 'appear in the critical tier and should be prioritized for irrigation and stand assessment.' : 'appeared in the critical tier this season -- candidates for post-season stand assessment and drainage review.') + '</p>';
  } else {
    html += '<p><strong>Key Variables.</strong> NDVI trend direction, soil AWC, and GDD accumulation are the three most important indicators in this analysis. Fields with low AWC ' + (isCurrent ? 'are the most drought-sensitive and warrant monitoring as dry conditions continue.' : 'were the most drought-sensitive fields this season.') + '</p>';
  }

  d3.select("#narrative-text").html(html);
}

// ===== FOOTER =====
function renderFooter() {
  var html =
    '<div><a href="https://midigitalvit.com" target="_blank" rel="noopener">MiDigitalVit</a></div>' +
    '<div class="footer-disclaimer">This dashboard is a decision-support tool based on remote sensing and modeled data. Field conditions should be verified on-site before acting on any recommendation.</div>' +
    '<div>&copy; 2026 MiDigitalVit</div>';
  d3.select("#footer-section").html(html);
}

// ===== HEADER LEGEND =====
function renderHeaderLegend() {
  var isCurrent = state.filters.selectedYear === String(new Date().getFullYear());
  var tiers, note;
  if (isCurrent) {
    tiers = [
      { key: "healthy", desc: "NDVI &ge; " + CONFIG.watch_threshold + " (scaled by growth stage before VT)" },
      { key: "watch",   desc: "NDVI " + CONFIG.stress_threshold + "&ndash;" + CONFIG.watch_threshold + " (all phases) or declining &gt;" + CONFIG.ndvi_decline_warning_pct + "% (vegetative only)" },
      { key: "critical",desc: "NDVI &lt; " + CONFIG.stress_threshold + " (all phases) or declining &gt;" + CONFIG.ndvi_decline_critical_pct + "% (vegetative only)" }
    ];
    note = '<em>NDVI alerts are off before emergence and late in the season, when low NDVI is normal. ' +
      'From silking through dough stage, a falling trend won&rsquo;t trigger an alert &mdash; but a genuinely low reading still will, ' +
      'since that can mean a real problem like disease.</em>';
  } else {
    tiers = [
      { key: "healthy", desc: "less than 10% of the season below the NDVI threshold" },
      { key: "watch",   desc: "10&ndash;30% of the season below the NDVI threshold" },
      { key: "critical",desc: "more than 30% of the season below the NDVI threshold" }
    ];
    note = '<em>Season stress duration only counts days below the NDVI threshold during Building and Early-reproductive stages (VE&ndash;R4) &mdash; ' +
      'pre-emergence and late-season senescence are excluded, since NDVI isn&rsquo;t diagnostic in those windows.</em>';
  }
  document.getElementById("legend-tiers-title").textContent = isCurrent ? 'Risk Tiers' : 'Season Stress Tiers';
  var html = "";
  tiers.forEach(function(t) {
    var tl = THRESHOLD_LABELS[t.key];
    var label = isCurrent ? tl.label : SEASON_TIER_LABELS[t.key];
    html += '<div class="legend-item"><span class="legend-swatch" style="background:' + tl.color + '"></span>' + label + ' - ' + t.desc + '</div>';
  });
  html += '<div style="margin-top:8px; font-size:0.78rem; color:#c8d8e8; line-height:1.4;">' + note + '</div>';
  d3.select("#legend-risk-tiers").html(html);
}

function toggleLegend() {
  var content = document.getElementById("legend-content");
  var chevron = document.getElementById("legend-chevron");
  content.classList.toggle("open");
  chevron.classList.toggle("open");
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
  renderHeaderLegend();
  renderFooter();
}

// ===== INIT =====
state.subscribe(renderAll);

document.addEventListener("click", function() {
  tooltip.classed("visible", false);
  d3.selectAll("[data-default-sw]").each(function() {
    var el = d3.select(this);
    el.attr("stroke-width", el.attr("data-default-sw")).attr("opacity", el.attr("data-default-op"));
  });
  d3.selectAll("[data-default-op]:not([data-default-sw])").each(function() {
    var el = d3.select(this);
    el.attr("opacity", el.attr("data-default-op"));
  });
  d3.selectAll("[data-default-r]").each(function() {
    d3.select(this).attr("r", d3.select(this).attr("data-default-r"));
  });
});

document.getElementById("field-filter-indicator").addEventListener("click", function(e) {
  if (e.target.classList.contains("clear-field-filter")) {
    state.filters.fieldIds = [];
    syncFilters();
  }
});

document.getElementById("year-select").addEventListener("change", function() {
  state.filters.selectedYear = this.value;
  state.filters.fieldIds = []; // always reset to All Crop Fields when year changes
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
    parser.add_argument("--crop-type", default=None, choices=sorted(CROP_CONFIG.keys()), help="Crop config to use (default: auto-detect from field CDL data)")
    parser.add_argument("--data-root", default=None, help="Runtime data root (default: $DATA_PIPELINE_DATA_ROOT or /home/coder/my-farm-advisor-runtime)")
    parser.add_argument("--d3-path", default=None, help="Path to local d3.v7.min.js (optional, downloads if not provided)")
    parser.add_argument("--output", default=None, help="Output HTML path (default: auto to runtime dashboards dir)")
    args = parser.parse_args()

    data_root = Path(args.data_root) if args.data_root else resolve_data_root()
    grower_root = grower_path(data_root, args.grower)

    if not grower_root.is_dir():
        print(f"Error: Grower path not found: {grower_root}", file=sys.stderr)
        sys.exit(1)

    crop_type = args.crop_type or detect_crop_type(grower_root)

    print(f"Reading data for grower: {args.grower} (crop: {crop_type})")
    fields, grower_name, weather_series = extract_all_field_data(grower_root, data_root, crop_type=crop_type)
    print(f"  Found {len(fields)} fields, {len(weather_series)} unique weather series")

    summary = build_summary(fields, crop_type=crop_type)
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
    html = build_html(data_json_str, d3_js, weather_series=weather_series, crop_type=crop_type)

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
