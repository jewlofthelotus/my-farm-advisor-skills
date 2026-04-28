# My Farm Advisor

My Farm Advisor is the farm-specific skill umbrella for this repository. It turns the upstream OpenClaw runtime into an evidence-first agricultural system that can rebuild farm data, analyze field conditions, generate operator-ready reports, and route day-to-day questions into the right agronomic workflow.

Use this skill when the request is fundamentally about fields, crops, weather, soil, imagery, reporting, or strategy. It is the top-level router for the farm domain in this repo.

## What This Skill Does

- Routes farm questions into the right operational subtree instead of dumping everything into one giant prompt.
- Connects field operations, data rebuilds, imagery, soil, weather, and strategy work into one coherent system.
- Preserves a field-level source of truth so summaries and recommendations stay traceable.
- Provides both quick guidance docs and deeper playbooks for repeatable farm workflows.
- Anchors the farm-specific skill layer that sits on top of upstream OpenClaw.

## Canonical Farm Data Structure

This skill is built around one deterministic storage model. The hierarchy is:

- `grower` - the customer or operating entity
- `farm` - a named operating unit inside a grower
- `field` - the atomic agronomic unit
- `asset` - the artifacts attached to a farm or field, such as boundaries, soil, satellite, weather, manifests, summaries, reports, and logs

```mermaid
flowchart TD
    accTitle: Farm Data Hierarchy
    accDescr: Canonical farm data hierarchy showing growers containing farms, farms containing fields, and fields or farms containing the operational assets used by the deterministic pipeline.

    grower([🌾 Grower]) --> farm([🚜 Farm])
    farm --> field([📍 Field])

    subgraph farm_assets ["📦 Farm-level assets"]
        direction TB
        farm_boundary[Boundary]
        farm_tables[Tables]
        farm_reports[Reports]
        farm_logs[Logs]
    end

    subgraph field_assets ["🧰 Field-level assets"]
        direction TB
        boundary_asset[Boundary]
        soil_asset[Soil]
        satellite_asset[Satellite]
        weather_asset[Weather]
        manifests_asset[Manifests]
        derived_asset[Derived features and reports]
    end

    farm --> farm_assets
    field --> field_assets

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef support fill:#ecfccb,stroke:#65a30d,stroke-width:2px,color:#365314

    class grower,farm,field primary
    class farm_boundary,farm_tables,farm_reports,farm_logs,boundary_asset,soil_asset,satellite_asset,weather_asset,manifests_asset,derived_asset support
```

The canonical on-disk shape looks like this:

```text
data/my-farm-advisor/
├── growers/
│   └── <grower_slug>/
│       ├── grower.json
│       ├── logs/
│       └── farms/
│           └── <farm_slug>/
│               ├── farm.json
│               ├── boundary/
│               ├── manifests/
│               ├── logs/
│               ├── derived/
│               │   ├── reports/
│               │   ├── summaries/
│               │   ├── dashboards/
│               │   └── tables/
│               └── fields/
│                   └── <field_slug>/
│                       ├── boundary/
│                       ├── soil/
│                       ├── weather/
│                       ├── satellite/
│                       ├── manifests/
│                       ├── derived/
│                       └── logs/
└── shared/
```

Representative metadata files already live in the skill tree:

- grower metadata: [`data/my-farm-advisor/growers/iowa-demo-grower/grower.json`](data/my-farm-advisor/growers/iowa-demo-grower/grower.json)
- farm metadata: [`data/my-farm-advisor/growers/iowa-demo-grower/farms/iowa-demo-farm/farm.json`](data/my-farm-advisor/growers/iowa-demo-grower/farms/iowa-demo-farm/farm.json)

## Deterministic Pipeline Entry Points

The farm skill is not just a loose set of guides. It also ships deterministic pipeline scripts that can rebuild or extend the canonical tree in a repeatable way.

```mermaid
flowchart LR
    accTitle: Deterministic Farm Pipeline Flow
    accDescr: Deterministic farm pipeline showing seed sources and ingest scripts feeding canonical grower, farm, field, and shared outputs before reporting and dashboard generation.

    seed_source([🧱 Seed source files]) --> ingest[📥 Ingest and normalize]
    ingest --> canonical[🌾 Canonical grower, farm, field tree]
    canonical --> reporting[📊 Reports and dashboards]
    canonical --> shared[🗂️ Shared resources]

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef support fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef outcome fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class seed_source,canonical primary
    class ingest,reporting support
    class shared outcome
```

