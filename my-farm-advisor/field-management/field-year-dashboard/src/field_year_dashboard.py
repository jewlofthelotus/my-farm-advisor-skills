from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HAS_RASTERIO = True
try:
    import rasterio
    from rasterio.mask import mask
except ImportError:
    _HAS_RASTERIO = False

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

_FIPS_TO_STATE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY",
}

# ---------------------------------------------------------------------------
# Crop thresholds derived from strategy/crop-strategy/resources/2026-usa-*.md
# ---------------------------------------------------------------------------
# Each entry: GDD base/cap in Celsius, heat-stress thresholds, key growth
# stages with approximate GDD accumulation ranges, and frost sensitivity.
# Reference files are loaded as context text, not parsed programmatically.

CROP_THRESHOLDS: dict[str, dict] = {
    "Corn": {
        "gdd_base_c": 10.0,
        "gdd_cap_c": 30.0,
        "heat_threshold_c": 35.0,
        "frost_threshold_c": 0.0,
        "resource": "2026-usa-corn.md",
        "stages": [
            ("V6", 280, "Vegetative growth accelerates"),
            ("V10", 475, "Peak nitrogen uptake"),
            ("R1 (Silking)", 1120, "Pollination; most water-sensitive"),
            ("R5 (Dent)", 1750, "Grain fill underway"),
            ("R6 (Maturity)", 2200, "Black layer; physiological maturity"),
        ],
    },
    "Soybeans": {
        "gdd_base_c": 10.0,
        "gdd_cap_c": 30.0,
        "heat_threshold_c": 33.0,
        "frost_threshold_c": 0.0,
        "resource": "2026-usa-soybean.md",
        "stages": [
            ("V3", 150, "Nodulation established"),
            ("R1 (Bloom)", 550, "Flowering begins; heat sensitive"),
            ("R3 (Pod set)", 800, "Pod elongation; moisture critical"),
            ("R5 (Seed fill)", 1150, "Seed fill; yield determination"),
            ("R7 (Maturity)", 1600, "Physiological maturity reached"),
        ],
    },
    "Cotton": {
        "gdd_base_c": 15.6,
        "gdd_cap_c": 37.8,
        "heat_threshold_c": 38.0,
        "frost_threshold_c": 0.0,
        "resource": "2026-usa-cotton.md",
        "stages": [
            ("Emergence", 50, "Crop emergence"),
            ("Squaring", 450, "First square; vegetative growth"),
            ("Bloom", 775, "Flowering peak"),
            ("Cutout", 1100, "Peak bloom to cutout"),
            ("Maturity", 1600, "Boll opening"),
        ],
    },
    "Winter Wheat": {
        "gdd_base_c": 0.0,
        "gdd_cap_c": 25.0,
        "heat_threshold_c": 32.0,
        "frost_threshold_c": -4.0,
        "resource": "2026-usa-wheat.md",
        "stages": [
            ("Green-up", 100, "Spring green-up"),
            ("Jointing", 450, "Stem elongation"),
            ("Heading", 750, "Head emergence"),
            ("Anthesis", 900, "Flowering; frost sensitive"),
            ("Maturity", 1400, "Harvest readiness"),
        ],
    },
    "Sorghum": {
        "gdd_base_c": 10.0,
        "gdd_cap_c": 37.0,
        "heat_threshold_c": 36.0,
        "frost_threshold_c": 0.0,
        "resource": "2026-us-sorghum.md",
        "stages": [
            ("Emergence", 80, "Crop emergence"),
            ("Growing point diff.", 200, "Panicle initiation"),
            ("Boot", 500, "Flag leaf visible"),
            ("Flowering", 700, "Bloom; heat sensitive"),
            ("Soft dough", 950, "Grain fill underway"),
            ("Maturity", 1300, "Physiological maturity"),
        ],
    },
}

_FALLBACK_THRESHOLDS = {
    "gdd_base_c": 10.0,
    "gdd_cap_c": 30.0,
    "heat_threshold_c": 35.0,
    "frost_threshold_c": 0.0,
    "resource": None,
    "stages": [],
}


def _load_crop_thresholds(crop_name: str) -> dict:
    normalized = crop_name.strip().lower()
    for key in CROP_THRESHOLDS:
        if key.lower() == normalized:
            return dict(CROP_THRESHOLDS[key])
    return dict(_FALLBACK_THRESHOLDS)


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------

