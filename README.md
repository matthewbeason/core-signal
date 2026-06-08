# Core Signal

Core Signal is a local-first interpretation layer for Prime Observer telemetry.

Prime Observer observes network behavior. Core Signal interprets the latest Prime
Observer export and writes a concise Markdown morning briefing about what
mattered in the most recent 24-hour observation window.

Current status: active local-first reporting tool for Prime Observer exports.

Portfolio context: Core Signal demonstrates deterministic local data
interpretation, privacy-safe summary reporting, and scheduled macOS automation
without cloud services, external APIs, or AI diagnosis.

## Why It Exists

Prime Observer already captures the measurements: WAN latency, jitter, packet
loss, historical baseline context, and optional DNS/security summaries. Core
Signal exists to turn those observations into local interpretation:

- what happened
- what was unusual
- what deserves attention
- what does not need action unless symptoms were reported
- what recurring rhythms may be emerging across history

It is intentionally not another dashboard. It produces readable Markdown that
can be reviewed locally or archived as daily and weekly reports.

## Relationship To Prime Observer

Prime Observer is an upstream observability system and remains a separate
project. Core Signal treats Prime Observer as a read-only data source.

Core Signal reads generated Prime Observer exports, but it does not modify Prime
Observer files, restructure that repository, or introduce dependencies into it.

Core Signal is intentionally narrow:

- deterministic Python
- standard library only at runtime
- no dashboards, databases, web scraping, external APIs, LLMs, or agent frameworks
- graceful handling when optional DNS context is missing
- observed recurring patterns only; no formal signature library yet

## Inputs

Morning briefings read:

- `/Users/mbeason/prime-observer/viz/latest.csv`
- `/Users/mbeason/prime-observer/viz/nextdns_summary.json` when present
- `/Users/mbeason/prime-observer/viz/network_attribution.json` when present

Weekly pattern reports read:

- `/Users/mbeason/prime-observer/data/bakeoff_YYYYMMDD.csv`
- `/Users/mbeason/prime-observer/viz/nextdns_summary.json` when present

DNS and attribution exports are optional. Missing or invalid optional context
does not prevent reports from being generated.

## Outputs

Briefings are written to:

- `reports/YYYY-MM-DD-morning-brief.md`
- `reports/latest.md`

Pattern reports are written to:

- `reports/patterns/YYYY-MM-DD-pattern-report.md`
- `reports/patterns/latest.md`

The report date is based on the newest telemetry timestamp found in the Prime
Observer export.

## Setup

Clone or place this repository at:

```text
/Users/mbeason/core-signal
```

Core Signal currently expects the Prime Observer repository to be available at:

```text
/Users/mbeason/prime-observer
```

No Python package installation is required. The runtime uses the Python standard
library only.

## Quick Start

From this repository, generate the default morning briefing:

```bash
PYTHONPATH=src python3 -m core_signal.cli
```

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Generate the weekly pattern report from the default Prime Observer history:

```bash
PYTHONPATH=src python3 -m core_signal.cli \
  --pattern-report \
  --history-dir /Users/mbeason/prime-observer/data \
  --history-days 30 \
  --dns-history data/dns_observations.jsonl
```

## Usage

From this repository:

```bash
PYTHONPATH=src python3 -m core_signal.cli
```

With explicit paths:

```bash
PYTHONPATH=src python3 -m core_signal.cli \
  --csv /Users/mbeason/prime-observer/viz/latest.csv \
  --dns /Users/mbeason/prime-observer/viz/nextdns_summary.json \
  --attribution /Users/mbeason/prime-observer/viz/network_attribution.json \
  --reports-dir reports
```

Skip optional DNS or attribution context:

```bash
PYTHONPATH=src python3 -m core_signal.cli --dns ""
PYTHONPATH=src python3 -m core_signal.cli --attribution ""
```

Generate a long-horizon pattern report:

```bash
PYTHONPATH=src python3 -m core_signal.cli \
  --pattern-report \
  --history-dir /Users/mbeason/prime-observer/data \
  --history-days 30
```

With explicit history and pattern output directories:

