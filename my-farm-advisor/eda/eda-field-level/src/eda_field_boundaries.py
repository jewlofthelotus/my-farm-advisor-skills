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
from scipy.stats import pearsonr

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
    # Compactness = 4*pi*area / perimeter^2 (circle = 1)
    gdf_proj["compactness"] = (4 * np.pi * gdf_proj.area) / (gdf_proj.length ** 2)
    gdf_proj["grower"] = g.grower_slug
    gdf_proj["grower_label"] = g.grower_display
    for _, r in gdf_proj.iterrows():
        rows.append({
            "grower": g.grower_slug,
            "grower_label": g.grower_display,
            "field_id": r.get("field_id", r.get("id", "unknown")),
            "area_ha": r["area_ha"],
            "perimeter_m": r["perimeter_m"],
            "compactness": r["compactness"],
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

# ---------------------------------------------------------------------------
# V2a — Compactness box plot by grower
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(max(8, len(grower_order) * 1.5), 6))

data_for_box = [df[df["grower_label"] == label]["compactness"].values for label in grower_order]
bp = ax.boxplot(data_for_box, tick_labels=grower_order, patch_artist=True)
for patch, label in zip(bp["boxes"], grower_order):
    patch.set_facecolor(colors_map[label])
    patch.set_alpha(0.7)

ax.set_ylabel("Compactness (4πA / P²)", fontsize=12)
ax.set_title("Field Compactness by Grower\n(Higher = more circular/compact)", fontsize=13, fontweight="bold")
ax.axhline(1.0, color="black", linestyle="--", linewidth=0.8, alpha=0.5, label="Perfect circle")
ax.legend()
plt.tight_layout()
fig.savefig(resolve_output_dir() / "compactness_boxplot.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: compactness_boxplot.png")

# ---------------------------------------------------------------------------
# A1a — Compactness vs field area scatter + Pearson r CSV
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(9, 7))

records = []
for label in grower_order:
    subset = df[df["grower_label"] == label]
    color = colors_map[label]
    ax.scatter(subset["area_ha"], subset["compactness"], label=label,
               color=color, edgecolor="black", alpha=0.75, s=80)
    if len(subset) >= 2:
        r, p = pearsonr(subset["area_ha"], subset["compactness"])
        records.append({"grower": label, "pearson_r": round(r, 4), "p_value": round(p, 4), "n": len(subset)})
        # Trend line
        z = np.polyfit(subset["area_ha"], subset["compactness"], 1)
        p_line = np.poly1d(z)
        x_line = np.linspace(subset["area_ha"].min(), subset["area_ha"].max(), 100)
        ax.plot(x_line, p_line(x_line), color=color, linestyle="--", alpha=0.6)

# Overall correlation
r_all, p_all = pearsonr(df["area_ha"], df["compactness"])
records.append({"grower": "All", "pearson_r": round(r_all, 4), "p_value": round(p_all, 4), "n": len(df)})

ax.set_xlabel("Field Area (hectares)", fontsize=12)
ax.set_ylabel("Compactness (4πA / P²)", fontsize=12)
ax.set_title("Compactness vs Field Area\n(Per grower + trend lines)", fontsize=13, fontweight="bold")
ax.legend(title="Grower")
plt.tight_layout()
fig.savefig(resolve_output_dir() / "compactness_vs_area_scatter.png", dpi=300, bbox_inches="tight")
plt.close(fig)
print("Saved: compactness_vs_area_scatter.png")

# Save CSV
stats_df = pd.DataFrame(records)
stats_df.to_csv(resolve_output_dir() / "compactness_vs_area.csv", index=False)
print("Saved: compactness_vs_area.csv")
print(stats_df.to_string(index=False))
