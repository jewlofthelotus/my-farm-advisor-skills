#!/usr/bin/env python3
"""Generate a grower-level interactive Leaflet web map from pipeline field boundaries."""

import argparse
import json
import os
import sys
from pathlib import Path


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


def generate_web_map(grower_slug, field_data_by_farm, output_path):
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
var map = L.map('map').setView([{center_lat}, {center_lon}], 10);

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
}}).addTo(map);

var geoLayer = L.geoJSON(fieldData, {{
    style: {{ color: '#1b5e20', weight: 2, fillColor: '#a5d6a7', fillOpacity: 0.35 }},
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
        generate_web_map(slug, field_data, out_path)
        field_count = sum(len(d["features"]) for d in field_data.values())
        print(f"  {slug}: {field_count} fields → {out_path}")


if __name__ == "__main__":
    main()