def _resolve_field(data_root: Path, raw_field_id: str) -> tuple[str, str, Path]:
    growers_dir = data_root / "growers"
    if not growers_dir.is_dir():
        raise FileNotFoundError(
            f"Growers directory not found: {growers_dir}\n"
            "Is DATA_PIPELINE_DATA_ROOT set correctly?"
        )

    def _norm(name: str) -> str:
        return name.replace("_", "-").replace(" ", "-").lower()

    normalized = _norm(raw_field_id)
    candidates: list[tuple[str, str, Path]] = []

    for grower_dir in sorted(growers_dir.iterdir()):
        farms_dir = grower_dir / "farms"
        if not farms_dir.is_dir():
            continue
        for farm_dir in sorted(farms_dir.iterdir()):
            fields_root = farm_dir / "fields"
            if not fields_root.is_dir():
                continue

            for field_slug_dir in sorted(fields_root.iterdir()):
                if _norm(field_slug_dir.name) == normalized:
                    candidates.append((grower_dir.name, farm_dir.name, field_slug_dir))

            if candidates:
                continue

            inventory = farm_dir / "manifests" / "field-inventory.csv"
            if inventory.exists():
                try:
                    import pandas as _pd
                    inv = _pd.read_csv(inventory)
                    for _, row in inv.iterrows():
                        inv_id = str(row.get("field_id", ""))
                        inv_slug = str(row.get("field_slug", ""))
                        if _norm(inv_id) == normalized or _norm(inv_slug) == normalized:
                            slug_dir = fields_root / (inv_slug if inv_slug else inv_id)
                            if slug_dir.is_dir():
                                candidates.append((grower_dir.name, farm_dir.name, slug_dir))
                                break
                except Exception:
                    pass

            if candidates:
                continue

            boundary_file = farm_dir / "boundary" / "field_boundaries.geojson"
            if boundary_file.exists():
                try:
                    import geopandas as _gpd
                    fields = _gpd.read_file(boundary_file)
                    fields["_norm_id"] = fields["field_id"].astype(str).apply(_norm)
                    match = fields[fields["_norm_id"] == normalized]
                    if not match.empty:
                        row = match.iloc[0]
                        raw_slug = str(row.get("field_id", raw_field_id))
                        for col in ("field_slug", "field_id"):
                            if col in row and str(row[col]).strip():
                                raw_slug = str(row[col])
                                break
                        slug_dir = fields_root / raw_slug
                        if slug_dir.exists():
                            candidates.append((grower_dir.name, farm_dir.name, slug_dir))
                except Exception:
                    pass

    if not candidates:
        raise FileNotFoundError(
            f"Field '{raw_field_id}' not found under {growers_dir}\n"
            f"Tried: normalized='{normalized}', exact directory, inventory CSV, and boundary GeoJSON"
        )
    if len(candidates) > 1:
        names = [f"{g}/{f}/{p.name}" for g, f, p in candidates]
        print(f"warning: multiple matches for '{raw_field_id}': {names}; using first", file=sys.stderr)
    return candidates[0]


def _resolve_field_location(farm_dir: Path, field_slug: str) -> str:
    boundary_file = farm_dir / "boundary" / "field_boundaries.geojson"
    if not boundary_file.exists():
        return ""
    try:
        import geopandas as _gpd
        fields = _gpd.read_file(boundary_file)
        match = fields[fields["field_id"].astype(str) == field_slug]
        if match.empty:
            match = fields[fields["field_id"].astype(str).str.replace("_", "-").str.lower()
                         == field_slug.replace("_", "-").lower()]
        if match.empty:
            return ""
        row = match.iloc[0]
        county = str(row.get("county_name", "")).strip()
        state_fips = str(row.get("state_fips", "")).strip().zfill(2)
        state = _FIPS_TO_STATE.get(state_fips, "")
        parts = [p for p in (state, county) if p]
        return " — ".join(parts) if parts else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_field_cdl(farm_tables_dir: Path, year: int, field_id: str) -> str | None:
    pattern = f"*_{year}_cdl.csv"
    matches = sorted(farm_tables_dir.glob(pattern))
    norm_id = str(field_id).replace("_", "-").replace(" ", "-").lower()
    for path in matches:
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        df["_norm_id"] = df["field_id"].astype(str).str.replace("_", "-").str.replace(" ", "-").str.lower()
        field_rows = df[df["_norm_id"] == norm_id]
        if field_rows.empty:
            field_rows = df[df["field_id"].astype(str) == str(field_id)]
        if field_rows.empty:
            continue
        if "pct" in field_rows.columns:
            dominant = field_rows.loc[field_rows["pct"].idxmax()]
            return str(dominant["crop_name"])
        return str(field_rows.iloc[0]["crop_name"])
    return None


