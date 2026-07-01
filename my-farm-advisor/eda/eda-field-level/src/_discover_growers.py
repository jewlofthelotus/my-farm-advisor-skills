#!/usr/bin/env python3
"""
_discover_growers.py
Shared helper for dynamic grower/farm discovery from the runtime data tree.
"""

import argparse
import json
import itertools
import os
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------

def resolve_data_root() -> Path:
    env = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env:
        return Path(env) / "data-pipeline"
    return Path("/home/coder/my-farm-advisor-runtime") / "data-pipeline"


def resolve_output_dir() -> Path:
    out = resolve_data_root() / "eda" / "field-level" / "output"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

_TAB10_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                 "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
_color_cycle = itertools.cycle(_TAB10_COLORS)


def auto_color(index: int) -> str:
    """Return a tab10 color by index (wraps after 10)."""
    return _TAB10_COLORS[index % len(_TAB10_COLORS)]


# ---------------------------------------------------------------------------
# Grower discovery
# ---------------------------------------------------------------------------

@dataclass
class GrowerInfo:
    grower_slug: str
    grower_display: str
    farm_slug: str
    state: str
    color: str


def discover_growers(cli_filter: list[str] | None = None) -> list[GrowerInfo]:
    """Scan growers/ directory and return list of GrowerInfo objects.
    
    If cli_filter is provided, only include growers whose slug is in the list.
    """
    data_root = resolve_data_root()
    growers_root = data_root / "growers"
    if not growers_root.exists():
        raise FileNotFoundError(f"Growers root not found: {growers_root}")
    
    results = []
    color_idx = 0
    for gdir in sorted(growers_root.iterdir()):
        if not gdir.is_dir():
            continue
        grower_json = gdir / "grower.json"
        if not grower_json.exists():
            continue
        gj = json.loads(grower_json.read_text())
        slug = gj.get("grower_slug", gdir.name)
        if cli_filter and slug not in cli_filter:
            continue
        
        farms_dir = gdir / "farms"
        if not farms_dir.exists():
            continue
        for fdir in sorted(farms_dir.iterdir()):
            if not fdir.is_dir():
                continue
            farm_json = fdir / "farm.json"
            if not farm_json.exists():
                continue
            fj = json.loads(farm_json.read_text())
            results.append(GrowerInfo(
                grower_slug=slug,
                grower_display=gj.get("display_name", slug),
                farm_slug=fj.get("farm_slug", fdir.name),
                state=fj.get("state", "US"),
                color=auto_color(color_idx),
            ))
            color_idx += 1
    return results


def parse_args(description: str) -> argparse.Namespace:
    """Common CLI parser used by all scripts."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--growers",
        default=None,
        help="Comma-separated grower slugs to process (default: all)",
    )
    return parser.parse_args()


def get_cli_filter(args: argparse.Namespace) -> list[str] | None:
    if args.growers:
        return [s.strip() for s in args.growers.split(",") if s.strip()]
    return None


# ---------------------------------------------------------------------------
# Geospatial helpers
# ---------------------------------------------------------------------------

def filter_states_from_fields(fields_gdf: gpd.GeoDataFrame, states_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return state polygons whose bounding boxes intersect the fields."""
    if fields_gdf.empty:
        return states_gdf
    bounds = fields_gdf.total_bounds  # [minx, miny, maxx, maxy]
    # Expand by 1 degree for context
    from shapely.geometry import box
    bbox = box(bounds[0] - 1, bounds[1] - 1, bounds[2] + 1, bounds[3] + 1)
    bbox_gdf = gpd.GeoDataFrame(geometry=[bbox], crs=fields_gdf.crs)
    mask = states_gdf.geometry.intersects(bbox_gdf.unary_union)
    return states_gdf[mask]


def load_states_geojson() -> gpd.GeoDataFrame:
    path = resolve_data_root() / "shared" / "geoadmin" / "l1_states" / "states_usa.geojson"
    if path.exists():
        return gpd.read_file(path)
    return gpd.GeoDataFrame(columns=["state_code", "geometry"], crs="EPSG:4326")


# ---------------------------------------------------------------------------
# Table locator helper
# ---------------------------------------------------------------------------

def find_farm_table(grower_slug: str, farm_slug: str, pattern: str) -> Path | None:
    """Find a table file matching pattern under farm derived/tables."""
    tables_dir = resolve_data_root() / "growers" / grower_slug / "farms" / farm_slug / "derived" / "tables"
    if not tables_dir.exists():
        return None
    matches = list(tables_dir.glob(pattern))
    return matches[0] if matches else None
