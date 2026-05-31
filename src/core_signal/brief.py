from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any


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


def headline(analysis: dict[str, Any]) -> str:
    wan = analysis["wan_health"]
    attribution = analysis["attribution"]["label"]
    sustained = len(analysis["sustained_buckets"])
    turbulence = len(analysis["turbulence_buckets"])
    if wan.get("samples", 0) == 0:
        return "No WAN telemetry was available in the latest export."
    if sustained:
        return f"WAN had sustained degradation in {sustained} 15-minute bucket(s), while the most recent attribution state is {attribution.lower()}."
    if turbulence:
        return f"WAN stayed mostly usable but showed turbulence in {turbulence} 15-minute bucket(s)."
    return "WAN looked stable across the latest observation window."


def render_brief(analysis: dict[str, Any]) -> str:
    date = report_date(analysis)
    wan = analysis["wan_health"]
    pattern = analysis["pattern"]
    attribution = analysis["attribution"]
    dns = analysis["dns"]
    counts = analysis["sample_counts"]
    sustained_count = len(analysis["sustained_buckets"])
    turbulence_count = len(analysis["turbulence_buckets"])
    raw_spike_count = len(analysis["raw_spikes"])

    lines: list[str] = [
        f"# Core Signal Morning Brief - {date}",
        "",
        f"Observation window: {fmt_ts(analysis.get('window_start'))} to {fmt_ts(analysis.get('window_end'))}",
        "",
        f"## Summary",
        "",
        headline(analysis),
        "",
        f"- WAN samples analyzed: {counts['wan_points']} from {counts['rows']} export rows.",
        f"- WAN p95 median: {fmt_num(wan.get('median_p95_ms'), 1, ' ms')}; p95-of-p95: {fmt_num(wan.get('p95_of_p95_ms'), 1, ' ms')}; jitter 95th: {fmt_num(wan.get('jitter_95_ms'), 1, ' ms')}.",
        f"- Sustained bad rate: {fmt_num(wan.get('bad_rate_pct'), 1, '%')}; bad minutes/hour: {fmt_num(wan.get('bad_minutes_per_hour'), 1)}.",
        "",
        "## What Happened?",
        "",
        f"The latest 24-hour export was interpreted using Prime Observer's WAN thresholds: p95 latency above 140 ms, jitter above 50 ms, or packet loss above 1%. A sustained bad moment requires two consecutive raw bad WAN samples.",
        "",
    ]

    if sustained_count:
        lines.append(f"Core Signal found {sustained_count} sustained degradation bucket(s). These are the intervals most likely to have been noticeable.")
        intervals = ", ".join(
            f"{fmt_ts(bucket['start'])}-{bucket['end'].strftime('%H:%M')}"
            for bucket in analysis["sustained_buckets"][:3]
        )
        if intervals:
            lines.append(f"First sustained interval(s): {intervals}.")
    elif turbulence_count:
        lines.append(f"No sustained degradation was found, but {turbulence_count} turbulence bucket(s) contained repeated isolated raw bad samples.")
    else:
        lines.append("No sustained degradation buckets were found. WAN behavior was mostly calm by the configured thresholds.")

    lines.extend(
        [
            "",
            "## What Was Unusual?",
            "",
        ]
    )

    if pattern.get("latest") is None:
        lines.append("Pattern context is still learning because no usable WAN baseline fields were present.")
    else:
        lines.append(
            f"Latest baseline comparison: {pattern['label']} ({fmt_num(pattern.get('delta_pct'), 1, '%')} vs baseline, {pattern['confidence'].lower()} confidence from {pattern.get('sample_count')} samples)."
        )

    if raw_spike_count and not sustained_count:
        lines.append(f"There were {raw_spike_count} isolated raw bad WAN sample(s), but they did not persist long enough to count as sustained degradation.")
    elif raw_spike_count:
        lines.append(f"There were also {raw_spike_count} isolated raw bad sample(s) outside sustained streaks.")
    else:
        lines.append("No isolated raw WAN spikes stood out.")

    lines.extend(
        [
            "",
            "## What Deserves Attention?",
            "",
            f"Recent attribution: {attribution['label']} ({attribution['confidence']} confidence). {attribution['why']}",
        ]
    )

    if sustained_count:
        lines.append("Review the sustained degradation buckets first, especially if they overlap with user-impacting work, calls, streaming, or gaming.")
    elif turbulence_count:
        lines.append("Watch turbulence if it repeats on future mornings; by itself it is informational rather than urgent.")
    else:
        lines.append("No immediate network action is suggested by this export.")

    lines.extend(["", "## DNS / Security Context", ""])
    if dns.get("available"):
        stale = " stale" if dns.get("stale") else ""
        lines.append(
            f"NextDNS context is available{stale}: {fmt_num(dns.get('block_rate_pct'), 1, '%')} blocked across {dns.get('total_queries', 'n/a')} queries; encrypted DNS rate {fmt_num(dns.get('encrypted_rate_pct'), 1, '%')}."
        )
        reasons = dns.get("top_reasons") or []
        if reasons:
            reason_text = ", ".join(f"{r.get('name', 'unknown')} ({r.get('queries', 'n/a')})" for r in reasons)
            lines.append(f"Top block reasons: {reason_text}.")
    else:
        lines.append("DNS/security context was unavailable, so this briefing is based on telemetry only.")

    lines.extend(["", "## What Can Be Safely Ignored?", ""])
    if not sustained_count:
        lines.append("Isolated raw spikes can be ignored for now because they did not form a sustained bad streak.")
    else:
        lines.append("Calm buckets and isolated raw spikes outside sustained intervals can be ignored unless users reported symptoms at those exact times.")
    if pattern.get("label") == "Better than usual for this time of day":
        lines.append("The latest baseline comparison was better than usual, so elevated-baseline concern can be ignored for this run.")
    elif pattern.get("label") == "Normal for this time of day":
        lines.append("The latest baseline comparison was normal for this time of day.")

    lines.extend(["", "## Evidence", ""])
    for phase, stats in analysis["wan_by_phase"].items():
        lines.append(
            f"- {phase}: {stats.get('samples', 0)} samples; median p95 {fmt_num(stats.get('median_p95_ms'), 1, ' ms')}; p95-of-p95 {fmt_num(stats.get('p95_of_p95_ms'), 1, ' ms')}; sustained bad {fmt_num(stats.get('bad_rate_pct'), 1, '%')}."
        )
    lines.append(f"- Turbulence buckets: {turbulence_count}; sustained degradation buckets: {sustained_count}.")
    lines.append(f"- Runtime is deterministic and uses only local Prime Observer exports.")
    lines.append("")
    return "\n".join(lines)


def write_reports(markdown: str, reports_dir: Path, date: str) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    dated = reports_dir / f"{date}-morning-brief.md"
    latest = reports_dir / "latest.md"
    dated.write_text(markdown)
    latest.write_text(markdown)
    return dated, latest
