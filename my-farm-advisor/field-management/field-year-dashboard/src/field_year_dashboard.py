from __future__ import annotations

import argparse
import json
import os
import re
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

_PLANTING_DOY: dict[str, int] = {
    "Corn": 110,
    "Soybeans": 130,
    "Cotton": 115,
    "Winter Wheat": 265,
    "Sorghum": 138,
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


def _resolve_field_location(farm_dir: Path, field_slug: str) -> tuple[str, str]:
    boundary_file = farm_dir / "boundary" / "field_boundaries.geojson"
    if not boundary_file.exists():
        return ("", "")
    try:
        import geopandas as _gpd
        fields = _gpd.read_file(boundary_file)
        match = fields[fields["field_id"].astype(str) == field_slug]
        if match.empty:
            match = fields[fields["field_id"].astype(str).str.replace("_", "-").str.lower()
                         == field_slug.replace("_", "-").lower()]
        if match.empty:
            return ("", "")
        row = match.iloc[0]
        county = str(row.get("county_name", "")).strip()
        state_fips = str(row.get("state_fips", "")).strip().zfill(2)
        county_fips = str(row.get("county_fips", "")).strip().zfill(3)
        state = _FIPS_TO_STATE.get(state_fips, "")
        parts = [p for p in (state, county) if p]
        location_str = " — ".join(parts) if parts else ""
        fips = (state_fips + county_fips) if (state_fips != "00" and county_fips != "000") else ""
        return (location_str, fips)
    except Exception:
        return ("", "")


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


def _load_county_maturity(
    data_root: Path, fips: str, year: int, crop_name: str | None
) -> dict:
    if not fips or not crop_name:
        return {}
    crop_lower = crop_name.lower()
    result: dict[str, object] = {}
    shared_dir = data_root / "data-pipeline" / "shared"
    try:
        if "corn" in crop_lower:
            parquet_path = shared_dir / "corn_maturity" / f"rm_by_fips_{year}.parquet"
            if parquet_path.exists():
                df = pd.read_parquet(parquet_path)
                df["fips"] = df["fips"].astype(str).str.zfill(5)
                match = df[df["fips"] == fips]
                if not match.empty:
                    result["rm"] = float(match.iloc[0]["rm_relative_maturity"])
                    result["gdd_total"] = float(match.iloc[0].get("gdd_total_c", 0))
        elif "soybean" in crop_lower:
            parquet_path = shared_dir / "soybean_maturity" / f"mg_by_fips_{year}.parquet"
            if parquet_path.exists():
                df = pd.read_parquet(parquet_path)
                df["fips"] = df["fips"].astype(str).str.zfill(5)
                match = df[df["fips"] == fips]
                if not match.empty:
                    result["mg"] = float(match.iloc[0]["mg_optimal"])
                    result["mg_early"] = float(match.iloc[0].get("mg_early", 0))
                    result["mg_late"] = float(match.iloc[0].get("mg_late", 0))
    except Exception:
        pass
    return result


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
    decline_indices: list[int] = []
    for i in range(1, len(df)):
        delta = df.iloc[i]["mean_ndvi"] - df.iloc[i - 1]["mean_ndvi"]
        if delta < -0.15:
            decline_indices.append(i)
    if decline_indices:
        runs: list[list[int]] = []
        current_run = [decline_indices[0]]
        for idx in decline_indices[1:]:
            if idx == current_run[-1] + 1:
                current_run.append(idx)
            else:
                runs.append(current_run)
                current_run = [idx]
        runs.append(current_run)
        for run in runs:
            if len(run) >= 2:
                d_start = df.iloc[run[0] - 1]["date"]
                d_end = df.iloc[run[-1]]["date"]
                doy_start = d_start.timetuple().tm_yday if hasattr(d_start, "timetuple") else 0
                doy_end = d_end.timetuple().tm_yday if hasattr(d_end, "timetuple") else 0
                events.append({
                    "doy": doy_start,
                    "doy_end": doy_end,
                    "label": "NDVI decline",
                    "color": "#d0d0d0",
                    "label_color": "#e65100",
                })
            else:
                d = df.iloc[run[0]]["date"]
                doy = d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
                events.append({
                    "doy": doy,
                    "label": "NDVI decline",
                    "color": "#d0d0d0",
                    "label_color": "#e65100",
                })
    gu_start = None
    gu_end = None
    for i in range(1, len(df)):
        vals_15d = df.iloc[max(0, i - 3):i + 1]["mean_ndvi"]
        if len(vals_15d) >= 2:
            rise = vals_15d.iloc[-1] - vals_15d.iloc[0]
            if rise > 0.3:
                if gu_start is None:
                    gu_start = df.iloc[i]["date"]
                gu_end = df.iloc[i]["date"]
            else:
                if gu_start is not None:
                    doy_start = gu_start.timetuple().tm_yday if hasattr(gu_start, "timetuple") else 0
                    doy_end = gu_end.timetuple().tm_yday if hasattr(gu_end, "timetuple") else 0
                    events.append({
                        "doy": doy_start,
                        "doy_end": doy_end,
                        "label": "Rapid green-up",
                        "color": "#1b5e20",
                    })
                    gu_start = None
                    gu_end = None
    if gu_start is not None:
        doy_start = gu_start.timetuple().tm_yday if hasattr(gu_start, "timetuple") else 0
        doy_end = gu_end.timetuple().tm_yday if hasattr(gu_end, "timetuple") else 0
        events.append({
            "doy": doy_start,
            "doy_end": doy_end,
            "label": "Rapid green-up",
            "color": "#1b5e20",
        })
    return events


def _detect_precip_events(weather: pd.DataFrame) -> list[dict]:
    events: list[dict] = []
    if weather is None or weather.empty:
        return events
    df = weather.sort_values("date").reset_index(drop=True)
    _to_doy = lambda d: d.timetuple().tm_yday if hasattr(d, "timetuple") else 0
    # Heavy rain: group consecutive (≤1 day gap) heavy rain days into bands
    hr_start = None
    hr_end = None
    hr_count = 0
    hr_total = 0.0
    hr_gap = 0
    for _, row in df.iterrows():
        if row["PRECTOTCORR"] > 25.0:
            if hr_start is None:
                hr_start = row["date"]
            hr_end = row["date"]
            hr_count += 1
            hr_total += row["PRECTOTCORR"]
            hr_gap = 0
        else:
            if hr_count > 0:
                hr_gap += 1
                if hr_gap >= 2:
                    if hr_count >= 2:
                        events.append({
                            "doy": _to_doy(hr_start),
                            "doy_end": _to_doy(hr_end),
                            "label": f"Heavy rain\n{hr_count} days, {hr_total:.0f} mm",
                            "color": "#1565c0",
                        })
                    else:
                        events.append({
                            "doy": _to_doy(hr_start),
                            "label": f"Heavy rain\n{hr_total:.0f} mm",
                            "color": "#1565c0",
                        })
                    hr_start = None; hr_end = None; hr_count = 0; hr_total = 0.0; hr_gap = 0
    if hr_count >= 2:
        events.append({
            "doy": _to_doy(hr_start),
            "doy_end": _to_doy(hr_end),
            "label": f"Heavy rain\n{hr_count} days, {hr_total:.0f} mm",
            "color": "#1565c0",
        })
    elif hr_count == 1:
        events.append({
            "doy": _to_doy(hr_start),
            "label": f"Heavy rain\n{hr_total:.0f} mm",
            "color": "#1565c0",
        })
    dry_doy = None
    dry_last_date = None
    dry_count = 0
    for _, row in df.iterrows():
        if row["PRECTOTCORR"] < 1.0:
            if dry_doy is None:
                dry_doy = row["date"]
            dry_last_date = row["date"]
            dry_count += 1
        else:
            if dry_count >= 7:
                doy_start = dry_doy.timetuple().tm_yday if hasattr(dry_doy, "timetuple") else 0
                doy_end = dry_last_date.timetuple().tm_yday if hasattr(dry_last_date, "timetuple") else doy_start
                events.append({
                    "doy": doy_start,
                    "doy_end": doy_end,
                    "label": f"Dry spell\n{dry_count} days",
                    "color": "#e65100",
                })
            dry_doy = None
            dry_last_date = None
            dry_count = 0
    if dry_count >= 7 and dry_doy is not None:
        doy_start = dry_doy.timetuple().tm_yday if hasattr(dry_doy, "timetuple") else 0
        doy_end = dry_last_date.timetuple().tm_yday if hasattr(dry_last_date, "timetuple") else doy_start
        events.append({
            "doy": doy_start,
            "doy_end": doy_end,
            "label": f"Dry spell\n{dry_count} days",
            "color": "#e65100",
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
    _to_doy = lambda d: d.timetuple().tm_yday if hasattr(d, "timetuple") else 0

    # Heat-wave detection: group consecutive (≤1 day gap) hot days into spans
    hw_start = None
    hw_end = None
    hw_count = 0
    hw_gap = 0
    for _, row in df.iterrows():
        if row["T2M_MAX"] > heat_thresh:
            if hw_start is None:
                hw_start = row["date"]
            hw_end = row["date"]
            hw_count += 1
            hw_gap = 0
        else:
            if hw_count > 0:
                hw_gap += 1
                if hw_gap >= 2:
                    if hw_count >= 2:
                        events.append({
                            "doy": _to_doy(hw_start),
                            "doy_end": _to_doy(hw_end),
                            "label": f"Heat wave\n{hw_count} days",
                            "color": "#e53935",
                        })
                    hw_start = None; hw_end = None; hw_count = 0; hw_gap = 0
    if hw_count >= 2:
        events.append({
            "doy": _to_doy(hw_start),
            "doy_end": _to_doy(hw_end),
            "label": f"Heat wave\n{hw_count} days",
            "color": "#e53935",
        })

    # Cool-period detection: 3+ consecutive cool days in May–Jul → shaded span
    cool_start = None
    cool_end = None
    cool_count = 0
    for _, row in df.iterrows():
        month = row["date"].month if hasattr(row["date"], "month") else 0
        if 5 <= month <= 7 and row["T2M_MAX"] < 20.0:
            if cool_start is None:
                cool_start = row["date"]
            cool_end = row["date"]
            cool_count += 1
        else:
            if cool_count >= 3:
                events.append({
                    "doy": _to_doy(cool_start),
                    "doy_end": _to_doy(cool_end),
                    "label": f"Cool period\n{cool_count} days",
                    "color": "#1e88e5",
                })
            cool_start = None; cool_end = None; cool_count = 0
    if cool_count >= 3 and cool_start is not None:
        events.append({
            "doy": _to_doy(cool_start),
            "doy_end": _to_doy(cool_end),
            "label": f"Cool period\n{cool_count} days",
            "color": "#1e88e5",
        })

    frost_thresh = thresholds.get("frost_threshold_c", 0.0)
    frost_doy = df.loc[df["T2M_MIN"] <= frost_thresh, "date"].apply(_to_doy)
    spring_frosts = frost_doy[frost_doy <= 182]
    fall_frosts = frost_doy[frost_doy > 182]
    if not spring_frosts.empty:
        events.append({
            "doy": int(spring_frosts.max()),
            "label": "Last frost",
            "color": "#1565c0",
        })
    if not fall_frosts.empty:
        events.append({
            "doy": int(fall_frosts.min()),
            "label": "First frost",
            "color": "#1565c0",
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
            match = re.match(r'^(\S+)\s*\((.+)\)$', stage_name)
            code = match.group(1) if match else stage_name
            desc = match.group(2) if match else description
            events.append({
                "doy": doy,
                "label": code,
                "descriptor": desc,
                "color": "#333333",
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
    maturity_info: dict | None = None,
) -> plt.Figure:
    ndvi_events = ndvi_events or []
    precip_events = precip_events or []
    temp_events = temp_events or []
    gdd_events = gdd_events or []
    thresholds = thresholds or {}
    maturity_info = maturity_info or {}
    num_panels = 4
    fig, axes = plt.subplots(
        num_panels, 1, figsize=(14, 10), sharex=True,
        gridspec_kw={"height_ratios": [1, 1, 1, 1], "hspace": 0.60},
    )
    title = f"Field {field_id} — {year} Growing Season"
    if location_prefix:
        title = f"{location_prefix} {title}"
    fig.suptitle(title, fontsize=14, fontweight="bold", y=0.98)
    fig.subplots_adjust(top=0.82)

    subtitle_parts = []
    if crop_name:
        subtitle_parts.append(f"Crop: {crop_name}")
    stage_descriptors = [f"{ev['label']} - {ev['descriptor']}" for ev in gdd_events if ev.get("descriptor")]
    if stage_descriptors:
        subtitle_parts.append(" | ".join(stage_descriptors))
    fig.text(
        0.5, 0.93, "  |  ".join(subtitle_parts),
        fontsize=9, ha="center", va="top",
        color="#555555",
    )

    summary = _build_interpretive_summary(
        crop_name, gdd_events, ndvi_events, precip_events, temp_events,
    )
    if summary:
        fig.text(
            0.5, 0.90, summary,
            fontsize=7.5, ha="center", va="top",
            color="#666666", style="italic",
        )

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
        ndvi_cmap = plt.get_cmap("RdYlGn")
        ndvi_norm = plt.Normalize(vmin=0, vmax=1.0)
        bar_colors = [ndvi_cmap(ndvi_norm(v)) for v in df["mean_ndvi"]]
        ax1.bar(
            df["doy"], df["mean_ndvi"], width=1.5, color=bar_colors,
            alpha=0.8, edgecolor="none", zorder=2,
        )
        ax1.plot(df["doy"], df["mean_ndvi"], color="#555555", linewidth=1.0, alpha=0.7, zorder=3, marker="o", markersize=2)
        _annotate_events(ax1, gdd_events, y_anchor=0.9)
        _annotate_events(ax1, ndvi_events, y_anchor=0.75)
        ax1.set_ylabel("NDVI", fontsize=10)
        ax1.set_ylim(0, 1.05)
        ax1.axhline(y=0, color="gray", linewidth=0.5)
    else:
        ax1.text(0.5, 0.5, "No NDVI data", ha="center", va="center", transform=ax1.transAxes, fontsize=11, color="gray")
        ax1.set_ylabel("NDVI", fontsize=10)
    ax1.set_title("NDVI Dynamics", fontsize=11, fontweight="bold", pad=18)

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
        _annotate_events(ax2, gdd_events, y_anchor=0.9)
        heavy_rain_events = [ev for ev in precip_events if not ev.get("label", "").startswith("Dry spell")]
        dry_spell_events = [ev for ev in precip_events if ev.get("label", "").startswith("Dry spell")]
        _annotate_events(ax2, heavy_rain_events, y_anchor=0.7)
        _annotate_events(ax2, dry_spell_events, y_anchor=0.5)
        ax2.set_ylabel("Daily precip (mm)", fontsize=10)
    else:
        ax2.text(0.5, 0.5, "No precipitation data", ha="center", va="center", transform=ax2.transAxes, fontsize=11, color="gray")
        ax2.set_ylabel("Daily precip (mm)", fontsize=10)
    ax2.set_title("Daily Precipitation", fontsize=11, fontweight="bold", pad=18)

    # ---- Panel 3: Temperature ----
    ax3 = axes[2]
    if weather is not None and not weather.empty:
        df = weather.sort_values("date").copy()
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        doy = df["doy"].values
        temp = df["T2M"].values
        ax3.fill_between(
            doy, 0, temp, where=(temp >= 0),
            color="#d4a017", alpha=0.45, zorder=2, label="Above 0°C",
        )
        ax3.fill_between(
            doy, 0, temp, where=(temp < 0),
            color="#00796b", alpha=0.45, zorder=2, label="Below 0°C",
        )
        ax3.axhline(y=0, color="gray", linewidth=0.5, zorder=3)
        heat_thresh = thresholds.get("heat_threshold_c", 35.0)
        ax3.axhline(y=heat_thresh, color="#d84315", linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.text(2, heat_thresh + 0.5, f"Heat threshold {heat_thresh}°C", fontsize=6, color="#d84315", alpha=0.7)
        gdd_base_line = thresholds.get("gdd_base_c", 10.0)
        ax3.axhline(y=gdd_base_line, color="#7b1fa2", linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.text(2, gdd_base_line + 0.5, f"GDD base {gdd_base_line}°C", fontsize=6, color="#7b1fa2", alpha=0.7)
        ax3.set_ylim(top=max(temp.max(), heat_thresh) + 6)
        _annotate_events(ax3, gdd_events, y_anchor=0.9)
        other_temp_events = [ev for ev in temp_events if ev.get("label", "") not in ("Last frost", "First frost")]
        _annotate_events(ax3, other_temp_events, y_anchor=0.1, stack_upward=True)
        ax3.set_ylabel("Temperature (°C)", fontsize=10)
    else:
        ax3.text(0.5, 0.5, "No temperature data", ha="center", va="center", transform=ax3.transAxes, fontsize=11, color="gray")
        ax3.set_ylabel("Temperature (°C)", fontsize=10)
    ax3.set_title("Air Temperature", fontsize=11, fontweight="bold", pad=18)

    # ---- Panel 4: Cumulative GDD ----
    ax4 = axes[3]
    if weather_gdd is not None and not weather_gdd.empty:
        df = weather_gdd.sort_values("date").copy()
        df["doy"] = df["date"].apply(_doy)
        doy_all.extend(df["doy"].tolist())
        ax4.bar(
            df["doy"], df["gdd"], width=1.0, color="#ce93d8",
            alpha=0.6, edgecolor="#6a1b9a", linewidth=0.3, zorder=2,
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
        _annotate_events(ax4, gdd_events, y_anchor=0.9)
        ax4.set_ylabel("Daily GDD", fontsize=10)
        ax4.set_xlabel("Day of Year", fontsize=10)
    else:
        ax4.text(0.5, 0.5, "No weather data for GDD", ha="center", va="center", transform=ax4.transAxes, fontsize=11, color="gray")
        ax4.set_ylabel("Daily GDD", fontsize=10)
        ax4.set_xlabel("Day of Year", fontsize=10)
    ax4.set_title("Cumulative Growing Degree Days", fontsize=11, fontweight="bold", pad=18)

    # ---- Shared x-axis ----
    if doy_all:
        x_min, x_max = max(1, min(doy_all) - 5), min(366, max(doy_all) + 5)
        for ax in axes:
            ax.set_xlim(x_min, x_max)
            ax.grid(True, axis="x", alpha=0.15)
            ax.grid(True, axis="y", alpha=0.2)

    for i in range(num_panels):
        axes[i].tick_params(axis="y", labelsize=8)
        axes[i].tick_params(axis="x", labelsize=8, labelbottom=True)

    if ndvi is not None and not ndvi.empty:
        bbox = ax1.get_position()
        cax = fig.add_axes([bbox.x1 + 0.005, bbox.y0, 0.01, bbox.height])
        matplotlib.colorbar.ColorbarBase(
            cax, cmap="RdYlGn",
            norm=plt.Normalize(vmin=0, vmax=1.0),
            orientation="vertical",
        )
        cax.tick_params(labelsize=7)

    return fig


def _build_interpretive_summary(
    crop_name: str | None,
    gdd_events: list[dict],
    ndvi_events: list[dict],
    precip_events: list[dict],
    temp_events: list[dict],
) -> str:
    context_parts: list[str] = []
    impact_parts: list[str] = []
    _PROXIMITY = 5
    _to_doy = lambda d: d.timetuple().tm_yday if hasattr(d, "timetuple") else 0

    sorted_events = sorted(
        [ev for ev in gdd_events if ev.get("doy") and ev.get("label") and ev.get("descriptor")],
        key=lambda e: e["doy"],
    )
    stage_ranges: list[dict] = []
    for i, ev in enumerate(sorted_events):
        end_doy = sorted_events[i + 1]["doy"] - 1 if i + 1 < len(sorted_events) else 366
        stage_ranges.append({
            "label": ev["label"],
            "descriptor": ev.get("descriptor", ""),
            "start_doy": ev["doy"],
            "end_doy": end_doy,
        })

    def _find_stage(doy: int) -> dict | None:
        for sr in stage_ranges:
            if sr["start_doy"] - _PROXIMITY <= doy <= sr["end_doy"] + _PROXIMITY:
                return sr
        return None

    def _stage_span(doy_start: int, doy_end: int) -> list[dict]:
        start = doy_start - _PROXIMITY
        end = doy_end + _PROXIMITY
        return [sr for sr in stage_ranges
                if sr["start_doy"] - _PROXIMITY <= end and sr["end_doy"] + _PROXIMITY >= start]

    def _stage_label(sr: dict) -> str:
        desc = sr.get("descriptor", "")
        return f"{sr['label']} ({desc})" if desc else sr["label"]

    # Peak NDVI with full stage label
    for ev in ndvi_events:
        label = ev.get("label", "")
        if "peak" in label.lower():
            doy = ev.get("doy", 0)
            sr = _find_stage(doy)
            val = label.replace("Peak NDVI = ", "")
            stage_info = f" during {_stage_label(sr)}" if sr else ""
            context_parts.append(f"Peak NDVI {val}{stage_info}.")
            break

    # Dry spells during moisture-critical stages
    for ev in precip_events:
        label = ev.get("label", "")
        if "dry spell" in label.lower():
            doy_start = ev.get("doy", 0)
            doy_end = ev.get("doy_end", doy_start)
            stages = _stage_span(doy_start, doy_end)
            critical = [s for s in stages if any(w in s.get("descriptor", "").lower() for w in ("water", "moisture", "critical"))]
            if critical:
                stage_names = " → ".join(_stage_label(s) for s in critical)
                days = label.replace("Dry spell\n", "").replace(" days", "")
                impact_parts.append(f"A {days}-day dry spell may have stressed {stage_names}.")

    # Heat waves during heat-sensitive stages
    for ev in temp_events:
        label = ev.get("label", "")
        if "heat wave" in label.lower():
            doy_start = ev.get("doy", 0)
            doy_end = ev.get("doy_end", doy_start)
            stages = _stage_span(doy_start, doy_end)
            sensitive = [s for s in stages if any(w in s.get("descriptor", "").lower() for w in ("heat", "sensitive"))]
            if sensitive:
                stage_names = " → ".join(_stage_label(s) for s in sensitive)
                days = label.replace("Heat wave\n", "").replace(" days", "")
                impact_parts.append(f"A {days}-day heat wave likely stressed {stage_names}.")

    # Cool periods during early vegetative stages
    for ev in temp_events:
        label = ev.get("label", "")
        if "cool period" in label.lower():
            doy_start = ev.get("doy", 0)
            doy_end = ev.get("doy_end", doy_start)
            stages = _stage_span(doy_start, doy_end)
            early_veg = [s for s in stages if any(w in s.get("descriptor", "").lower() for w in ("vegetative", "nodulation", "establish", "emergence"))]
            if early_veg:
                stage_names = " → ".join(_stage_label(s) for s in early_veg)
                days = label.replace("Cool period\n", "").replace(" days", "")
                impact_parts.append(f"A {days}-day cool spell may have slowed {stage_names}.")

    # Frost only if it falls within a growth stage (before final maturity)
    last_stage = stage_ranges[-1] if stage_ranges else None
    for ev in temp_events:
        label = ev.get("label", "")
        if "last frost" in label.lower() or "first frost" in label.lower():
            doy = ev.get("doy", 0)
            sr = _find_stage(doy)
            if sr and sr != last_stage:
                impact_parts.append(f"{label} likely arrived during {_stage_label(sr)}.")

    if impact_parts:
        return " ".join(context_parts + impact_parts)
    return "No major weather impacts detected during key growth stages this season."


def _annotate_events(ax, events: list[dict], y_anchor: float = 0.85, stack_upward: bool = False):
    used_ys: dict[int, float] = {}
    direction = 1 if stack_upward else -1
    for ev in events:
        doy = ev["doy"]
        doy_end = ev.get("doy_end")
        span_mid = doy if doy_end is None else (doy + doy_end) / 2
        offset_y = 0.05
        base_y = y_anchor
        col = used_ys.get(span_mid, 0) * offset_y * 2
        used_ys[span_mid] = used_ys.get(span_mid, 0) + 1
        y_pos = base_y + direction * col
        label_color = ev.get("label_color", ev["color"])
        if doy_end is not None:
            ax.axvspan(doy, doy_end, color=ev["color"], alpha=0.12, zorder=1)
        else:
            ax.axvline(x=doy, color=ev["color"], linewidth=0.8, linestyle=":", alpha=0.6, zorder=1)
        ax.annotate(
            ev["label"], xy=(span_mid, y_pos),
            fontsize=5.5, color=label_color,
            ha="center", va="bottom",
            xycoords=ax.get_xaxis_transform(),
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor=label_color, alpha=0.7, linewidth=0.5),
            zorder=5,
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
    location, fips = _resolve_field_location(farm_dir, field_path.name)
    location_prefix = f"{grower_slug} — {location} —" if location else f"{grower_slug} —"

    print(f"grower: {grower_slug}, farm: {farm_slug}, field: {field_path.name}")

    crop_name = _load_field_cdl(farm_tables_dir, year, field_id)
    if crop_name:
        print(f"dominant CDL crop: {crop_name}")
    else:
        print("warning: could not determine CDL crop", file=sys.stderr)

    thresholds = _load_crop_thresholds(crop_name or "Unknown")

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

    planting_doy = _PLANTING_DOY.get(crop_name or "")
    if planting_doy:
        gdd_events = [{
            "doy": planting_doy,
            "label": "Planting",
            "color": "#333333",
        }] + gdd_events

    for ev in temp_events:
        label = ev.get("label", "")
        if label in ("Last frost", "First frost"):
            gdd_events.append({
                "doy": ev["doy"],
                "label": label,
                "color": "#1565c0",
            })

    maturity_info = _load_county_maturity(data_root, fips, year, crop_name)

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
        maturity_info=maturity_info,
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
