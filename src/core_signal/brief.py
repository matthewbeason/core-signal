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
    attribution = analysis.get("report_attribution") or analysis["attribution"]
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
    label = (analysis.get("report_attribution") or analysis["attribution"]).get("label", "")
    if label == "Likely local LAN / Wi-Fi":
        return "Likely local Wi-Fi/router issue"
    if label == "Likely upstream ISP / path":
        return "Likely upstream/ISP issue"
    if label in {"No recent issue detected", "No issue detected"}:
        return "No clear source identified"
    return "No clear source identified"


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


def status_reason(analysis: dict[str, Any]) -> str:
    status = status_label(analysis)
    sustained = len(analysis["sustained_buckets"])
    turbulence = len(analysis["turbulence_buckets"])
    raw_spikes = len(analysis["raw_spikes"])
    location = issue_location(analysis)

    if analysis["wan_health"].get("samples", 0) == 0:
        return "Core Signal could not make a normal judgment because usable internet telemetry was missing."
    if status == "Healthy":
        if raw_spikes:
            return "No meaningful instability was detected. The brief blips were isolated, no issue source was identified, and no action is recommended."
        return "No meaningful instability was detected, no issue source was identified, and no action is recommended."
    if status == "Attention":
        if sustained:
            if issue_location(analysis) == "No clear source identified":
                return "Sustained slowdown was detected, which means user impact was possible. The slowdown was real, but the available evidence does not clearly point to either local Wi-Fi/router or upstream ISP."
            return "Sustained slowdown was detected, which means user impact was possible. Investigation is recommended if symptoms matched the affected time."
        if location == "Likely local Wi-Fi/router issue":
            return "The local Wi-Fi/router signal was strong enough to make user impact possible. Investigation is recommended if people noticed symptoms."
        return "The evidence was strong enough that user impact was possible. Investigation is recommended if symptoms matched the affected time."
    if turbulence:
        return "Brief instability repeated enough to be noteworthy, but it was not actionable because no sustained slowdown was detected."
    if repeated_isolated_instability(analysis):
        return "Brief instability repeated enough to be noteworthy, but it was not actionable because the events did not continue long enough to suggest user impact."
    if notable_baseline_slowdown(analysis):
        return "Performance was noticeably different from historical norms, but it was not actionable because no sustained instability or user-impacting issue was detected."
    return "Something was notable enough to watch, but no sustained instability or user-impacting issue was detected."


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


def source_evidence_note(analysis: dict[str, Any]) -> str | None:
    status = status_label(analysis)
    if status == "Healthy":
        return None
    attribution = analysis.get("report_attribution") or {}
    label = attribution.get("label")
    if label == "Likely local LAN / Wi-Fi":
        return "Issue source evidence: local gateway evidence was persistently elevated."
    if label == "Likely upstream ISP / path":
        return "Issue source evidence: internet-side degradation with local gateway stable."
    if status == "Attention":
        return "Issue source evidence: slowdown confirmed, source not clear from available local vs internet signals."
    return None


def attribution_source_note(analysis: dict[str, Any]) -> str:
    source = (analysis.get("report_attribution") or {}).get("source")
    if source == "prime_observer_incident":
        return "Attribution source: Prime Observer incident attribution"
    if source == "prime_observer_window":
        return "Attribution source: Prime Observer window attribution"
    if source == "prime_observer_current":
        return "Attribution source: Prime Observer current attribution"
    return "Attribution source: Core Signal fallback"


def event_reference_note(analysis: dict[str, Any]) -> str | None:
    events = analysis.get("events") or []
    if not events:
        return None
    reference = (events[0].get("prime_observer_reference") or {}) if isinstance(events[0], dict) else {}
    url = reference.get("url")
    if not url:
        return None
    return f"Prime Observer investigation: {url}"


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


def performance_context_note(analysis: dict[str, Any]) -> str | None:
    note = pattern_note(analysis["pattern"])
    if note == "Performance was better than usual for this time of day." and analysis["sustained_buckets"]:
        return "Outside those periods, performance was better than usual for this time of day."
    return note


def stability_note(analysis: dict[str, Any]) -> str:
    sustained = len(analysis["sustained_buckets"])
    turbulence = len(analysis["turbulence_buckets"])
    if sustained:
        return f"{sustained} sustained slowdown period(s) were found."
    if turbulence:
        return "Brief instability happened, but it was not sustained."
    return "No sustained slowdowns were found."


