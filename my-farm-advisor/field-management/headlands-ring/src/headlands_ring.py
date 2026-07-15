from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd

ACRES_PER_SQM = 0.0002471053814671653


def _validate_projected(gdf: gpd.GeoDataFrame) -> None:
    if gdf.crs is None:
        raise ValueError("GeoDataFrame must have a CRS before headlands operations")
    if gdf.crs.is_geographic:
        raise ValueError("Headlands operations require a projected CRS with meter units")


def create_field_interior(field_gdf: gpd.GeoDataFrame, width_m: float = 9.0) -> gpd.GeoDataFrame:
    _validate_projected(field_gdf)
    interiors = []
    for geom in field_gdf.geometry:
        inner = geom.buffer(-width_m)
        if not inner.is_empty:
            interiors.append(inner)
    return gpd.GeoDataFrame(geometry=interiors, crs=field_gdf.crs)


def create_headlands_ring(field_gdf: gpd.GeoDataFrame, width_m: float = 9.0) -> gpd.GeoDataFrame:
    _validate_projected(field_gdf)
    rings = []
    for geom in field_gdf.geometry:
        inner = geom.buffer(-width_m)
        rings.append(geom if inner.is_empty else geom.difference(inner))
    valid = [geom for geom in rings if not geom.is_empty]
    return gpd.GeoDataFrame(geometry=valid, crs=field_gdf.crs)


