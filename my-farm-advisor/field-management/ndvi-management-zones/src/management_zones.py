from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.features import shapes as raster_shapes
from rasterio.fill import fillnodata
from rasterio.warp import reproject
from shapely.geometry import shape
from sklearn.cluster import KMeans


def resolve_field_paths(
    data_root: str,
    grower_slug: str,
    farm_slug: str,
    field_slug: str,
) -> tuple[Path, Path, Path, Path]:
    """Resolve canonical field-level paths.

    Returns (field_dir, boundary_path, sentinel_manifest, features_dir).
    """
    root = Path(data_root)
    field_dir = (
        root
        / "data-pipeline"
        / "growers"
        / grower_slug
        / "farms"
        / farm_slug
        / "fields"
        / field_slug
    )
    boundary_path = field_dir / "boundary" / "field_boundary.geojson"
    sentinel_manifest = field_dir / "satellite" / "sentinel" / "manifest.json"
    features_dir = field_dir / "derived" / "features"
    return field_dir, boundary_path, sentinel_manifest, features_dir


def select_best_scene_per_year(
    manifest_path: Path,
    start_date: str = "06-21",
    end_date: str = "08-31",
) -> dict[int, dict[str, Any]]:
    """From sentinel manifest, pick best scene per year in Jun–Aug window.

    Returns dict of {year: scene_dict} for the lowest-cloud scene in the
    window, or an empty dict if no year has qualifying scenes.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    best: dict[int, dict[str, Any]] = {}
    for year_entry in manifest.get("years", []):
        year = year_entry["year"]
        candidates = []
        for scene in year_entry.get("scenes", []):
            month_day = scene["scene_date"][5:]
            if start_date <= month_day <= end_date:
                candidates.append(scene)

        if not candidates:
            continue

        candidates.sort(key=lambda s: s["cloud_cover"])
        best[year] = candidates[0]

    return best


def resolve_ndvi_path(scene: dict[str, Any], runtime_base: Path) -> Path:
    """Resolve an NDVI GeoTIFF path relative to the runtime base."""
    return runtime_base / scene["ndvi_tif"]


def read_ndvi(ndvi_path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read an NDVI raster. Returns (array [H,W], profile)."""
    with rasterio.open(ndvi_path) as src:
        profile = src.profile.copy()
        data = src.read(1).astype("float32")
    return data, profile


def read_reference_profile(raster_paths: list[Path]) -> dict[str, Any]:
    """Return the profile of the first raster for use as alignment target."""
    with rasterio.open(raster_paths[0]) as src:
        return src.profile.copy()


def align_ndvi_to_grid(
    ndvi_array: np.ndarray,
    src_profile: dict[str, Any],
    ref_profile: dict[str, Any],
    out_path: Path | None = None,
) -> np.ndarray:
    """Align an NDVI array to a reference grid via reprojection.

    If input and reference already match in shape/CRS/transform, returns
    the input unchanged.
    """
    if (
        ndvi_array.shape == (ref_profile["height"], ref_profile["width"])
        and src_profile["crs"] == ref_profile["crs"]
        and src_profile["transform"] == ref_profile["transform"]
    ):
        aligned = ndvi_array.copy()
    else:
        destination = np.full(
            (ref_profile["height"], ref_profile["width"]), np.nan, dtype="float32"
        )
        reproject(
            source=ndvi_array,
            destination=destination,
            src_transform=src_profile["transform"],
            src_crs=src_profile["crs"],
            dst_transform=ref_profile["transform"],
            dst_crs=ref_profile["crs"],
            resampling=Resampling.bilinear,
        )
        aligned = destination

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_profile = ref_profile.copy()
        out_profile.update(dtype="float32", count=1, compress="lzw", nodata=np.nan)
        with rasterio.open(out_path, "w", **out_profile) as dst:
            dst.write(aligned, 1)

    return aligned


