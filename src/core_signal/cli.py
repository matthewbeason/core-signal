from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .analyze import analyze
from .brief import render_brief, report_date, write_reports
from .ingest import (
    DEFAULT_ATTRIBUTION,
    DEFAULT_CSV,
    DEFAULT_DNS,
    DEFAULT_HISTORY_DIR,
    CsvSchemaError,
    load_pattern_history,
    load_inputs,
    read_dns_summary,
)
from .patterns import (
    analyze_patterns,
    render_pattern_report,
    report_date as pattern_report_date,
    write_pattern_reports,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Core Signal reports.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Prime Observer latest.csv path.")
    parser.add_argument(
        "--dns",
        default=str(DEFAULT_DNS),
        help='Optional NextDNS summary path. Use "" to disable DNS context.',
    )
    parser.add_argument(
        "--attribution",
        default=str(DEFAULT_ATTRIBUTION),
        help='Optional Prime Observer network_attribution.json path. Use "" to disable exported attribution.',
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"), help="Report output directory.")
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=DEFAULT_HISTORY_DIR,
        help="Prime Observer historical bakeoff CSV directory for pattern reports.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=30,
        help="Number of latest telemetry days to analyze for pattern reports.",
    )
    parser.add_argument(
        "--patterns-dir",
        type=Path,
        default=Path("reports/patterns"),
        help="Pattern report output directory.",
    )
    parser.add_argument("--window-hours", type=int, default=24, help="Observation window in hours.")
    parser.add_argument(
        "--pattern-report",
        action="store_true",
        help="Generate a weekly long-horizon pattern report instead of the morning briefing.",
    )
    parser.add_argument(
        "--verbose-evidence",
        action="store_true",
        help="Include deeper technical metrics in the generated briefing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dns_path = Path(args.dns) if args.dns else None
    attribution_path = Path(args.attribution) if args.attribution else None

    if args.pattern_report:
        try:
            history = load_pattern_history(args.history_dir, args.history_days)
        except FileNotFoundError as exc:
            print(f"Error: {exc}")
            return 2
        except ValueError as exc:
            print(f"Error: {exc}")
            return 2
        except (OSError, csv.Error, UnicodeDecodeError, CsvSchemaError) as exc:
            print(f"Error: Could not read Prime Observer history exports: {exc}")
            return 2

        analysis = analyze_patterns(
            history.ingest.observations,
            history={
                "history_dir": history.history_dir,
                "requested_days": history.requested_days,
                "files_available": history.files_available,
                "source_files": history.source_files,
                "window_start": history.window_start,
                "window_end": history.window_end,
                "warnings": history.ingest.warnings,
                "ignored_hosts": history.ingest.ignored_hosts,
            },
            dns=read_dns_summary(dns_path),
        )
        markdown = render_pattern_report(analysis)
        dated, latest = write_pattern_reports(markdown, args.patterns_dir, pattern_report_date(analysis))
        print(f"Wrote {dated}")
        print(f"Wrote {latest}")
        return 0

    try:
        ingest_result, dns, attribution, start, end = load_inputs(
            args.csv,
            dns_path,
            attribution_path,
            args.window_hours,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 2
    except (OSError, csv.Error, UnicodeDecodeError, CsvSchemaError) as exc:
        print(f"Error: Could not read Prime Observer CSV export: {exc}")
        return 2

    analysis = analyze(
        ingest_result.observations,
        dns,
        attribution,
        start,
        end,
        warnings=ingest_result.warnings,
        ignored_hosts=ingest_result.ignored_hosts,
    )
    markdown = render_brief(analysis, verbose_evidence=args.verbose_evidence)
    dated, latest = write_reports(markdown, args.reports_dir, report_date(analysis))
    print(f"Wrote {dated}")
    print(f"Wrote {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