```bash
PYTHONPATH=src python3 -m core_signal.cli \
  --pattern-report \
  --history-dir /Users/mbeason/prime-observer/data \
  --history-days 30 \
  --patterns-dir reports/patterns
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## macOS LaunchAgent

Core Signal can install a user LaunchAgent that runs the morning briefing daily
at 6:00 AM. The LaunchAgent calls the local Core Signal CLI and writes the
normal dated report plus `reports/latest.md`.

Preview the generated plist:

```bash
./scripts/setup_launchagent.sh print-plist
```

Install and load the LaunchAgent:

```bash
./scripts/setup_launchagent.sh install
```

Check status:

```bash
./scripts/setup_launchagent.sh status
```

Remove the LaunchAgent:

```bash
./scripts/setup_launchagent.sh uninstall
```

The plist is installed at:

```text
~/Library/LaunchAgents/com.mbeason.core-signal.morning-brief.plist
```

Logs are written to:

```text
logs/launchagent.out.log
logs/launchagent.err.log
```

If Python lives somewhere other than `/opt/homebrew/bin/python3`, install with:

```bash
PYTHON_BIN=/path/to/python3 ./scripts/setup_launchagent.sh install
```

The LaunchAgent does not run at install time. It is scheduled for the next daily
6:00 AM run. To test report generation immediately, run the CLI manually:

```bash
PYTHONPATH=src python3 -m core_signal.cli
```

## Weekly Pattern Report LaunchAgent

Core Signal can also install a separate user LaunchAgent that runs the pattern
report weekly on Sunday at 7:00 AM. This is separate from the daily morning
briefing and writes only to `reports/patterns/`.

Preview the generated plist:

```bash
./scripts/setup_pattern_launchagent.sh print-plist
```

Install and load the LaunchAgent:

```bash
./scripts/setup_pattern_launchagent.sh install
```

Check status:

```bash
./scripts/setup_pattern_launchagent.sh status
```

Remove the LaunchAgent:

```bash
./scripts/setup_pattern_launchagent.sh uninstall
```

The plist is installed at:

```text
~/Library/LaunchAgents/com.mbeason.core-signal.pattern-report.plist
```

Logs are written to:

```text
logs/pattern-launchagent.out.log
logs/pattern-launchagent.err.log
```

If Python lives somewhere other than `/opt/homebrew/bin/python3`, install with:

```bash
PYTHON_BIN=/path/to/python3 ./scripts/setup_pattern_launchagent.sh install
```

The pattern LaunchAgent does not run at install time. It is scheduled for the
next Sunday 7:00 AM run. To test pattern generation immediately, run:

```bash
PYTHONPATH=src python3 -m core_signal.cli \
  --pattern-report \
  --history-dir /Users/mbeason/prime-observer/data \
  --history-days 30