def _load_crop_strategy_resource(
    crop_name: str, skill_base: Path
) -> str | None:
    thresholds = _load_crop_thresholds(crop_name)
    resource_name = thresholds.get("resource")
    if not resource_name:
        return None
    resource_path = (
        skill_base / "strategy" / "crop-strategy" / "resources" / resource_name
    )
    if resource_path.exists():
        return resource_path.read_text(encoding="utf-8")
    return None


def _load_field_weather(field_path: Path) -> pd.DataFrame | None:
    weather_path = field_path / "weather" / "daily_weather.csv"
    if not weather_path.exists():
        return None
    try:
        df = pd.read_csv(weather_path, parse_dates=["date"])
        required = {"date", "T2M", "T2M_MAX", "T2M_MIN", "PRECTOTCORR"}
        missing = required - set(df.columns)
        if missing:
            print(f"warning: weather CSV missing columns {missing}", file=sys.stderr)
            return None
        return df
    except Exception as exc:
        print(f"warning: failed to load weather: {exc}", file=sys.stderr)
    return None


def _load_field_ndvi(field_path: Path, year: int) -> pd.DataFrame | None:
    sentinel_dir = field_path / "satellite" / "sentinel"
    if not sentinel_dir.is_dir():
        return None

    manifest_path = sentinel_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = []
            for y_entry in manifest.get("years", []):
                if y_entry.get("year") != year:
                    continue
                for scene in y_entry.get("scenes", []):
                    ndvi_rel = scene.get("ndvi_tif") or ""
                    scene_date_str = str(scene.get("scene_date", ""))
                    ndvi_path = field_path.parents[2] / ndvi_rel
                    if not ndvi_path.exists():
                        ndvi_path = sentinel_dir / ndvi_rel
                    if not ndvi_path.exists():
                        date_parts = scene_date_str.replace("-", "")
                        ndvi_path = (
                            sentinel_dir
                            / str(year)
                            / f"sentinel_{date_parts}"
                            / f"sentinel_{date_parts}_ndvi.tif"
                        )
                    ndvi_val = _extract_mean_ndvi(ndvi_path)
                    if ndvi_val is not None:
                        rows.append({
                            "date": pd.to_datetime(scene_date_str),
                            "mean_ndvi": ndvi_val,
                        })
            if rows:
                return pd.DataFrame(rows)
        except Exception as exc:
            print(f"warning: failed to parse manifest: {exc}", file=sys.stderr)

    ndvi_rows = _scan_ndvi_tiffs(sentinel_dir, year)
    if ndvi_rows:
        return pd.DataFrame(ndvi_rows)

    csv = _find_ndvi_csv(sentinel_dir, year)
    if csv is not None:
        return csv

    return None


def _extract_mean_ndvi(tif_path: Path) -> float | None:
    if not _HAS_RASTERIO or not tif_path.exists():
        return None
    try:
        with rasterio.open(tif_path) as src:
            data = src.read(1)
            valid = data[~np.isnan(data)]
            if valid.size == 0:
                return None
            return float(np.mean(valid))
    except Exception:
        return None


def _scan_ndvi_tiffs(sentinel_dir: Path, year: int) -> list[dict]:
    rows: list[dict] = []
    pattern = f"{year}/*/sentinel_*_ndvi.tif"
    for tif_path in sorted(sentinel_dir.glob(pattern)):
        date_match = _extract_date_from_name(tif_path.stem)
        if date_match is None:
            continue
        ndvi_val = _extract_mean_ndvi(tif_path)
        if ndvi_val is not None:
            rows.append({"date": pd.to_datetime(date_match.isoformat()), "mean_ndvi": ndvi_val})
    return rows


def _extract_date_from_name(stem: str) -> date | None:
    parts = stem.split("_")
    for part in parts:
        if len(part) == 8 and part.isdigit():
            try:
                return datetime.strptime(part, "%Y%m%d").date()
            except ValueError:
                pass
    return None