High-value pipeline and bootstrap entrypoints include:

- full farm pipeline runner: [`r2-seed-pipeline/src/scripts/run_farm_pipeline.py`](r2-seed-pipeline/src/scripts/run_farm_pipeline.py)
- runtime bootstrap: [`r2-seed-pipeline/src/scripts/bootstrap_runtime.py`](r2-seed-pipeline/src/scripts/bootstrap_runtime.py)
- county bootstrap: [`r2-seed-pipeline/src/scripts/ingest/bootstrap_farm_from_county.py`](r2-seed-pipeline/src/scripts/ingest/bootstrap_farm_from_county.py)
- field download: [`r2-seed-pipeline/src/scripts/ingest/download_fields.py`](r2-seed-pipeline/src/scripts/ingest/download_fields.py)
- weather download: [`r2-seed-pipeline/src/scripts/ingest/download_weather.py`](r2-seed-pipeline/src/scripts/ingest/download_weather.py)
- soil download: [`r2-seed-pipeline/src/scripts/ingest/download_soil.py`](r2-seed-pipeline/src/scripts/ingest/download_soil.py)
- satellite download: [`r2-seed-pipeline/src/scripts/ingest/download_satellite_imagery.py`](r2-seed-pipeline/src/scripts/ingest/download_satellite_imagery.py)
- reporting bootstrap: [`r2-seed-pipeline/src/scripts/reporting_bootstrap.py`](r2-seed-pipeline/src/scripts/reporting_bootstrap.py)
- farm markdown/html outputs: [`r2-seed-pipeline/src/scripts/reporting/generate_farm_markdown.py`](r2-seed-pipeline/src/scripts/reporting/generate_farm_markdown.py), [`r2-seed-pipeline/src/scripts/reporting/generate_farm_html.py`](r2-seed-pipeline/src/scripts/reporting/generate_farm_html.py)
- field posters: [`r2-seed-pipeline/src/scripts/reporting/generate_field_posters.py`](r2-seed-pipeline/src/scripts/reporting/generate_field_posters.py)

The most important high-level orchestration docs are:

- deterministic rebuild contract: [`data-sources/farm-data-rebuild/PLAYBOOK.md`](data-sources/farm-data-rebuild/PLAYBOOK.md)
- farm reporting pipeline: [`data-sources/farm-intelligence-reporting/PLAYBOOK.md`](data-sources/farm-intelligence-reporting/PLAYBOOK.md)
- runtime seed/bootstrap behavior: [`r2-seed-pipeline/PLAYBOOK.md`](r2-seed-pipeline/PLAYBOOK.md)

## Shared Resources

The farm tree includes a `shared/` layer for reusable datasets that are not specific to one field or one farm.

Examples already included in the skill tree:

- geoadmin layers under [`r2-seed-pipeline/src/shared/geoadmin/`](r2-seed-pipeline/src/shared/geoadmin)
- corn maturity baselines under [`r2-seed-pipeline/src/shared/corn_maturity/`](r2-seed-pipeline/src/shared/corn_maturity)
- soybean maturity baselines under [`r2-seed-pipeline/src/shared/soybean_maturity/`](r2-seed-pipeline/src/shared/soybean_maturity)
- shared manifest examples under [`r2-seed-pipeline/src/shared/manifests/`](r2-seed-pipeline/src/shared/manifests)

These shared resources support deterministic rebuilds and reporting without forcing every grower or farm to duplicate the same baseline datasets.

Geoadmin payloads are handled a little differently from most small reference files: the committed items under `r2-seed-pipeline/src/shared/geoadmin/` are metadata records plus downloader code, while the generated GeoJSON/Parquet payloads are rebuilt at runtime under `data/my-farm-advisor/shared/geoadmin/{l0_countries,l1_states,l2_counties}/`. See [`docs/GEODATA.md`](docs/GEODATA.md) for the metadata locations, source URL conventions, runtime destinations, and downloader commands.

