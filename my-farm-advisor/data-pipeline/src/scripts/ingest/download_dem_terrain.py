#!/usr/bin/env python3
# ruff: noqa: E402
"""Resolve, cache, clip, and derive DEM terrain products for farm fields."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_bounds

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from paths import (  # pyright: ignore[reportMissingImports]
    DATA_ROOT,
    farm_boundary_path,
    farm_dem_summary_table_path,
    farm_manifest_dir,
    field_boundary_path,
    field_dem_dir,
    field_dem_manifest_path,
    field_tables_dir,
    field_terrain_derived_dir,
    ensure_data_root_path,
    shared_dem_cache_path,
    validate_path_slug,
)
from reporting_bootstrap import (  # pyright: ignore[reportMissingImports]
    ensure_skill_path,
    field_slug_map_from_inventory,
)

ensure_skill_path("dem-terrain")

from dem_terrain.raster_processing import (  # pyright: ignore[reportMissingImports]
    DEFAULT_NODATA,
    clip_dem_tiles_to_buffer,
    file_size,
    prepare_analysis_geometry,
    sha256_file,
)
from dem_terrain.source_resolver import (  # pyright: ignore[reportMissingImports]
    ADAPTER_ALOS_AW3D30,
    ADAPTER_COPERNICUS_GLO30,
    ADAPTER_ILLINOIS_ILHMP,
    ADAPTER_NASADEM,
    ADAPTER_USGS_TNM,
    REGION_POLICY_UNKNOWN,
    SURFACE_DEM,
    SourceAOI,
    SourceCandidate,
    SourceRankingPolicy,
    SourceSelection,
    instantiate_default_adapters,
    select_best_candidate,
)
from dem_terrain.terrain_contract import (  # pyright: ignore[reportMissingImports]
    CLIPPED_DEM_FILENAME,
    CONDITIONED_DEM_FILENAME,
    DERIVED_RASTER_FILENAMES,
    SOURCE_REFERENCE_FILENAME,
    SUMMARY_CSV_FILENAME,
    SUMMARY_JSON_FILENAME,
)
from dem_terrain.terrain_derivatives import (  # pyright: ignore[reportMissingImports]
    TerrainDerivativeResult,
    derive_terrain_products,
)
from dem_terrain.usgs_tnm import USGSTNMAdapter  # pyright: ignore[reportMissingImports]


CONTROLLED_FAILURE = 2
DEFAULT_CONTEXT_METERS = 20.0
SOURCE_POLICIES = (
    "auto",
    "us",
    "global",
    "usgs-tnm",
    "illinois",
    "nasadem",
    "copernicus-glo30",
    "alos-aw3d30",
)


class ControlledFieldError(RuntimeError):
    """Expected per-field failure that should become a structured manifest."""

    def __init__(self, reason: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


def parse_args() -> argparse.Namespace:
    default_grower = os.environ.get("AG_GROWER_SLUG", "default-grower")
    default_farm = os.environ.get("AG_FARM_SLUG", "default-farm")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower", default=default_grower, help="Grower slug")
    parser.add_argument("--farm", default=default_farm, help="Farm slug")
    parser.add_argument(
        "--inventory-csv",
        default=os.environ.get("AG_INVENTORY_CSV"),
        help="Field inventory CSV with field_id,field_slug columns. Defaults to the parsed grower/farm runtime manifest.",
    )
    parser.add_argument(
        "--boundaries",
        default=os.environ.get("AG_BOUNDARIES"),
        help="Farm boundary GeoJSON override. Defaults to canonical runtime farm boundary.",
    )
    parser.add_argument(
        "--context-meters",
        type=float,
        default=DEFAULT_CONTEXT_METERS,
        help="Meter buffer around each field before clipping DEM rasters",
    )
    parser.add_argument(
        "--source-policy",
        choices=SOURCE_POLICIES,
        default="auto",
        help="DEM source family to consider for resolver planning",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan sources and paths without writes or downloads")
    parser.add_argument(
        "--limit-fields",
        type=int,
        default=None,
        help="Process at most N inventory fields, useful for smoke tests",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=os.environ.get("AG_FORCE") == "1",
        help="Rebuild even when manifest and outputs are reusable",
    )
    parser.add_argument(
        "--allow-live-downloads",
        action="store_true",
        help="Permit live provider discovery/download. Default is no live network/download.",
    )
    parser.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Generate tiny synthetic DEM fixtures in the runtime cache instead of using live providers",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.context_meters < 0:
        print("ERROR: --context-meters must be non-negative", file=sys.stderr)
        return CONTROLLED_FAILURE
    if args.limit_fields is not None and args.limit_fields < 1:
        print("ERROR: --limit-fields must be >= 1", file=sys.stderr)
        return CONTROLLED_FAILURE
    try:
        args.grower = validate_path_slug(args.grower, "grower_slug")
        args.farm = validate_path_slug(args.farm, "farm_slug")
    except ValueError as exc:
        _print_controlled_error(
            ControlledFieldError(
                "unsafe_slug",
                str(exc),
                details={"grower_slug": args.grower, "farm_slug": args.farm},
            )
        )
        return CONTROLLED_FAILURE

    inventory_path = (
        _resolve_runtime_path(args.inventory_csv)
        if args.inventory_csv
        else farm_manifest_dir(args.grower, args.farm) / "field-inventory.csv"
    )
    boundaries_path = _resolve_runtime_path(args.boundaries) if args.boundaries else farm_boundary_path(args.grower, args.farm)
    field_slug_map = field_slug_map_from_inventory(inventory_path if inventory_path.exists() else None)

    print("=" * 60)
    print("DEM terrain ingest")
    print("=" * 60)
    print(f"Runtime root: {DATA_ROOT}")
    print(f"Grower/farm: {args.grower}/{args.farm}")
    print(f"Inventory: {inventory_path}")
    print(f"Farm boundaries: {boundaries_path}")
    print(f"Source policy: {args.source_policy}")
    print(f"Mode: {'dry-run' if args.dry_run else 'offline-fixtures' if args.offline_fixtures else 'runtime'}")

    try:
        fields = _load_field_plan(
            grower_slug=args.grower,
            farm_slug=args.farm,
            inventory_path=inventory_path,
            boundaries_path=boundaries_path,
            field_slug_map=field_slug_map,
            limit=args.limit_fields,
        )
    except ControlledFieldError as exc:
        _print_controlled_error(exc)
        return CONTROLLED_FAILURE

    failures = 0
    completed_or_skipped = 0
    for field in fields:
        try:
            result = _process_field(field, args)
            if result["status"] in {"complete", "skipped", "planned"}:
                completed_or_skipped += 1
        except ControlledFieldError as exc:
            failures += 1
            _write_failure_manifest(field, args, exc)
            _print_controlled_error(exc, field_slug=field["field_slug"])

    print(
        json.dumps(
            {
                "status": "failed" if failures else "ok",
                "field_count": len(fields),
                "completed_or_skipped": completed_or_skipped,
                "failures": failures,
                "dry_run": bool(args.dry_run),
                "offline_fixtures": bool(args.offline_fixtures),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return CONTROLLED_FAILURE if failures else 0


def _process_field(field: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    field_id = field["field_id"]
    field_slug = field["field_slug"]
    boundary_path = field["boundary_path"]
    if not boundary_path.exists():
        raise ControlledFieldError(
            "missing_field_boundary",
            f"Canonical field boundary is missing: {boundary_path}",
            details={"boundary_path": str(boundary_path), "field_id": field_id, "field_slug": field_slug},
        )

    boundary = gpd.read_file(boundary_path)
    if boundary.empty:
        raise ControlledFieldError(
            "empty_field_boundary",
            f"Canonical field boundary has no features: {boundary_path}",
            details={"boundary_path": str(boundary_path), "field_id": field_id, "field_slug": field_slug},
        )
    boundary = boundary.to_crs("EPSG:4326")
    geometry = boundary.geometry.iloc[0]
    if geometry.is_empty:
        raise ControlledFieldError(
            "empty_field_geometry",
            f"Canonical field geometry is empty: {boundary_path}",
            details={"boundary_path": str(boundary_path), "field_id": field_id, "field_slug": field_slug},
        )

    outputs = _expected_paths(args.grower, args.farm, field_slug)
    if _manifest_reusable(outputs["manifest"], outputs) and not args.force and not args.dry_run:
        print(f"skip  {field_slug} DEM terrain (manifest+outputs reusable)")
        _append_farm_summary(args.grower, args.farm, _farm_summary_row(field, outputs, status="skipped"))
        return {"status": "skipped", "field_slug": field_slug, "manifest": str(outputs["manifest"])}

    aoi = _source_aoi(boundary)
    selection = _select_source(
        aoi,
        source_policy=args.source_policy,
        allow_live_downloads=args.allow_live_downloads,
        offline_fixtures=args.offline_fixtures,
        dry_run=args.dry_run,
    )
    analysis = prepare_analysis_geometry(geometry, field_crs="EPSG:4326", buffer_meters=args.context_meters)

    if args.dry_run:
        plan = _dry_run_plan(field, outputs, selection, analysis, args)
        print(json.dumps(plan, indent=2, sort_keys=True))
        return {"status": "planned", "field_slug": field_slug}

    source_paths = _prepare_source_rasters(
        selection=selection,
        geometry=geometry,
        field=field,
        args=args,
    )
    _write_source_reference(outputs["source_reference"], selection, source_paths=source_paths)

    clipped_record = clip_dem_tiles_to_buffer(
        source_paths,
        geometry,
        outputs["dem_clipped"],
        field_crs="EPSG:4326",
        buffer_meters=args.context_meters,
        nodata=DEFAULT_NODATA,
        source_vertical_datum="unknown",
        target_vertical_datum="unknown",
    )
    derivative_result = derive_terrain_products(
        outputs["dem_clipped"],
        outputs["derived_dir"],
        preview_dir=outputs["preview_dir"],
        tables_dir=outputs["tables_dir"],
        summary_json_path=outputs["summary_json"],
        summary_csv_path=outputs["summary_csv"],
        write_previews=True,
    )
    manifest = _build_success_manifest(
        field=field,
        args=args,
        selection=selection,
        clipped_record=clipped_record.to_dict(),
        derivative_result=derivative_result,
        outputs=outputs,
        source_paths=source_paths,
        analysis_crs=clipped_record.analysis_crs,
    )
    _write_json(outputs["manifest"], manifest)
    _append_farm_summary(args.grower, args.farm, _farm_summary_row(field, outputs, status="complete"))
    print(f"run   {field_slug} DEM terrain complete: {outputs['manifest']}")
    return {"status": "complete", "field_slug": field_slug, "manifest": str(outputs["manifest"])}


def _load_field_plan(
    *,
    grower_slug: str,
    farm_slug: str,
    inventory_path: Path,
    boundaries_path: Path,
    field_slug_map: dict[str, str],
    limit: int | None,
) -> list[dict[str, Any]]:
    if not field_slug_map:
        raise ControlledFieldError(
            "missing_or_empty_inventory",
            f"Field inventory is missing or has no field_id,field_slug rows: {inventory_path}",
            details={"inventory_path": str(inventory_path)},
        )
    fields_gdf: gpd.GeoDataFrame | None = None
    if boundaries_path.exists():
        fields_gdf = gpd.read_file(boundaries_path).to_crs("EPSG:4326")

    planned: list[dict[str, Any]] = []
    for field_id, field_slug in field_slug_map.items():
        try:
            safe_field_slug = validate_path_slug(str(field_slug), "field_slug")
        except ValueError as exc:
            raise ControlledFieldError(
                "unsafe_field_slug",
                str(exc),
                details={
                    "inventory_path": str(inventory_path),
                    "field_id": str(field_id),
                    "field_slug": str(field_slug),
                },
            ) from exc
        row: dict[str, Any] = {
            "field_id": str(field_id),
            "field_slug": safe_field_slug,
            "boundary_path": field_boundary_path(grower_slug, farm_slug, safe_field_slug),
        }
        if fields_gdf is not None and "field_id" in fields_gdf.columns:
            match = fields_gdf[fields_gdf["field_id"].astype(str) == str(field_id)]
            if not match.empty:
                row["farm_boundary_geometry"] = match.geometry.iloc[0]
        planned.append(row)
        if limit is not None and len(planned) >= limit:
            break
    if not planned:
        raise ControlledFieldError(
            "no_planned_fields",
            f"No fields could be planned from inventory: {inventory_path}",
            details={"inventory_path": str(inventory_path)},
        )
    return planned


def _select_source(
    aoi: SourceAOI,
    *,
    source_policy: str,
    allow_live_downloads: bool,
    offline_fixtures: bool,
    dry_run: bool,
) -> SourceSelection:
    if offline_fixtures:
        fixture = SourceCandidate(
            adapter_id="offline_fixtures",
            adapter_name="Offline synthetic DEM fixture",
            source_name="Tiny synthetic DEM fixture generated in runtime cache",
            source_urls=("runtime-cache://offline-fixtures/synthetic-dem.tif",),
            metadata_urls=(),
            license="synthetic fixture; generated by My Farm Advisor smoke test",
            citation="Synthetic DEM generated locally for offline validation; not an operational elevation source.",
            region_policy=REGION_POLICY_UNKNOWN,
            resolution_m=1.0,
            surface_type=SURFACE_DEM,
            coverage_score=1.0,
            direct_no_auth=True,
            requires_auth=False,
            warnings=("offline_fixture_mode=true",),
            fallback_reason="offline_fixture_mode",
            bbox_wgs84=aoi.bbox_wgs84,
        )
        return select_best_candidate((fixture,), aoi=aoi, policy=SourceRankingPolicy())

    adapters = _adapters_for_policy(source_policy, allow_live_downloads=allow_live_downloads)
    candidates: list[SourceCandidate] = []
    for adapter in adapters:
        try:
            candidates.extend(adapter.discover(aoi))
        except NotImplementedError:
            continue
        except Exception as exc:
            raise ControlledFieldError(
                "source_discovery_failed",
                f"DEM source discovery failed for {getattr(adapter, 'adapter_id', 'unknown_adapter')}: {exc}",
                details={
                    "adapter_id": getattr(adapter, "adapter_id", "unknown_adapter"),
                    "source_policy": source_policy,
                    "bbox_wgs84": aoi.bbox_wgs84,
                },
            ) from exc
    if not candidates:
        if dry_run:
            return _dry_run_source_selection(aoi, source_policy=source_policy, reason="no_source_candidates")
        raise ControlledFieldError(
            "no_source_candidates",
            "No DEM source candidates were discovered for the field AOI.",
            details={"source_policy": source_policy, "bbox_wgs84": aoi.bbox_wgs84},
        )
    try:
        return select_best_candidate(candidates, aoi=aoi, policy=SourceRankingPolicy())
    except Exception as exc:
        if dry_run:
            return _dry_run_source_selection(
                aoi,
                source_policy=source_policy,
                reason="no_selectable_source",
                candidate_count=len(candidates),
            )
        raise ControlledFieldError(
            "no_selectable_source",
            f"No selectable DEM source covered the field AOI: {exc}",
            details={
                "source_policy": source_policy,
                "bbox_wgs84": aoi.bbox_wgs84,
                "candidate_count": len(candidates),
                "candidates": [candidate.to_dict() for candidate in candidates],
            },
        ) from exc


def _dry_run_source_selection(
    aoi: SourceAOI,
    *,
    source_policy: str,
    reason: str,
    candidate_count: int = 0,
) -> SourceSelection:
    planned = SourceCandidate(
        adapter_id="dry_run_planned_source",
        adapter_name="Dry-run planned DEM source",
        source_name="Dry-run DEM source placeholder; no provider download or credential check performed",
        source_urls=("planned://dry-run/no-download-dem-source",),
        metadata_urls=(),
        license="not applicable; dry-run planning placeholder",
        citation="Dry-run planning candidate generated locally by My Farm Advisor; not an operational elevation source.",
        region_policy=REGION_POLICY_UNKNOWN,
        resolution_m=None,
        surface_type="unknown",
        coverage_score=1.0,
        direct_no_auth=True,
        requires_auth=False,
        warnings=(
            "dry_run_planning_candidate=true",
            "no_download_performed=true",
            f"source_policy={source_policy}",
            f"planning_reason={reason}",
            f"candidate_count={candidate_count}",
        ),
        fallback_reason=reason,
        bbox_wgs84=aoi.bbox_wgs84,
        access_note="Dry-run only: selected so planned paths can be inspected without live provider access or raster writes.",
    )
    return select_best_candidate((planned,), aoi=aoi, policy=SourceRankingPolicy())


def _adapters_for_policy(source_policy: str, *, allow_live_downloads: bool) -> tuple[Any, ...]:
    adapters = list(instantiate_default_adapters())
    replacements = {
        ADAPTER_USGS_TNM: USGSTNMAdapter(network_enabled=allow_live_downloads),
    }
    adapters = [replacements.get(getattr(adapter, "adapter_id", ""), adapter) for adapter in adapters]
    allowed_by_policy = {
        "auto": None,
        "us": {ADAPTER_USGS_TNM, ADAPTER_ILLINOIS_ILHMP},
        "global": {ADAPTER_NASADEM, ADAPTER_COPERNICUS_GLO30, ADAPTER_ALOS_AW3D30},
        "usgs-tnm": {ADAPTER_USGS_TNM},
        "illinois": {ADAPTER_ILLINOIS_ILHMP},
        "nasadem": {ADAPTER_NASADEM},
        "copernicus-glo30": {ADAPTER_COPERNICUS_GLO30},
        "alos-aw3d30": {ADAPTER_ALOS_AW3D30},
    }[source_policy]
    if allowed_by_policy is None:
        return tuple(adapters)
    return tuple(adapter for adapter in adapters if getattr(adapter, "adapter_id", "") in allowed_by_policy)


def _prepare_source_rasters(
    *,
    selection: SourceSelection,
    geometry: Any,
    field: dict[str, Any],
    args: argparse.Namespace,
) -> list[Path]:
    selected = selection.selected
    cache_dir = ensure_data_root_path(shared_dem_cache_path(selected.adapter_id) / field["field_slug"])
    if args.offline_fixtures:
        return [_write_offline_fixture_dem(cache_dir / "offline_fixture_dem.tif", geometry)]

    cached = _cached_source_paths(selected, cache_dir)
    if cached:
        return cached

    if not args.allow_live_downloads:
        raise ControlledFieldError(
            "live_downloads_not_allowed",
            "DEM source is not cached and live downloads are disabled. Pass --allow-live-downloads or --offline-fixtures.",
            details={"selected_source": selected.to_dict(), "cache_dir": str(cache_dir)},
        )

    if selected.adapter_id == ADAPTER_USGS_TNM:
        adapter = USGSTNMAdapter(network_enabled=True)
        return [adapter.download(selected, cache_dir)]

    raise ControlledFieldError(
        "provider_download_not_implemented",
        f"Selected DEM provider does not yet implement safe live downloads: {selected.adapter_id}",
        details={"selected_source": selected.to_dict(), "cache_dir": str(cache_dir)},
    )


def _cached_source_paths(candidate: SourceCandidate, cache_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for source_url in candidate.source_urls:
        local = _local_path_from_source_url(source_url)
        if local is not None and local.exists():
            paths.append(local)
            continue
        name = Path(urlparse(source_url).path).name
        if name:
            cached = cache_dir / name
            if cached.exists():
                paths.append(cached)
                continue
        return []
    return paths


def _local_path_from_source_url(source_url: str) -> Path | None:
    if source_url.startswith("file://"):
        return Path(urlparse(source_url).path)
    parsed = urlparse(source_url)
    if parsed.scheme:
        return None
    path = Path(source_url)
    return path if path.is_absolute() else None


def _write_offline_fixture_dem(path: Path, geometry: Any) -> Path:
    path = ensure_data_root_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    minx, miny, maxx, maxy = geometry.bounds
    dx = max(maxx - minx, 0.001)
    dy = max(maxy - miny, 0.001)
    bounds = (minx - dx, miny - dy, maxx + dx, maxy + dy)
    width = 96
    height = 96
    x = np.linspace(0.0, 1.0, width, dtype="float32")
    y = np.linspace(0.0, 1.0, height, dtype="float32")
    xx, yy = np.meshgrid(x, y)
    dem = (240.0 + 8.0 * xx + 5.0 * yy + 0.5 * np.sin(xx * np.pi * 4.0)).astype("float32")
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_bounds(*bounds, width=width, height=height),
        "nodata": DEFAULT_NODATA,
        "compress": "deflate",
        "predictor": 3,
        "tiled": True,
        "blockxsize": 16,
        "blockysize": 16,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem.reshape((1, height, width)))
        dst.update_tags(source="offline_fixture", advisory="not_operational_elevation")
    return path


def _source_aoi(boundary: gpd.GeoDataFrame) -> SourceAOI:
    raw_bounds = [float(value) for value in boundary.total_bounds]
    bounds = (raw_bounds[0], raw_bounds[1], raw_bounds[2], raw_bounds[3])
    centroid = boundary.geometry.iloc[0].centroid
    country = "US" if -125.0 <= centroid.x <= -66.0 and 24.0 <= centroid.y <= 50.0 else None
    intersects_illinois = _bbox_intersects(bounds, (-91.55, 36.95, -87.00, 42.55))
    region = "Illinois" if intersects_illinois else None
    return SourceAOI(
        country=country,
        region=region,
        intersects_illinois=intersects_illinois,
        bbox_wgs84=bounds,
    )


def _bbox_intersects(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    return first[0] <= second[2] and first[2] >= second[0] and first[1] <= second[3] and first[3] >= second[1]


def _expected_paths(grower_slug: str, farm_slug: str, field_slug: str) -> dict[str, Path]:
    dem_dir = field_dem_dir(grower_slug, farm_slug, field_slug)
    derived_dir = field_terrain_derived_dir(grower_slug, farm_slug, field_slug)
    tables_dir = field_tables_dir(grower_slug, farm_slug, field_slug)
    preview_dir = ensure_data_root_path(derived_dir / "previews")
    return {
        "manifest": field_dem_manifest_path(grower_slug, farm_slug, field_slug),
        "dem_dir": dem_dir,
        "derived_dir": derived_dir,
        "tables_dir": tables_dir,
        "preview_dir": preview_dir,
        "source_reference": ensure_data_root_path(dem_dir / SOURCE_REFERENCE_FILENAME),
        "dem_clipped": ensure_data_root_path(dem_dir / CLIPPED_DEM_FILENAME),
        "dem_conditioned": ensure_data_root_path(derived_dir / CONDITIONED_DEM_FILENAME),
        "summary_json": ensure_data_root_path(tables_dir / SUMMARY_JSON_FILENAME),
        "summary_csv": ensure_data_root_path(tables_dir / SUMMARY_CSV_FILENAME),
        **{
            filename.removesuffix(".tif"): ensure_data_root_path(derived_dir / filename)
            for filename in DERIVED_RASTER_FILENAMES
        },
    }


def _manifest_reusable(manifest_path: Path, outputs: dict[str, Path]) -> bool:
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("status") not in {"complete", None}:
        return False
    required = [
        outputs["source_reference"],
        outputs["dem_clipped"],
        outputs["dem_conditioned"],
        outputs["summary_json"],
        outputs["summary_csv"],
        *(outputs[filename.removesuffix(".tif")] for filename in DERIVED_RASTER_FILENAMES),
    ]
    return all(path.exists() for path in required)


def _dry_run_plan(
    field: dict[str, Any],
    outputs: dict[str, Path],
    selection: SourceSelection,
    analysis: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "status": "planned",
        "field_id": field["field_id"],
        "field_slug": field["field_slug"],
        "boundary_path": str(field["boundary_path"]),
        "context_meters": args.context_meters,
        "analysis_geometry": analysis.to_record(),
        "selected_source": selection.selected.to_dict(),
        "candidate_sources": [candidate.to_dict() for candidate in selection.ranked_candidates],
        "output_paths": {key: str(value) for key, value in outputs.items()},
        "will_download": False,
        "will_write_rasters": False,
        "allow_live_downloads": bool(args.allow_live_downloads),
        "offline_fixtures": bool(args.offline_fixtures),
    }


def _build_success_manifest(
    *,
    field: dict[str, Any],
    args: argparse.Namespace,
    selection: SourceSelection,
    clipped_record: dict[str, Any],
    derivative_result: TerrainDerivativeResult,
    outputs: dict[str, Path],
    source_paths: list[Path],
    analysis_crs: str,
) -> dict[str, Any]:
    selected = selection.selected
    output_assets = _manifest_outputs(
        selected=selected,
        outputs=outputs,
        clipped_record=clipped_record,
        derivative_result=derivative_result,
    )
    checksums = {
        name: asset.get("checksum:sha256")
        for name, asset in output_assets.items()
        if asset.get("checksum:sha256")
    }
    warnings = list(selected.warnings)
    warnings.extend(str(warning) for warning in derivative_result.quality_warnings)
    return {
        "status": "complete",
        "run_id": str(uuid.uuid4()),
        "field_id": field["field_id"],
        "field_slug": field["field_slug"],
        "grower_slug": args.grower,
        "farm_slug": args.farm,
        "buffer_meters": float(args.context_meters),
        "analysis_crs": analysis_crs,
        "selected_source": selected.to_dict(),
        "candidate_sources": [candidate.to_dict() for candidate in selection.ranked_candidates],
        "fallback_reason": selected.fallback_reason,
        "surface_type": selected.surface_type,
        "source_resolution_m": selected.resolution_m,
        "source_horizontal_crs": clipped_record.get("source_crs"),
        "source_vertical_datum": "unknown",
        "source_urls": list(selected.source_urls),
        "source_paths": [str(path) for path in source_paths],
        "license": selected.license,
        "citation": selected.citation,
        "acquisition_date": selected.acquisition_date,
        "publication_date": selected.publication_date,
        "processing_parameters": {
            "context_meters": float(args.context_meters),
            "source_policy": args.source_policy,
            "allow_live_downloads": bool(args.allow_live_downloads),
            "offline_fixtures": bool(args.offline_fixtures),
            "conditioning_status": derivative_result.conditioning_status,
            "conditioning_backend": derivative_result.conditioning_backend,
        },
        "warnings": list(dict.fromkeys(warnings)),
        "outputs": output_assets,
        "checksums": checksums,
        "generated_at": _utc_now(),
    }


def _manifest_outputs(
    *,
    selected: SourceCandidate,
    outputs: dict[str, Path],
    clipped_record: dict[str, Any],
    derivative_result: TerrainDerivativeResult,
) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    assets["dem_source_reference"] = _asset_record(
        outputs["source_reference"],
        product_name="dem_source_reference",
        title="DEM source reference",
        media_type="application/json",
        roles=["metadata", "source"],
        selected=selected,
    )
    assets["dem_clipped"] = _asset_record(
        outputs["dem_clipped"],
        product_name="dem_clipped",
        title="Clipped DEM",
        media_type="image/tiff; application=geotiff",
        roles=["data", "source-dem"],
        selected=selected,
        unit="meters",
        data_type=clipped_record.get("dtype"),
        nodata=clipped_record.get("nodata"),
        resolution=clipped_record.get("bounds"),
        analysis_crs=clipped_record.get("analysis_crs"),
    )
    conditioned = derivative_result.conditioned_dem
    assets["dem_conditioned"] = _product_asset(conditioned, selected=selected, roles=["data", "conditioned-dem"])
    for product in derivative_result.products:
        assets[product.product_name] = _product_asset(product, selected=selected, roles=["data", "derived-terrain"])
        if product.preview_path:
            preview = Path(product.preview_path)
            assets[f"{product.product_name}_preview"] = _asset_record(
                preview,
                product_name=f"{product.product_name}_preview",
                title=f"{product.product_name} PNG preview",
                media_type="image/png",
                roles=["preview"],
                selected=selected,
            )
    assets["dem_terrain_summary_json"] = _asset_record(
        outputs["summary_json"],
        product_name="dem_terrain_summary_json",
        title="DEM terrain summary JSON",
        media_type="application/json",
        roles=["metadata", "summary"],
        selected=selected,
    )
    assets["dem_terrain_summary_csv"] = _asset_record(
        outputs["summary_csv"],
        product_name="dem_terrain_summary_csv",
        title="DEM terrain summary CSV",
        media_type="text/csv",
        roles=["metadata", "summary"],
        selected=selected,
    )
    return assets


def _product_asset(product: Any, *, selected: SourceCandidate, roles: list[str]) -> dict[str, Any]:
    resolution = list(product.resolution) if product.resolution else None
    return _asset_record(
        Path(product.path),
        product_name=product.product_name,
        title=product.product_name.replace("_", " ").title(),
        media_type="image/tiff; application=geotiff",
        roles=roles,
        selected=selected,
        unit=product.unit,
        data_type=product.dtype,
        nodata=product.nodata,
        resolution=resolution,
        analysis_crs=None,
        warnings=product.warnings,
    )


def _asset_record(
    path: Path,
    *,
    product_name: str,
    title: str,
    media_type: str,
    roles: list[str],
    selected: SourceCandidate,
    unit: str | None = None,
    data_type: Any = None,
    nodata: Any = None,
    resolution: Any = None,
    analysis_crs: str | None = None,
    warnings: Any = None,
) -> dict[str, Any]:
    exists = path.exists()
    return {
        "href": str(path),
        "type": media_type,
        "roles": roles,
        "title": title,
        "description": title,
        "product_name": product_name,
        "filename": path.name,
        "proj:epsg": None,
        "proj:wkt2": analysis_crs,
        "raster:bands": [{"unit": unit}] if unit else None,
        "raster:nodata": nodata,
        "raster:data_type": str(data_type) if data_type is not None else None,
        "raster:unit": unit,
        "raster:resolution": resolution,
        "file:size": file_size(path) if exists else None,
        "checksum:sha256": sha256_file(path) if exists else None,
        "source:hrefs": list(selected.source_urls),
        "source:license": selected.license,
        "source:citation": selected.citation,
        "source:acquisition_date": selected.acquisition_date,
        "source:publication_date": selected.publication_date,
        "dem:surface_type": selected.surface_type,
        "dem:dsm_dtm_warning": "; ".join(selected.warnings) if selected.warnings else None,
        "warnings": warnings,
    }


def _write_source_reference(path: Path, selection: SourceSelection, *, source_paths: list[Path]) -> None:
    payload = {
        "generated_at": _utc_now(),
        "selected_source": selection.selected.to_dict(),
        "candidate_sources": [candidate.to_dict() for candidate in selection.ranked_candidates],
        "source_paths": [str(path) for path in source_paths],
        "quality_warning": selection.quality_warning,
        "warnings": list(selection.warnings),
    }
    _write_json(path, payload)


def _write_failure_manifest(field: dict[str, Any], args: argparse.Namespace, exc: ControlledFieldError) -> None:
    if args.dry_run:
        return
    manifest_path = field_dem_manifest_path(args.grower, args.farm, field["field_slug"])
    payload = {
        "status": "failed",
        "run_id": str(uuid.uuid4()),
        "field_id": field["field_id"],
        "field_slug": field["field_slug"],
        "grower_slug": args.grower,
        "farm_slug": args.farm,
        "reason": exc.reason,
        "message": str(exc),
        "details": exc.details,
        "generated_at": _utc_now(),
    }
    _write_json(manifest_path, payload)


def _append_farm_summary(grower_slug: str, farm_slug: str, row: dict[str, Any]) -> None:
    path = ensure_data_root_path(farm_dem_summary_table_path(grower_slug, farm_slug))
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    rows = [existing for existing in rows if existing.get("field_id") != str(row["field_id"])]
    rows.append(row)
    fieldnames = list(row.keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _farm_summary_row(field: dict[str, Any], outputs: dict[str, Path], *, status: str) -> dict[str, Any]:
    summary = _read_json(outputs["summary_json"])
    elevation = summary.get("elevation_range_m", {}) if isinstance(summary, dict) else {}
    slope = summary.get("slope_percentiles", {}) if isinstance(summary, dict) else {}
    return {
        "field_id": field["field_id"],
        "field_slug": field["field_slug"],
        "status": status,
        "manifest_path": str(outputs["manifest"]),
        "dem_clipped": str(outputs["dem_clipped"]),
        "conditioning_status": summary.get("conditioning_status", "") if isinstance(summary, dict) else "",
        "analysis_crs": summary.get("analysis_crs", "") if isinstance(summary, dict) else "",
        "elevation_min_m": elevation.get("min", ""),
        "elevation_max_m": elevation.get("max", ""),
        "slope_percent_p50": slope.get("p50", ""),
        "updated_at": _utc_now(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = ensure_data_root_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_runtime_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else DATA_ROOT / path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "to_record"):
        return _jsonable(value.to_record())
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _print_controlled_error(exc: ControlledFieldError, *, field_slug: str | None = None) -> None:
    payload = {
        "status": "failed",
        "field_slug": field_slug,
        "reason": exc.reason,
        "message": str(exc),
        "details": exc.details,
    }
    print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
