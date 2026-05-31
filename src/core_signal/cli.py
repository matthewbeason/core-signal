from __future__ import annotations

import argparse
from pathlib import Path

from .analyze import analyze
from .brief import render_brief, report_date, write_reports
from .ingest import DEFAULT_CSV, DEFAULT_DNS, load_inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a Core Signal morning briefing.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Prime Observer latest.csv path.")
    parser.add_argument(
        "--dns",
        default=str(DEFAULT_DNS),
        help='Optional NextDNS summary path. Use "" to disable DNS context.',
    )
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"), help="Report output directory.")
    parser.add_argument("--window-hours", type=int, default=24, help="Observation window in hours.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dns_path = Path(args.dns) if args.dns else None
    observations, dns, start, end = load_inputs(args.csv, dns_path, args.window_hours)
    analysis = analyze(observations, dns, start, end)
    markdown = render_brief(analysis)
    dated, latest = write_reports(markdown, args.reports_dir, report_date(analysis))
    print(f"Wrote {dated}")
    print(f"Wrote {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

