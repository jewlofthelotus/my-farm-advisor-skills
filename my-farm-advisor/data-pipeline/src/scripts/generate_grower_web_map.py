#!/usr/bin/env python3
"""Generate a grower-level interactive Leaflet web map from pipeline field boundaries."""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import rasterio


def find_grower_dirs(data_root):
    growers_dir = Path(data_root) / "data-pipeline" / "growers"
    if not growers_dir.is_dir():
        return []
    return sorted(d.name for d in growers_dir.iterdir() if d.is_dir())


def find_farms(grower_dir):
    farms_dir = grower_dir / "farms"
    if not farms_dir.is_dir():
        return []
    return sorted(d.name for d in farms_dir.iterdir() if d.is_dir())


def load_field_boundaries(farm_dir):
    path = farm_dir / "boundary" / "field_boundaries.geojson"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _slugify(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return normalized.strip("-")


def _load_ssurgo_csv_lookup(farm_dir, field_slug):
    csv_path = farm_dir / "fields" / field_slug / "soil" / "ssurgo_full.csv"
    if not csv_path.is_file():
        return {}
    lookup = {}
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mukey = row.get("mukey", "").strip()
            if not mukey:
                continue
            lookup[mukey] = row
    return lookup


_SSURGO_CSV_PROPS = [
    "compname", "comppct_r", "drainagecl",
    "om_r", "ph1to1h2o_r", "awc_r", "claytotal_r",
    "sandtotal_r", "silttotal_r", "dbthirdbar_r", "cec7_r",
]


def load_ssurgo_features(grower_dir, field_data_by_farm):
    features = []
    for farm_slug, data in field_data_by_farm.items():
        farm_dir = grower_dir / "farms" / farm_slug
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            field_slug = props.get("field_slug") or _slugify(props.get("field_id", ""))
            if not field_slug:
                continue
            soil_path = (
                farm_dir / "fields" / field_slug / "soil" / "ssurgo_soil_types.geojson"
            )
            if not soil_path.is_file():
                continue
            try:
                soil_gj = json.loads(soil_path.read_text(encoding="utf-8"))
                if not soil_gj.get("features"):
                    continue
                csv_lookup = _load_ssurgo_csv_lookup(farm_dir, field_slug)
                for sf in soil_gj.get("features", []):
                    sprops = sf.setdefault("properties", {})
                    sprops["field_id"] = props.get("field_id", "")
                    sprops["farm"] = farm_slug
                    if csv_lookup:
                        mukey = str(sprops.get("mukey", "")).strip()
                        csv_row = csv_lookup.get(mukey)
                        if csv_row:
                            for k in _SSURGO_CSV_PROPS:
                                v = csv_row.get(k)
                                if v is not None and v != "":
                                    try:
                                        sprops[k] = float(v)
                                    except (ValueError, TypeError):
                                        sprops[k] = v
                    features.append(sf)
            except (json.JSONDecodeError, OSError):
                continue
    return {"type": "FeatureCollection", "features": features}


def compute_field_ndvi(grower_dir, field_data_by_farm):
    ndvi_values = {}
    for farm_slug, data in field_data_by_farm.items():
        farm_dir = grower_dir / "farms" / farm_slug
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            field_slug = props.get("field_slug") or _slugify(props.get("field_id", ""))
            if not field_slug:
                continue
            field_id = props.get("field_id", "")
            features_dir = farm_dir / "fields" / field_slug / "derived" / "features"
            if not features_dir.is_dir():
                continue
            ndvi_tifs = sorted(features_dir.glob("ndvi_year_*_composite.tif"))
            if not ndvi_tifs:
                continue
            latest = ndvi_tifs[-1]
            try:
                with rasterio.open(latest) as src:
                    arr = src.read(1)
                    mask = src.read_masks(1)
                    valid = (mask > 0) & np.isfinite(arr)
                    if valid.any():
                        ndvi_values[field_id] = float(np.mean(arr[valid]))
            except Exception:
                continue
    return ndvi_values


def compute_center(features):
    lats, lons = [], []
    for f in features:
        coords = f["geometry"]["coordinates"]
        rings = coords if f["geometry"]["type"] == "Polygon" else coords[0]
        for ring in rings:
            for c in ring:
                lons.append(c[0])
                lats.append(c[1])
    if not lats:
        return 40.0, -93.0
    return (min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0


def generate_web_map(grower_slug, field_data_by_farm, output_path, ssurgo_fc=None, ndvi_values=None):
    all_features = []
    field_list_items = []

    for farm_slug, data in field_data_by_farm.items():
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            props["grower"] = grower_slug
            props["farm"] = farm_slug
            fid = props.get("field_id", "unknown")
            label = f"{grower_slug} / {farm_slug} / {fid}"
            field_list_items.append({
                "label": label,
                "field_id": fid,
                "grower": grower_slug,
                "farm": farm_slug,
                "crop": props.get("crop_name", ""),
                "area": props.get("area_acres", 0),
                "county": props.get("county_name", ""),
            })
            all_features.append(feature)

    fc = {"type": "FeatureCollection", "features": all_features}
    geojson_str = json.dumps(fc)
    field_json = json.dumps(field_list_items)

    ssurgo_str = json.dumps(ssurgo_fc) if ssurgo_fc and ssurgo_fc.get("features") else "null"
    ndvi_str = json.dumps(ndvi_values) if ndvi_values else "{}"

    center_lat, center_lon = compute_center(all_features)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{grower_slug} — Grower Field Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
#container {{ display: flex; height: 100vh; }}
#sidebar {{ width: 320px; min-width: 320px; background: #f5f5f5; border-right: 1px solid #ccc; overflow-y: auto; padding: 12px; }}
#sidebar h2 {{ font-size: 1.1em; margin-bottom: 8px; color: #1a5e1a; }}
#sidebar .field-item {{ padding: 6px 8px; margin: 2px 0; background: #fff; border-radius: 4px; cursor: pointer; font-size: 0.85em; border: 1px solid #ddd; }}
#sidebar .field-item:hover {{ background: #e8f5e9; }}
#sidebar .field-item .fid {{ font-weight: 600; }}
#sidebar .field-item .meta {{ color: #666; font-size: 0.9em; }}
#map {{ flex: 1; }}
.leaflet-popup-content {{ font-size: 0.9em; line-height: 1.4; }}
.info.legend {{ background: white; padding: 8px 12px; border-radius: 4px; box-shadow: 0 1px 5px rgba(0,0,0,0.4); font-size: 13px; line-height: 1.6; }}
.info.legend i {{ width: 14px; height: 14px; display: inline-block; margin-right: 4px; border: 1px solid #555; vertical-align: middle; }}
</style>
</head>
<body>
<div id="container">
<div id="sidebar">
<h2>Fields — {grower_slug}</h2>
<div id="field-list"></div>
</div>
<div id="map"></div>
</div>
<script>
var fieldData = {geojson_str};
var fieldList = {field_json};
var ssurgoData = {ssurgo_str};
var ndviValues = {ndvi_str};

var map = L.map('map').setView([{center_lat}, {center_lon}], 10);

var satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
    maxZoom: 19
}});

var osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
}});