## Storage Modes

The same farm data model can run in multiple storage modes.

```mermaid
flowchart LR
    accTitle: Farm Storage Modes
    accDescr: Storage options for the farm data tree, showing the same canonical structure running in local checkout, mounted volume, or object-storage-backed runtime modes.

    canonical([🌾 Canonical farm tree]) --> local[💻 Local checkout]
    canonical --> volume[📦 Mounted volume]
    canonical --> object_store[☁️ S3 or R2]

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef support fill:#ecfccb,stroke:#65a30d,stroke-width:2px,color:#365314

    class canonical primary
    class local,volume,object_store support
```

The shipped pipeline/runtime docs explicitly support:

- local checkout mode: `data/my-farm-advisor/`
- volume-backed runtime mode: `/data/workspace/data/my-farm-advisor/`
- object-storage-backed sync paths such as S3 or R2, with `rsync --no-times` semantics documented for those mounts in [`r2-seed-pipeline/PLAYBOOK.md`](r2-seed-pipeline/PLAYBOOK.md)

The runtime installer in [`r2-seed-pipeline/README.md`](r2-seed-pipeline/README.md) resolves the writable data root in this order:

1. `R2_SEED_DATA_ROOT`
2. `/data/workspace/data/my-farm-advisor`
3. local checkout-relative `data/my-farm-advisor`

That is why this skill can work in local mode, bind-volume mode, and S3/R2-backed persistent runtime setups without changing the conceptual farm model.

## How It Runs

```mermaid
flowchart TD
    accTitle: Farm Skill Routing Overview
    accDescr: Overview of how the My Farm Advisor umbrella skill routes a farm request into the right operational area and then into guides or playbooks that produce field-ready outputs.

    request([🌾 Farm request]) --> router[🧭 Umbrella skill router]

    subgraph routing_areas ["📚 Farm workflow areas"]
        direction TB
        admin[Admin]
        data_sources[Data sources]
        eda[EDA]
        field_management[Field management]
        imagery[Imagery]
        soil[Soil]
        strategy[Strategy]
        weather[Weather]
    end

    router --> routing_areas
    routing_areas --> guides[[🗂️ Guide or playbook]]
    guides --> outputs([✅ Field actions, reports, maps, and rebuild steps])

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef support fill:#ecfccb,stroke:#65a30d,stroke-width:2px,color:#365314
    classDef outcome fill:#f3e8ff,stroke:#9333ea,stroke-width:2px,color:#581c87

    class request,router primary
    class guides support
    class outputs outcome
```

The umbrella entrypoint is [`SKILL.md`](SKILL.md). From there, the skill routes into one of the subtree indexes, and then into the actual guide or playbook that does the work.

## Core Capability Areas

| Area             | What it covers                                                         | Start here                                               |
| ---------------- | ---------------------------------------------------------------------- | -------------------------------------------------------- |
| Admin            | Geospatial administration and interactive map workflows                | [`admin/INDEX.md`](admin/INDEX.md)                       |
| Data Sources     | Canonical rebuilds, seed pipelines, and farm intelligence reporting    | [`data-sources/INDEX.md`](data-sources/INDEX.md)         |
| EDA              | Exploratory analysis, comparisons, correlations, and time-series views | [`eda/INDEX.md`](eda/INDEX.md)                           |
| Field Management | Boundaries, field sampling, and headlands workflows                    | [`field-management/INDEX.md`](field-management/INDEX.md) |
| Imagery          | Landsat and Sentinel-2 workflows for vegetation and scene analysis     | [`imagery/INDEX.md`](imagery/INDEX.md)                   |
| Soil             | SSURGO, poster-card outputs, and CDL-based soil/crop context           | [`soil/INDEX.md`](soil/INDEX.md)                         |
| Strategy         | Crop strategy and maturity planning workflows                          | [`strategy/INDEX.md`](strategy/INDEX.md)                 |
| Weather          | NASA POWER weather ingestion and downstream weather analysis           | [`weather/INDEX.md`](weather/INDEX.md)                   |

## Typical Workflow

