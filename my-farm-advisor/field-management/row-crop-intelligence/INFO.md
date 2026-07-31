# Row Crop Intelligence & Data Dashboard — Project Info

## Project Overview

This project creates a **single-page, offline-functional operational dashboard** for row crop fields at the grower level. It ingests field boundaries, satellite-derived NDVI time series, SSURGO soil metrics, and daily weather data from the My Farm Advisor runtime data pipeline and renders a comprehensive analytical view, supporting both **current-season (actionable)** and **historical (reference)** analysis across every year with corn-field data.

The dashboard is built as a **single self-contained HTML file** with all dependencies inlined:
- D3.js for interactive charts and map rendering
- GeoJSON field boundaries rendered via D3 `geoPath` (no tile basemaps)
- Inline SVG icons
- All data embedded as JSON

This architecture ensures the dashboard works with **zero network connectivity** after generation.

### Design Principles

- **Crop-agnostic data schema** — the core data model (field_id, geometry, metrics) does not reference crop-specific concepts. Crop-specific thresholds, growth-stage labels, and KPI definitions live in a separate `CROP_CONFIG` block keyed by `crop_type`, making it straightforward to add additional crop support later by adding a config block.
- **Risk-tiered visualization** — all fields are classified into a fixed 3-tier scale (Healthy / Watch / Critical) using the same color scheme (blue / amber / orange-red) across KPIs, map, charts, and action lists. Colors are paired with icons and text labels (not color alone) for colorblind safety — blue/amber/red was deliberately chosen over the more intuitive **green**/amber/red for Healthy, since green would reintroduce the classic red-green confusion pattern for the most common form of colorblindness.
- **Growth-stage-aware, not just threshold-aware** — NDVI risk classification accounts for where a field is in its corn phenology (accumulated GDD relative to VE/V6/VT/R1–R6), not just a flat NDVI number. See Risk Classification Logic below.
- **No framework** — vanilla JavaScript with a pub/sub state object. Every chart, KPI, and map subscribes to the shared state and re-renders on filter changes. No React, no Vue, no build step.

## Dataset Description

The runtime data for `il-grower` (Iroquois County, Illinois) contains:

### Fields (10 total)

| Dashboard | Field ID | Area (ac) | Crop (2025) |
|---|---|---|---|
| Field 1 | osm-1253853022 | 17.1 | Soybeans |
| Field 2 | osm-1254255035 | 16.1 | Corn |
| Field 3 | osm-1280521064 | 0.9 | Grass/Pasture |
| Field 4 | osm-1288035236 | 3.2 | Grass/Pasture |
| Field 5 | osm-1293013560 | 13.4 | Corn |
| Field 6 | osm-1293013562 | 4.0 | Corn |
| Field 7 | osm-1296444203 | 7.7 | Grass/Pasture |
| Field 8 | osm-1499317763 | 259.5 | Corn |
| Field 9 | osm-1525396389 | 50.6 | Soybeans |
| Field 10 | osm-889020586 | 238.5 | Soybeans |

Crop identity rotates year to year for most fields — see Crop Rotation below. Which fields display as corn (and are included in the dashboard) therefore changes depending on the selected year. The 2025 sample above is the most recent complete year of USDA CDL data. For the current year (2026), the dashboard uses the **predicted next crop** from the rotation analysis rather than CDL (which is not yet published as of mid-2026).

### NDVI Data

- **Source:** Sentinel-2 and Landsat 8/9 imagery, 2021–2026
- **Scene count:** ~7–16 scenes per satellite per year per field
- **Products:** Per-scene NDVI GeoTIFFs, annual composites, crop-rolled averages (corn vs. soybean), peak 95th percentile composites
- **Time series:** ~70–100 date-value pairs per field over 5+ years
- **Known characteristic:** fields that fall within the same satellite scene footprint share identical observation dates. This occasionally produces identical season-level statistics (e.g., stress-duration day counts) across geographically clustered fields — this is a genuine property of the imagery, not a data or calculation bug (verified by independently recomputing from raw per-date values).

### Soil Data

- **Source:** NRCS SSURGO database
- **Metrics per field:** AWS (available water storage, total inches across the profile — not a per-inch ratio), organic matter % (1.1–3.7%), pH (5.2–6.9), CEC, sand/silt/clay fractions, drainage class, erosion risk
- **Structure:** Horizon-level component data aggregated to field-level summary
- **Static across years:** soil properties are read from a shared, read-only per-field object and do not vary by selected year.

