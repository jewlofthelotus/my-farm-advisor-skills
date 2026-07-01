# Local Instructions

## Purpose

This folder owns NDVI-based management zone delineation workflows. It uses Sentinel-2 NDVI rasters, nearest-valid-neighbor gap filling, and k-means clustering (k=3) to produce field-level management zone rasters, polygonized GeoPackages, and per-year zone visualizations.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader skill change. Do not change parent `SKILL.md`, sibling field-management workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first, then `examples/README.md` for sample usage. If routing context is needed, read `../INDEX.md` and `../../SKILL.md`.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes. If the guide or examples name a runnable command, run it against the smallest available sample when dependencies are available.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not duplicate root-wide asset, vendor, or validation policy here except this pointer to `../../../AGENTS.md`.