def attribution_note(analysis: dict[str, Any]) -> str | None:
    if status_label(analysis) == "Healthy":
        return None
    location = issue_location(analysis)
    if location == "Likely upstream/ISP issue":
        return "Evidence points to an upstream/ISP issue."
    if location == "Likely local Wi-Fi/router issue":
        return "Evidence points to a local Wi-Fi/router issue."
    if location == "No clear source identified" and analysis["sustained_buckets"]:
        return "The source was not clear from the available local-vs-internet evidence."
    return None


def dns_note(dns: dict[str, Any]) -> str:
    if not dns.get("available"):
        return "DNS filtering information was not available."
    stale = " The DNS summary may be stale." if dns.get("stale") else ""
    return f"DNS filtering looked normal: {fmt_num(dns.get('block_rate_pct'), 1, '%')} of queries were blocked.{stale}"


def worth_knowing(analysis: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    status = status_label(analysis)
    raw_spikes = len(analysis["raw_spikes"])
    perf = performance_context_note(analysis)
    attribution = attribution_note(analysis)
    stability = stability_note(analysis)

    if status == "Attention":
        notes.append(stability)
        if attribution:
            notes.append(attribution)
        if perf:
            notes.append(perf)
    elif status == "Watch":
        if analysis["turbulence_buckets"]:
            notes.append("Brief instability repeated enough to be worth noting.")
        elif repeated_isolated_instability(analysis):
            notes.append("Brief unstable moments repeated enough to be worth noting.")
        elif notable_baseline_slowdown(analysis):
            notes.append("Performance was slower than usual for this time of day.")
        else:
            notes.append(stability)
        if attribution:
            notes.append(attribution)
        if perf and perf not in notes:
            notes.append(perf)
    else:
        if perf:
            notes.append(perf)
        notes.append(stability)

    if raw_spikes and not analysis["sustained_buckets"] and not repeated_isolated_instability(analysis):
        notes.append("A few brief blips were not operationally significant.")
    elif raw_spikes and not analysis["sustained_buckets"] and status != "Watch":
        notes.append("Brief unstable moments were lower priority; no action is suggested unless users noticed symptoms.")

    notes.append(dns_note(analysis["dns"]))

    if analysis.get("warnings"):
        notes.append("Some optional export fields were missing; the briefing used the available data.")
    if analysis.get("ignored_hosts"):
        notes.append("Some telemetry rows were skipped because they came from unclassified hosts.")

    return notes


def compact_evidence_lines(analysis: dict[str, Any]) -> list[str]:
    counts = analysis["sample_counts"]
    sustained_count = len(analysis["sustained_buckets"])
    turbulence_count = len(analysis["turbulence_buckets"])
    raw_spike_count = len(analysis["raw_spikes"])
    lines = [
        f"- Window: {fmt_ts(analysis.get('window_start'))} to {fmt_ts(analysis.get('window_end'))}",
        f"- Internet samples: {counts['wan_points']}",
        f"- Sustained slowdowns: {sustained_count}",
        f"- Brief instability: {raw_spike_count} isolated crossings across {turbulence_count} turbulence buckets",
    ]
    note = source_evidence_note(analysis)
    if note:
        lines.append(f"- {note}")
    reference = event_reference_note(analysis)
    if reference:
        lines.append(f"- {reference}")
    lines.append(f"- {attribution_source_note(analysis)}")
    lines.append("- Prime Observer policy: v0.5.0-aligned")
    return lines


def verbose_evidence_lines(analysis: dict[str, Any]) -> list[str]:
    wan = analysis["wan_health"]
    counts = analysis["sample_counts"]
    lines = [
        f"- Export rows: {counts['rows']}",
        f"- Median p95 latency: {fmt_num(wan.get('median_p95_ms'), 1, ' ms')}",
        f"- p95-of-p95 latency: {fmt_num(wan.get('p95_of_p95_ms'), 1, ' ms')}",
        f"- Jitter 95th percentile: {fmt_num(wan.get('jitter_95_ms'), 1, ' ms')}",
    ]
    for phase, stats in analysis["wan_by_phase"].items():
        lines.append(
            f"- {phase}: {stats.get('samples', 0)} samples; sustained degradation rate {fmt_num(stats.get('bad_rate_pct'), 1, '%')}"
        )
    return lines


def render_brief(analysis: dict[str, Any], verbose_evidence: bool = False) -> str:
    date = report_date(analysis)

    lines: list[str] = [
        f"# Core Signal Morning Brief - {date}",
        "",
        f"Status: {status_label(analysis)}",
        "",
        main_summary(analysis),
        "",
        "Why This Status:",
        status_reason(analysis),
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
    lines.extend(["", "Technical Evidence:"])
    lines.extend(compact_evidence_lines(analysis))
    if verbose_evidence:
        lines.extend(["", "Verbose Evidence:"])
        lines.extend(verbose_evidence_lines(analysis))

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
