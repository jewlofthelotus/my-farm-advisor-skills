---
name: ndvi-management-zones
description: Delineate field-level management zones from multi-year Sentinel-2 NDVI imagery. Selects the best cloud-free scene per year (late Jun–Aug), fills gaps via nearest-valid-neighbor interpolation, runs per-year KMeans k=3, polygonizes zones, and produces per-year zone visualizations.
license: MIT
compatibility: Requires Python 3.11+, ${DATA_PIPELINE_DATA_ROOT} pointing to an existing runtime with Sentinel-2 scene manifests and NDVI GeoTIFFs. Uses scikit-learn for KMeans clustering and rasterio for all raster processing.
metadata:
  author: Boreal Bytes
  version: "1.0.0"
  category: geospatial
  tags: ndvi, management-zones, kmeans, sentinel-2, rasterio, scikit-learn, agriculture
---

# NDVI Management Zones

_Field-level management zone delineation from multi-year Sentinel-2 NDVI._

---

## What this skill covers

1. **Scene selection per year** — rank all Sentinel-2 scenes within the late June–August window by coverage (>95% of field) and cloud cover; keep the best one per year
2. **NDVI read** — load the existing NDVI GeoTIFF for each selected scene
3. **Gap filling** — fill masked/NaN pixels within the field extent using nearest-valid-neighbor interpolation (`rasterio.fill.fillnodata`)
4. **Per-year KMeans** — run KMeans(k=3) independently on each year's NDVI, sorted by mean NDVI (0=low, 1=medium, 2=high)
5. **Per-year zone rasters** — write one integer-label GeoTIFF per year (`mgmt_zones_<year>.tif`)
6. **Polygonize** — convert each year's zone raster to polygons, clip to field boundary, accumulate into a single GeoPackage (`management_zones_k3.gpkg`) with a `year` column
7. **Scene selection visualization** — horizontal bar chart per year showing the top 10 lowest-cloud scenes, highlighting which fall in the Jun–Aug window and which was selected (`sentinel_scene_selection_per_year.png`)
8. **Zone visualization** — one panel per year using polygonized vector zones clipped to field boundary (`management_zones_per_year.png`)

## Zone labels

| Zone | Label | Meaning |
|------|-------|---------|
| 0 | low | Lowest NDVI within that year (stressed / low productivity) |
| 1 | medium | Moderate NDVI within that year |
| 2 | high | Highest NDVI within that year (vigorous / high productivity) |

## Prerequisites

This workflow builds on existing Sentinel-2 imagery. Before running, ensure the data pipeline has produced Sentinel-2 scenes and NDVI rasters for the target field(s). See the [Sentinel-2 Imagery Guide](../sentinel2-imagery/GUIDE.md) to generate those inputs.

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
```

## Module overview

The core logic lives in `src/management_zones.py`. Key functions:

| Function | Purpose |
|----------|---------|
| `resolve_field_paths()` | Resolve field directory, boundary, sentinel manifest, and features output dir |
| `select_best_scene_per_year()` | From the sentinel manifest, pick the lowest-cloud scene in Jun–Aug per year |
| `resolve_ndvi_path()` | Resolve the NDVI GeoTIFF path for a scene |
| `read_ndvi()` | Load NDVI array + raster profile |
| `fill_ndvi_gaps()` | Fill NaN pixels via nearest-valid-neighbor interpolation |
| `compute_management_zones()` | KMeans k=3 on a single year's NDVI array |
| `write_zone_raster()` | Write integer-label zone GeoTIFF |
| `polygonize_zones()` | Raster to polygons, clip to field boundary, accumulate into GeoPackage |
| `plot_scene_selection()` | Per-year vertical bar chart of all Jun–Aug scenes with selected highlight |
| `plot_zones_per_year()` | Multi-panel PNG from polygonized vector zones |
| `process_field()` | **High-level orchestration** — runs the full workflow for one field |

## End-to-end example: single field

This example processes one field (`osm-889020586` under grower `il-grower`) using the high-level `process_field()` function.

```bash
cd my-farm-advisor/field-management/ndvi-management-zones

