#!/usr/bin/env python3
"""
eda_geospatial_map.py
Produce a cross-grower centroid field map with area-scaled markers on a
coastline-clipped state-outline basemap with lat/lon graticule.
Dynamically discovers growers from the runtime data tree.
"""

import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.colors import to_rgb
from matplotlib.patches import Patch
from shapely.geometry import box

from _discover_growers import (
    discover_growers,
    load_states_geojson,
    parse_args,
    get_cli_filter,
    resolve_output_dir,
    resolve_data_root,
)

# ---------------------------------------------------------------------------
# Parse CLI
# ---------------------------------------------------------------------------
args = parse_args("Geospatial map of all field boundaries")
growers = discover_growers(get_cli_filter(args))
if not growers:
    print("No growers discovered. Exiting.")
    exit(1)

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
states_gdf = load_states_geojson()

# Water-body overlay (lakes) — rendered above state fills so lake
# areas appear blue instead of grey, e.g. Illinois extending into Lake Michigan
lakes_path = resolve_data_root() / "shared" / "geoadmin" / "l3_lakes" / "lakes.geojson"
if lakes_path.exists():
    lakes_gdf = gpd.read_file(lakes_path)
all_fields = []
for g in growers:
    boundary_path = (
        resolve_data_root() / "growers" / g.grower_slug / "farms" / g.farm_slug / "boundary" / "field_boundaries.geojson"
    )
    if not boundary_path.exists():
        print(f"Warning: missing {boundary_path}")
        continue
    gdf = gpd.read_file(boundary_path)
    gdf["grower"] = g.grower_slug
    gdf["grower_label"] = g.grower_display
    gdf["color"] = g.color
    all_fields.append(gdf)

if not all_fields:
    print("No field boundary files found.")
    exit(1)

fields_gdf = gpd.GeoDataFrame(
    gpd.pd.concat(all_fields, ignore_index=True),
    crs=all_fields[0].crs,
)

# Project to equal-area CRS for acreage + centroids, then extract lon/lat for scatter
fields_projected = fields_gdf.to_crs("EPSG:5070")
fields_gdf["area_acres"] = fields_projected.geometry.area / 4046.86
centroids_projected = fields_projected.geometry.centroid
centroids_geo = centroids_projected.to_crs(fields_gdf.crs)
fields_gdf["centroid_lon"] = centroids_geo.x
fields_gdf["centroid_lat"] = centroids_geo.y

# ---------------------------------------------------------------------------
# Determine context states from plot extent
# ---------------------------------------------------------------------------
field_bounds = fields_gdf.total_bounds
pad = 1.0
plot_bbox = box(field_bounds[0] - pad, field_bounds[1] - pad,
                field_bounds[2] + pad, field_bounds[3] + pad)
plot_bbox_gdf = gpd.GeoDataFrame(geometry=[plot_bbox], crs=fields_gdf.crs)
context_states = states_gdf[states_gdf.geometry.intersects(plot_bbox_gdf.union_all())]
grower_state_codes = {g.state for g in growers}
grower_states = states_gdf[states_gdf["state_code"].isin(grower_state_codes)]
context_states = gpd.GeoDataFrame(
    gpd.pd.concat([context_states, grower_states]).drop_duplicates(subset="state_code"),
    crs=states_gdf.crs,
)
our_states = context_states[context_states["state_code"].isin(grower_state_codes)]

# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(14, 10))

# Background context states (light grey)
if not context_states.empty:
    context_states.plot(ax=ax, color="#f5f5f5", edgecolor="#bbbbbb", linewidth=0.8, zorder=1)

# Our states (slightly darker outline)
if not our_states.empty:
    our_states.plot(ax=ax, color="#eeeeee", edgecolor="#666666", linewidth=1.2, zorder=2)

# Water bodies — rendered above state fills so lake areas appear blue
if lakes_path.exists():
    lakes_in_view = lakes_gdf[lakes_gdf.geometry.intersects(plot_bbox_gdf.union_all())]
    if not lakes_in_view.empty:
        lakes_in_view.plot(ax=ax, color="#b3d4f0", edgecolor="none", linewidth=0, zorder=2.5)

# Field locations — scaled circles: larger fields = lighter, smaller fields = darker
for g in growers:
    subset = fields_gdf[fields_gdf["grower"] == g.grower_slug].sort_values("area_acres", ascending=False)
    if not subset.empty:
        sizes = 15 + subset["area_acres"] ** 0.5 * 30
        base = np.array(to_rgb(g.color))
        n = len(subset)
        t = np.arange(n)[::-1] / max(n - 1, 1)
        dark = base * 0.4
        light = base + (1.0 - base) * 0.5
        colors = np.clip(dark[None, :] + t[:, None] * (light - dark)[None, :], 0, 1)
        ax.scatter(
            subset["centroid_lon"],
            subset["centroid_lat"],
            s=sizes,
            c=colors,
            alpha=1.0,
            edgecolors="none",
            zorder=3,
            label=g.grower_display,
        )

# Legend outside plot area (right side) — uniform swatches per grower
handles = [Patch(color=g.color, label=g.grower_display) for g in growers]
ax.legend(handles=handles, title="Grower", loc="upper left", bbox_to_anchor=(1.02, 1), framealpha=0.9)
state_names = ", ".join(sorted({g.state for g in growers}))
ax.set_title(f"Cross Grower Field Map — {state_names}\n({len(fields_gdf)} fields · centroid markers scaled by area)", fontsize=14, fontweight="bold")

# Graticule
ax.set_xlim(field_bounds[0] - pad, field_bounds[2] + pad)
ax.set_ylim(field_bounds[1] - pad, field_bounds[3] + pad)
ax.set_axisbelow(False)
ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
ax.yaxis.set_major_locator(mticker.MultipleLocator(1))
ax.grid(True, linestyle="--", alpha=0.4, color="#666666")
ax.set_xlabel("Longitude", fontsize=11)
ax.set_ylabel("Latitude", fontsize=11)

fig.subplots_adjust(right=0.82)
plt.tight_layout()
out_path = resolve_output_dir() / "cross_grower_field_centroid_map.png"
fig.savefig(out_path, dpi=300, bbox_inches="tight")
plt.close(fig)

print(f"Saved: {out_path}")
