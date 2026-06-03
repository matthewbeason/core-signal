from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import policy


DEFAULT_CSV = Path("/Users/mbeason/prime-observer/viz/latest.csv")
DEFAULT_HISTORY_DIR = Path("/Users/mbeason/prime-observer/data")
DEFAULT_DNS = Path("/Users/mbeason/prime-observer/viz/nextdns_summary.json")
DEFAULT_ATTRIBUTION = Path("/Users/mbeason/prime-observer/viz/network_attribution.json")


@dataclass(frozen=True)
class Observation:
    ts: dt.datetime
    phase: str
    host: str
    p95_ms: float | None
    jitter_ms: float
    loss_pct: float
    baseline_p95: float | None = None
    baseline_delta_pct: float | None = None
    baseline_sample_count: int | None = None


@dataclass(frozen=True)
class IngestResult:
    observations: list[Observation]
    warnings: list[str]
    ignored_hosts: dict[str, int]


@dataclass(frozen=True)
class HistoryIngestResult:
    ingest: IngestResult
    source_files: list[Path]
    files_available: int
    history_dir: Path
    requested_days: int
    window_start: dt.datetime | None
    window_end: dt.datetime | None


class CsvSchemaError(ValueError):
    """Raised when a Prime Observer CSV export is unreadable for Core Signal."""


def parse_timestamp(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def parse_float(value: Any, default: float | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def parse_int(value: Any, default: int | None = None) -> int | None:
    number = parse_float(value, None)
    if number is None:
        return default
    return int(number)


def read_observations(csv_path: Path) -> IngestResult:
    if not csv_path.exists():
        raise FileNotFoundError(f"Prime Observer CSV export not found: {csv_path}")

    observations: list[Observation] = []
    warnings: list[str] = []
    ignored_hosts: dict[str, int] = {}
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])
        missing = sorted(policy.REQUIRED_CSV_HEADERS - headers)
        if missing:
            raise CsvSchemaError(
                f"Prime Observer CSV export is missing required header(s): {', '.join(missing)}"
            )

        missing_optional = sorted(policy.OPTIONAL_CSV_HEADERS - headers)
        if missing_optional:
            warnings.append(
                "CSV export is missing optional header(s): "
                + ", ".join(missing_optional)
            )

        for row in reader:
            ts = parse_timestamp(row.get("ts", ""))
            host = (row.get("host") or "").strip()
            p95 = parse_float(row.get("p95_ms"), None)
            if ts is None or not host or p95 is None:
                continue
            if host != policy.GATEWAY_HOST and host not in policy.WAN_HOSTS:
                ignored_hosts[host] = ignored_hosts.get(host, 0) + 1
                continue
            observations.append(
                Observation(
                    ts=ts,
                    phase=(row.get("phase_label") or "UNKNOWN").strip().upper() or "UNKNOWN",
                    host=host,
                    p95_ms=p95,
                    jitter_ms=parse_float(row.get("jitter_ms"), 0.0) or 0.0,
                    loss_pct=parse_float(row.get("loss_pct"), 0.0) or 0.0,
                    baseline_p95=parse_float(row.get("baseline_p95"), None),
                    baseline_delta_pct=parse_float(row.get("baseline_delta_pct"), None),
                    baseline_sample_count=parse_int(row.get("baseline_sample_count"), None),
                )
            )
    return IngestResult(observations, warnings, ignored_hosts)


def history_files(history_dir: Path) -> list[Path]:
    if not history_dir.exists():
        raise FileNotFoundError(f"Prime Observer history directory not found: {history_dir}")
    return sorted(
        path
        for path in history_dir.glob("bakeoff_*.csv")
        if path.stem.removeprefix("bakeoff_").isdigit()
    )


def merge_ignored_hosts(results: list[IngestResult]) -> dict[str, int]:
    ignored: dict[str, int] = {}
    for result in results:
        for host, count in result.ignored_hosts.items():
            ignored[host] = ignored.get(host, 0) + count
    return ignored


def load_pattern_history(
    history_dir: Path = DEFAULT_HISTORY_DIR,
    history_days: int = 30,
) -> HistoryIngestResult:
    if history_days <= 0:
        raise ValueError("history_days must be greater than zero")

    files = history_files(history_dir)
    if not files:
        raise FileNotFoundError(f"No Prime Observer bakeoff CSV files found in: {history_dir}")

    per_file: list[tuple[Path, IngestResult]] = [(path, read_observations(path)) for path in files]
    all_observations = [obs for _, result in per_file for obs in result.observations]
    if not all_observations:
        return HistoryIngestResult(
            ingest=IngestResult([], [f"No usable telemetry found in {history_dir}."], merge_ignored_hosts([result for _, result in per_file])),
            source_files=[],
            files_available=len(files),
            history_dir=history_dir,
            requested_days=history_days,
            window_start=None,
            window_end=None,
        )

    end = max(obs.ts for obs in all_observations)
    start = end - dt.timedelta(days=history_days)
    filtered: list[Observation] = []
    source_files: list[Path] = []
    warnings: list[str] = []
    kept_results: list[IngestResult] = []

    for path, result in per_file:
        kept = [obs for obs in result.observations if start <= obs.ts <= end]
        if kept:
            source_files.append(path)
            filtered.extend(kept)
            warnings.extend(result.warnings)
            kept_results.append(result)

    actual_start = min((obs.ts for obs in filtered), default=None)
    return HistoryIngestResult(
        ingest=IngestResult(filtered, sorted(set(warnings)), merge_ignored_hosts(kept_results)),
        source_files=source_files,
        files_available=len(files),
        history_dir=history_dir,
        requested_days=history_days,
        window_start=actual_start,
        window_end=end if filtered else None,
    )


def latest_window(
    observations: list[Observation],
    hours: int = 24,
) -> tuple[list[Observation], dt.datetime | None, dt.datetime | None]:
    if not observations:
        return [], None, None
    end = max(o.ts for o in observations)
    start = end - dt.timedelta(hours=hours)
    return [o for o in observations if start <= o.ts <= end], start, end


def read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        with path.open("r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def read_dns_summary(dns_path: Path | None) -> dict[str, Any] | None:
    return read_json_file(dns_path)


def read_network_attribution(attribution_path: Path | None) -> dict[str, Any] | None:
    return read_json_file(attribution_path)


def load_inputs(
    csv_path: Path = DEFAULT_CSV,
    dns_path: Path | None = DEFAULT_DNS,
    attribution_path: Path | None = DEFAULT_ATTRIBUTION,
    window_hours: int = 24,
) -> tuple[IngestResult, dict[str, Any] | None, dict[str, Any] | None, dt.datetime | None, dt.datetime | None]:
    result = read_observations(csv_path)
    window, start, end = latest_window(result.observations, window_hours)
    return (
        IngestResult(window, result.warnings, result.ignored_hosts),
        read_dns_summary(dns_path),
        read_network_attribution(attribution_path),
        start,
        end,
    )