### Weather Data

- **Source:** NASA POWER (daily, 2021–2026)
- **Variables:** T2M_MIN / T2M_MAX (°C), PRECTOTCORR (mm/day), solar radiation, humidity, wind speed
- **Coverage:** ~2,190 daily records per field
- **Grid key assignment** — each field's centroid is mapped to the nearest NASA POWER grid cell (0.5° × 0.625° resolution) and recorded as a `grid_key` column in the weather CSV. Fields sharing a grid cell download weather once rather than per-field, deduplicating the downstream weather series. The dashboard uses this key to build shared weather lookups.
- **Known limitation:** NASA POWER's most recent ~1–2 months of data are frequently unavailable (a real-time publication lag, not a pipeline bug). The dashboard handles this explicitly: GDD/rain calculations skip missing days rather than propagating invalid values, and a shaded "Weather gap" region marks affected chart ranges so the limitation is visible rather than hidden.

### Crop Rotation

- Corn-soybean 2-year rotation pattern across the corn/soybean fields; Grass/Pasture and Winter Wheat fields are stable
- Pre-computed rotation sequences with `predicted_next_crop` and `predicted_following_crop`
- **Current-year crops in the dashboard** — because USDA CDL data for the current season is not yet published at generation time, the dashboard uses `predicted_next_crop` from the rotation table as the crop type for the current (incomplete) year. Past-year selections use actual CDL classifications.

## Dashboard Explanation

### Filter Bar

- **Field filter** — map-driven, no dropdown: clicking a field on the map filters the whole dashboard to that field; clicking it again (or the ✕ on the header indicator) clears back to "All Corn Fields". The header shows a read-only indicator ("All Corn Fields" or "Field N ✕"). Field selection never constrains the Year list.
- **Year selector** — always lists every year present in the data (never filtered by field selection); choose the current season (labeled "(Current)") or any past year with corn-field data. This is the primary control switching the dashboard between actionable and reference mode. Changing the year always resets the field filter to "All Corn Fields" for the new year.
- **Dashboard Legend** — collapsible header panel documenting risk tier thresholds, data sources, and the NDVI flagging methodology (including growth-stage suppression rules)

### Sections (top to bottom)

1. **KPI/Summary Header** — content depends on mode:
   - **Current-season (actionable):** Fields Requiring Attention, Average NDVI (with declining/improving breakdown), GDD Accumulated (with current growth stage, e.g. "V6–VT · Vegetative"), Days Since Significant Rain
   - **Reference (past year):** Season Stress Duration (headline — average days spent in Watch/Critical), Peak NDVI, GDD Accumulated (vs. Target and Annual Avg), Cumulative Rain for the season

2. **Priority Actions / Season Recap & Notable Events** (paired with the map) — mode-dependent:
   - **Current-season:** ranked, field-specific action list (e.g., "Monitor weekly. Low AWS raises drought sensitivity. Extended dry period. Check soil moisture and NDVI trend next week.")
   - **Reference:** season recap ranked by stress duration, with a notable-event callout when detected — a sharp NDVI drop correlated with either a dry spell or a temperature extreme, tagged with the growth stage it occurred in (e.g., "Sharp NDVI drop Oct 2–Nov 11, coinciding with a 14-day dry spell during R6+ (maturation).")

3. **Field Risk Map** — D3 choropleth of field boundaries (or minimum-size markers for polygons too small to render legibly), colored by risk tier, with collision-avoided labels and leader lines for offset labels. Click a field to filter the whole dashboard. Clicking it again (or the ✕ in the header) clears the filter.

