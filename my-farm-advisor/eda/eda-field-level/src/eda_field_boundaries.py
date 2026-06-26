#!/usr/bin/env python3
"""
eda_field_boundaries.py
Produce static charts and a correlation table comparing field size and shape
across dynamically discovered growers.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import geopandas as gpd
from _discover_growers import (
    discover_growers,
    parse_args,
    get_cli_filter,
    resolve_output_dir,
    resolve_data_root,
)

# ---------------------------------------------------------------------------
# Parse CLI
# ---------------------------------------------------------------------------
args = parse_args("Field boundary analysis")
growers = discover_growers(get_cli_filter(args))
if not growers:
    print("No growers discovered. Exiting.")
    exit(1)

# ---------------------------------------------------------------------------
# Load + compute geometry metrics
# ---------------------------------------------------------------------------
rows = []
for g in growers:
    boundary_path = (
        resolve_data_root() / "growers" / g.grower_slug / "farms" / g.farm_slug / "boundary" / "field_boundaries.geojson"
    )
    if not boundary_path.exists():
        continue
    gdf = gpd.read_file(boundary_path)
    # Project to Albers Equal Area for accurate area / perimeter
    gdf_proj = gdf.to_crs(epsg=5070)
    gdf_proj["area_ha"] = gdf_proj.area / 10_000.0
    gdf_proj["perimeter_m"] = gdf_proj.length
    gdf_proj["grower"] = g.grower_slug
    gdf_proj["grower_label"] = g.grower_display
    for _, r in gdf_proj.iterrows():
        rows.append({
            "grower": g.grower_slug,
            "grower_label": g.grower_display,
            "field_id": r.get("field_id", r.get("id", "unknown")),
            "area_ha": r["area_ha"],
            "perimeter_m": r["perimeter_m"],
        })

df = pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# V1c — Cumulative area bar per grower (stacked by field)
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(10, 6))

grower_order = sorted(df["grower_label"].unique())
colors_map = {g.grower_display: g.color for g in growers}

bar_width = 0.6
x_positions = np.arange(len(grower_order))

for i, label in enumerate(grower_order):
    subset = df[df["grower_label"] == label].sort_values("area_ha", ascending=False)
    bottom = 0.0
    for _, row in subset.iterrows():
        ax.bar(i, row["area_ha"], bar_width, bottom=bottom,
               color=colors_map[label], edgecolor="white", linewidth=0.5, alpha=0.85)
        bottom += row["area_ha"]

ax.set_xticks(x_positions)
ax.set_xticklabels(grower_order)
ax.set_ylabel("Area (hectares)", fontsize=12)
ax.set_title("Cumulative Field Area by Grower\n(Each segment = one field, stacked largest to smallest)", fontsize=13, fontweight="bold")
ax.set_ylim(0, df.groupby("grower_label")["area_ha"].sum().max() * 1.05)
plt.tight_layout()
fig.savefig(resolve_output_dir() / "cumulative_field_area.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: cumulative_field_area.png")
