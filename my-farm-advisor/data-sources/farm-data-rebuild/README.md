# farm-data-rebuild

Document-routed workflow that rebuilds the canonical `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/` tree from a boundary input using the installed data-pipeline runtime scripts.

## Run

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py \
  --boundaries "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/input/fields.geojson" \
  --grower-slug demo-grower \
  --farm-slug demo-farm \
  --farm-name "Demo Farm"
```

## Deterministic behavior

- Sorts input boundaries by `field_id`.
- Writes stable slug mapping to `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/iowa-demo-grower/farms/iowa-demo-farm/manifests/field-inventory.csv`.
- Uses fixed canonical output locations.
- Verifies required files per field before reporting success.

## Notes

- Run from `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src` after `data-pipeline/scripts/install.sh` has copied the committed source into the runtime tree.
- Use `scripts/ingest/bootstrap_farm_from_county.py` from the same runtime source tree when a county bootstrap should create or append field boundaries and inventory mappings.
- Use `scripts/run_farm_pipeline.py --structure-test` for a no-download smoke check of the canonical directory layout.
