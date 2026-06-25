# Grower Web-Map Local Instructions

## Purpose

This subskill owns the grower-level interactive web map generator. It reads pipeline field-boundary GeoJSON files and produces a self-contained Leaflet HTML map for each grower with optional SSURGO soil and NDVI overlay layers.

## Safe edit scope

Edits should stay in this folder and the generator script at `../src/scripts/generate_grower_web_map.py`. Do not change parent docs, sibling workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. Review `../AGENTS.md` for the data-pipeline runtime contract. Review the generator script to understand CLI arguments before running.

## Quick start

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/generate_grower_web_map.py --grower-slug il-grower
```

## Local validation

Open the generated HTML in a browser. There are no automated tests for this subskill.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not duplicate root-wide asset, vendor, or validation policy here except this pointer to `../../../AGENTS.md`.