${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python << 'PY'
import sys
sys.path.insert(0, 'src')

from management_zones import process_field
import json

result = process_field(
    data_root="${DATA_PIPELINE_DATA_ROOT}",
    grower_slug="il-grower",
    farm_slug="il-grower-illinois",
    field_slug="osm-889020586",
    n_clusters=3,
    zone_labels_map={0: "low", 1: "medium", 2: "high"},
)
print(json.dumps(result, indent=2, default=str))
PY
```

## Running against all growers

To process every field under every grower, use `process_field()` in a directory walk:

```bash
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python << 'PY'
import sys
sys.path.insert(0, 'my-farm-advisor/field-management/ndvi-management-zones/src')

from management_zones import process_field
from pathlib import Path

DATA_ROOT = "${DATA_PIPELINE_DATA_ROOT}"
runtime_base = Path(DATA_ROOT) / "data-pipeline"
growers_dir = runtime_base / "growers"

zone_labels_map = {0: "low", 1: "medium", 2: "high"}

for grower_dir in sorted(growers_dir.iterdir()):
    if not grower_dir.is_dir():
        continue
    grower = grower_dir.name
    farms_dir = grower_dir / "farms"
    if not farms_dir.exists():
        continue
    for farm_dir in sorted(farms_dir.iterdir()):
        farm = farm_dir.name
        fields_dir = farm_dir / "fields"
        if not fields_dir.exists():
            continue
        for field_dir in sorted(fields_dir.iterdir()):
            field = field_dir.name
            result = process_field(
                DATA_ROOT, grower, farm, field,
                n_clusters=3,
                zone_labels_map=zone_labels_map,
            )
            print(f"{result['status']:>8} {grower}/{farm}/{field}"
                  f"  {result.get('reason', '')}")
PY
```

## Expected outputs

For each field processed:

```
<field>/derived/features/
  sentinel_scene_selection_per_year.png   # Scene ranking per year with selection highlight
  ndvi_mgmt_zones_<year>.tif              # Per-year zone raster (int16, 0/1/2, nodata=-1)
  ndvi_management_zones_k3.gpkg           # Combined GeoPackage with all years' zones
  ndvi_management_zones_per_year.png      # Multi-panel vector-based zone visualization
```

The GeoPackage contains one layer (`management_zones`) with columns:

| Column | Type | Description |
|--------|------|-------------|
| `zone_id` | int | Zone class (0=low, 1=medium, 2=high) |
| `zone_label` | str | Human-readable label |
| `year` | int | Year the zone was derived from |
| `area_m2` | float | Zone polygon area in square meters (EPSG:5070) |
| `geometry` | Geometry | Zone polygon in the raster's native CRS |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No scenes found for a year | No Sentinel-2 coverage in Jun–Aug with >95% field coverage | Widen the date window or lower the coverage threshold |
| All pixels in one zone | Insufficient NDVI variability within that year | Check that the scene has vegetation and isn't entirely bare soil |
| Small fragmented polygons | KMeans overfitting to pixel-level noise | Increase `max_search_distance` in `fill_ndvi_gaps()` or smooth input rasters |
| Missing field boundary | Sentinel manifest exists but boundary not found | Run the field-boundaries workflow first |
| `fillnodata` leaves edge gaps | No valid neighbors within search distance | Increase `max_search_distance` or reduce nodata pixels with wider scene clip |

## References

- [Sentinel-2 Imagery Guide](../../imagery/sentinel2-imagery/GUIDE.md) — scene acquisition and NDVI computation
- [rasterio.fill.fillnodata](https://rasterio.readthedocs.io/en/stable/api/rasterio.fill.html) — nearest-valid-neighbor gap fill
- [scikit-learn KMeans](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.KMeans.html) — clustering algorithm
- [Data Pipeline Guide](../../data-pipeline/GUIDE.md) — runtime path conventions
