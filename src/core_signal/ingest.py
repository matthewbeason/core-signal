from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import policy


DEFAULT_CSV = Path("/Users/mbeason/prime-observer/viz/latest.csv")
DEFAULT_DNS = Path("/Users/mbeason/prime-observer/viz/nextdns_summary.json")


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


def latest_window(
    observations: list[Observation],
    hours: int = 24,
) -> tuple[list[Observation], dt.datetime | None, dt.datetime | None]:
    if not observations:
        return [], None, None
    end = max(o.ts for o in observations)
    start = end - dt.timedelta(hours=hours)
    return [o for o in observations if start <= o.ts <= end], start, end


def read_dns_summary(dns_path: Path | None) -> dict[str, Any] | None:
    if dns_path is None or not dns_path.exists():
        return None
    try:
        with dns_path.open("r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_inputs(
    csv_path: Path = DEFAULT_CSV,
    dns_path: Path | None = DEFAULT_DNS,
    window_hours: int = 24,
) -> tuple[IngestResult, dict[str, Any] | None, dt.datetime | None, dt.datetime | None]:
    result = read_observations(csv_path)
    window, start, end = latest_window(result.observations, window_hours)
    return IngestResult(window, result.warnings, result.ignored_hosts), read_dns_summary(dns_path), start, end
