#!/usr/bin/env python3
"""Generate a self-contained offline grower dashboard with satellite basemap + GDD/precip time-series.

Usage:
  DATA_PIPELINE_DATA_ROOT=/path/to/runtime \\
  python3 admin/grower-dashboard/src/generate_dashboard.py \\
    --grower-slug ia-grower --farm-slug ia-grower-iowa
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

LAST_FROST_DOY = 112
GDD_BASE_TEMP = 10.0
YEARS = [2021, 2022, 2023, 2024, 2025]

_FIELD_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"
]

_runtime_scripts_added = False


def _ensure_runtime_paths():
    global _runtime_scripts_added
    if _runtime_scripts_added:
        return
    data_root = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if not data_root:
        print("ERROR: DATA_PIPELINE_DATA_ROOT must be set.", file=sys.stderr)
        sys.exit(1)
    scripts_dir = str(Path(data_root) / "data-pipeline" / "src" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    _runtime_scripts_added = True


def load_plotly_js(skip_download: bool = False) -> str:
    path = Path("/tmp/plotly.min.js")
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if skip_download:
        print("ERROR: plotly.min.js not found in /tmp/ and --skip-download set.", file=sys.stderr)
        sys.exit(1)
    import urllib.request
    url = "https://cdn.plot.ly/plotly-2.35.2.min.js"
    with urllib.request.urlopen(url) as r:
        data = r.read()
    path.write_bytes(data)
    return data.decode("utf-8")


def load_leaflet_js(skip_download: bool = False) -> str:
    path = Path("/tmp/leaflet.js")
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if skip_download:
        print("ERROR: leaflet.js not found in /tmp/ and --skip-download set.", file=sys.stderr)
        sys.exit(1)
    import urllib.request
    url = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    with urllib.request.urlopen(url) as r:
        data = r.read()
    path.write_bytes(data)
    return data.decode("utf-8")


def load_leaflet_css(skip_download: bool = False) -> str:
    path = Path("/tmp/leaflet.css")
    fixed_path = Path("/tmp/leaflet_fixed.css")
    if fixed_path.is_file():
        return fixed_path.read_text(encoding="utf-8")
    if skip_download:
        print("ERROR: leaflet.css not found in /tmp/ and --skip-download set.", file=sys.stderr)
        sys.exit(1)
    import urllib.request
    url = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    with urllib.request.urlopen(url) as r:
        data = r.read().decode("utf-8")
    path.write_text(data, encoding="utf-8")
    base = "https://unpkg.com/leaflet@1.9.4/dist/images/"
    data = data.replace("url(images/", f"url({base}")
    data = data.replace("url('images/", f"url('{base}")
    data = data.replace('url("images/', f'url("{base}')
    fixed_path.write_text(data, encoding="utf-8")
    return data


def compute_doy(date_str: str) -> int:
    from datetime import datetime
    return datetime.strptime(date_str, "%Y-%m-%d").timetuple().tm_yday


def build_weather_json(weather_rows: list[dict]) -> dict:
    by_year_field: dict = {}
    for row in weather_rows:
        year = row["date"][:4]
        if year not in by_year_field:
            by_year_field[year] = {}
        field_id = row["field_id"]
        if field_id not in by_year_field[year]:
            by_year_field[year][field_id] = []
        tmin = float(row["T2M_MIN"])
        tmax = float(row["T2M_MAX"])
        tavg = (tmin + tmax) / 2.0
        gdd = max(0.0, tavg - GDD_BASE_TEMP)
        precip = float(row["PRECTOTCORR"])
        doy = compute_doy(row["date"])
        by_year_field[year][field_id].append({
            "doy": doy,
            "gdd": round(gdd, 2),
            "precip": round(precip, 2)
        })

    result = {}
    for year in YEARS:
        syear = str(year)
        if syear not in by_year_field:
            continue
        result[syear] = {}
        for fid, days in by_year_field[syear].items():
            days.sort(key=lambda d: d["doy"])
            gdd_cum = 0.0
            precip_cum = 0.0
            out = []
            for d in days:
                if d["doy"] >= LAST_FROST_DOY:
                    gdd_cum += d["gdd"]
                precip_cum += d["precip"]
                out.append({
                    "doy": d["doy"],
                    "gdd": d["gdd"],
                    "gddCum": round(gdd_cum, 2),
                    "precip": d["precip"],
                    "precipCum": round(precip_cum, 2)
                })
            result[syear][fid] = out
    return result


def compute_center_and_extent(fc: dict):
    lats, lons = [], []
    for f in fc["features"]:
        coords = f["geometry"]["coordinates"]
        rings = coords if f["geometry"]["type"] == "Polygon" else coords[0]
        for ring in rings:
            for c in ring:
                lons.append(c[0])
                lats.append(c[1])
    if not lats:
        return 43.33, -94.20, 43.0, 43.6, -94.5, -93.8
    return (
        (min(lats) + max(lats)) / 2.0,
        (min(lons) + max(lons)) / 2.0,
        min(lats), max(lats), min(lons), max(lons)
    )


def assign_colors(fc: dict) -> dict:
    colors = {}
    for i, f in enumerate(fc["features"]):
        fid = f["properties"]["field_id"]
        colors[fid] = _FIELD_COLORS[i % len(_FIELD_COLORS)]
    return colors


def discover_growers() -> list[dict]:
    _ensure_runtime_paths()
    from lib.paths import GROWERS_ROOT
    results = []
    if not GROWERS_ROOT.is_dir():
        return results
    for gdir in sorted(GROWERS_ROOT.iterdir()):
        if not gdir.is_dir():
            continue
        grower_slug = gdir.name
        farms_dir = gdir / "farms"
        farms = []
        if farms_dir.is_dir():
            for fdir in sorted(farms_dir.iterdir()):
                if fdir.is_dir():
                    farms.append(fdir.name)
        results.append({"grower": grower_slug, "farms": farms})
    return results


def validate_inputs(data_root: str, grower_slug: str, farm_slug: str) -> list[str]:
    errors = []
    base = Path(data_root) / "data-pipeline"
    gj_path = base / "growers" / grower_slug / "farms" / farm_slug / "boundary" / "field_boundaries.geojson"
    if not gj_path.is_file():
        errors.append(f"Field boundaries not found: {gj_path}")
    prefix = farm_slug.replace("-", "_")
    weather_path = base / "growers" / grower_slug / "farms" / farm_slug / "derived" / "tables" / f"{prefix}_weather_2021_2025.csv"
    if not weather_path.is_file():
        errors.append(f"Weather CSV not found: {weather_path}")
    for p in ["/tmp/plotly.min.js", "/tmp/leaflet.js", "/tmp/leaflet_fixed.css"]:
        if not Path(p).is_file():
            errors.append(f"Missing cached asset (run without --validate-only first): {p}")
    return errors


def generate_html(data_root: str, grower_slug: str, farm_slug: str,
                  plotly_js: str, leaflet_js: str, leaflet_css: str) -> str:
    base = Path(data_root) / "data-pipeline"
    gj_path = base / "growers" / grower_slug / "farms" / farm_slug / "boundary" / "field_boundaries.geojson"
    prefix = farm_slug.replace("-", "_")
    weather_path = base / "growers" / grower_slug / "farms" / farm_slug / "derived" / "tables" / f"{prefix}_weather_2021_2025.csv"

    with open(gj_path, encoding="utf-8") as f:
        fc = json.load(f)
    weather_rows = []
    with open(weather_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            weather_rows.append(row)

    weather_json = build_weather_json(weather_rows)
    field_colors = assign_colors(fc)
    center_lat, center_lon, min_lat, max_lat, min_lon, max_lon = compute_center_and_extent(fc)

    field_list = []
    for f in fc["features"]:
        p = f["properties"]
        field_list.append({
            "id": p["field_id"],
            "crop": p.get("crop_name", ""),
            "area": round(p["area_acres"], 1),
            "county": p.get("county_name", ""),
            "color": field_colors[p["field_id"]]
        })

    geojson_str = json.dumps(fc)
    weather_str = json.dumps(weather_json)
    fields_str = json.dumps(field_list)
    colors_str = json.dumps(field_colors)
    years_str = json.dumps(YEARS)
    default_year = YEARS[-1]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{grower_slug} — Field Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; overflow: hidden; height: 100vh; -webkit-font-smoothing: antialiased; }}
#header {{ display: flex; align-items: center; gap: 14px; padding: 8px 18px; background: #1e3a5f; color: #fff; flex-shrink: 0; height: 50px; }}
#header h1 {{ font-size: 1rem; font-weight: 600; margin-right: auto; letter-spacing: 0.01em; }}
#header label {{ font-size: 0.82rem; display: flex; align-items: center; gap: 5px; color: #c8d6e5; }}
#header select {{ font-size: 0.82rem; padding: 4px 8px; border-radius: 4px; border: 1px solid #4a6a8a; background: #2a4a6a; color: #fff; outline: none; cursor: pointer; max-height: 28px; }}
#header select option {{ background: #fff; color: #333; }}
#header select[multiple] {{ min-width: 220px; height: 26px; overflow-y: auto; cursor: default; }}
#header select[multiple] option {{ padding: 1px 4px; }}
#header button {{ font-size: 0.82rem; padding: 5px 14px; border-radius: 4px; border: 1px solid #4a90d9; background: #4a90d9; color: #fff; cursor: pointer; font-weight: 500; }}
#header button:hover {{ background: #357abd; border-color: #357abd; }}
#header button:active {{ background: #2a6aaa; }}
#main {{ display: flex; height: calc(100vh - 50px); }}
#left-pane {{ flex: 1; position: relative; min-width: 0; border-right: 1px solid #d0d0d0; }}
#right-pane {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
#right-pane > div {{ flex: 1; min-height: 0; }}
#right-pane > div:first-child {{ border-bottom: 1px solid #e0e0e0; }}
#map-container {{ width: 100%; height: 100%; }}
#field-zoom {{ position: absolute; top: 12px; left: 12px; z-index: 1000; }}
#field-zoom select {{ font-size: 0.82rem; padding: 5px 10px; border-radius: 4px; border: 1px solid #bbb; background: rgba(255,255,255,0.95); box-shadow: 0 2px 8px rgba(0,0,0,0.15); min-width: 230px; cursor: pointer; }}
#field-zoom select:focus {{ outline: none; border-color: #4a90d9; box-shadow: 0 2px 8px rgba(74,144,217,0.3); }}
</style>
<style>{leaflet_css}</style>
</head>
<body>
<div id="header">
<h1>🌾 {grower_slug} — Field Dashboard</h1>
<label>Fields:
<select id="field-select" multiple size="1"></select>
</label>
<label>Year:
<select id="year-select"></select>
</label>
<button id="reset-btn">Reset</button>
</div>
<div id="main">
<div id="left-pane">
<div id="field-zoom">
<select id="zoom-select"><option value="">All Fields</option></select>
</div>
<div id="map-container"></div>
</div>
<div id="right-pane">
<div id="gdd-chart"></div>
<div id="precip-chart"></div>
</div>
</div>
<script>
{plotly_js}
</script>
<script>
{leaflet_js}
</script>
<script>
var GEOJSON = {geojson_str};
var WEATHER = {weather_str};
var FIELDS = {fields_str};
var FIELD_COLORS = {colors_str};
var YEARS = {years_str};
var LAST_FROST_DOY = {LAST_FROST_DOY};
var DEFAULT_YEAR = {default_year};

var fieldSelect = document.getElementById('field-select');
var yearSelect = document.getElementById('year-select');
var zoomSelect = document.getElementById('zoom-select');
var resetBtn = document.getElementById('reset-btn');
var mapDiv = document.getElementById('map-container');
var gddDiv = document.getElementById('gdd-chart');
var precipDiv = document.getElementById('precip-chart');

var map, geoLayer;

function populateSelects() {{
    FIELDS.forEach(function(f) {{
        var opt1 = document.createElement('option');
        opt1.value = f.id;
        opt1.textContent = f.id + ' (' + f.area + ' ac)';
        opt1.selected = true;
        fieldSelect.appendChild(opt1);

        var opt2 = document.createElement('option');
        opt2.value = f.id;
        opt2.textContent = f.id + ' (' + f.area + ' ac)';
        zoomSelect.appendChild(opt2);
    }});

    YEARS.forEach(function(y) {{
        var opt = document.createElement('option');
        opt.value = y;
        opt.textContent = y;
        if (y === DEFAULT_YEAR) opt.selected = true;
        yearSelect.appendChild(opt);
    }});
}}

function getSelectedFields() {{
    var opts = fieldSelect.selectedOptions;
    return Array.from(opts).map(function(o) {{ return o.value; }});
}}

function getSelectedYear() {{
    return parseInt(yearSelect.value);
}}

// --- Leaflet Map ---
function buildMap() {{
    var satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
        maxZoom: 19
    }});

    map = L.map('map-container', {{
        layers: [satellite],
        center: [{center_lat}, {center_lon}],
        zoom: 10
    }});

    geoLayer = L.geoJSON(GEOJSON, {{
        style: function(feature) {{
            var fid = feature.properties.field_id;
            return {{
                color: '#ffffff',
                weight: 1.5,
                fillColor: FIELD_COLORS[fid] || '#888888',
                fillOpacity: 0.85
            }};
        }},
        onEachFeature: function(feature, layer) {{
            var p = feature.properties;
            layer.fieldId = p.field_id;
            layer.bindPopup(
                '<b>' + p.field_id + '</b><br>' +
                (p.crop_name || '') + '<br>' +
                (p.area_acres ? p.area_acres.toFixed(1) + ' ac' : '')
            );
        }}
    }}).addTo(map);

    map.fitBounds(geoLayer.getBounds(), {{ padding: [20, 20] }});
}}

function updateMapZoom() {{
    var val = zoomSelect.value;
    if (!val) {{
        map.fitBounds(geoLayer.getBounds(), {{ padding: [20, 20] }});
        return;
    }}
    geoLayer.eachLayer(function(layer) {{
        if (layer.fieldId === val) {{
            map.fitBounds(layer.getBounds(), {{ padding: [30, 30] }});
        }}
    }});
}}

function updateMapFields() {{
    var visFields = getSelectedFields();
    geoLayer.eachLayer(function(layer) {{
        var fid = layer.fieldId;
        if (visFields.indexOf(fid) !== -1) {{
            layer.setStyle({{
                fillOpacity: 0.85,
                opacity: 1.0,
                color: '#ffffff'
            }});
        }} else {{
            layer.setStyle({{
                fillOpacity: 0.03,
                opacity: 0.08,
                color: '#888888'
            }});
        }}
    }});
}}

// --- GDD Chart ---
function buildGddChart(year) {{
    var visFields = getSelectedFields();
    var traces = [];
    visFields.forEach(function(fid) {{
        var data = (WEATHER[String(year)] || {{}})[fid];
        if (!data) return;
        var doys = data.map(function(d) {{ return d.doy; }});
        var cum = data.map(function(d) {{ return d.gddCum; }});
        traces.push({{
            type: 'scatter',
            mode: 'lines',
            x: doys,
            y: cum,
            name: fid,
            legendgroup: fid,
            line: {{ color: FIELD_COLORS[fid], width: 2 }},
            hoverinfo: 'name+x+y'
        }});
    }});

    if (traces.length === 0) {{
        traces.push({{
            type: 'scatter',
            mode: 'lines',
            x: [],
            y: [],
            name: 'No data',
            line: {{ color: '#999' }}
        }});
    }}

    var frostLine = {{
        type: 'scatter',
        mode: 'lines',
        x: [LAST_FROST_DOY, LAST_FROST_DOY],
        y: [0, 3000],
        line: {{ color: '#888', width: 1.5, dash: 'dash' }},
        name: 'Frost date (DOY ' + LAST_FROST_DOY + ')',
        showlegend: true,
        hoverinfo: 'name'
    }};
    traces.push(frostLine);

    var layout = {{
        title: {{ text: 'Cumulative GDD (base 10°C, accumulation starts DOY ' + LAST_FROST_DOY + ')', font: {{ size: 12 }} }},
        xaxis: {{ title: 'Day of Year', range: [1, 366], dtick: 30 }},
        yaxis: {{ title: 'Cumulative GDD (°C days)' }},
        margin: {{ l: 55, r: 18, t: 32, b: 38 }},
        paper_bgcolor: '#ffffff',
        plot_bgcolor: '#fafafa',
        hovermode: 'x unified',
        showlegend: true,
        legend: {{ x: 1.02, y: 1, font: {{ size: 9 }}, tracegroupgap: 2 }}
    }};

    Plotly.newPlot(gddDiv, traces, layout, {{ responsive: true, displayModeBar: false }});
}}

// --- Precip Chart ---
function buildPrecipChart(year) {{
    var visFields = getSelectedFields();
    var barTraces = [];
    var lineTraces = [];
    visFields.forEach(function(fid) {{
        var data = (WEATHER[String(year)] || {{}})[fid];
        if (!data) return;
        var doys = data.map(function(d) {{ return d.doy; }});
        var precip = data.map(function(d) {{ return d.precip; }});
        var cum = data.map(function(d) {{ return d.precipCum; }});

        barTraces.push({{
            type: 'bar',
            x: doys,
            y: precip,
            name: fid + ' (daily)',
            legendgroup: fid,
            marker: {{ color: FIELD_COLORS[fid], opacity: 0.25 }},
            yaxis: 'y',
            hoverinfo: 'name+x+y',
            showlegend: true
        }});

        lineTraces.push({{
            type: 'scatter',
            mode: 'lines',
            x: doys,
            y: cum,
            name: fid + ' (cumulative)',
            legendgroup: fid,
            line: {{ color: FIELD_COLORS[fid], width: 2.5 }},
            yaxis: 'y2',
            hoverinfo: 'name+x+y',
            showlegend: false
        }});
    }});

    if (barTraces.length === 0) {{
        barTraces.push({{
            type: 'bar', x: [], y: [], name: 'No data', marker: {{ color: '#999' }}
        }});
    }}

    var allTraces = barTraces.concat(lineTraces);

    var frostLine = {{
        type: 'scatter',
        mode: 'lines',
        x: [LAST_FROST_DOY, LAST_FROST_DOY],
        y: [0, 2000],
        line: {{ color: '#888', width: 1.5, dash: 'dash' }},
        name: 'Frost date (DOY ' + LAST_FROST_DOY + ')',
        showlegend: true,
        hoverinfo: 'name',
        yaxis: 'y'
    }};
    allTraces.push(frostLine);

    var layout = {{
        title: {{ text: 'Daily & Cumulative Precipitation', font: {{ size: 12 }} }},
        xaxis: {{ title: 'Day of Year', range: [1, 366], dtick: 30 }},
        yaxis: {{ title: 'Daily Precipitation (mm)', side: 'left', color: '#333' }},
        yaxis2: {{
            title: 'Cumulative Precipitation (mm)',
            side: 'right',
            overlaying: 'y',
            color: '#555'
        }},
        barmode: 'overlay',
        margin: {{ l: 55, r: 55, t: 32, b: 38 }},
        paper_bgcolor: '#ffffff',
        plot_bgcolor: '#fafafa',
        hovermode: 'x unified',
        showlegend: true,
        legend: {{ x: 1.02, y: 1, font: {{ size: 9 }}, tracegroupgap: 2 }}
    }};

    Plotly.newPlot(precipDiv, allTraces, layout, {{ responsive: true, displayModeBar: false }});
}}

// --- Rebuild all ---
function rebuildAll() {{
    var year = getSelectedYear();
    buildGddChart(year);
    buildPrecipChart(year);
    updateMapFields();
}}

// --- Reset ---
function resetView() {{
    zoomSelect.value = '';
    for (var i = 0; i < fieldSelect.options.length; i++) {{
        fieldSelect.options[i].selected = true;
    }}
    yearSelect.value = String(DEFAULT_YEAR);
    rebuildAll();
    updateMapZoom();
}}

// --- Event Listeners ---
fieldSelect.addEventListener('change', rebuildAll);
yearSelect.addEventListener('change', function() {{
    var year = getSelectedYear();
    buildGddChart(year);
    buildPrecipChart(year);
}});
zoomSelect.addEventListener('change', updateMapZoom);
resetBtn.addEventListener('click', resetView);

// --- Init ---
populateSelects();
buildMap();
buildGddChart(DEFAULT_YEAR);
buildPrecipChart(DEFAULT_YEAR);
</script>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(
        description="Generate a self-contained offline grower dashboard with satellite map + GDD/precip charts."
    )
    parser.add_argument("--grower-slug", help="Grower slug (e.g. ia-grower)")
    parser.add_argument("--farm-slug", help="Farm slug (e.g. ia-grower-iowa)")
    parser.add_argument("--output", help="Custom output path (default: <farm>/derived/dashboards/grower_dashboard.html)")
    parser.add_argument("--list-growers", action="store_true", help="List available growers and farms")
    parser.add_argument("--validate-only", action="store_true", help="Only validate input files exist")
    parser.add_argument("--skip-download", action="store_true", help="Skip downloading assets if cached")
    args = parser.parse_args()

    if args.list_growers:
        growers = discover_growers()
        if not growers:
            print("No growers found. Check DATA_PIPELINE_DATA_ROOT.")
            sys.exit(0)
        for g in growers:
            farms_str = ", ".join(g["farms"]) if g["farms"] else "(no farms)"
            print(f"  {g['grower']}: {farms_str}")
        sys.exit(0)

    if not args.grower_slug or not args.farm_slug:
        parser.error("--grower-slug and --farm-slug are required (or use --list-growers)")

    data_root = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if not data_root:
        print("ERROR: DATA_PIPELINE_DATA_ROOT must be set.", file=sys.stderr)
        sys.exit(1)

    if args.validate_only:
        errors = validate_inputs(data_root, args.grower_slug, args.farm_slug)
        if errors:
            for e in errors:
                print(f"  FAIL: {e}")
            sys.exit(1)
        else:
            print("  All inputs validated successfully.")
            sys.exit(0)

    print(f"Dashboard: {args.grower_slug} / {args.farm_slug}")

    print("  Loading Plotly.js...")
    plotly_js = load_plotly_js(skip_download=args.skip_download)
    print(f"    {len(plotly_js):,} bytes")

    print("  Loading Leaflet...")
    leaflet_js = load_leaflet_js(skip_download=args.skip_download)
    leaflet_css = load_leaflet_css(skip_download=args.skip_download)
    print(f"    JS: {len(leaflet_js):,} bytes, CSS: {len(leaflet_css):,} bytes")

    print("  Generating HTML...")
    html = generate_html(data_root, args.grower_slug, args.farm_slug,
                         plotly_js, leaflet_js, leaflet_css)

    if args.output:
        out_path = Path(args.output)
    else:
        _ensure_runtime_paths()
        from lib.paths import farm_dashboards_dir, ensure_parent
        out_path = ensure_parent(farm_dashboards_dir(args.grower_slug, args.farm_slug) / "grower_dashboard.html")

    out_path.write_text(html, encoding="utf-8")
    mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  Written: {out_path} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
