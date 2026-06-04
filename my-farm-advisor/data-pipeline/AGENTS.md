# Data Pipeline Local Instructions

## Purpose

This folder owns the deterministic data-pipeline subskill. It copies committed baseline `src/` files into live runtime storage, then runs farm data, reporting, and poster scripts from that runtime copy.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader skill change. Do not change parent `SKILL.md`, sibling workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `README.md` first. Review `scripts/install.sh`, `src/scripts/bootstrap_runtime.py`, and `src/scripts/run_farm_pipeline.py` before changing runtime or pipeline behavior. Read `../data-sources/INDEX.md` for related rebuild and reporting workflows.

## Runtime contract

- `DATA_PIPELINE_DATA_ROOT` is required and must be an absolute writable path outside the skill checkout.
- Runtime base is `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`.
- Runtime source is `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src`; run pipeline scripts from that copy, not from the checkout `src/`.
- Runtime venv is `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv` unless `DATA_PIPELINE_VENV_DIR` points to another absolute venv path.
- Generated outputs, downloaded payloads, reports, logs, and manifests belong under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`, not in the skill checkout, and must stay out of Git.
- User-level persistence defaults to `${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/60-my-farm-advisor.conf` with simple `KEY=VALUE` lines. This does not update already-running shells, so also export variables in the current shell before running commands.
- Non-interactive runs fail fast when required environment is missing. They must not fall back to `/data/workspace` or checkout-relative roots.
- Runtime source drift prompts in interactive runs. Non-interactive CI or smoke runs must use `--force-refresh` when replacing a divergent runtime source is intended.

## Command runbook

First-time interactive install with an explicit external data root:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh
```

Current-shell export for an existing runtime:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
export DATA_PIPELINE_VENV_DIR="${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv"
```

Persist the default data root for future login sessions:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d"
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/60-my-farm-advisor.conf" <<'EOF'
DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
EOF
```

Smoke install into a temporary external root without writing repo-local `data/`:

```bash
tmp_root="$(mktemp -d)"
DATA_PIPELINE_DATA_ROOT="$tmp_root" ./scripts/install.sh --non-interactive --force-refresh --no-install-deps
```

Run a structure test from the runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py --structure-test
```

Force-refresh runtime source for non-interactive CI or smoke tests:

```bash
tmp_root="$(mktemp -d)"
DATA_PIPELINE_DATA_ROOT="$tmp_root" \
  ./scripts/install.sh --non-interactive --force-refresh --no-install-deps
```

Root repository validation after documentation or structure changes:

```bash
cd ../..
./scripts/validate.sh
```

## Local workflow notes

- Keep this skill tiny and operational: copy baseline files from `src/` into live storage, preserve live data across reboot or redeploy, and use auditable `rsync` commands.
- Live data wins unless the user explicitly runs an upgrade or `--force-refresh` workflow.
- Safe data seeding uses `rsync -r --no-times --ignore-existing` and must not overwrite or delete existing files.
- Upgrade data seeding uses `rsync -r --no-times --checksum` and must not delete existing files.
- Keep `src/` shaped like the canonical runtime tree with `growers/`, `shared/`, and `scripts/`.

## Local validation

For runtime setup changes, run a temp-root installer smoke command with `--non-interactive --force-refresh --no-install-deps` when dependencies are unavailable, then run `scripts/run_farm_pipeline.py --structure-test` from `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src`. Otherwise run `./scripts/validate.sh` from the repository root after structural changes.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or root files. Do not duplicate root-wide asset, vendor, or validation policy here except this pointer to `../../AGENTS.md`.
