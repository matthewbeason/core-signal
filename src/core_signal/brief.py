from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import policy


def fmt_ts(value: dt.datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M %Z").strip()


def fmt_num(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return f"{value}{suffix}"


def report_date(analysis: dict[str, Any]) -> str:
    end = analysis.get("window_end")
    if isinstance(end, dt.datetime):
        return end.strftime("%Y-%m-%d")
    return dt.date.today().isoformat()


def notable_baseline_slowdown(analysis: dict[str, Any]) -> bool:
    pattern = analysis["pattern"]
    return (
        pattern.get("label") == "Highly elevated for this time of day"
        and pattern.get("confidence") in {"Medium", "High"}
    )


def repeated_isolated_instability(analysis: dict[str, Any]) -> bool:
    return len(analysis["raw_spikes"]) >= policy.TURBULENCE_MIN_RAW_BAD


def status_label(analysis: dict[str, Any]) -> str:
    attribution = analysis["attribution"]
    if analysis["wan_health"].get("samples", 0) == 0:
        return "Watch"
    if analysis["sustained_buckets"]:
        return "Attention"
    if attribution.get("label") == "Likely local LAN / Wi-Fi" and attribution.get("confidence") == "High":
        return "Attention"
    if attribution.get("label") in {"Likely local LAN / Wi-Fi", "Likely upstream ISP / path"}:
        return "Watch"
    if analysis["turbulence_buckets"]:
        return "Watch"
    if repeated_isolated_instability(analysis):
        return "Watch"
    if notable_baseline_slowdown(analysis):
        return "Watch"
    return "Healthy"


def issue_location(analysis: dict[str, Any]) -> str:
    if status_label(analysis) == "Healthy":
        return ""
    label = analysis["attribution"].get("label", "")
    if label == "Likely local LAN / Wi-Fi":
        return "Likely local Wi-Fi/router issue"
    if label == "Likely upstream ISP / path":
        return "Likely upstream/ISP issue"
    if label == "No recent issue detected":
        return "Unclear source"
    return "Unclear source"


def main_summary(analysis: dict[str, Any]) -> str:
    status = status_label(analysis)
    sustained = len(analysis["sustained_buckets"])
    turbulence = len(analysis["turbulence_buckets"])
    raw_spikes = len(analysis["raw_spikes"])

    if analysis["wan_health"].get("samples", 0) == 0:
        return "Core Signal did not find usable internet telemetry in the latest export."
    if status == "Attention":
        return f"The network had {sustained} sustained slowdown period(s). User impact was possible."
    if status == "Watch":
        if turbulence:
            return "Brief instability repeated enough to be worth noting, but no immediate action is required."
        if repeated_isolated_instability(analysis):
            return f"There were {raw_spikes} brief unstable moments. They did not become sustained slowdowns."
        if notable_baseline_slowdown(analysis):
            return "Performance was unusually slow compared with the normal pattern for that time of day."
        return "Something notable appeared in the latest telemetry, but no immediate action is required."
    return "Everything looked normal yesterday."


def recommended_action(analysis: dict[str, Any]) -> str:
    status = status_label(analysis)
    location = issue_location(analysis)
    if status == "Healthy":
        return "None."
    if status == "Watch":
        return "No action unless people noticed slow calls, buffering, gaming lag, or dropped connections."
    if location == "Likely local Wi-Fi/router issue":
        return "Check the router or Wi-Fi if people noticed symptoms during the affected time."
    if location == "Likely upstream/ISP issue":
        return "No home-network change is suggested. Check provider status or contact the ISP only if symptoms matched the affected time."
    return "Check whether symptoms matched the affected time. If this repeats, compare it with what people were doing."


def pattern_note(pattern: dict[str, Any]) -> str | None:
    label = pattern.get("label")
    if pattern.get("latest") is None:
        return None
    if label == "Better than usual for this time of day":
        return "Performance was better than usual for this time of day."
    if label == "Normal for this time of day":
        return "Performance was normal for this time of day."
    if label in {"Slightly elevated for this time of day", "Highly elevated for this time of day"}:
        return "Performance was slower than usual for this time of day."
    return None


def dns_note(dns: dict[str, Any]) -> str:
    if not dns.get("available"):
        return "DNS filtering information was not available."
    stale = " The DNS summary may be stale." if dns.get("stale") else ""
    return f"DNS filtering looked normal: {fmt_num(dns.get('block_rate_pct'), 1, '%')} of queries were blocked.{stale}"


def worth_knowing(analysis: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    sustained = len(analysis["sustained_buckets"])
    turbulence = len(analysis["turbulence_buckets"])
    raw_spikes = len(analysis["raw_spikes"])

    note = pattern_note(analysis["pattern"])
    if note:
        notes.append(note)

    if sustained:
        notes.append(f"{sustained} sustained slowdown period(s) were found.")
    elif turbulence:
        notes.append("Brief instability happened, but it was not sustained.")
    else:
        notes.append("No sustained slowdowns were found.")

    if raw_spikes and not sustained and not repeated_isolated_instability(analysis):
        notes.append("A few brief blips were not operationally significant.")
    elif raw_spikes and not sustained:
        notes.append("Brief unstable moments were lower priority; no action is suggested unless users noticed symptoms.")

    notes.append(dns_note(analysis["dns"]))

    if analysis.get("warnings"):
        notes.append("Some optional export fields were missing; the briefing used the available data.")
    if analysis.get("ignored_hosts"):
        notes.append("Some telemetry rows were skipped because they came from unclassified hosts.")

    return notes


def render_brief(analysis: dict[str, Any]) -> str:
    date = report_date(analysis)
    wan = analysis["wan_health"]
    counts = analysis["sample_counts"]
    sustained_count = len(analysis["sustained_buckets"])
    turbulence_count = len(analysis["turbulence_buckets"])
    raw_spike_count = len(analysis["raw_spikes"])

    lines: list[str] = [
        f"# Core Signal Morning Brief - {date}",
        "",
        f"Status: {status_label(analysis)}",
        "",
        main_summary(analysis),
        "",
    ]

    location = issue_location(analysis)
    if location:
        lines.extend([f"Issue Location: {location}", ""])

    lines.extend(
        [
            f"Recommended Action: {recommended_action(analysis)}",
            "",
            "Worth knowing:",
        ]
    )
    lines.extend(f"- {note}" for note in worth_knowing(analysis))
    lines.extend(
        [
            "",
            "Technical Evidence:",
            f"- Observation window: {fmt_ts(analysis.get('window_start'))} to {fmt_ts(analysis.get('window_end'))}",
            f"- Samples analyzed: {counts['wan_points']} internet samples from {counts['rows']} export rows",
            f"- Sustained degradation: {sustained_count}",
            f"- Turbulence buckets: {turbulence_count}",
            f"- Isolated threshold crossings: {raw_spike_count}",
            f"- Median p95 latency: {fmt_num(wan.get('median_p95_ms'), 1, ' ms')}",
            f"- p95-of-p95 latency: {fmt_num(wan.get('p95_of_p95_ms'), 1, ' ms')}",
            f"- Jitter 95th percentile: {fmt_num(wan.get('jitter_95_ms'), 1, ' ms')}",
            f"- Prime Observer policy: v0.4.1-aligned thresholds ({policy.WAN_BAD_P95_MS:.0f} ms p95, {policy.WAN_BAD_JITTER_MS:.0f} ms jitter, {policy.WAN_BAD_LOSS_PCT:.0f}% packet loss, {policy.WAN_BAD_PERSISTENCE} consecutive samples)",
        ]
    )

    for phase, stats in analysis["wan_by_phase"].items():
        lines.append(
            f"- {phase}: {stats.get('samples', 0)} samples; sustained degradation rate {fmt_num(stats.get('bad_rate_pct'), 1, '%')}"
        )

    if analysis.get("warnings") or analysis.get("ignored_hosts"):
        lines.append("- Data notes:")
        for warning in analysis.get("warnings") or []:
            lines.append(f"  - {warning}")
        for host, count in sorted((analysis.get("ignored_hosts") or {}).items()):
            lines.append(f"  - Ignored unclassified host telemetry: {host} ({count} rows)")

    lines.append("")
    return "\n".join(lines)


def write_reports(markdown: str, reports_dir: Path, date: str) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{date}-morning-brief.md"
    latest = reports_dir / "latest.md"
    dated.write_text(markdown)
    latest.write_text(markdown)
    return dated, latest
