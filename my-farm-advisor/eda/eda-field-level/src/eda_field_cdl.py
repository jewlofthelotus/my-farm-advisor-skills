#!/usr/bin/env python3
"""
eda_field_cdl.py
Produce crop-transition flow charts, per-field rotation heatmaps,
and Shannon diversity analysis — dynamically discovered growers.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.colors import ListedColormap
from matplotlib.patches import FancyBboxPatch, Polygon

from _discover_growers import (
    discover_growers,
    find_farm_table,
    parse_args,
    get_cli_filter,
    resolve_output_dir,
)

# ---------------------------------------------------------------------------
# Parse CLI
# ---------------------------------------------------------------------------
args = parse_args("CDL analysis")
growers = discover_growers(get_cli_filter(args))
if not growers:
    print("No growers discovered. Exiting.")
    exit(1)

# Crop code → name mapping (common CDL codes)
CDL_CROPS = {
    1: "Corn",
    5: "Soybeans",
    24: "Winter Wheat",
    61: "Fallow/Idle",
    176: "Grass/Pasture",
    111: "Open Water",
    121: "Developed/Open",
    122: "Developed/Low",
    123: "Developed/Med",
    124: "Developed/High",
    131: "Barren",
    141: "Forest/Deciduous",
    142: "Forest/Evergreen",
    143: "Forest/Mixed",
    152: "Shrubland",
    190: "Woody Wetlands",
    195: "Herbaceous Wetlands",
}

CROP_COLORS = {
    "Corn": "#FFD700",
    "Soybeans": "#228B22",
    "Winter Wheat": "#DAA520",
    "Fallow/Idle": "#A9A9A9",
    "Grass/Pasture": "#8FBC8F",
    "Other": "#B0C4DE",
}


def load_cdl_full(grower_slug, farm_slug):
    """Load full composition CSV."""
    path = find_farm_table(grower_slug, farm_slug, "*_cdl_*_full_composition.csv")
    if path is None or not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["crop_name"] = df["crop_code"].map(lambda c: CDL_CROPS.get(int(c), "Other"))
    return df


def load_rotation(grower_slug, farm_slug):
    """Load rotation CSV."""
    path = find_farm_table(grower_slug, farm_slug, "*_crop_rotation.csv")
    if path is None or not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Build combined datasets
# ---------------------------------------------------------------------------
cdl_all = []
rot_all = []
for g in growers:
    cdl = load_cdl_full(g.grower_slug, g.farm_slug)
    if not cdl.empty:
        cdl["grower"] = g.grower_slug
        cdl["grower_label"] = g.grower_display
        cdl_all.append(cdl)
    rot = load_rotation(g.grower_slug, g.farm_slug)
    if not rot.empty:
        rot["grower"] = g.grower_slug
        rot["grower_label"] = g.grower_display
        rot_all.append(rot)

cdl_df = pd.concat(cdl_all, ignore_index=True) if cdl_all else pd.DataFrame()
rot_df = pd.concat(rot_all, ignore_index=True) if rot_all else pd.DataFrame()

# ---------------------------------------------------------------------------
# Helper: alluvial / flow between years
# ---------------------------------------------------------------------------
def _draw_flow_on_ax(ax, cdl_sub, grower_label):
    """Draw stacked bars + flow bands on a given axes."""
    if cdl_sub.empty:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, fontsize=12, color="gray")
        ax.set_title(grower_label, fontsize=13, fontweight="bold")
        return
    agg = cdl_sub.groupby(["year", "crop_name"])["pixel_count"].sum().reset_index()
    total_per_year = agg.groupby("year")["pixel_count"].sum().to_dict()

    years = sorted(agg["year"].unique())
    crops = sorted(agg["crop_name"].unique())
    crop_color = {c: CROP_COLORS.get(c, "#B0C4DE") for c in crops}

    bar_w = 0.35
    gap = 1.5
    xs = [i * gap for i in range(len(years))]

    # Draw bars
    for xi, yr in enumerate(years):
        y_bottom = 0.0
        yr_data = agg[agg["year"] == yr].sort_values("pixel_count", ascending=False)
        for _, row in yr_data.iterrows():
            crop = row["crop_name"]
            h = row["pixel_count"]
            rect = FancyBboxPatch(
                (xs[xi] - bar_w / 2, y_bottom), bar_w, h,
                boxstyle="round,pad=0,rounding_size=0.02",
                facecolor=crop_color[crop], edgecolor="white", linewidth=0.5, alpha=0.9,
            )
            ax.add_patch(rect)
            y_bottom += h

    # Draw connecting flows between consecutive years
    for xi in range(len(years) - 1):
        yr1, yr2 = years[xi], years[xi + 1]
        left = agg[agg["year"] == yr1].set_index("crop_name")["pixel_count"].to_dict()
        right = agg[agg["year"] == yr2].set_index("crop_name")["pixel_count"].to_dict()
        all_crops = set(left) | set(right)
        left_base = {c: 0.0 for c in all_crops}
        right_base = {c: 0.0 for c in all_crops}
        for crop in sorted(all_crops):
            lh = left.get(crop, 0)
            rh = right.get(crop, 0)
            if lh == 0 and rh == 0:
                continue
            x1 = xs[xi] + bar_w / 2
            x2 = xs[xi + 1] - bar_w / 2
            y1a = left_base[crop]
            y1b = y1a + lh
            y2a = right_base[crop]
            y2b = y2a + rh
            poly = Polygon(
                [(x1, y1a), (x1, y1b), (x2, y2b), (x2, y2a)],
                facecolor=crop_color.get(crop, "#B0C4DE"),
                edgecolor="none", alpha=0.35,
            )
            ax.add_patch(poly)
            left_base[crop] += lh
            right_base[crop] += rh

    ax.set_xlim(xs[0] - 0.6, xs[-1] + 0.6)
    ax.set_ylim(0, max(total_per_year.values()) * 1.05)
    ax.set_xticks(xs)
    ax.set_xticklabels(years)
    ax.set_xlabel("Year", fontsize=12)
    ax.set_ylabel("Pixel Count (CDL)", fontsize=12)
    ax.set_title(grower_label, fontsize=13, fontweight="bold")

    handles = [plt.Rectangle((0, 0), 1, 1, color=crop_color[c]) for c in crops if c in crop_color]
    labels = [c for c in crops if c in crop_color]
    ax.legend(handles, labels, title="Crop", loc="upper left", bbox_to_anchor=(1.02, 1), framealpha=0.9)


# ---------------------------------------------------------------------------
# V3c — Cross-grower crop composition flow (combined)
# ---------------------------------------------------------------------------
n_growers = len(growers)
ncols = min(3, n_growers)
nrows = int(np.ceil(n_growers / ncols))
fig, axes = plt.subplots(nrows, ncols, figsize=(10 * ncols, 7 * nrows))
axes_flat = axes.flatten() if n_growers > 1 else [axes]

for i, g in enumerate(growers):
    sub = cdl_df[cdl_df["grower"] == g.grower_slug]
    _draw_flow_on_ax(axes_flat[i], sub, g.grower_slug)

for j in range(i + 1, len(axes_flat)):
    axes_flat[j].set_visible(False)

fig.suptitle("Cross-Grower Crop Composition Flow", fontsize=16, fontweight="bold")
plt.tight_layout(rect=[0, 0, 1, 0.96])
out_path = resolve_output_dir() / "cross_grower_crop_composition_flow.png"
fig.savefig(out_path, dpi=300, bbox_inches="tight")
plt.close(fig)
print(f"Saved: {out_path.name}")

# ---------------------------------------------------------------------------
# V4a — Per-field rotation heatmap
# ---------------------------------------------------------------------------
for g in growers:
    sub = cdl_df[cdl_df["grower"] == g.grower_slug]
    if sub.empty:
        continue
    # Pivot to field × year
    pivot = sub.groupby(["field_id", "year", "crop_name"])["pixel_count"].sum().reset_index()
    dominant = pivot.loc[pivot.groupby(["field_id", "year"])["pixel_count"].idxmax()]
    heat = dominant.pivot(index="field_id", columns="year", values="crop_name")
    years = sorted(dominant["year"].unique())
    heat = heat.reindex(columns=years)

    all_crops = sorted(dominant["crop_name"].unique())
    crop_colors_list = [CROP_COLORS.get(c, "#B0C4DE") for c in all_crops]
    cmap = ListedColormap(crop_colors_list)
    crop_to_int = {c: i for i, c in enumerate(all_crops)}
    heat_num = heat.map(lambda x: crop_to_int.get(x, -1) if pd.notna(x) else -1)

    fig, ax = plt.subplots(figsize=(8, max(4, len(heat) * 0.4)))
    sns.heatmap(
        heat_num, cmap=cmap, linewidths=0.5, linecolor="white",
        cbar=False, ax=ax, vmin=-0.5, vmax=len(all_crops) - 0.5,
    )
    for i, row in enumerate(heat_num.values):
        for j, val in enumerate(row):
            if val >= 0:
                label = all_crops[int(val)]
                ax.text(j + 0.5, i + 0.5, label, ha="center", va="center",
                        fontsize=7, color="black", fontweight="bold")

    ax.set_xticklabels(years, rotation=0)
    ax.set_yticklabels(heat.index, rotation=0, fontsize=8)
    ax.set_xlabel("Year", fontsize=12)
    ax.set_ylabel("Field ID", fontsize=12)
    ax.set_title(f"Per-Field Crop Rotation Heatmap — {g.grower_display}\n(Dominant CDL crop per field-year)", fontsize=13, fontweight="bold")

    handles = [plt.Rectangle((0, 0), 1, 1, color=cmap.colors[i]) for i in range(len(all_crops))]
    ax.legend(handles, all_crops, title="Crop", loc="upper left", bbox_to_anchor=(1.02, 1), framealpha=0.9)

    plt.tight_layout()
    out = resolve_output_dir() / f"{g.grower_slug}_crop_rotation_heatmap.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")