def _find_ndvi_csv(sentinel_dir: Path, year: int) -> pd.DataFrame | None:
    for csv_path in sentinel_dir.glob("*.csv"):
        try:
            df = pd.read_csv(csv_path)
            date_col = None
            for col in ("date", "scene_date", "acquisition_date"):
                if col in df.columns:
                    date_col = col
                    break
            if date_col is None:
                continue
            if "mean_ndvi" not in df.columns:
                continue
            df[date_col] = pd.to_datetime(df[date_col])
            df = df[df[date_col].dt.year == year]
            if df.empty:
                continue
            return df.rename(columns={date_col: "date"})[["date", "mean_ndvi"]]
        except Exception:
            continue
    return None


def _find_ndvi_csv_in_derived(field_path: Path, year: int) -> pd.DataFrame | None:
    for csv_path in (field_path / "derived").rglob("*ndvi*.csv"):
        try:
            df = pd.read_csv(csv_path)
            date_col = None
            for col in ("date", "scene_date", "acquisition_date"):
                if col in df.columns:
                    date_col = col
                    break
            if date_col is None or "mean_ndvi" not in df.columns:
                continue
            df[date_col] = pd.to_datetime(df[date_col])
            df = df[df[date_col].dt.year == year]
            if df.empty:
                continue
            return df.rename(columns={date_col: "date"})[["date", "mean_ndvi"]]
        except Exception:
            continue
    return None


def _try_load_ndvi(field_path: Path, year: int) -> pd.DataFrame | None:
    ndvi = _load_field_ndvi(field_path, year)
    if ndvi is not None:
        return ndvi
    ndvi = _find_ndvi_csv_in_derived(field_path, year)
    if ndvi is not None:
        return ndvi
    alt_csv = field_path / "satellite" / "field_ndvi_stats.csv"
    if alt_csv.exists():
        try:
            df = pd.read_csv(alt_csv, parse_dates=["date"])
            if "mean_ndvi" in df.columns:
                df = df[df["date"].dt.year == year]
                if not df.empty:
                    return df[["date", "mean_ndvi"]]
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# GDD calculation
# ---------------------------------------------------------------------------

def _compute_gdd(
    weather: pd.DataFrame, base_c: float, cap_c: float
) -> pd.DataFrame:
    df = weather.copy()
    t_avg = (df["T2M_MAX"] + df["T2M_MIN"]) / 2.0
    gdd_raw = np.maximum(0.0, t_avg - base_c)
    if cap_c > base_c:
        gdd_raw = np.minimum(gdd_raw, cap_c - base_c)
    df["gdd"] = gdd_raw
    df["gdd_cumulative"] = df.sort_values("date")["gdd"].cumsum()
    return df


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------

def _detect_ndvi_events(ndvi_df: pd.DataFrame) -> list[dict]:
    events: list[dict] = []
    if ndvi_df is None or len(ndvi_df) < 2:
        return events
    df = ndvi_df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["mean_ndvi"]).reset_index(drop=True)
    if df.empty or len(df) < 2:
        return events
    peak_row = df.loc[df["mean_ndvi"].idxmax()]
    events.append({
        "doy": peak_row["date"].timetuple().tm_yday if hasattr(peak_row["date"], "timetuple") else 0,
        "label": f"Peak NDVI = {peak_row['mean_ndvi']:.2f}",
        "color": "#2e7d32",
    })
    for i in range(1, len(df)):
        delta = df.iloc[i]["mean_ndvi"] - df.iloc[i - 1]["mean_ndvi"]
        if delta < -0.15:
            d = df.iloc[i]["date"]
            doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
            events.append({
                "doy": doy,
                "label": "NDVI decline",
                "color": "#c62828",
            })
    for i in range(1, len(df)):
        vals_15d = df.iloc[max(0, i - 3):i + 1]["mean_ndvi"]
        if len(vals_15d) >= 2:
            rise = vals_15d.iloc[-1] - vals_15d.iloc[0]
            if rise > 0.3:
                d = df.iloc[i]["date"]
                doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
                events.append({
                    "doy": doy,
                    "label": "Rapid green-up",
                    "color": "#1b5e20",
                })
    return events


