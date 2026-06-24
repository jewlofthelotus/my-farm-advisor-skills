#!/usr/bin/env python3
"""
eda_geospatial_map.py
Produce a single static map showing all field boundaries with state outlines
and lat/lon graticule. Dynamically discovers growers from the runtime data tree.
"""

import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from _discover_growers import (
    discover_growers,
    filter_states_from_fields,
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

# ---------------------------------------------------------------------------
# Determine context states from field bounds
# ---------------------------------------------------------------------------
context_states = filter_states_from_fields(fields_gdf, states_gdf)
our_states = context_states[context_states["state_code"].isin({g.state for g in growers})]

# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(14, 10))

# Background context states (light grey)
if not context_states.empty:
    context_states.plot(ax=ax, color="#f5f5f5", edgecolor="#cccccc", linewidth=0.5, zorder=1)

# Our states (slightly darker outline)
if not our_states.empty:
    our_states.plot(ax=ax, color="#eeeeee", edgecolor="#666666", linewidth=1.2, zorder=2)

# Field boundaries
for g in growers:
    subset = fields_gdf[fields_gdf["grower"] == g.grower_slug]
    if not subset.empty:
        subset.plot(
            ax=ax,
            facecolor=g.color,
            edgecolor="black",
            linewidth=0.6,
            alpha=0.55,
            zorder=3,
            label=g.grower_display,
        )

# Legend + labels
ax.legend(title="Grower", loc="lower right", framealpha=0.9)
state_names = ", ".join(sorted({g.state for g in growers}))
ax.set_title(f"Field Boundaries — {state_names}\n({len(fields_gdf)} fields total)", fontsize=14, fontweight="bold")

# Graticule
bounds = fields_gdf.total_bounds
ax.set_xlim(bounds[0] - 1, bounds[2] + 1)
ax.set_ylim(bounds[1] - 1, bounds[3] + 1)
ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
ax.yaxis.set_major_locator(mticker.MultipleLocator(2))
ax.grid(True, linestyle="--", alpha=0.4, color="#666666")
ax.set_xlabel("Longitude", fontsize=11)
ax.set_ylabel("Latitude", fontsize=11)

plt.tight_layout()
out_path = resolve_output_dir() / "geospatial_map.png"
fig.savefig(out_path, dpi=300, bbox_inches="tight")
plt.close(fig)

print(f"Saved: {out_path}")
