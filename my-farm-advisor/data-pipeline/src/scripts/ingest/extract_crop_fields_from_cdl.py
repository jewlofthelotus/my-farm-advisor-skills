#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportArgumentType=false, reportCallIssue=false, reportReturnType=false, reportGeneralTypeIssues=false
"""Bootstrap a farm from CDL crop-classified pixels in a target county.

This utility creates a deterministic boundary GeoJSON + inventory CSV for the
requested CDL crop code (default 69, grapes) clipped to a target county, then
optionally runs the canonical farm pipeline. It is the CDL-based alternative to
the OSM Overpass bootstrap in `bootstrap_farm_from_county.py` for crops like
grapes that are not reliably tagged as `landuse` polygons in OpenStreetMap.

The current CDL legend classifies grapes as code 69 (legacy code 55 is used for
grapes in a few states; 54 is almonds). Check the crop presence in the target
county before choosing a code.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import rasterio
from rasterio.features import shapes as raster_shapes
from rasterio.windows import from_bounds as window_from_bounds
from shapely.geometry import shape as shapely_geom

from naming import field_slug_from_id
from paths import (
    DATA_ROOT,
    SCRIPTS_ROOT,
    farm_boundary_path,
    farm_manifest_dir,
    shared_cdl_state_raster_path,
    shared_geoadmin_counties_dir,
)

COUNTIES_PATH = shared_geoadmin_counties_dir() / "counties_usa.geojson"
CROP_NAMES = {69: "grapes", 55: "grapes", 1: "corn", 5: "soybeans", 24: "wheat", 2: "cotton"}
DEFAULT_CDL_YEAR = 2025
DEFAULT_MIN_ACRES = 0.5


def _runtime_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else DATA_ROOT / candidate


def _runtime_relative(path: Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(DATA_ROOT))
    except ValueError:
        return str(path)


def _normalize_county_name(name: str) -> str:
    cleaned = name.strip().lower().replace(" county", "")
    return " ".join(cleaned.split())


def _ensure_counties_layer() -> None:
    if COUNTIES_PATH.exists():
        return
    cmd = [
        sys.executable,
        str(SCRIPTS_ROOT / "ingest" / "download_geoadmin.py"),
        "--levels",
        "l2_counties",
    ]
    subprocess.run(cmd, cwd=str(DATA_ROOT), check=True)


def _load_target_county(state_fips: str, county_name: str) -> gpd.GeoDataFrame:
    _ensure_counties_layer()
    counties = gpd.read_file(COUNTIES_PATH)
    target_state = state_fips.zfill(2)
    target_county = _normalize_county_name(county_name)

    candidates = counties[counties["state_fips"].astype(str).str.zfill(2) == target_state].copy()
    if candidates.empty:
        raise ValueError(f"No counties found for state_fips={target_state}")

    names = candidates["county_name"].astype(str).map(_normalize_county_name)
    exact = candidates[names == target_county].copy()
    if exact.empty:
        available = ", ".join(sorted(candidates["county_name"].astype(str).unique())[:15])
        raise ValueError(
            f"County '{county_name}' not found in state_fips={target_state}. "
            f"Sample available counties: {available}"
        )
    return exact.iloc[[0]].copy()


def _ensure_cdl_raster(year: int, state_fips: str) -> Path:
    cdl_path = shared_cdl_state_raster_path(year, state_fips)
    if cdl_path.exists():
        return cdl_path
    from download_cdl import download_cdl

    return download_cdl(year, state_fips=state_fips)


def _extract_crop_polygons(
    *,
    cdl_path: Path,
    county_geom,
    crop_code: int,
    min_acres: float,
    state_fips: str,
    county_fips: str,
    county_name: str,
) -> gpd.GeoDataFrame:
    with rasterio.open(cdl_path) as src:
        raster_crs = src.crs
        county_in_crs = gpd.GeoDataFrame([{"geometry": county_geom}], crs="EPSG:4326").to_crs(
            raster_crs
        )
        minx, miny, maxx, maxy = county_in_crs.total_bounds
        window = window_from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        data = src.read(1, window=window, masked=False)
        transform = src.window_transform(window)

    masked = np.where(data == crop_code, 1, 0).astype(np.uint8)
    polygons = [
        shapely_geom(geom)
        for geom, value in raster_shapes(masked, transform=transform)
        if value == 1
    ]

    records: list[dict] = []
    crop_name = CROP_NAMES.get(crop_code, str(crop_code))
    for polygon in polygons:
        if polygon.is_empty or not polygon.is_valid:
            continue
        records.append(
            {
                "source": f"USDA NASS CDL {cdl_path.stem}",
                "crop_name": crop_name,
                "cdl_crop_code": crop_code,
                "state_fips": state_fips.zfill(2),
                "county_fips": county_fips.zfill(3),
                "county_name": county_name,
                "geometry": polygon,
            }
        )

    if not records:
        return gpd.GeoDataFrame(
            columns=["field_id", "geometry"], geometry="geometry", crs="EPSG:4326"
        )

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=raster_crs)
    gdf = gdf.to_crs("EPSG:4326")
    gdf["area_acres"] = gdf.to_crs("EPSG:5070").geometry.area / 4046.8564224
    gdf = gdf[gdf["area_acres"] >= min_acres].copy()
    gdf = gdf.sort_values(
        ["area_acres", "geometry"], ascending=[False, True]
    ).reset_index(drop=True)
    gdf["field_id"] = [
        f"cdl-{crop_code}-{index + 1:04d}" for index in range(len(gdf))
    ]
    return gdf


def _sample_fields(gdf: gpd.GeoDataFrame, *, count: int, seed: int) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    if len(gdf) <= count:
        return gdf.sort_values("field_id").reset_index(drop=True)
    sampled = gdf.sample(n=count, random_state=seed).copy()
    return sampled.sort_values("field_id").reset_index(drop=True)


def _write_inventory(path: Path, fields: gpd.GeoDataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field_id", "field_slug"])
        for field_id in fields["field_id"].astype(str).tolist():
            writer.writerow([field_id, field_slug_from_id(field_id)])


def _run_farm_pipeline(args, boundary_path: Path, inventory_path: Path) -> None:
    cmd = [
        sys.executable,
        str(SCRIPTS_ROOT / "run_farm_pipeline.py"),
        "--boundaries",
        str(boundary_path),
        "--grower-slug",
        args.grower_slug,
        "--farm-slug",
        args.farm_slug,
        "--farm-name",
        args.farm_name,
        "--inventory-csv",
        str(inventory_path),
        "--weather-backend",
        args.weather_backend,
        "--weather-start-year",
        str(args.weather_start_year),
        "--weather-end-year",
        str(args.weather_end_year),
        "--weather-time-standard",
        args.weather_time_standard,
        "--imagery-start-year",
        str(args.imagery_start_year),
        "--imagery-end-year",
        str(args.imagery_end_year),
    ]
    if args.force:
        cmd.append("--force")
    subprocess.run(cmd, cwd=str(DATA_ROOT), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a CDL crop-code farm boundary for a grower in a target county"
    )
    parser.add_argument(
        "--state-fips", required=True, help="Two-digit state FIPS, e.g. 26 for Michigan"
    )
    parser.add_argument("--county-name", required=True, help="County name, e.g. Leelanau")
    parser.add_argument(
        "--cdl-year", type=int, default=DEFAULT_CDL_YEAR, help="CDL crop year to extract from"
    )
    parser.add_argument(
        "--crop-code",
        type=int,
        default=69,
        help="CDL crop code; 69 is grapes",
    )
    parser.add_argument(
        "--min-acres",
        type=float,
        default=DEFAULT_MIN_ACRES,
        help="Minimum parcel size in acres to keep",
    )
    parser.add_argument("--count", type=int, default=8, help="Number of parcels to sample")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic seed")
    parser.add_argument("--grower-slug", required=True)
    parser.add_argument("--farm-slug", required=True)
    parser.add_argument("--farm-name", required=True)
    parser.add_argument(
        "--inventory-csv",
        default=None,
        help=(
            "Inventory output path. Defaults to "
            "growers/<grower>/farms/<farm>/manifests/field-inventory.csv under the runtime root"
        ),
    )
    parser.add_argument(
        "--boundary-out",
        default=None,
        help="Boundary output path. Defaults to canonical farm boundary path.",
    )
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="Run full farm pipeline after bootstrap",
    )
    parser.add_argument(
        "--weather-backend",
        choices=["zarr", "api"],
        default="zarr",
        help="Farm weather backend for --run-pipeline",
    )
    parser.add_argument("--weather-start-year", type=int, default=2021)
    parser.add_argument("--weather-end-year", type=int, default=2025)
    parser.add_argument("--weather-time-standard", choices=["lst", "utc"], default="lst")
    parser.add_argument("--imagery-start-year", type=int, default=2021)
    parser.add_argument("--imagery-end-year", type=int, default=2026)
    parser.add_argument("--force", action="store_true", help="Pass --force to run_farm_pipeline")
    args = parser.parse_args()

    if args.weather_start_year > args.weather_end_year:
        raise ValueError("--weather-start-year must be <= --weather-end-year")
    if args.imagery_start_year > args.imagery_end_year:
        raise ValueError("--imagery-start-year must be <= --imagery-end-year")

    county = _load_target_county(args.state_fips, args.county_name)
    county_geom = county.geometry.iloc[0]
    county_name = str(county["county_name"].iloc[0])
    county_fips = str(county["county_fips"].iloc[0]).zfill(3)

    cdl_path = _ensure_cdl_raster(args.cdl_year, args.state_fips)
    fields = _extract_crop_polygons(
        cdl_path=cdl_path,
        county_geom=county_geom,
        crop_code=args.crop_code,
        min_acres=args.min_acres,
        state_fips=args.state_fips,
        county_fips=county_fips,
        county_name=county_name,
    )
    sampled = _sample_fields(fields, count=args.count, seed=args.seed)
    if sampled.empty:
        raise RuntimeError(
            f"No CDL crop code {args.crop_code} parcels found in {county_name} County "
            f"for {args.cdl_year}"
        )

    default_inventory = (
        farm_manifest_dir(args.grower_slug, args.farm_slug) / "field-inventory.csv"
    )
    inventory_path = Path(args.inventory_csv) if args.inventory_csv else default_inventory
    inventory_path = inventory_path if inventory_path.is_absolute() else _runtime_path(inventory_path)

    boundary_out = (
        Path(args.boundary_out)
        if args.boundary_out
        else farm_boundary_path(args.grower_slug, args.farm_slug)
    )
    boundary_out = boundary_out if boundary_out.is_absolute() else _runtime_path(boundary_out)
    boundary_out.parent.mkdir(parents=True, exist_ok=True)

    _write_inventory(inventory_path, sampled)
    sampled.to_file(boundary_out, driver="GeoJSON")

    summary = {
        "grower_slug": args.grower_slug,
        "farm_slug": args.farm_slug,
        "farm_name": args.farm_name,
        "state_fips": args.state_fips.zfill(2),
        "county_name": county_name,
        "county_fips": county_fips,
        "cdl_year": args.cdl_year,
        "crop_code": args.crop_code,
        "crop_name": CROP_NAMES.get(args.crop_code, str(args.crop_code)),
        "available_count": len(fields),
        "sample_count": len(sampled),
        "seed": args.seed,
        "boundary_path": _runtime_relative(boundary_out),
        "inventory_csv": _runtime_relative(inventory_path),
    }
    print(json.dumps(summary, indent=2))

    if args.run_pipeline:
        _run_farm_pipeline(args, boundary_out, inventory_path)


if __name__ == "__main__":
    main()