def _detect_precip_events(weather: pd.DataFrame) -> list[dict]:
    events: list[dict] = []
    if weather is None or weather.empty:
        return events
    df = weather.sort_values("date").reset_index(drop=True)
    for _, row in df.iterrows():
        if row["PRECTOTCORR"] > 25.0:
            d = row["date"]
            doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
            events.append({
                "doy": doy,
                "label": f"Heavy rain\n{row['PRECTOTCORR']:.0f} mm",
                "color": "#1565c0",
            })
    dry_doy = None
    dry_count = 0
    for _, row in df.iterrows():
        if row["PRECTOTCORR"] < 1.0:
            if dry_doy is None:
                dry_doy = row["date"]
            dry_count += 1
        else:
            if dry_count >= 10:
                d = dry_doy
                doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
                events.append({
                    "doy": doy,
                    "label": f"Dry spell\n{dry_count} days",
                    "color": "#bf360c",
                })
            dry_doy = None
            dry_count = 0
    if dry_count >= 10 and dry_doy is not None:
        doy = dry_doy.timetuple().tm_yday if hasattr(dry_doy, "timetuple") else 0
        events.append({
            "doy": doy,
            "label": f"Dry spell\n{dry_count} days",
            "color": "#bf360c",
        })
    return events


def _detect_temp_events(
    weather: pd.DataFrame, thresholds: dict
) -> list[dict]:
    events: list[dict] = []
    if weather is None or weather.empty:
        return events
    df = weather.sort_values("date").reset_index(drop=True)
    heat_thresh = thresholds.get("heat_threshold_c", 35.0)
    for _, row in df.iterrows():
        if row["T2M_MAX"] > heat_thresh:
            d = row["date"]
            doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
            events.append({
                "doy": doy,
                "label": "Heat stress",
                "color": "#d84315",
            })
    cool_doy = None
    cool_count = 0
    for _, row in df.iterrows():
        month = row["date"].month if hasattr(row["date"], "month") else 0
        if 5 <= month <= 7 and row["T2M_MAX"] < 20.0:
            if cool_doy is None:
                cool_doy = row["date"]
            cool_count += 1
        else:
            if cool_count >= 3:
                d = cool_doy
                doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
                events.append({
                    "doy": doy,
                    "label": "Cool period",
                    "color": "#0d47a1",
                })
            cool_doy = None
            cool_count = 0
    if cool_count >= 3 and cool_doy is not None:
        doy = cool_doy.timetuple().tm_yday if hasattr(cool_doy, "timetuple") else 0
        events.append({
            "doy": doy,
            "label": "Cool period",
            "color": "#0d47a1",
        })
    frost_thresh = thresholds.get("frost_threshold_c", 0.0)
    frost_doy = df.loc[df["T2M_MIN"] <= frost_thresh, "date"].apply(
        lambda d: d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
    )
    spring_frosts = frost_doy[frost_doy <= 182]
    fall_frosts = frost_doy[frost_doy > 182]
    if not spring_frosts.empty:
        events.append({
            "doy": int(spring_frosts.max()),
            "label": "Last spring frost",
            "color": "#1565c0",
        })
    if not fall_frosts.empty:
        events.append({
            "doy": int(fall_frosts.min()),
            "label": "First fall frost",
            "color": "#e65100",
        })
    return events


def _detect_gdd_events(
    weather_gdd: pd.DataFrame, thresholds: dict
) -> list[dict]:
    events: list[dict] = []
    if weather_gdd is None or weather_gdd.empty:
        return events
    stages = thresholds.get("stages", [])
    for stage_name, gdd_target, description in stages:
        cross = weather_gdd[weather_gdd["gdd_cumulative"] >= gdd_target]
        if not cross.empty:
            d = cross.iloc[0]["date"]
            doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
            events.append({
                "doy": doy,
                "label": f"{stage_name}",
                "color": "#4a148c",
            })
    return events


# ---------------------------------------------------------------------------
# Dashboard figure
# ---------------------------------------------------------------------------