4. **NDVI Time Series** — one line per field, with:
   - Growth-stage annotations (Planting, VE, V6, VT, R1–R6) positioned by actual accumulated GDD
   - A flat Stress reference line (NDVI 0.5) and a growth-stage-scaled Watch reference line (ramps from ~0.21 near planting to 0.7 by VT, reflecting that full-canopy NDVI isn't achievable before then)
   - Dashed line segments marking ≥30-day gaps between satellite observations (lower interpolation confidence)
   - A shaded region marking any known weather-data gap

5. **Field Ranking** — bar chart, by Current NDVI (actionable) or Peak NDVI (reference), color-coded by risk tier

6. **Driver/Correlation View** — scatter plot; NDVI vs. AWS in actionable mode, Season Stress Duration vs. AWS in reference mode. The relationship description (e.g., "a visible inverse relationship") is computed from the actual plotted correlation each time, not a fixed statement.

7. **Weather / GDD** — GDD accumulation for the season, actual vs. Target (2500 °F-days, the corn-maturity requirement); Annual Avg (the location's typical full-season climatological total) is shown alongside in the KPI for context.

8. **Soil / Sustainability** — bar chart of AWS or organic matter % per field, color-coded by risk tier

9. **Narrative Panel** — written interpretation referencing live computed numbers, phrased differently by mode (e.g., actionable: "Continue routine monitoring..."; reference: "No fields required immediate intervention this season..."):
   - Patterns/trends observed across fields
   - Healthier vs. at-risk fields
   - Environmental/soil condition variation
   - Decisions/actions the analysis informs
   - Most important predictive variables

10. **Footer** — branded navigation bar (decision-support disclaimer, verify-on-site note) plus the Dashboard Legend's threshold/methodology documentation and data freshness timestamp.

## Analytical Interpretation

### Key Questions the Dashboard Answers

1. **Which fields need attention right now?** (current season) — The Fields Requiring Attention KPI surfaces fields below the growth-stage-adjusted threshold; Priority Actions ranks them by severity.

2. **How did a past season play out, and what does it suggest for next season?** (reference mode) — Season Stress Duration and the Season Recap panel summarize each field's season, including any detected weather-correlated events, as input for next season's irrigation or drainage planning.

3. **How is NDVI trending relative to what's normal for this point in the season?** — The time-series chart's growth-stage-scaled Watch line and stage annotations let a declining or low NDVI reading be judged against what's actually expected at that phenological stage, not a single fixed number.

4. **Which soil and weather variables drive field health?** — The correlation scatter (NDVI or Season Stress Duration vs. AWS) and the GDD chart show whether water-holding capacity or heat accumulation better explain field-to-field variation, computed fresh from the data each time.

5. **How do fields compare spatially?** — The choropleth map gives instant visual identification of spatial clusters of at-risk fields.

6. **What should I do and in what order?** — Priority Actions (current season) or the Season Recap panel (past season) combine NDVI trend, soil limitation, and weather recency into a ranked list.

### Risk Classification Logic

Corn phenology is tracked via accumulated GDD (base 50°F) against standard growth-stage boundaries (VE, V6, VT, R1–R6). Risk classification is gated by phase:

- **Establishing (pre-VE):** NDVI flagging is fully suppressed — bare soil/emergence NDVI isn't diagnostic of field health.
- **Building canopy (VE–VT) & early reproductive (R1–R4):** absolute floor checks are active — **Critical** if NDVI < 0.5, **Watch** if NDVI is between 0.5 and the growth-stage-scaled Healthy threshold (see below). A genuinely low NDVI during grain fill (R1–R4) can still indicate a real problem (disease, plant loss) and is worth flagging.
- **R1–R4 specifically:** decline-based flags (a recent downward NDVI trend) are suppressed, since gradual senescence onset is expected here — but the absolute floor stays active, per above.
- **Late reproductive (R5–R6+):** flagging is fully suppressed — universal senescence by this point makes NDVI level non-diagnostic.
- **Healthy threshold scaling:** before VT, the Healthy cutoff (normally 0.7) scales from 30% of that value near planting up to the full 0.7 at VT, since full canopy closure isn't physically achievable earlier in the season. From VT onward the threshold is flat at 0.7.

Thresholds and the growth-stage boundary GDD values are derived from the `CROP_CONFIG` block and can be adjusted per crop type.

### Assumptions & Limitations

- **Growth-stage-scaled Healthy threshold** is a working geometric model (linear ramp to VT), not a research-validated NDVI-by-growth-stage curve for this specific hybrid or region. Calibration to actual field trials per hybrid maturity group would improve accuracy.
- **Weather data currency:** NASA POWER's most recent 1–2 months are frequently unavailable at generation time; GDD and rain KPIs reflect the last date with valid data, which the dashboard surfaces via a shaded gap region rather than silently extrapolating.
- **Shared satellite scene footprints** can produce identical season-level statistics across geographically clustered fields (see NDVI Data above) — a real data characteristic, confirmed by independently recomputing from raw values, not a bug.

## AI Usage Documentation

Two AI tools were used in different roles throughout this project: **opencode**, which implemented and iterated on the actual codebase directly, and **Claude**, used as a planning and independent-review partner throughout the build.

### opencode — Implementation

Across ~49 commits and roughly 2,000 session messages (Jul 24–30), opencode wrote and iterated on `generate_dashboard.py` and the embedded dashboard JS directly:
- Scaffolded the subskill (README.md, INFO.md, INDEX.md, AGENTS.md) and wrote the initial single-page D3 dashboard
- Built out the filter bar (year selector, corn-only implicit filtering, map-driven field filter) and made KPIs/risk classification date- and context-aware
- Iterated extensively on the geospatial map (from an initial satellite-basemap approach through a full rebuild as a plain D3 choropleth with label collision avoidance, after determining the basemap approach conflicted with the offline/low-file-size requirements)
- Fixed a significant set of correctness bugs identified during review (see below): GDD NaN propagation, AWS unit/label mismatch, NDVI trend using percent-change instead of absolute delta on a bounded 0–1 index, the missing planting-date gate, growth-stage-aware risk suppression, and the file-size optimization that took the dashboard from 5.76 MB to 532 KB
- Investigated and resolved the underlying weather-data gap (confirmed as a genuine NASA POWER publication lag rather than a pipeline defect) and the NDVI staleness issue affecting several fields (confirmed as a pipeline extraction gap, since fixed)

### Claude — Planning & Independent Review

Claude was used across the full project lifecycle, in a role distinct from opencode's implementation work:

- **Initial spec design** — before any code was written, worked through the dashboard's structure (section layout, mode-dependent KPI design, offline single-file architecture, D3 vs. alternatives, colorblind-safe palette selection) to lock a build spec.
- **Bug diagnosis by reading source, not just screenshots** — for several reported issues, independently re-implemented the relevant JS logic in Python against the actual embedded data to confirm root causes before recommending a fix, rather than reasoning from visual symptoms alone. This caught, among others: a Celsius/Fahrenheit unit bug in GDD accumulation, a missing planting-date gate causing GDD to include pre-season warmth, and the exact mechanism by which a month of missing July weather data (`Math.max(0, NaN)` evaluating to `NaN` in JavaScript) was silently breaking the NDVI chart's threshold line.
- **Catching its own false positives** — on at least one occasion, flagged something as a likely bug from a screenshot (near-identical Season Recap entries across fields) that turned out, on independent verification against raw data, to be a genuine and correct result of shared satellite scene dates. Retracted the claim once checked rather than defaulting to "fix it anyway." This is also why the "known characteristic" note above exists — one AI's mistaken bug report became documentation once verified.
- **Design tradeoffs argued from domain reasoning, not just aesthetics** — e.g., recommending Season Stress Duration (not Peak NDVI) as the reference-mode correlation partner for AWS, since peak NDVI represents a field's best moment and isn't the thing water storage capacity is meant to explain; recommending the R1–R4 vs. R5–R6+ split for absolute-floor suppression, since grain-fill NDVI is still diagnostic but late-season senescence isn't; flagging that switching the Healthy tier color to green would reintroduce a red-green colorblind confusion pattern the original palette was chosen to avoid.
- **Data pipeline investigation prompts** — wrote structured diagnostic prompts (not just "fix this") for tracing the root cause of the NDVI staleness and weather-gap issues back through the data pipeline, since both turned out to be upstream data issues rather than dashboard bugs.
- **Iterative fix-prompt authoring** — across roughly a dozen review rounds, diagnosed issues from dashboard screenshots and source code, then wrote specific, code-referenced fix instructions for opencode to implement, rather than making direct code edits itself.
- **This documentation** — README.md and INFO.md updates, including this AI usage section.

All AI-suggested fixes and design decisions were reviewed and verified before being accepted — several were pushed back on, revised, or overridden entirely (e.g., the green-vs-blue Healthy tier color decision, the choice to keep absolute NDVI floors active through R4, and the confirmation that the "identical Season Recap" pattern was real data rather than a bug) rather than applied automatically.