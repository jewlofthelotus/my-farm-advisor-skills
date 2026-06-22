---
name: dem-terrain
description: >
  Field-level DEM terrain documentation and runtime contract for My Farm Advisor.
  Use when a farm request needs elevation source selection, DEM provenance,
  terrain derivatives, hydrology warnings, or runtime-only DEM asset guidance.
license: Apache-2.0
metadata:
  author: Clayton Young / Superior Byte Works, LLC (@borealBytes)
  version: "0.1.0"
  skill-author: Clayton Young / Superior Byte Works, LLC (@borealBytes)
  skill-version: "0.1.0"
---

# DEM Terrain

Use this nested skill for field-scale elevation and terrain work in My Farm Advisor. It covers DEM source routing, source provenance, runtime output names, hydrology warnings, and the rule that generated DEM assets stay outside Git.

## Start Here

- Read [README.md](README.md) for the human guide, source hierarchy, outputs, runtime asset policy, and Open-Elevation rules.
- Read [INDEX.md](INDEX.md) for file-level navigation inside this package.
- Open [`src/dem_terrain/terrain_contract.py`](src/dem_terrain/terrain_contract.py) for product names, path templates, and manifest fields.
- Open [`src/dem_terrain/source_resolver.py`](src/dem_terrain/source_resolver.py) for adapter ids, candidate records, ranking policy, and fallback warnings.
- Use [`src/dem_terrain/package_validation.py`](src/dem_terrain/package_validation.py) to validate runtime DEM package invariants from a temp or external runtime manifest.
- Use the runtime CLI at `data-pipeline/src/scripts/ingest/download_dem_terrain.py` from the installed data-pipeline source copy when a farm needs actual DEM terrain products.
- Read [PROVENANCE.md](PROVENANCE.md) before adding or changing elevation sources.

## Routing Notes

- Invoke this skill when the request mentions DEM, elevation, slope, aspect, hillshade, curvature, terrain wetness, flow accumulation, depressions, relative elevation, erosion proxies, or DEM source provenance.
- Use it for U.S. 3DEP, Illinois ILHMP/ISGS, NASADEM, Copernicus GLO-30, ALOS AW3D30, optional OpenTopography, and Open-Elevation research-only policy questions.
- Keep execution explicit: dry-run is no-download planning, offline fixtures are the safe full-package smoke, and live downloads require `--allow-live-downloads`.
- Do not call hosted Open-Elevation APIs and do not commit generated DEM rasters, previews, caches, summaries, manifests, or downloaded tiles.
