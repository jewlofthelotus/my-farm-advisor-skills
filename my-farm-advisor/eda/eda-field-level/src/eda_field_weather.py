#!/usr/bin/env python3
"""
eda_field_weather.py
Produce GDD comparison, cumulative annual precipitation, and per-grower daily
precipitation visualizations — dynamically discovered growers.
"""

import math
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

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
# V7a — Cumulative annual precipitation per grower (grouped bar chart)
# ---------------------------------------------------------------------------
if not wdf.empty:
    # Total annual precip per field per year
    field_annual = wdf.groupby(
        ["grower", "grower_label", "year", "field_id"]
    )["PRECTOTCORR"].sum().reset_index()

    # Average across fields to get grower-year precipitation
    annual_precip = field_annual.groupby(
        ["grower", "grower_label", "year"]
    )["PRECTOTCORR"].agg(["mean", "std"]).reset_index()
    annual_precip.columns = ["grower", "grower_label", "year", "precip_mm", "precip_std"]
    annual_precip["precip_std"] = annual_precip["precip_std"].fillna(0.0)
    annual_precip["year"] = annual_precip["year"].astype(int)

    years_sorted = sorted(annual_precip["year"].unique())
    grower_order = [g.grower_display for g in growers]
    n_growers = len(grower_order)
    n_years = len(years_sorted)

    fig, ax = plt.subplots(figsize=(max(8, n_years * 1.5), 6))
    bar_width = 0.8 / max(n_growers, 1)

    for gi, g_label in enumerate(grower_order):
        sub = annual_precip[annual_precip["grower_label"] == g_label]
        color = next((g.color for g in growers if g.grower_display == g_label), "#333333")
        x_offsets = np.arange(n_years) + gi * bar_width - (n_growers - 1) * bar_width / 2
        vals = []
        errs = []
        for y in years_sorted:
            row = sub[sub["year"] == y]
            if not row.empty:
                vals.append(row["precip_mm"].values[0])
                errs.append(row["precip_std"].values[0])
            else:
                vals.append(0)
                errs.append(0)
        ax.bar(
            x_offsets, vals, bar_width, label=g_label,
            color=color, alpha=0.85, yerr=errs, capsize=3,
            edgecolor="black", linewidth=0.5,
        )

    ax.set_xticks(np.arange(n_years))
    ax.set_xticklabels([str(y) for y in years_sorted], fontsize=10)
    ax.set_xlabel("Year", fontsize=12)
    ax.set_ylabel("Total Precipitation (mm)", fontsize=12)
    ax.set_title(
        "Average Cumulative Annual Precipitation by Grower\n"
        "Error bars reflect per-field precipitation variability",
        fontsize=12, fontweight="bold")
    ax.legend(title="Grower", fontsize=9, bbox_to_anchor=(1.02, 1), loc="upper left")
    fig.subplots_adjust(right=0.82)

    plt.tight_layout()
    out = resolve_output_dir() / "cross_grower_weather_average_cumulative_annual_precip.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")

# ---------------------------------------------------------------------------
# V7c — Per-grower daily precipitation faceted by year, colored by season
# ---------------------------------------------------------------------------
if not wdf.empty:
    season_colors = {
        "Winter": "#1f77b4", "Spring": "#2ca02c",
        "Summer": "#ff7f0e", "Fall": "#d62728",
    }
    season_order = ["Spring", "Summer", "Fall", "Winter"]
    month_doy = [1, 60, 121, 182, 244, 305]
    month_lbl = ["Jan", "Mar", "May", "Jul", "Sep", "Nov"]

    for g in growers:
        sub = (wdf[wdf["grower"] == g.grower_slug]
               .copy()
               .assign(doy=lambda x: x["date"].dt.dayofyear)
               .groupby(["year", "doy", "season"], as_index=False)["PRECTOTCORR"].mean())
        years = sorted(sub["year"].unique())
        n = len(years)
        cols = 1
        rows = max(n, 1)
        fig, axes = plt.subplots(
            rows, cols, figsize=(8, 3 * rows), sharex=True, sharey=True,
        )
        if n == 1:
            axes = np.array([[axes]])
        axes = axes.flatten() if isinstance(axes, np.ndarray) else np.array(axes).flatten()

        for i, y in enumerate(years):
            ax = axes[i]
            yr = sub[sub["year"] == y]
            for season in season_order:
                mask = (yr["season"] == season) & (yr["PRECTOTCORR"] > 0)
                ax.bar(
                    yr.loc[mask, "doy"], yr.loc[mask, "PRECTOTCORR"],
                    width=1.0, color=season_colors[season], alpha=0.9,
                    label=season if i == 0 else "",
                )
            ax.set_title(str(int(y)), fontsize=10, fontweight="bold")
            ax.set_xticks(month_doy)
            ax.set_xticklabels(month_lbl, fontsize=7)
            ax.tick_params(axis="y", labelsize=7)
            ax.tick_params(axis="x", labelbottom=True)
            if i == 0:
                ax.set_ylabel("Precipitation (mm/day)", fontsize=8)

        for j in range(n, rows * cols):
            axes[j].set_visible(False)

        fig.suptitle(
            f"{g.grower_display} — Mean Daily Precipitation by Year and Season",
            fontsize=13, fontweight="bold", y=0.96,
        )

        from matplotlib.lines import Line2D
        season_handles = [
            Line2D([0], [0], marker="o", color="w",
                   markerfacecolor=season_colors[s], markersize=8, label=s)
            for s in season_order
        ]
        fig.legend(
            handles=season_handles, title="Season", loc="upper center",
            ncol=4, fontsize=9, bbox_to_anchor=(0.5, 0.94),
        )

        plt.tight_layout(rect=[0, 0, 1, 0.885], h_pad=3.0)
        out = resolve_output_dir() / f"{g.grower_slug}_weather_mean_daily_precip_by_season.png"
        fig.savefig(out, dpi=300, bbox_inches="tight")
        plt.close(fig)
        print(f"Saved: {out.name}")
