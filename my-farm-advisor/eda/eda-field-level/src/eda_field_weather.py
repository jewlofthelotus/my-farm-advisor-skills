#!/usr/bin/env python3
"""
eda_field_weather.py
Produce seasonal weather violin plots, GDD comparison,
and weather-anomaly vs crop-diversity correlation — dynamically discovered growers.
"""

import math
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from scipy.stats import pearsonr

from _discover_growers import (
    discover_growers,
    find_farm_table,
    parse_args,
    get_cli_filter,
    resolve_output_dir,
    resolve_data_root,
)

# ---------------------------------------------------------------------------
# Parse CLI
# ---------------------------------------------------------------------------
args = parse_args("Weather analysis")
growers = discover_growers(get_cli_filter(args))
if not growers:
    print("No growers discovered. Exiting.")
    exit(1)


def season_from_month(m):
    if m in [12, 1, 2]:
        return "Winter"
    if m in [3, 4, 5]:
        return "Spring"
    if m in [6, 7, 8]:
        return "Summer"
    return "Fall"


def load_weather(grower_slug, farm_slug):
    path = find_farm_table(grower_slug, farm_slug, "*_weather_*.csv")
    if path is None or not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["season"] = df["month"].apply(season_from_month)
    return df


def load_cdl_full(grower_slug, farm_slug):
    path = find_farm_table(grower_slug, farm_slug, "*_cdl_*_full_composition.csv")
    if path is None or not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    return df


# ---------------------------------------------------------------------------
# Load weather
# ---------------------------------------------------------------------------
weather_all = []
for g in growers:
    df = load_weather(g.grower_slug, g.farm_slug)
    if not df.empty:
        df["grower"] = g.grower_slug
        df["grower_label"] = g.grower_display
        weather_all.append(df)

wdf = pd.concat(weather_all, ignore_index=True) if weather_all else pd.DataFrame()

# ---------------------------------------------------------------------------
# V5a — Seasonal temperature & precipitation violin plot
# ---------------------------------------------------------------------------
if not wdf.empty:
    n = len(growers)
    cols = min(n, 3)
    rows = 2
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 8), sharex=True)
    if n == 1:
        axes = np.array([[axes[0]], [axes[1]]])
    axes = axes.flatten() if isinstance(axes, np.ndarray) else np.array(axes).flatten()

    seasons_order = ["Spring", "Summer", "Fall", "Winter"]
    grower_order = [g.grower_display for g in growers]

    for idx, g in enumerate(growers):
        sub = wdf[wdf["grower"] == g.grower_slug]
        if sub.empty:
            continue
        # Temperature
        ax_t = axes[idx]
        sns.violinplot(data=sub, x="season", y="T2M", order=seasons_order,
                       hue="season", palette="coolwarm", inner="box", ax=ax_t, legend=False)
        ax_t.set_title(f"{g.grower_display} — Temperature", fontsize=11, fontweight="bold")
        ax_t.set_xlabel("")
        ax_t.set_ylabel("Temperature (°C)", fontsize=9)
        ax_t.tick_params(axis="x", labelsize=8)

        # Precipitation
        ax_p = axes[idx + cols]
        sub_precip = sub.copy()
        sub_precip["PRECTOTCORR"] = sub_precip["PRECTOTCORR"].clip(upper=sub_precip["PRECTOTCORR"].quantile(0.99))
        sns.violinplot(data=sub_precip, x="season", y="PRECTOTCORR", order=seasons_order,
                       hue="season", palette="Blues", inner="box", ax=ax_p, legend=False)
        ax_p.set_title(f"{g.grower_display} — Precipitation", fontsize=11, fontweight="bold")
        ax_p.set_xlabel("Season", fontsize=9)
        ax_p.set_ylabel("Precipitation (mm/day)", fontsize=9)
        ax_p.tick_params(axis="x", labelsize=8)

    # Hide unused subplots
    for j in range(n, cols):
        axes[j].set_visible(False)
        axes[j + cols].set_visible(False)

    fig.suptitle("Seasonal Weather Distributions by Grower", fontsize=13, fontweight="bold", y=1.02)
    plt.tight_layout()
    out = resolve_output_dir() / "weather_seasonal_violin.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")

# ---------------------------------------------------------------------------
# V6b — GDD comparison by grower (accumulated Apr–Sep, base 10°C)
# ---------------------------------------------------------------------------
if not wdf.empty:
    gdd_records = []
    for (grower, field_id, year), sub in wdf.groupby(["grower", "field_id", "year"]):
        growing = sub[(sub["month"] >= 4) & (sub["month"] <= 9)]
        if growing.empty:
            continue
        gdd = ((growing["T2M"] - 10).clip(lower=0)).sum()
        g_label = next(g.grower_display for g in growers if g.grower_slug == grower)
        gdd_records.append({
            "grower": grower,
            "grower_label": g_label,
            "field_id": field_id,
            "year": year,
            "gdd": gdd,
        })
    gdd_df = pd.DataFrame(gdd_records)

    fig, ax = plt.subplots(figsize=(max(9, len(growers) * 2), 6))
    data_for_box = [gdd_df[gdd_df["grower_label"] == g.grower_display]["gdd"].values for g in growers]
    bp = ax.boxplot(data_for_box, tick_labels=[g.grower_display for g in growers], patch_artist=True)
    for patch, g in zip(bp["boxes"], growers):
        patch.set_facecolor(g.color)
        patch.set_alpha(0.7)

    ax.set_ylabel("Accumulated GDD (°C, base 10°C)", fontsize=12)
    ax.set_title("Growing Degree Days (Apr–Sep) by Grower\n(Per field per year)", fontsize=13, fontweight="bold")
    plt.tight_layout()
    out = resolve_output_dir() / "gdd_comparison_boxplot.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")

