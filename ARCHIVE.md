# Core Signal Archive Notes

Core Signal has been archived as a standalone repository.

## Why It Was Archived

- Core Signal's durable value is its deterministic interpretation and reporting
  layer, not its continued existence as a separate project.
- The long-term architectural direction is to bring those capabilities into
  Prime Observer so evidence generation and interpretation can live under a
  single owner.
- This repository is being preserved as a readable historical artifact so the
  original contracts, logic, tests, and operational docs remain available for
  reference during future extraction or migration work.

## Capabilities To Preserve For Prime Observer

- Deterministic morning-brief interpretation from Prime Observer telemetry
- Structured event metadata, including supporting facts, confidence reasons,
  evidence strength, uncertainty notes, and recommendation traces
- Exported attribution normalization and fallback attribution logic
- Weekly recurring-pattern analysis and confidence scoring
- Privacy-safe DNS concentration and local-history interpretation
- Read-only ingest contracts for Prime Observer CSV and optional JSON exports

## Archival Only

- This standalone repository structure
- Local report output paths under `reports/`
- Standalone CLI entrypoint and repository-local invocation workflow
- Repository-specific LaunchAgent setup scripts for scheduled runs
- Release/version surfaces that existed only to ship Core Signal separately

## Archive Guidance

- Keep source, tests, and historical documentation intact.
- Do not treat this repository as the active destination for new feature work.
- Use it as a reference when merging bounded interpretation/reporting behavior
  into Prime Observer.
