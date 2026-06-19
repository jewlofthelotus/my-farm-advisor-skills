# Headlands Ring Examples

This directory contains runnable examples for the `headlands-ring` subskill.

## `create_headlands_ring_from_boundary.py`

Create a headlands ring from a single field boundary file and export both
the field boundary and the headlands ring to GeoPackage files.

```bash
cd my-farm-advisor/field-management/headlands-ring
python examples/create_headlands_ring_from_boundary.py \
    --input field-boundaries/examples/real_10_fields_iowa.geojson \
    --width 21.0
```

Output files (written to the current working directory):
- `field_boundary.gpkg` — original boundary with `meters_squared` and `acres`
- `headlands_ring.gpkg` — headlands ring with `meters_squared` and `acres`

The example auto-detects the UTM zone from the boundary centroid,
performs all area calculations in meters, then reprojects the results
back to EPSG:4326.
