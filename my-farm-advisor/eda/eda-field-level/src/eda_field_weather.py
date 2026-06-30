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
    out = resolve_output_dir() / "average_cumulative_annual_precip_per_grower.png"
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out.name}")

# ---------------------------------------------------------------------------
# V7b — Per-grower year-over-year cumulative precipitation trajectory
# ---------------------------------------------------------------------------
if not wdf.empty:
    wdf["doy"] = wdf["date"].dt.dayofyear

    # Average precip across fields per grower-year-doy
    daily = wdf.groupby(
        ["grower", "grower_label", "year", "doy"]
    )["PRECTOTCORR"].mean().reset_index()
    daily = daily.sort_values(["grower", "grower_label", "year", "doy"])
    daily["cumulative"] = daily.groupby(["grower", "grower_label", "year"])["PRECTOTCORR"].cumsum()

    n = len(growers)
    cols = min(n, 3)
    rows = max(math.ceil(n / cols), 1)
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows), sharex=True, sharey=False)
    if n == 1:
        axes = np.array([[axes]])
    axes = axes.flatten() if isinstance(axes, np.ndarray) else np.array(axes).flatten()

    cmap = plt.colormaps["Dark2"]

    # Collect all years across growers for a consistent legend
    all_years = sorted(daily["year"].unique())

    for idx, g in enumerate(growers):
        ax = axes[idx]
        sub = daily[daily["grower"] == g.grower_slug]
        if sub.empty:
            ax.set_visible(False)
            continue

        yr_list = sorted(sub["year"].unique())
        for y in yr_list:
            yr_data = sub[sub["year"] == y].sort_values("doy")
            ax.plot(
                yr_data["doy"], yr_data["cumulative"],
                color=cmap(yr_list.index(y) % 8), label=str(int(y)), linewidth=1.5,
            )

        ax.set_title(g.grower_display, fontsize=11, fontweight="bold")
        ax.set_xlabel("Month", fontsize=9)
        ax.set_ylabel("Cumulative Precip (mm)", fontsize=9)
        ax.tick_params(axis="both", labelsize=8)
        month_doy = [1, 60, 121, 182, 244, 305]
        month_lbl = ["Jan", "Mar", "May", "Jul", "Sep", "Nov"]
        ax.set_xticks(month_doy)
        ax.set_xticklabels(month_lbl, fontsize=8)

    for j in range(n, rows * cols):
        axes[j].set_visible(False)

    fig.suptitle(
        "Year-over-Year Cumulative Precipitation by Grower",
        fontsize=13, fontweight="bold", y=1.02,
    )

    # Shared legend
    if len(all_years) > 1:
        handles = [
            plt.Line2D([], [], color=cmap(i % 8), label=str(int(y)))
            for i, y in enumerate(all_years)
        ]
        fig.legend(
            handles=handles, title="Year", loc="lower center",
            ncol=min(len(handles), 6), fontsize=9,
            bbox_to_anchor=(0.5, -0.06),
        )

    plt.tight_layout()
    out = resolve_output_dir() / "per_grower_precip_timeseries.png"
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
                ax.scatter(
                    yr.loc[mask, "doy"], yr.loc[mask, "PRECTOTCORR"],
                    c=season_colors[season], s=8, alpha=1.0,
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
        out = resolve_output_dir() / f"{g.grower_slug}_mean_daily_precip_by_season.png"
        fig.savefig(out, dpi=300, bbox_inches="tight")
        plt.close(fig)
        print(f"Saved: {out.name}")