# ---------------------------------------------------------------------------
# A3c — Weather anomaly vs crop diversity
# ---------------------------------------------------------------------------
# For each grower-year: compute growing-season precip, GDD, and Shannon index

anomaly_records = []
for g in growers:
    # Load CDL
    cdl = load_cdl_full(g.grower_slug, g.farm_slug)
    # Load weather
    w_sub = wdf[wdf["grower"] == g.grower_slug]
    if cdl.empty or w_sub.empty:
        continue

    # Compute Shannon per year
    for year in sorted(cdl["year"].unique()):
        yr_cdl = cdl[cdl["year"] == year]
        totals = yr_cdl.groupby("crop_name")["pixel_count"].sum()
        if totals.sum() == 0:
            continue
        proportions = totals / totals.sum()
        shannon = -sum(p * np.log(p) for p in proportions if p > 0)

        # Compute growing-season weather
        yr_w = w_sub[w_sub["year"] == year]
        growing = yr_w[(yr_w["month"] >= 4) & (yr_w["month"] <= 9)]
        if growing.empty:
            continue
        precip = growing["PRECTOTCORR"].sum()
        gdd = ((growing["T2M"] - 10).clip(lower=0)).sum()

        anomaly_records.append({
            "grower": g.grower_display,
            "year": int(year),
            "precip_mm": round(precip, 1),
            "gdd": round(gdd, 1),
            "shannon_index": round(shannon, 4),
        })

anom_df = pd.DataFrame(anomaly_records)
if not anom_df.empty:
    # Z-score per grower
    for g_label in anom_df["grower"].unique():
        mask = anom_df["grower"] == g_label
        sub = anom_df.loc[mask]
        if len(sub) >= 2:
            anom_df.loc[mask, "precip_zscore"] = (sub["precip_mm"] - sub["precip_mm"].mean()) / sub["precip_mm"].std()
            anom_df.loc[mask, "gdd_zscore"] = (sub["gdd"] - sub["gdd"].mean()) / sub["gdd"].std()
        else:
            anom_df.loc[mask, "precip_zscore"] = 0.0
            anom_df.loc[mask, "gdd_zscore"] = 0.0

    # Save CSV
    anom_df.to_csv(resolve_output_dir() / "weather_diversity_anomaly.csv", index=False)
    print("Saved: weather_diversity_anomaly.csv")

    # Scatter plot: 2 panels
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    color_map = {g.grower_display: g.color for g in growers}

    # Panel 1: precip z-score vs Shannon
    ax1 = axes[0]
    for g_label in anom_df["grower"].unique():
        sub = anom_df[anom_df["grower"] == g_label]
        ax1.scatter(sub["precip_zscore"], sub["shannon_index"],
                    color=color_map.get(g_label, "#333333"),
                    edgecolor="black", alpha=0.75, s=100, label=g_label)
    if len(anom_df) >= 2:
        r, p = pearsonr(anom_df["precip_zscore"], anom_df["shannon_index"])
        ax1.set_title(f"Precip Anomaly vs Crop Diversity\nr = {r:.3f}, p = {p:.4f}", fontsize=12, fontweight="bold")
    else:
        ax1.set_title("Precip Anomaly vs Crop Diversity", fontsize=12, fontweight="bold")
    ax1.set_xlabel("Precipitation Z-Score (Apr–Sep)", fontsize=11)
    ax1.set_ylabel("Shannon Diversity Index", fontsize=11)
    ax1.axvline(0, color="black", linestyle="--", alpha=0.4)
    ax1.legend(title="Grower")

    # Panel 2: GDD z-score vs Shannon
    ax2 = axes[1]
    for g_label in anom_df["grower"].unique():
        sub = anom_df[anom_df["grower"] == g_label]
        ax2.scatter(sub["gdd_zscore"], sub["shannon_index"],
                    color=color_map.get(g_label, "#333333"),
                    edgecolor="black", alpha=0.75, s=100, label=g_label)
    if len(anom_df) >= 2:
        r2, p2 = pearsonr(anom_df["gdd_zscore"], anom_df["shannon_index"])
        ax2.set_title(f"GDD Anomaly vs Crop Diversity\nr = {r2:.3f}, p = {p2:.4f}", fontsize=12, fontweight="bold")
    else:
        ax2.set_title("GDD Anomaly vs Crop Diversity", fontsize=12, fontweight="bold")
    ax2.set_xlabel("GDD Z-Score (Apr–Sep)", fontsize=11)
    ax2.set_ylabel("Shannon Diversity Index", fontsize=11)
    ax2.axvline(0, color="black", linestyle="--", alpha=0.4)
    ax2.legend(title="Grower")

    plt.tight_layout()
    out = resolve_output_dir() / "weather_diversity_anomaly_scatter.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")
    print(anom_df.to_string(index=False))
else:
    print("No anomaly records generated (check data availability).")