satellite.addTo(map);

var baseLayers = {{
    'Satellite': satellite,
    'OpenStreetMap': osm
}};

var fieldStyles = {{
    satellite: {{ color: '#ffeb3b', weight: 2, fillColor: '#ffeb3b', fillOpacity: 0.15 }},
    osm: {{ color: '#1b5e20', weight: 2, fillColor: '#a5d6a7', fillOpacity: 0.35 }}
}};

function getFieldStyle() {{
    return fieldStyles.satellite;
}}

var geoLayer = L.geoJSON(fieldData, {{
    style: getFieldStyle,
    onEachFeature: function(feature, layer) {{
        var p = feature.properties;
        var popupHtml = '<b>Field:</b> ' + p.field_id + '<br>' +
            '<b>Grower:</b> ' + p.grower + '<br>' +
            '<b>Farm:</b> ' + p.farm + '<br>' +
            '<b>Crop:</b> ' + (p.crop_name || '—') + '<br>' +
            '<b>Area:</b> ' + (p.area_acres ? p.area_acres.toFixed(1) : '—') + ' ac<br>' +
            '<b>County:</b> ' + (p.county_name || '—');
        layer.bindPopup(popupHtml);
        layer.fieldId = p.field_id;
    }}
}}).addTo(map);

map.on('baselayerchange', function(e) {{
    var key = e.name === 'OpenStreetMap' ? 'osm' : 'satellite';
    geoLayer.eachLayer(function(layer) {{
        layer.setStyle(fieldStyles[key]);
    }});
}});