def fill_ndvi_gaps(
    ndvi_array: np.ndarray,
    profile: dict[str, Any],
    out_path: Path | None = None,
) -> np.ndarray:
    """Fill NaN/NoData gaps using nearest-valid-neighbor interpolation.
    """
    valid_mask = np.isfinite(ndvi_array).astype("uint8")

    fill_value = -3.0
    prepared = np.where(valid_mask == 1, ndvi_array, fill_value).astype("float32")
    filled = fillnodata(prepared, mask=valid_mask, max_search_distance=100)
    result = np.where(valid_mask == 1, ndvi_array, filled).astype("float32")
    result[~np.isfinite(result)] = np.nan

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_profile = profile.copy()
        out_profile.update(dtype="float32", count=1, compress="lzw", nodata=np.nan)
        with rasterio.open(out_path, "w", **out_profile) as dst:
            dst.write(result, 1)

    return result


def compute_management_zones(
    ndvi_array: np.ndarray,
    n_clusters: int = 3,
    random_state: int = 42,
) -> np.ndarray:
    """Run KMeans clustering on a single-year or multi-year NDVI stack.

    Args:
        ndvi_array: (n_years, height, width) or (height, width) array.
        n_clusters: Number of zones (default 3).
        random_state: Seed for reproducibility.

    Returns:
        (height, width) int16 array with zone labels 0..n_clusters-1,
        sorted by mean NDVI (0 = lowest NDVI). Pixels that are NaN get -1.
    """
    if ndvi_array.ndim == 2:
        ndvi_array = ndvi_array[np.newaxis, :, :]

    n_bands, height, width = ndvi_array.shape
    pixels = ndvi_array.reshape(n_bands, -1).T
    all_nan = np.all(np.isnan(pixels), axis=1)
    valid_mask = ~all_nan
    valid_pixels = pixels[valid_mask]

    if len(valid_pixels) < n_clusters:
        zone_labels = np.full(height * width, -1, dtype="int16")
        return zone_labels.reshape(height, width)

    kmeans = KMeans(n_clusters=n_clusters, random_state=random_state, n_init="auto")
    labels = kmeans.fit_predict(valid_pixels)

    zone_labels = np.full(height * width, -1, dtype="int16")
    zone_labels[valid_mask] = labels
    zone_labels = zone_labels.reshape(height, width)

    cluster_means = []
    for i in range(n_clusters):
        mask = zone_labels == i
        cluster_means.append(np.nanmean(ndvi_array[:, mask]))

    order = np.argsort(cluster_means)
    remap = {old: new for new, old in enumerate(order)}
    remapped = np.full_like(zone_labels, -1)
    for old, new in remap.items():
        remapped[zone_labels == old] = new

    return remapped