def _build_dashboard(
    field_id: str,
    year: int,
    location_prefix: str = "",
    crop_name: str | None = None,
    weather: pd.DataFrame | None = None,
    weather_gdd: pd.DataFrame | None = None,
    ndvi: pd.DataFrame | None = None,
    ndvi_events: list[dict] | None = None,
    precip_events: list[dict] | None = None,
    temp_events: list[dict] | None = None,
    gdd_events: list[dict] | None = None,
    thresholds: dict | None = None,
    resource_text: str | None = None,
) -> plt.Figure:
    ndvi_events = ndvi_events or []
    precip_events = precip_events or []
    temp_events = temp_events or []
    gdd_events = gdd_events or []
    thresholds = thresholds or {}
    num_panels = 4
    fig, axes = plt.subplots(
        num_panels, 1, figsize=(14, 10), sharex=True,
        gridspec_kw={"height_ratios": [1, 1, 1, 1], "hspace": 0.35},
    )
    title = f"Field {field_id} — {year} Growing Season"
    if location_prefix:
        title = f"{location_prefix} {title}"
    fig.suptitle(title, fontsize=14, fontweight="bold", y=0.98)

    doy_all: list[int] = []

    def _doy(d):
        try:
            return pd.to_datetime(d).timetuple().tm_yday
        except Exception:
            return 0

    # ---- Panel 1: NDVI ----
    ax1 = axes[0]
    if ndvi is not None and not ndvi.empty:
        df = ndvi.sort_values("date").copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna(subset=["mean_ndvi"])
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        ax1.bar(
            df["doy"], df["mean_ndvi"], width=1.5, color="#2e7d32",
            alpha=0.7, edgecolor="none", zorder=2,
        )
        if len(df) >= 3 and df["doy"].nunique() > 2:
            x_smooth = np.linspace(df["doy"].min(), df["doy"].max(), 200)
            with np.errstate(invalid="ignore", divide="ignore"):
                try:
                    degree = min(len(df) - 1, 3)
                    coeffs = np.polyfit(df["doy"], df["mean_ndvi"], degree)
                    p = np.poly1d(coeffs)
                    trend = p(x_smooth)
                    ax1.plot(x_smooth, trend, color="#1b5e20", linewidth=1.5, alpha=0.8, zorder=3)
                except Exception:
                    ax1.plot(df["doy"], df["mean_ndvi"], color="#1b5e20", linewidth=1.2, alpha=0.8, zorder=3)
        _annotate_events(ax1, ndvi_events)
        ax1.set_ylabel("NDVI", fontsize=10)
        ax1.set_ylim(-0.1, 1.05)
        ax1.axhline(y=0, color="gray", linewidth=0.5)
    else:
        ax1.text(0.5, 0.5, "No NDVI data", ha="center", va="center", transform=ax1.transAxes, fontsize=11, color="gray")
        ax1.set_ylabel("NDVI", fontsize=10)

    # ---- Panel 2: Precipitation ----
    ax2 = axes[1]
    if weather is not None and not weather.empty:
        df = weather.sort_values("date").copy()
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        ax2.bar(
            df["doy"], df["PRECTOTCORR"], width=0.8, color="#1565c0",
            alpha=0.6, edgecolor="none", zorder=2,
        )
        df["precip_cumulative"] = df["PRECTOTCORR"].cumsum()
        ax2_twin = ax2.twinx()
        ax2_twin.plot(
            df["doy"], df["precip_cumulative"], color="#0d47a1",
            linewidth=1.5, alpha=0.8, zorder=3,
        )
        ax2_twin.set_ylabel("Cumulative (mm)", fontsize=8, color="#0d47a1")
        ax2_twin.tick_params(axis="y", labelsize=7, colors="#0d47a1")
        _annotate_events(ax2, precip_events)
        ax2.set_ylabel("Daily precip (mm)", fontsize=10)
    else:
        ax2.text(0.5, 0.5, "No precipitation data", ha="center", va="center", transform=ax2.transAxes, fontsize=11, color="gray")
        ax2.set_ylabel("Daily precip (mm)", fontsize=10)

    # ---- Panel 3: Temperature (no cumulative line) ----
    ax3 = axes[2]
    if weather is not None and not weather.empty:
        df = weather.sort_values("date").copy()
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        doy = df["doy"].values
        temp = df["T2M"].values
        ax3.fill_between(
            doy, 0, temp, where=(temp >= 0),
            color="#d32f2f", alpha=0.45, zorder=2, label="Above 0°C",
        )
        ax3.fill_between(
            doy, 0, temp, where=(temp < 0),
            color="#1565c0", alpha=0.45, zorder=2, label="Below 0°C",
        )
        ax3.axhline(y=0, color="gray", linewidth=0.5, zorder=3)
        heat_thresh = thresholds.get("heat_threshold_c", 35.0)
        ax3.axhline(y=heat_thresh, color="#d84315", linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.text(2, heat_thresh + 0.5, f"Heat threshold {heat_thresh}°C", fontsize=6, color="#d84315", alpha=0.7)
        _annotate_events(ax3, temp_events)
        ax3.set_ylabel("Temperature (°C)", fontsize=10)
        ax3.legend(fontsize=6, loc="upper right")
    else:
        ax3.text(0.5, 0.5, "No temperature data", ha="center", va="center", transform=ax3.transAxes, fontsize=11, color="gray")
        ax3.set_ylabel("Temperature (°C)", fontsize=10)

    # ---- Panel 4: Cumulative GDD ----
    ax4 = axes[3]
    if weather_gdd is not None and not weather_gdd.empty:
        df = weather_gdd.sort_values("date").copy()
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        ax4.bar(
            df["doy"], df["gdd"], width=0.8, color="#6a1b9a",
            alpha=0.5, edgecolor="none", zorder=2,
        )
        ax4_twin = ax4.twinx()
        cum_norm = df["gdd_cumulative"] / df["gdd_cumulative"].max() if df["gdd_cumulative"].max() > 0 else df["gdd_cumulative"]
        ax4_twin.plot(
            df["doy"], df["gdd_cumulative"], color="#4a148c",
            linewidth=1.5, alpha=0.8, zorder=3,
        )
        max_cum = df["gdd_cumulative"].max()
        ax4_twin.set_ylabel(f"Cumulative GDD (max {max_cum:.0f})", fontsize=8, color="#4a148c")
        ax4_twin.tick_params(axis="y", labelsize=7, colors="#4a148c")
        _annotate_events(ax4, gdd_events)
        ax4.set_ylabel("Daily GDD", fontsize=10)
        ax4.set_xlabel("Day of Year", fontsize=10)
    else:
        ax4.text(0.5, 0.5, "No weather data for GDD", ha="center", va="center", transform=ax4.transAxes, fontsize=11, color="gray")
        ax4.set_ylabel("Daily GDD", fontsize=10)
        ax4.set_xlabel("Day of Year", fontsize=10)

    # ---- Shared x-axis ----
    if doy_all:
        x_min, x_max = max(1, min(doy_all) - 5), min(366, max(doy_all) + 5)
        for ax in axes:
            ax.set_xlim(x_min, x_max)
            ax.grid(True, axis="x", alpha=0.15)
            ax.grid(True, axis="y", alpha=0.2)

    for i in range(num_panels):
        axes[i].tick_params(axis="y", labelsize=8)
    axes[-1].tick_params(axis="x", labelsize=8)

    _add_caption_box(
        fig, field_id, year, crop_name, thresholds,
        ndvi_events, precip_events, temp_events, gdd_events,
        resource_text,
    )

    return fig


def _annotate_events(ax, events: list[dict]):
    y_min, y_max = ax.get_ylim()
    used_ys: dict[int, float] = {}
    for ev in events:
        doy = ev["doy"]
        offset_y = y_max * 0.05
        base_y = y_max * 0.85
        col = used_ys.get(doy, 0) * offset_y * 2
        used_ys[doy] = used_ys.get(doy, 0) + 1
        y_pos = base_y - col
        ax.axvline(x=doy, color=ev["color"], linewidth=0.8, linestyle=":", alpha=0.6, zorder=1)
        ax.annotate(
            ev["label"], xy=(doy, y_pos),
            fontsize=5.5, color=ev["color"],
            ha="center", va="bottom",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor=ev["color"], alpha=0.7, linewidth=0.5),
            zorder=5,
        )


