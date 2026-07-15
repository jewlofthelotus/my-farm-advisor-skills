# Local Instructions

## Purpose

This folder owns the field-year-dashboard workflow that generates a single multi-panel dashboard image for one field and year.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader skill change. Do not change parent `INDEX.md`, sibling field-management workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. If routing context is needed, read `../INDEX.md` and `../../SKILL.md`.

## Local validation

Run `./scripts/validate.sh` from the repository root after structural changes. After changing Python code, also run `python -m py_compile` on changed modules.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not duplicate root-wide asset, vendor, or validation policy here except this pointer to `../../../AGENTS.md`.
