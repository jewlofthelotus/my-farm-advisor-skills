# Local Instructions

## Purpose

This folder owns the grower dashboard generator — a script that produces a self-contained HTML dashboard with a Leaflet satellite map and Plotly GDD/precipitation charts for any farm.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader change. Do not change parent `SKILL.md`, sibling workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first for CLI usage and prerequisites. If routing context is needed, read `../INDEX.md` and `../../SKILL.md`.

## Local workflow notes

- The script requires `DATA_PIPELINE_DATA_ROOT` to be set.
- Plotly.js, Leaflet.js, and Leaflet CSS are downloaded to `/tmp/` on first run and cached thereafter. Use `--skip-download` to avoid re-downloading.
- Use `--list-growers` to discover available grower/farm combinations.
- Use `--validate-only` to check that required input files exist before generating.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes. After editing the generator, test with:
```bash
export DATA_PIPELINE_DATA_ROOT=/home/coder/my-farm-advisor-runtime
python3 admin/grower-dashboard/src/generate_dashboard.py --grower-slug ia-grower --farm-slug ia-grower-iowa --validate-only
```

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not duplicate root-wide asset, vendor, or validation policy here except this pointer to `../../../AGENTS.md`.