def _add_caption_box(
    fig,
    field_id: str,
    year: int,
    crop_name: str | None,
    thresholds: dict,
    ndvi_events: list[dict],
    precip_events: list[dict],
    temp_events: list[dict],
    gdd_events: list[dict],
    resource_text: str | None,
):
    lines: list[str] = []
    if crop_name:
        lines.append(f"Crop: {crop_name}")
    gdd_base = thresholds.get("gdd_base_c", 10)
    resource_name = thresholds.get("resource")
    lines.append(f"GDD base: {gdd_base}°C")
    if resource_name:
        lines.append(f"Reference: {resource_name}")

    ndvi_text = "; ".join(
        sorted(set(e["label"] for e in ndvi_events if e["doy"] > 0))
    )
    precip_text = "; ".join(
        sorted(set(e["label"] for e in precip_events if e["doy"] > 0))
    )
    temp_text = "; ".join(
        sorted(set(e["label"] for e in temp_events if e["doy"] > 0))
    )
    gdd_text = "; ".join(
        sorted(set(e["label"] for e in gdd_events if e["doy"] > 0))
    )

    if ndvi_text:
        lines.append(f"NDVI: {ndvi_text}")
    if precip_text:
        lines.append(f"Precip: {precip_text}")
    if temp_text:
        lines.append(f"Temp: {temp_text}")
    if gdd_text:
        lines.append(f"GDD: {gdd_text}")

    if not lines:
        lines.append("No notable events detected for this season.")

    full_text = "\n".join(lines)
    fig.text(
        0.5, 0.01, full_text,
        fontsize=7, ha="center", va="bottom",
        family="monospace",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#f5f5f5", edgecolor="#bdbdbd", alpha=0.9),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_field_year_dashboard(
    field_id: str,
    year: int,
    data_root: str | Path | None = None,
    skill_base: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    if data_root is None:
        data_root = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if not data_root:
        raise RuntimeError(
            "DATA_PIPELINE_DATA_ROOT is not set. "
            "Pass data_root= or export DATA_PIPELINE_DATA_ROOT."
        )
    data_root = Path(data_root).expanduser().resolve()
    runtime_base = data_root / "data-pipeline"

    if skill_base is None:
        skill_base = Path(__file__).resolve().parents[3]
    skill_base = Path(skill_base)

    grower_slug, farm_slug, field_path = _resolve_field(runtime_base, field_id)
    farm_dir = field_path.parents[1]
    farm_tables_dir = farm_dir / "derived" / "tables"
    location = _resolve_field_location(farm_dir, field_path.name)
    location_prefix = f"{grower_slug} — {location} |" if location else f"{grower_slug} |"

    print(f"grower: {grower_slug}, farm: {farm_slug}, field: {field_path.name}")

    crop_name = _load_field_cdl(farm_tables_dir, year, field_id)
    if crop_name:
        print(f"dominant CDL crop: {crop_name}")
    else:
        print("warning: could not determine CDL crop", file=sys.stderr)

    thresholds = _load_crop_thresholds(crop_name or "Unknown")
    resource_text = _load_crop_strategy_resource(crop_name or "Unknown", skill_base)

    weather = _load_field_weather(field_path)
    if weather is not None:
        weather_year = weather[weather["date"].dt.year == year].copy()
        if weather_year.empty:
            weather_year = weather[weather["date"].dt.year == year - 1].copy()
        weather = weather_year if not weather_year.empty else None

    weather_gdd = None
    if weather is not None:
        weather_gdd = _compute_gdd(weather, thresholds["gdd_base_c"], thresholds["gdd_cap_c"])

    ndvi = _try_load_ndvi(field_path, year)

    ndvi_events = _detect_ndvi_events(ndvi)
    precip_events = _detect_precip_events(weather)
    temp_events = _detect_temp_events(weather, thresholds)
    gdd_events = _detect_gdd_events(weather_gdd, thresholds)

    fig = _build_dashboard(
        field_id=field_id,
        year=year,
        location_prefix=location_prefix,
        crop_name=crop_name,
        weather=weather,
        weather_gdd=weather_gdd,
        ndvi=ndvi,
        ndvi_events=ndvi_events,
        precip_events=precip_events,
        temp_events=temp_events,
        gdd_events=gdd_events,
        thresholds=thresholds,
        resource_text=resource_text,
    )

    if output_path is None:
        reports_dir = field_path / "derived" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        output_path = reports_dir / f"{year}_field_dashboard.png"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Dashboard saved: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate a field-year dashboard image."
    )
    parser.add_argument(
        "--field-id", required=True, help="Field identifier (e.g., OSM_1428284928)"
    )
    parser.add_argument(
        "--year", type=int, required=True, help="Target year (e.g., 2024)"
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="DATA_PIPELINE_DATA_ROOT (defaults to env var)",
    )
    parser.add_argument(
        "--skill-base",
        default=None,
        help="Path to my-farm-advisor skill root (for strategy resources)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output PNG path (default: field/derived/reports/{year}_field_dashboard.png)",
    )
    args = parser.parse_args()
    generate_field_year_dashboard(
        field_id=args.field_id,
        year=args.year,
        data_root=args.data_root,
        skill_base=args.skill_base,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
