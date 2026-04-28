# Repository Asset Audit

Audit date: 2026-04-28

## Scope

- repository: `my-farm-advisor-skills`
- command basis: `git ls-files`, `git lfs ls-files`, `git check-ignore -v`, `du -sh`, `find . -type f -size +5M -print`, `./scripts/validate.sh`

## Summary

- working tree size: `11M`
- tracked file count: `355`
- aggregate tracked file size: `3,406,919 bytes` (`3.25 MiB`)
- validation result: `PASS` (`38 pass, 0 warn, 0 fail`)

## Forbidden tracked paths audit

Checked tracked paths for:

- `node_modules/`
- `.cache/`
- `data/`
- `.sisyphus/`
- `countries.geojson`
- `states_usa.geojson`

Result: no forbidden paths or geoadmin payload basenames are tracked.

## Large and binary tracked asset audit

Audit thresholds and formats:

- any tracked file larger than `5 MB`
- any tracked `.npy`, `.npz`, or `.raw` file
- LFS status for any tracked binary/large asset

### Findings

No tracked files larger than `5 MB` were found.

No tracked `.npy`, `.npz`, or `.raw` files were found.

No currently tracked files required Git LFS action.

## Geoadmin exclusion verification

- `git check-ignore -v countries.geojson states_usa.geojson` resolves to `.gitignore` lines `14-15`
- `./scripts/validate.sh` passed the tracked-asset checks for both forbidden geoadmin payload names
- repository still retains structured geoadmin metadata under `my-farm-advisor/r2-seed-pipeline/src/shared/geoadmin/` without tracking generated payload dumps

Outcome: geoadmin payload exclusions are working as intended.

## QTL asset policy outcome verification

- asset policy requires generated runtime assets to stay out of Git and binary assets larger than `1 MB` to use LFS if tracked
- current `git ls-files` audit found no tracked `.npy`, `.npz`, `.raw`, or `>5 MB` files
- existing `my-farm-qtl-analysis/ASSET_AUDIT.md` records the prior removal of generated QTL example outputs and states that no such tracked assets remain

Outcome: QTL asset policy remains satisfied; prior generated outputs stayed excluded and no new tracked binary/LFS candidates were introduced.

## Action ledger

| Path | Size | Action | Rationale |
| --- | ---: | --- | --- |
| _None_ | — | kept | No tracked large or tracked binary assets matched the audit criteria. |

## Conclusion

The repository currently meets the asset policy expectations for forbidden tracked paths, geoadmin payload exclusions, and large/binary asset handling. No new LFS migration or exclusion changes are required from this audit.
