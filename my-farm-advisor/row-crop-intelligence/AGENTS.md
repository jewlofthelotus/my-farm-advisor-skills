# Local Instructions

## Purpose

This folder owns the Row Crop Intelligence & Data Dashboard workflow — generating a single-page, offline-functional operational dashboard for a grower's corn fields.

## Safe edit scope

Edits should stay in `my-farm-advisor/row-crop-intelligence/` unless the user explicitly asks for broader farm-advisor work. Do not modify sibling subskill trees from a dashboard task.

## Read nearby docs first

- `INDEX.md` for navigation
- `README.md` for how to run and data locations
- `INFO.md` for project context, dataset description, and analysis

## Local workflow notes

- The `scripts/generate_dashboard.py` script reads runtime data from `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/` and outputs to the `derived/dashboards/` subdirectory of the farm.
- Default grower is `il-grower`. Pass `--grower <slug>` for others.
- The runtime venv at `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/` has all Python dependencies.
- Generated dashboard HTML is not committed to the repository per the asset policy.
- The crop-type config block in the script is designed for extension — adding grape/vineyard requires only a new config dict, no logic changes.

## Local validation

Run the generator after code changes:

```bash
python my-farm-advisor/row-crop-intelligence/scripts/generate_dashboard.py --grower il-grower
```

Verify the output file exists in the runtime tree and open it in a browser. Also run `./scripts/validate.sh` from the repo root after structural changes.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. See `../AGENTS.md` and `../../AGENTS.md` for broader policy.
