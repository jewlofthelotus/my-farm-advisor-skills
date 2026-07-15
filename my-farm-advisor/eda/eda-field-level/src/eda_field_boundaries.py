#!/usr/bin/env python3
"""
eda_field_boundaries.py
Produce static charts and a correlation table comparing field size and shape
across dynamically discovered growers.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
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

out_path = resolve_output_dir() / "cross_grower_field_boundary_metrics.csv"
df.to_csv(out_path, index=False)
print(f"Saved: {out_path.name}")

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
fig.savefig(resolve_output_dir() / "cross_grower_field_area_cumulative_stacked_bar.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: cross_grower_field_area_cumulative_stacked_bar.png")

# ---------------------------------------------------------------------------
# V1d — Field area histogram by grower (faceted, shared axes)
# ---------------------------------------------------------------------------
grower_order = sorted(df["grower_label"].unique())
n = len(grower_order)
fig, axes = plt.subplots(1, n, figsize=(5 * n, 4), sharex=True, sharey=True)
if n == 1:
    axes = [axes]

bin_edges = np.histogram_bin_edges(df["area_ha"], bins=15)

for ax, g_label in zip(axes, grower_order):
    subset = df[df["grower_label"] == g_label]
    color = colors_map[g_label]
    sns.histplot(
        subset["area_ha"], bins=bin_edges, color=color,
        edgecolor="white", linewidth=0.5, alpha=0.85, ax=ax,
    )
    median_val = subset["area_ha"].median()
    ax.axvline(median_val, color="black", linestyle="--", linewidth=1.0,
               label=f"Median: {median_val:.1f} ha")
    ax.set_title(g_label, fontsize=12, fontweight="bold")
    ax.set_xlabel("Area (ha)" if ax == axes[-1] else "")
    ax.set_ylabel("Count" if ax == axes[0] else "")
    ax.legend(fontsize=8)

fig.suptitle("Field Size Distribution by Grower", fontsize=14, fontweight="bold")
plt.tight_layout()
fig.savefig(resolve_output_dir() / "cross_grower_field_area_histogram.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: cross_grower_field_area_histogram.png")

# ---------------------------------------------------------------------------
# V2c — Field count and total acreage paired bar
# ---------------------------------------------------------------------------
summary = df.groupby("grower_label").agg(
    field_count=("field_id", "count"),
    total_area_ha=("area_ha", "sum"),
).reset_index()

out_path = resolve_output_dir() / "cross_grower_field_count_acreage_summary.csv"
summary.to_csv(out_path, index=False)
print(f"Saved: {out_path.name}")

grower_order = sorted(summary["grower_label"].unique())
colors_map = {g.grower_display: g.color for g in growers}
bar_colors = [colors_map[g] for g in grower_order]

fig, ax1 = plt.subplots(figsize=(9, 5))
x = np.arange(len(grower_order))
width = 0.35

bars1 = ax1.bar(x - width / 2, summary.set_index("grower_label").loc[grower_order, "field_count"],
                width, color=bar_colors, alpha=0.7, label="Field Count", edgecolor="white")
ax1.set_ylabel("Number of Fields", fontsize=12)
ax1.set_ylim(0, summary["field_count"].max() * 1.25)

ax2 = ax1.twinx()
bars2 = ax2.bar(x + width / 2, summary.set_index("grower_label").loc[grower_order, "total_area_ha"],
                width, color=bar_colors, alpha=0.3, label="Total Area (ha)", edgecolor="white")
ax2.set_ylabel("Total Area (hectares)", fontsize=12)
ax2.set_ylim(0, summary["total_area_ha"].max() * 1.25)

ax1.set_xticks(x)
ax1.set_xticklabels(grower_order)
ax1.set_title(
    "Field Count vs Total Acreage by Grower",
    fontsize=13, fontweight="bold",
)

lines1, labels1 = ax1.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=9)

plt.tight_layout()
fig.savefig(resolve_output_dir() / "cross_grower_field_count_vs_acreage.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: cross_grower_field_count_vs_acreage.png")
