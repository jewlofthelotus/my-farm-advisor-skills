# Geoadmin Runtime Data

The geoadmin GeoJSON and Parquet payloads are **runtime data**, not repository content. This repo keeps the downloader code plus per-layer metadata, then rebuilds the payloads into the writable runtime tree when needed.

## Why the payloads are excluded from git

- `countries.geojson`, `states_usa.geojson`, and the related Parquet outputs are generated artifacts.
- They are rebuilt from the recorded upstream URLs, so committing them would duplicate large runtime payloads and make imports noisier.
- The committed source of truth is the metadata JSON plus the downloader implementation, not the downloaded GeoJSON payload itself.

## Metadata files and what they do

The committed metadata lives under `my-farm-advisor/r2-seed-pipeline/src/shared/geoadmin/`:

- `l0_countries/metadata.json` — Natural Earth countries source URL, archive name, and expected runtime output paths.
- `l1_states/metadata.json` — Census TIGER/Line states source URL, archive name, and expected runtime output paths.
- `l2_counties/metadata.json` — Census TIGER/Line counties source URL, archive name, expected runtime output paths, plus the county lookup parquet path.

Those metadata files currently resolve to these upstream archives:

- `l0_countries` → `https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip`
- `l1_states` → `https://www2.census.gov/geo/tiger/TIGER2025/STATE/tl_2025_us_state.zip`
- `l2_counties` → `https://www2.census.gov/geo/tiger/TIGER2025/COUNTY/tl_2025_us_county.zip`

Required metadata fields for each layer are:

- `source_url`
- `archive_name`
- `output_geojson`
- `output_parquet`

## Runtime destination

The downloader writes the standardized payloads into the canonical runtime tree at:

- `data/my-farm-advisor/shared/geoadmin/l0_countries/`
- `data/my-farm-advisor/shared/geoadmin/l1_states/`
- `data/my-farm-advisor/shared/geoadmin/l2_counties/`

Expected outputs are:

- `data/my-farm-advisor/shared/geoadmin/l0_countries/countries.geojson`
- `data/my-farm-advisor/shared/geoadmin/l0_countries/countries.parquet`
- `data/my-farm-advisor/shared/geoadmin/l1_states/states_usa.geojson`
- `data/my-farm-advisor/shared/geoadmin/l1_states/states_usa.parquet`
- `data/my-farm-advisor/shared/geoadmin/l2_counties/counties_usa.geojson`
- `data/my-farm-advisor/shared/geoadmin/l2_counties/counties_usa.parquet`
- `data/my-farm-advisor/shared/geoadmin/l2_counties/fips_lookup.parquet`

## How to run the downloader

From `my-farm-advisor/r2-seed-pipeline/`:

```bash
./scripts/install.sh
source /data/workspace/data/my-farm-advisor/r2-seed-pipeline/.venv/bin/activate
python src/scripts/ingest/download_geoadmin.py --levels l0_countries l1_states l2_counties --census-year 2025
```

Useful variants:

```bash
python src/scripts/ingest/download_geoadmin.py --list-sources
python src/scripts/ingest/download_geoadmin.py --levels l2_counties --force
```

The script resolves the upstream download URLs from the committed metadata/catalog and performs the downloads itself. No manual asset download step is required.