```mermaid
flowchart LR
    accTitle: Farm Workflow Progression
    accDescr: Typical progression from a farm question through area selection, guide selection, execution, and delivery of a field-level output.

    intake([🌱 Start with the question]) --> choose_area{Choose the right farm area}
    choose_area --> open_index[📂 Open subtree index]
    open_index --> select_guide[🗂️ Use guide or playbook]
    select_guide --> run_workflow[⚙️ Run analysis or rebuild]
    run_workflow --> produce_output[📦 Produce field-level output]
    produce_output --> share_result([📣 Share recommendation or artifact])

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef process fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef outcome fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class intake,choose_area primary
    class open_index,select_guide,run_workflow,produce_output process
    class share_result outcome
```

Examples:

- "Rebuild the farm from source systems" -> [`data-sources/farm-data-rebuild/PLAYBOOK.md`](data-sources/farm-data-rebuild/PLAYBOOK.md)
- "Generate field boundaries or map views" -> [`field-management/field-boundaries/GUIDE.md`](field-management/field-boundaries/GUIDE.md)
- "Check weather and maturity planning" -> [`weather/INDEX.md`](weather/INDEX.md) and [`strategy/INDEX.md`](strategy/INDEX.md)
- "Prepare a farm intelligence report" -> [`data-sources/farm-intelligence-reporting/PLAYBOOK.md`](data-sources/farm-intelligence-reporting/PLAYBOOK.md)

## Why It Matters In This Repo

```mermaid
flowchart TD
    accTitle: Farm Skill Value In The Repo
    accDescr: Diagram showing how the upstream OpenClaw runtime connects to the My Farm Advisor skill layer and how that layer produces operational farm outputs, lineage, reporting, and strategy support.

    upstream([⚙️ Upstream OpenClaw runtime]) --> farm_skill[🌾 My Farm Advisor skill layer]

    farm_skill --> field_outputs[🚜 Field operations outputs]
    farm_skill --> lineage[🧬 Traceable data lineage]
    farm_skill --> reporting[📊 Farm reports and dashboards]
    farm_skill --> strategy_outputs[📈 Crop and maturity strategy]

    classDef primary fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef outcome fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class upstream,farm_skill primary
    class field_outputs,lineage,reporting,strategy_outputs outcome
```

This skill is the main farm-specific intelligence layer. The rest of the repository gives you runtime, channels, gateway behavior, and deployment. This skill tells the system how to think and work like a farm advisor.

## Important Entry Points

- Umbrella router: [`SKILL.md`](SKILL.md)
- Top-level navigation: [`INDEX.md`](INDEX.md)
- Farm data rebuild: [`data-sources/farm-data-rebuild/PLAYBOOK.md`](data-sources/farm-data-rebuild/PLAYBOOK.md)
- Farm reporting: [`data-sources/farm-intelligence-reporting/PLAYBOOK.md`](data-sources/farm-intelligence-reporting/PLAYBOOK.md)
- Field boundaries: [`field-management/field-boundaries/GUIDE.md`](field-management/field-boundaries/GUIDE.md)
- SSURGO workflows: [`soil/ssurgo-soil/GUIDE.md`](soil/ssurgo-soil/GUIDE.md)
- Sentinel-2 workflows: [`imagery/sentinel2-imagery/GUIDE.md`](imagery/sentinel2-imagery/GUIDE.md)
- Weather workflows: [`weather/nasa-power-weather/GUIDE.md`](weather/nasa-power-weather/GUIDE.md)

## Data and Runtime Notes

- This skill suite ships large supporting examples and shared data assets.
- The deterministic scripts and data-tree helpers are part of the skill, not just external repo utilities.
- Canonical path helpers live in [`r2-seed-pipeline/src/scripts/lib/paths.py`](r2-seed-pipeline/src/scripts/lib/paths.py).
- Some workflows assume pulled large files or generated artifacts are available locally.
- The nested subtree documents are the real operating surface; this README is the map, not the full manual.

## Quick Start

1. Start with [`SKILL.md`](SKILL.md).
2. Open the matching area in [`INDEX.md`](INDEX.md).
3. Follow the linked `GUIDE.md` or `PLAYBOOK.md`.
4. Keep outputs tied back to fields, source data, and reproducible methods.