def split_headlands_and_interior(
    field_gdf: gpd.GeoDataFrame, width_m: float = 9.0
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    ring = create_headlands_ring(field_gdf, width_m=width_m)
    interior = create_field_interior(field_gdf, width_m=width_m)
    return ring, interior


def summarize_headlands(field_gdf: gpd.GeoDataFrame, ring_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    _validate_projected(field_gdf)
    field_area_sqm = float(field_gdf.geometry.area.sum())
    ring_area_sqm = float(ring_gdf.geometry.area.sum()) if not ring_gdf.empty else 0.0
    pct = (ring_area_sqm / field_area_sqm * 100.0) if field_area_sqm else 0.0
    return pd.DataFrame(
        [
            {
                "field_area_sqm": field_area_sqm,
                "field_area_acres": field_area_sqm * ACRES_PER_SQM,
                "headlands_area_sqm": ring_area_sqm,
                "headlands_area_acres": ring_area_sqm * ACRES_PER_SQM,
                "headlands_pct": pct,
            }
        ]
    )


def flag_points_in_headlands(
    points_gdf: gpd.GeoDataFrame, ring_gdf: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    if points_gdf.crs != ring_gdf.crs:
        points_gdf = points_gdf.to_crs(ring_gdf.crs)
    result = points_gdf.copy()
    union = ring_gdf.unary_union if not ring_gdf.empty else None
    result["in_headlands"] = result.geometry.intersects(union) if union is not None else False
    return result


def clip_polygons_to_headlands(
    polygons_gdf: gpd.GeoDataFrame, ring_gdf: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    if polygons_gdf.empty or ring_gdf.empty:
        return gpd.GeoDataFrame(columns=polygons_gdf.columns, geometry=[], crs=polygons_gdf.crs)
    if polygons_gdf.crs != ring_gdf.crs:
        polygons_gdf = polygons_gdf.to_crs(ring_gdf.crs)
    return gpd.overlay(polygons_gdf, ring_gdf, how="intersection")


def plot_headlands_map(
    field_gdf: gpd.GeoDataFrame,
    ring_gdf: gpd.GeoDataFrame,
    interior_gdf: gpd.GeoDataFrame | None = None,
    title: str = "Headlands ring overview",
    save_path: str | Path | None = None,
) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(8, 8))
    field_gdf.boundary.plot(ax=ax, color="darkgreen", linewidth=2, label="Field boundary")
    if interior_gdf is not None and not interior_gdf.empty:
        interior_gdf.plot(ax=ax, color="#d9f99d", alpha=0.6, edgecolor="none", label="Interior")
    if not ring_gdf.empty:
        ring_gdf.plot(ax=ax, color="#fdba74", alpha=0.7, edgecolor="#c2410c", label="Headlands")
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_axis_off()
    ax.legend(loc="lower right")
    plt.tight_layout()
    if save_path is not None:
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(save_path, dpi=200, bbox_inches="tight")
    return fig


def _get_utm_crs(longitude: float, latitude: float) -> str:
    """Return the EPSG code for the UTM zone containing the given coordinate."""
    zone = math.floor((longitude + 180) / 6) + 1
    epsg = 32600 + zone if latitude >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def create_headlands_ring_from_boundary(
    boundary_gdf: gpd.GeoDataFrame,
    width_m: float = 21.0,
    output_dir: str | Path | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Create a headlands ring from a field boundary in EPSG:4326.

    Reads a field boundary (assumed EPSG:4326), reprojects to the
    auto-detected UTM zone, calculates area metrics, creates an inward
    buffer, derives the headlands ring, and writes both boundary and ring
    to GeoPackage files.

    Parameters
    ----------
    boundary_gdf : gpd.GeoDataFrame
        Input field boundary GeoDataFrame. Should be in EPSG:4326.
    width_m : float, optional
        Inward buffer width in meters (default 21.0).
    output_dir : str or Path, optional
        Directory to write output GeoPackage files. If None, files are
        written to the current working directory.

    Returns
    -------
    tuple of (gpd.GeoDataFrame, gpd.GeoDataFrame)
        (original_gdf with meters_squared and acres columns,
         headlands_ring_gdf in EPSG:4326 with meters_squared and acres)
    """
    # Ensure EPSG:4326
    original_gdf = boundary_gdf.copy()
    if original_gdf.crs is None:
        original_gdf.set_crs(epsg=4326, inplace=True)
    else:
        original_gdf = original_gdf.to_crs("EPSG:4326")

    # Auto-detect UTM zone from centroid
    centroid = original_gdf.geometry.unary_union.centroid
    utm_crs = _get_utm_crs(centroid.x, centroid.y)

    # Transform to UTM for meter-based operations
    utm_gdf = original_gdf.to_crs(utm_crs)

    # Calculate full boundary area per feature
    utm_gdf["meters_squared"] = utm_gdf.geometry.area
    utm_gdf["acres"] = utm_gdf["meters_squared"] * ACRES_PER_SQM

    # Write attributes back to original_gdf (EPSG:4326)
    original_gdf["meters_squared"] = utm_gdf["meters_squared"].values
    original_gdf["acres"] = utm_gdf["acres"].values

    # Create headlands ring per feature
    rings = []
    ring_areas_sqm = []
    for geom in utm_gdf.geometry:
        inner = geom.buffer(-width_m)
        if inner.is_empty:
            ring = geom
        else:
            ring = geom.difference(inner)
        if not ring.is_empty:
            rings.append(ring)
            ring_areas_sqm.append(float(ring.area))

    if rings:
        headlands_gdf = gpd.GeoDataFrame(geometry=rings, crs=utm_crs)
        headlands_gdf["meters_squared"] = ring_areas_sqm
        headlands_gdf["acres"] = [a * ACRES_PER_SQM for a in ring_areas_sqm]
    else:
        headlands_gdf = gpd.GeoDataFrame(
            {"meters_squared": [], "acres": []}, geometry=[], crs=utm_crs
        )

    # Convert headlands ring back to EPSG:4326
    headlands_gdf = headlands_gdf.to_crs("EPSG:4326")

    # Write GeoPackage files
    out_path = Path(output_dir) if output_dir else Path(".")
    out_path.mkdir(parents=True, exist_ok=True)

    original_gdf.to_file(out_path / "field_boundary.gpkg", driver="GPKG")
    headlands_gdf.to_file(out_path / "headlands_ring.gpkg", driver="GPKG")

    return original_gdf, headlands_gdf