def write_zone_raster(
    zone_labels: np.ndarray,
    profile: dict[str, Any],
    out_path: Path,
) -> Path:
    """Write zone label GeoTIFF."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_profile = profile.copy()
    out_profile.update(dtype="int16", count=1, compress="lzw", nodata=-1)
    with rasterio.open(out_path, "w", **out_profile) as dst:
        dst.write(zone_labels.astype("int16"), 1)
    return out_path


def polygonize_zones(
    zone_raster_path: Path,
    field_boundary_gdf: gpd.GeoDataFrame,
    out_path: Path | None = None,
    zone_labels_map: dict[int, str] | None = None,
    year: int | None = None,
) -> gpd.GeoDataFrame:
    """Polygonize a zone raster, clip to field boundary.

    Args:
        zone_raster_path: Path to zone label GeoTIFF.
        field_boundary_gdf: Field boundary GeoDataFrame.
        out_path: Optional output GeoPackage path. When set, appends or
            writes a layer named ``management_zones``.
        zone_labels_map: Mapping of zone_id to label string.
        year: Optional year to include as a column in the output GDF.

    Returns:
        GeoDataFrame with zone polygons, area_m2, zone_id, zone_label,
        and optionally year.
    """
    if zone_labels_map is None:
        zone_labels_map = {0: "zone_0", 1: "zone_1", 2: "zone_2"}

    with rasterio.open(zone_raster_path) as src:
        img = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    results: list[dict[str, Any]] = []
    for geom, value in raster_shapes(img, mask=img != nodata, transform=transform):
        zone_id = int(value)
        results.append(
            {
                "geometry": shape(geom),
                "zone_id": zone_id,
                "zone_label": zone_labels_map.get(zone_id, str(zone_id)),
            }
        )

    if not results:
        gdf = gpd.GeoDataFrame(
            [{"zone_id": -1, "zone_label": "empty", "geometry": None}],
        )
        if out_path is not None:
            gdf.to_file(out_path, layer="management_zones", driver="GPKG")
        return gdf

    gdf = gpd.GeoDataFrame(results, crs=crs)

    if field_boundary_gdf.crs != crs:
        field_boundary_gdf = field_boundary_gdf.to_crs(crs)

    field_geom = field_boundary_gdf.unary_union
    if field_geom is not None:
        gdf = gdf[gdf.intersects(field_geom)].copy()
        gdf["geometry"] = gdf.intersection(field_geom)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()

    if gdf.empty:
        gdf = gpd.GeoDataFrame(
            [{"zone_id": -1, "zone_label": "empty", "geometry": None}],
            crs=crs,
        )
        if out_path is not None:
            gdf.to_file(out_path, layer="management_zones", driver="GPKG")
        return gdf

    gdf_aea = gdf.to_crs("EPSG:5070")
    gdf["area_m2"] = gdf_aea.geometry.area
    gdf = gdf.to_crs(crs)

    if year is not None:
        gdf["year"] = year

    if out_path is not None:
        if out_path.exists():
            existing = gpd.read_file(out_path, layer="management_zones")
            combined = gpd.GeoDataFrame(pd.concat([existing, gdf], ignore_index=True), crs=crs)
            combined.to_file(out_path, layer="management_zones", driver="GPKG")
        else:
            gdf.to_file(out_path, layer="management_zones", driver="GPKG")

    return gdf


def plot_scene_selection(
    manifest_path: Path,
    out_path: Path,
    field_name: str | None = None,
    window_start: str = "06-21",
    window_end: str = "08-31",
) -> Path:
    """Plot a per-year vertical bar chart of all Jun–Aug Sentinel-2 scenes.

    For each year, renders all scenes in the Jun–Aug window as a vertical
    bar chart with date on x and cloud percentage on y. The lowest-cloud
    scene (the one selected for processing) is highlighted in green.

    Args:
        manifest_path: Path to the sentinel manifest JSON.
        out_path: Output PNG path.
        field_name: Optional field label for the figure title.
        window_start: Window start MM-DD.
        window_end: Window end MM-DD.

    Returns:
        Path to the saved PNG.
    """
    from matplotlib.patches import Patch
    from datetime import datetime

    with open(manifest_path) as f:
        manifest = json.load(f)

    year_entries = manifest.get("years", [])
    year_data: dict[int, list[dict[str, Any]]] = {}

    selected_scenes: dict[int, dict[str, Any]] = {}

    for ye in year_entries:
        year = ye["year"]
        window_scenes = [
            s for s in ye.get("scenes", [])
            if window_start <= s["scene_date"][5:] <= window_end
        ]
        window_scenes.sort(key=lambda s: s["cloud_cover"])
        selected_scenes[year] = window_scenes[0] if window_scenes else None
        window_scenes.sort(key=lambda s: s["scene_date"])
        year_data[year] = window_scenes

    years = sorted(year_data)
    if not years:
        raise ValueError("No years found in manifest")

    n = len(years)
    fig, axes = plt.subplots(n, 1, figsize=(10, 3.2 * n), sharex=False)
    if n == 1:
        axes = [axes]

    for idx, (year, ax) in enumerate(zip(years, axes)):
        scenes = year_data[year]
        if not scenes:
            ax.text(0.5, 0.5, "No Jun–Aug scenes", ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            ax.set_title(str(year), fontsize=11, fontweight="bold")
            ax.set_ylabel("Cloud cover %")
            ax.set_ylim(0, 105)
            continue

        sel = selected_scenes.get(year)
        dates = []
        clouds = []
        colors = []

        for s in scenes:
            dt = datetime.strptime(s["scene_date"], "%Y-%m-%d")
            label = dt.strftime("%b %d")
            dates.append(label)
            clouds.append(s["cloud_cover"])
            colors.append("#2ca02c" if sel and s["scene_id"] == sel["scene_id"] else "#4A90D9")

        x_pos = range(len(dates))
        ax.bar(x_pos, clouds, width=0.55, color=colors, edgecolor="white", linewidth=0.5)

        for i, (c, col) in enumerate(zip(clouds, colors)):
            ax.text(i, c + 1.5, f"{c:.1f}%", ha="center", va="bottom",
                    fontsize=7, fontweight="bold" if col == "#2ca02c" else "normal",
                    color="#1a1a1a")

        ax.set_xticks(list(x_pos))
        ax.set_xticklabels(dates, fontsize=7.5, rotation=25, ha="right")
        ax.set_ylabel("Cloud cover %", fontsize=8)
        ax.set_ylim(0, max(clouds) * 1.5 + 5 if clouds else 100)
        ax.set_title(f"{year}  (best of {len(scenes)} in window)",
                     fontsize=11, fontweight="bold", loc="left")
        ax.grid(axis="y", alpha=0.3, linestyle=":")
        ax.tick_params(labelsize=7.5)

        if n > 1 and idx < n - 1:
            ax.set_xlabel("")

    field_label = f"Field {field_name} — " if field_name else ""
    fig.suptitle(f"{field_label}Best Sentinel-2 scene per growing season (Jun–Aug)",
                 fontsize=13, fontweight="bold", y=0.97)
    legend_elements = [
        Patch(facecolor="#2ca02c", label="Selected (lowest cloud)"),
        Patch(facecolor="#4A90D9", label="Candidate in Jun–Aug window"),
    ]
    fig.legend(handles=legend_elements, loc="upper center",
               bbox_to_anchor=(0.5, 0.92), ncol=2, frameon=True, fontsize=9)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(top=0.88, hspace=0.40)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_zones_per_year(
    zone_gdfs: dict[int, gpd.GeoDataFrame],
    field_boundary_gdf: gpd.GeoDataFrame,
    years: list[int],
    out_path: Path,
    zone_labels_map: dict[int, str] | None = None,
    scene_dates: dict[int, str] | None = None,
) -> Path:
    """Plot one panel per year using polygonized vector zones.

    Each panel draws the field boundary outline and fills zone polygons
    with solid colors clipped to the field extent.

    Args:
        zone_gdfs: Mapping of {year: GeoDataFrame} with zone polygons.
        field_boundary_gdf: Field boundary outline.
        years: Sorted list of years to plot.
        out_path: Output PNG path.
        zone_labels_map: Mapping of zone_id to label string.

    Returns:
        Path to the saved PNG.
    """
    from matplotlib.patches import Patch

    if zone_labels_map is None:
        zone_labels_map = {0: "low", 1: "medium", 2: "high"}

    palette = {0: "#e41a1c", 1: "#ffd700", 2: "#2ca02c"}
    n = len(years)

    fig, axes = plt.subplots(1, n, figsize=(5 * n, 4.5))
    if n == 1:
        axes = [axes]

    for ax, year in zip(axes, years):
        gdf = zone_gdfs.get(year)
        if gdf is None or gdf.empty:
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(str(year), fontsize=13, fontweight="bold")
            ax.axis("off")
            continue

        crs = gdf.crs
        if field_boundary_gdf.crs != crs:
            fb = field_boundary_gdf.to_crs(crs)
        else:
            fb = field_boundary_gdf

        fb.boundary.plot(ax=ax, color="black", linewidth=0.8)

        from matplotlib.colors import ListedColormap

        n_zones = max(zone_labels_map.keys()) + 1
        cmap_colors = [palette.get(i, "#888888") for i in range(n_zones)]
        gdf.plot(
            ax=ax,
            column="zone_id",
            cmap=ListedColormap(cmap_colors),
            legend=False,
            edgecolor="none",
        )

        date_label = scene_dates.get(year, "") if scene_dates else ""
        title = str(year) + (f" — {date_label}" if date_label else "")
        ax.set_title(title, fontsize=13, fontweight="bold")
        ax.set_aspect("equal")
        ax.axis("off")

    fig.suptitle("NDVI management zones per year", fontsize=13, fontweight="bold", y=0.98)
    legend_elements = [
        Patch(facecolor=palette[i], label=zone_labels_map.get(i, f"Zone {i}"))
        for i in range(max(zone_labels_map.keys()) + 1)
    ]
    fig.legend(handles=legend_elements, loc="upper center",
               bbox_to_anchor=(0.5, 0.93), ncol=len(legend_elements),
               frameon=True, fontsize=11)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(top=0.905)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def process_field(
    data_root: str,
    grower_slug: str,
    farm_slug: str,
    field_slug: str,
    n_clusters: int = 3,
    zone_labels_map: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Run the full per-year management zone workflow on a single field.

    For each year with a qualifying Sentinel-2 scene:
        1. Fill NDVI gaps via nearest-valid-neighbor interpolation
        2. KMeans clustering on that single year's NDVI
        3. Write per-year zone GeoTIFF
        4. Polygonize to per-year clipped vector layer
        5. Accumulate into combined GeoPackage

    Then produce a per-year panel plot from the vector layers.

    Returns a dict of output paths and metadata.
    """
    if zone_labels_map is None:
        zone_labels_map = {0: "low", 1: "medium", 2: "high"}

    field_dir, boundary_path, manifest_path, features_dir = resolve_field_paths(
        data_root, grower_slug, farm_slug, field_slug,
    )

    if not boundary_path.exists():
        return {"status": "skipped", "reason": f"Boundary not found: {boundary_path}"}

    if not manifest_path.exists():
        return {"status": "skipped", "reason": f"Sentinel manifest not found: {manifest_path}"}

    scene_plot_path = features_dir / "sentinel_scene_selection_per_year.png"
    plot_scene_selection(manifest_path, scene_plot_path, field_name=field_slug)

    boundary = gpd.read_file(boundary_path)
    best_scenes = select_best_scene_per_year(manifest_path)
    if not best_scenes:
        return {"status": "skipped", "reason": "No qualifying scenes found in Jun–Aug window"}

    runtime_base = Path(data_root) / "data-pipeline"
    features_dir.mkdir(parents=True, exist_ok=True)

    zone_gdfs: dict[int, gpd.GeoDataFrame] = {}
    zone_raster_paths: list[Path] = []
    combined_gpkg = features_dir / "ndvi_management_zones_k3.gpkg"
    if combined_gpkg.exists():
        combined_gpkg.unlink()

    for year in sorted(best_scenes):
        scene = best_scenes[year]
        ndvi_path = resolve_ndvi_path(scene, runtime_base)
        if not ndvi_path.exists():
            continue

        data, profile = read_ndvi(ndvi_path)
        filled = fill_ndvi_gaps(data, profile)
        zones = compute_management_zones(filled, n_clusters=n_clusters)

        raster_path = features_dir / f"ndvi_mgmt_zones_{year}.tif"
        write_zone_raster(zones, profile, raster_path)
        zone_raster_paths.append(raster_path)

        gdf = polygonize_zones(
            raster_path, boundary,
            out_path=combined_gpkg,
            zone_labels_map=zone_labels_map,
            year=year,
        )
        zone_gdfs[year] = gdf

    if not zone_gdfs:
        return {"status": "skipped", "reason": "No valid NDVI rasters processed"}

    years = sorted(zone_gdfs.keys())
    plot_path = features_dir / "ndvi_management_zones_per_year.png"
    from datetime import datetime
    scene_dates = {
        year: datetime.strptime(best_scenes[year]["scene_date"], "%Y-%m-%d").strftime("%b %d")
        for year in years if year in best_scenes
    }
    plot_zones_per_year(zone_gdfs, boundary, years, plot_path, zone_labels_map,
                        scene_dates=scene_dates)

    return {
        "status": "ok",
        "grower": grower_slug,
        "farm": farm_slug,
        "field": field_slug,
        "years": years,
        "scene_selection_plot": str(scene_plot_path),
        "zone_rasters": [str(p) for p in zone_raster_paths],
        "combined_gpkg": str(combined_gpkg),
        "plot": str(plot_path),
    }
