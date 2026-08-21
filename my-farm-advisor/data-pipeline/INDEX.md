# Data Pipeline Index

Use this area for runtime data pipeline orchestration, asset generation, and grower web-map workflows.

- [Runtime Setup](README.md) - install, runtime paths, and pipeline quick start
- [Pipeline Instructions](AGENTS.md) - runtime contract, command runbook, and edit-scope rules
- [Grower Web-Map Guide](grower-web-map/GUIDE.md) - self-contained Leaflet field maps with SSURGO and NDVI overlays

## CDL crop bootstrap

For crops such as grapes that are not reliably mapped as `landuse` polygons in
OpenStreetMap, bootstrap a farm from USDA NASS CDL classified pixels instead of
Overpass. `src/scripts/ingest/extract_crop_fields_from_cdl.py` downloads the
shared state CDL raster, clips it to a target county, polygonizes the requested
crop code (default `55` grapes), and writes the canonical boundary GeoJSON and
field inventory CSV. Use `--run-pipeline` to run the full farm pipeline after
the boundary is written.