```

## Patterns And Signatures

Pattern reports track observed recurring patterns: repeated telemetry behavior
with accumulating evidence and deterministic Low, Medium, or High confidence.
They are intentionally not diagnoses and do not assume root cause, application
behavior, or user impact.

A signature is a future concept: a mature recurring pattern with high
confidence, stable timing and characteristics, enough recurrence, and sufficient
history. This release does not promote patterns to signatures. It only reports
whether an observed pattern is approaching that future status.

Pattern reports use bounded historical Prime Observer telemetry from
`data/bakeoff_YYYYMMDD.csv`. By default, `--history-days 30` analyzes the latest
30 days based on telemetry timestamps, including today's file when present.

## Attribution Export

When `viz/network_attribution.json` is available, Core Signal uses Prime
Observer's exported attribution for morning brief issue-location evidence. It
prefers per-incident attribution for sustained slowdowns, then window-level
attribution, then current attribution. If the export is missing or unusable,
Core Signal falls back to its deterministic local attribution logic.

## Event Metadata

Morning-brief analysis includes an additive `events` list for downstream
consumers. Each Core Signal event is interpretation and navigation metadata:
deterministic `core-signal-...` ID, event kind, status, severity, confidence,
confidence rationale, affected window, summary, why, recommended action,
recommendation trace, issue location, attribution source, supporting facts, and
a compact Prime Observer reference when an evidence window is navigable.

Significant events normalize the following explanation fields:

- `summary`: a brief interpretation of what Core Signal found.
- `why`: why the event is meaningful to the morning-brief decision.
- `supporting_facts`: compact factual inputs or references that support the
  interpretation, without copying raw timelines or evidence blobs.
- `recommended_action`: the user-facing next step, softened or omitted as
  `None.` when evidence does not justify action.
- `confidence`: Low, Medium, High, Unknown, or an existing compatibility value.
- `confidence_reason`: why the confidence value is appropriate for this event.
- `interpretation_source`: always `core_signal` for Core Signal events.
- `related_events`: an additive list reserved for future relationship metadata.

Recommendations are not emitted as orphan claims. An event recommendation is
paired with `recommendation_trace`, which points back to the event ID, the
supporting fact IDs, the event confidence, and the confidence rationale. If Core
Signal has only current-state attribution for a historical event, it keeps the
recommendation softer by lowering the interpreted event confidence rather than
treating the current-state source as event-specific proof.

Supporting facts use compact structured references:

- `kind`: the kind of fact, such as `telemetry_window`,
  `network_attribution`, `telemetry_observation`, or `historical_baseline`.
- `summary`: a short factual summary.
- `source`: the factual input family, such as telemetry observation, DNS
  observation, network attribution observation, or Prime Observer investigation
  reference.
- `reference`: an optional local reference such as a Prime Observer
  investigation URL.
- `observed_at` or `window`: when the fact was observed or the bounded evidence
  window it covers.

Relationship metadata is prepared as `related_events[]` with future entries
shaped as `event_id`, `relationship_type`, `why`, and `confidence`. Wave 2A
emits an empty list unless a relationship is already justified by existing
event construction; it does not add a correlation algorithm.

Prime Observer owns observations, evidence, investigations, timelines, factual
nearby-event discovery, and historical evidence references. Core Signal does
not copy timelines, samples, DNS details, nearby-event lists, or investigation
presentation. It only points to compact local references such as
`viz/investigate.html?start=...&end=...` and includes matching `--start`/`--end`
command arguments so Olivaw or another local consumer can navigate to or
generate the Prime Observer evidence view.

Olivaw owns presentation, synthesis, navigation, and attribution display. Core
Signal does not add Olivaw formatting to event metadata; it emits structured
interpretation fields that downstream presentation layers may choose to render.

Downstream consumers should use Core Signal event IDs for stable interpreted
events, affected windows for matching or grouping, and Prime Observer references
for local evidence navigation. If Prime Observer does not provide an incident
ID, Core Signal still emits a deterministic event ID and an honest time-window
reference rather than inventing an upstream ID.

## DNS Interpretation

Weekly pattern reports include a DNS Interpretation section when the optional
`viz/nextdns_summary.json` export includes safe top-N summary data. Core Signal
appends privacy-safe DNS observations to ignored local history at
`data/dns_observations.jsonl`, deduplicated by source `generated_at` and
`window`, then compares the current DNS summary against that local history.

DNS interpretation prefers total-activity domain/entity concentration from
`top_queried_domain`, `top_resolved_domain`, and privacy-safe `top_entities`
fields, then considers blocked-domain concentration across blocked DNS activity.
Blocked DNS reasons are used only as a fallback when no more specific
domain/entity concentration signal is available. If local DNS history is
insufficient, Core Signal says so and avoids treating one summary as a stable
pattern.

Core Signal respects Prime Observer's privacy boundary. It does not read raw DNS
logs, call the NextDNS API, expose client IPs or device names, or reveal full
profile IDs. If Prime Observer redacts entity names, Core Signal reports the
redacted label and explains that inspection must happen locally.

## Prime Observer Assumptions

Core Signal assumes Prime Observer exports rows shaped like `viz/latest.csv` and
`data/bakeoff_YYYYMMDD.csv`:

- `ts` is ISO-8601 compatible and may include a timezone offset.
- `host` identifies LAN/WAN targets.
- `phase_label` identifies the active WAN path or collection phase.
- `p95_ms`, `jitter_ms`, and `loss_pct` are numeric when present.
- WAN baseline fields may be present for WAN hosts:
  `baseline_p95`, `baseline_delta_pct`, `baseline_sample_count`.

Default target assumptions match Prime Observer v0.4.1:

- LAN gateway: `192.168.1.1`
- WAN targets: `1.1.1.1`, `9.9.9.9`

For morning briefings, Core Signal uses the newest telemetry timestamp in
`viz/latest.csv` as the end of the briefing window, then analyzes the previous
24 hours of rows. For pattern reports, Core Signal reads historical bakeoff
files and bounds the analysis by telemetry timestamps.

## Limitations

Core Signal is deliberately small:

- It only reads Prime Observer telemetry exports.
- It does not call external services.
- It does not perform AI diagnosis or speculative root-cause analysis.
- It does not maintain a database or serve a browser UI.
- Its interpretation policy is aligned to Prime Observer v0.4.1 concepts and
  may need review if Prime Observer changes targets, thresholds, or export
  schema.
- Optional DNS context is included only when `viz/nextdns_summary.json` exists
  and is readable.

## License

MIT License. See [LICENSE](LICENSE).
