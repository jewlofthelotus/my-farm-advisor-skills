# Field-Year Dashboard Example

## Quick start

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd /path/to/field-year-dashboard/src
python field_year_dashboard.py --field-id OSM_1428284928 --year 2024
```

## Output

The dashboard is saved to `{runtime_root}/growers/{grower}/farms/{farm}/fields/{field}/derived/reports/{year}_field_dashboard.png`.

## Requirements

- `DATA_PIPELINE_DATA_ROOT` environment variable set
- pandas, numpy, matplotlib, rasterio (optional for NDVI)

See `GUIDE.md` for full documentation.