// --- SSURGO soil overlay ---
var ssurgoLayer = null;
var ssurgoColorMap = {{}};
var ssurgoPalette = ['#a6cee3','#1f78b4','#b2df8a','#33a02c','#fb9a99','#e31a1c','#fdbf6f','#ff7f00','#cab2d6','#6a3d9a','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
var ssurgoIdx = 0;

function ssurgoColor(name) {{
    if (!ssurgoColorMap[name]) {{
        ssurgoColorMap[name] = ssurgoPalette[ssurgoIdx % ssurgoPalette.length];
        ssurgoIdx++;
    }}
    return ssurgoColorMap[name];
}}

if (ssurgoData && ssurgoData.features && ssurgoData.features.length > 0) {{
    ssurgoLayer = L.geoJSON(ssurgoData, {{
        style: function(feature) {{
            return {{
                fillColor: ssurgoColor(feature.properties.compname || 'Unknown'),
                fillOpacity: 0.6,
                color: '#555',
                weight: 1
            }};
        }},
        onEachFeature: function(feature, layer) {{
            var p = feature.properties;
            var popupHtml = '<b>Field:</b> ' + (p.field_id || '') + '<br>' +
                '<b>Soil Component:</b> ' + (p.compname || '—') + '<br>' +
                '<b>Drainage:</b> ' + (p.drainagecl || '—') + '<br>' +
                '<b>OM:</b> ' + (p.om_r !== undefined && p.om_r !== null ? Number(p.om_r).toFixed(1) + '%' : '—') + '<br>' +
                '<b>pH:</b> ' + (p.ph1to1h2o_r !== undefined && p.ph1to1h2o_r !== null ? Number(p.ph1to1h2o_r).toFixed(1) : '—');
            layer.bindPopup(popupHtml);
        }}
    }});
}}

// --- NDVI overlay ---
var ndviLayer = null;

function ndviColor(value) {{
    var v = Math.max(0, Math.min(1, value));
    var colors = [
        [165, 0, 38], [215, 48, 39], [244, 109, 67], [253, 174, 97],
        [254, 224, 139], [255, 255, 191], [217, 239, 139], [166, 217, 106],
        [102, 189, 99], [26, 152, 80], [0, 104, 55]
    ];
    var idx = v * (colors.length - 1);
    var lo = Math.min(Math.floor(idx), colors.length - 2);
    var hi = lo + 1;
    var t = idx - lo;
    var c = colors[lo].map(function(x, i) {{ return Math.round(x + (colors[hi][i] - x) * t); }});
    return 'rgb(' + c.join(',') + ')';
}}

if (Object.keys(ndviValues).length > 0) {{
    ndviLayer = L.geoJSON(fieldData, {{
        style: function(feature) {{
            var ndvi = ndviValues[feature.properties.field_id];
            if (ndvi === undefined) {{
                return {{ fillOpacity: 0, color: '#999', weight: 1, dashArray: '3 3' }};
            }}
            return {{
                fillColor: ndviColor(ndvi),
                fillOpacity: 0.7,
                color: 'transparent',
                weight: 0
            }};
        }},
        onEachFeature: function(feature, layer) {{
            var p = feature.properties;
            var ndvi = ndviValues[p.field_id];
            var popupHtml = '<b>Field:</b> ' + p.field_id + '<br>' +
                '<b>Mean NDVI:</b> ' + (ndvi !== undefined ? ndvi.toFixed(3) : 'N/A');
            layer.bindPopup(popupHtml);
        }}
    }});
}}

// --- Layer controls ---
var overlays = {{}};
if (ssurgoLayer) {{ overlays['SSURGO Soil Units'] = ssurgoLayer; }}
if (ndviLayer) {{ overlays['NDVI (mean)'] = ndviLayer; }}
L.control.layers(baseLayers, overlays).addTo(map);

// --- Dynamic legend ---
var legendDiv;
var legendCtrl = L.control({{position: 'bottomright'}});
legendCtrl.onAdd = function() {{
    legendDiv = L.DomUtil.create('div', 'info legend');
    return legendDiv;
}};
legendCtrl.addTo(map);

function updateLegend() {{
    if (!legendDiv) return;
    var html;
    if (ssurgoLayer && map.hasLayer(ssurgoLayer)) {{
        var comps = {{}};
        ssurgoData.features.forEach(function(f) {{
            var name = (f.properties && f.properties.compname) || 'Unknown';
            comps[name] = ssurgoColor(name);
        }});
        html = '<b>SSURGO Soil Component</b><br>';
        for (var name in comps) {{
            html += '<i style="background:' + comps[name] + '"></i> ' + name + '<br>';
        }}
    }} else if (ndviLayer && map.hasLayer(ndviLayer)) {{
        html = '<b>Mean NDVI</b><br>';
        var steps = 6;
        for (var i = 0; i < steps; i++) {{
            var v = i / (steps - 1);
            html += '<i style="background:' + ndviColor(v) + '"></i> ' + v.toFixed(1) + '<br>';
        }}
    }} else {{
        html = '<b>Field Boundaries</b><br><span style="font-size:11px;color:#888;">enable overlays above</span>';
    }}
    legendDiv.innerHTML = html;
}}
updateLegend();
map.on('overlayadd overlayremove', updateLegend);

// --- Sidebar field list ---
var listEl = document.getElementById('field-list');
fieldList.forEach(function(item, idx) {{
    var div = document.createElement('div');
    div.className = 'field-item';
    div.innerHTML = '<div class="fid">' + item.field_id + '</div>' +
        '<div class="meta">' + item.grower + ' / ' + item.farm + ' &middot; ' +
        (item.area ? item.area.toFixed(1) + ' ac' : '') + '</div>';
    div.addEventListener('click', function() {{
        geoLayer.eachLayer(function(layer) {{
            if (layer.fieldId === item.field_id) {{
                map.fitBounds(layer.getBounds(), {{padding: [30, 30]}});
                layer.openPopup();
            }}
        }});
    }});
    listEl.appendChild(div);
}});
</script>
</body>
</html>'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate a grower-level interactive web map from pipeline field boundaries."
    )
    parser.add_argument(
        "--grower-slug",
        help="Specific grower slug. Omit to generate maps for all growers.",
    )
    parser.add_argument(
        "--output-dir",
        help="Custom output directory. Defaults to <grower-dir>/web-map/.",
    )
    parser.add_argument(
        "--no-ssurgo",
        action="store_true",
        help="Skip SSURGO soil overlay even if data exists.",
    )
    parser.add_argument(
        "--no-ndvi",
        action="store_true",
        help="Skip NDVI overlay even if data exists.",
    )
    args = parser.parse_args()

    data_root = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if not data_root:
        print("ERROR: DATA_PIPELINE_DATA_ROOT must be set.", file=sys.stderr)
        sys.exit(1)

    grower_slugs = [args.grower_slug] if args.grower_slug else find_grower_dirs(data_root)
    if not grower_slugs:
        print("No growers found.", file=sys.stderr)
        sys.exit(0)

    for slug in grower_slugs:
        grower_dir = Path(data_root) / "data-pipeline" / "growers" / slug
        if not grower_dir.is_dir():
            print(f"  Skipping {slug}: grower directory not found")
            continue

        farms = find_farms(grower_dir)
        if not farms:
            print(f"  Skipping {slug}: no farms found")
            continue

        field_data = {}
        for farm in farms:
            farm_dir = grower_dir / "farms" / farm
            data = load_field_boundaries(farm_dir)
            if data and data.get("features"):
                field_data[farm] = data

        if not field_data:
            print(f"  Skipping {slug}: no field boundary data found")
            continue

        if args.output_dir:
            out_dir = Path(args.output_dir)
        else:
            out_dir = grower_dir / "web-map"

        out_path = out_dir / "grower_map.html"

        ssurgo_fc = None
        ndvi_values = None

        if not args.no_ssurgo:
            ssurgo_fc = load_ssurgo_features(grower_dir, field_data)
            if ssurgo_fc.get("features"):
                print(f"  {slug}: {len(ssurgo_fc['features'])} SSURGO polygons loaded")

        if not args.no_ndvi:
            ndvi_values = compute_field_ndvi(grower_dir, field_data)
            if ndvi_values:
                print(f"  {slug}: NDVI mean computed for {len(ndvi_values)} fields")

        generate_web_map(slug, field_data, out_path, ssurgo_fc=ssurgo_fc, ndvi_values=ndvi_values)
        field_count = sum(len(d["features"]) for d in field_data.values())
        print(f"  {slug}: {field_count} fields → {out_path}")


if __name__ == "__main__":
    main()
