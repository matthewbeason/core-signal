# Core Signal

Core Signal is a local-first interpretation layer for Prime Observer telemetry.

Prime Observer observes network behavior. Core Signal interprets the latest Prime
Observer export and writes a concise Markdown morning briefing about what
mattered in the most recent 24-hour observation window.

Core Signal v0.1 is intentionally narrow:

- deterministic Python
- standard library only at runtime
- no dashboards, databases, web scraping, external APIs, LLMs, or agent frameworks
- graceful handling when optional DNS context is missing

## Inputs

By default Core Signal reads:

- `/Users/mbeason/prime-observer/viz/latest.csv`
- `/Users/mbeason/prime-observer/viz/nextdns_summary.json` when present

The DNS summary is optional. Missing or invalid DNS context does not prevent a
briefing from being generated.

## Outputs

Briefings are written to:

- `reports/YYYY-MM-DD-morning-brief.md`
- `reports/latest.md`

The report date is based on the newest telemetry timestamp found in the Prime
Observer export.

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
  --reports-dir reports
```

Skip DNS context:

```bash
PYTHONPATH=src python3 -m core_signal.cli --dns ""
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Prime Observer Assumptions

Core Signal v0.1 assumes Prime Observer exports rows shaped like
`viz/latest.csv`:

- `ts` is ISO-8601 compatible and may include a timezone offset.
- `host` identifies LAN/WAN targets.
- `phase_label` identifies the active WAN path or collection phase.
- `p95_ms`, `jitter_ms`, and `loss_pct` are numeric when present.
- WAN baseline fields may be present for WAN hosts:
  `baseline_p95`, `baseline_delta_pct`, `baseline_sample_count`.

Default target assumptions match Prime Observer v0.4.1:

- LAN gateway: `192.168.1.1`
- WAN targets: `1.1.1.1`, `9.9.9.9`

Core Signal uses the newest telemetry timestamp in the CSV as the end of the
briefing window, then analyzes the previous 24 hours of rows.
