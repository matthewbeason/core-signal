from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import median
from typing import Any

from .ingest import Observation, parse_timestamp
from . import policy


@dataclass(frozen=True)
class SeriesPoint:
    ts: dt.datetime
    phase: str
    host: str
    p95_ms: float
    jitter_ms: float
    loss_pct: float
    baseline_p95: float | None
    baseline_delta_pct: float | None
    baseline_sample_count: int | None
    raw_bad: bool = False
    sustained_bad: bool = False


def quantile(values: list[float], q: float) -> float | None:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    idx = (len(clean) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(clean) - 1)
    if lo == hi:
        return clean[lo]
    frac = idx - lo
    return clean[lo] * (1 - frac) + clean[hi] * frac


def is_raw_wan_bad(point: SeriesPoint) -> bool:
    return (
        point.p95_ms > policy.WAN_BAD_P95_MS
        or point.jitter_ms > policy.WAN_BAD_JITTER_MS
        or point.loss_pct > policy.WAN_BAD_LOSS_PCT
    )


def collapse_series(observations: list[Observation]) -> tuple[list[SeriesPoint], list[SeriesPoint]]:
    lan_by_key: dict[tuple[str, dt.datetime], SeriesPoint] = {}
    wan_by_key: dict[tuple[str, dt.datetime], SeriesPoint] = {}

    for obs in observations:
        if obs.p95_ms is None:
            continue
        point = SeriesPoint(
            ts=obs.ts,
            phase=obs.phase,
            host=obs.host,
            p95_ms=obs.p95_ms,
            jitter_ms=obs.jitter_ms,
            loss_pct=obs.loss_pct,
            baseline_p95=obs.baseline_p95,
            baseline_delta_pct=obs.baseline_delta_pct,
            baseline_sample_count=obs.baseline_sample_count,
        )
        key = (obs.phase, obs.ts)
        if obs.host == policy.GATEWAY_HOST:
            prev = lan_by_key.get(key)
            if prev is None or point.p95_ms > prev.p95_ms:
                lan_by_key[key] = point
        elif obs.host in policy.WAN_HOSTS:
            prev = wan_by_key.get(key)
            if prev is None or point.p95_ms > prev.p95_ms:
                wan_by_key[key] = point

    lan = sorted(lan_by_key.values(), key=lambda p: p.ts)
    wan = sorted(wan_by_key.values(), key=lambda p: p.ts)
    return lan, mark_sustained_bad(wan)


def mark_sustained_bad(wan: list[SeriesPoint]) -> list[SeriesPoint]:
    marked: list[SeriesPoint] = []
    streak = 0
    for point in wan:
        raw_bad = is_raw_wan_bad(point)
        streak = streak + 1 if raw_bad else 0
        marked.append(
            SeriesPoint(
                **{
                    **point.__dict__,
                    "raw_bad": raw_bad,
                    "sustained_bad": streak >= policy.WAN_BAD_PERSISTENCE,
                }
            )
        )
    return marked


def bucket_start(ts: dt.datetime, minutes: int = policy.TURBULENCE_BUCKET_MINUTES) -> dt.datetime:
    epoch = ts.timestamp()
    bucket_seconds = minutes * 60
    return dt.datetime.fromtimestamp(
        int(epoch // bucket_seconds) * bucket_seconds,
        tz=ts.tzinfo,
    )


def classify_buckets(wan: list[SeriesPoint]) -> list[dict[str, Any]]:
    buckets: dict[dt.datetime, list[SeriesPoint]] = {}
    for point in wan:
        buckets.setdefault(bucket_start(point.ts), []).append(point)

    results: list[dict[str, Any]] = []
    for start, rows in sorted(buckets.items()):
        rows = sorted(rows, key=lambda p: p.ts)
        raw_bad = sum(1 for p in rows if p.raw_bad)
        sustained = sum(1 for p in rows if p.sustained_bad)
        raw_run = 0
        max_raw_run = 0
        for row in rows:
            raw_run = raw_run + 1 if row.raw_bad else 0
            max_raw_run = max(max_raw_run, raw_run)
        results.append(
            {
                "start": start,
                "end": start + dt.timedelta(minutes=policy.TURBULENCE_BUCKET_MINUTES),
                "total": len(rows),
                "raw_bad": raw_bad,
                "sustained_bad": sustained,
                "max_raw_run": max_raw_run,
                "is_sustained": sustained > 0,
                "is_turbulence": sustained == 0
                and raw_bad >= policy.TURBULENCE_MIN_RAW_BAD
                and max_raw_run < policy.WAN_BAD_PERSISTENCE,
            }
        )
    return results


def stats_for(points: list[SeriesPoint]) -> dict[str, Any]:
    if not points:
        return {"samples": 0}
    p95s = [p.p95_ms for p in points]
    jitters = [p.jitter_ms for p in points]
    bad = sum(1 for p in points if p.sustained_bad)
    minute_buckets = {bucket_start(p.ts, 1): p for p in points}
    bad_minutes = sum(1 for p in minute_buckets.values() if p.sustained_bad)
    return {
        "samples": len(points),
        "median_p95_ms": median(p95s),
        "p95_of_p95_ms": quantile(p95s, 0.95),
        "jitter_95_ms": quantile(jitters, 0.95),
        "sustained_bad_samples": bad,
        "bad_rate_pct": (bad / len(points)) * 100.0,
        "bad_minutes_per_hour": 60.0 * (bad_minutes / max(len(minute_buckets), 1)),
        "raw_bad_samples": sum(1 for p in points if p.raw_bad),
    }


def pattern_context(wan: list[SeriesPoint]) -> dict[str, Any]:
    latest = next(
        (
            p
            for p in reversed(wan)
            if p.baseline_delta_pct is not None and p.baseline_sample_count is not None
        ),
        None,
    )
    if latest is None:
        return {"label": "Learning", "confidence": "None", "latest": None}

    count = latest.baseline_sample_count or 0
    if count < 4:
        confidence = "Low"
    elif count >= 40:
        confidence = "High"
    elif count >= 16:
        confidence = "Medium"
    else:
        confidence = "Low"

    delta = latest.baseline_delta_pct or 0.0
    if abs(delta) < 10:
        label = "Normal for this time of day"
    elif delta > 25:
        label = "Highly elevated for this time of day"
    elif delta > 10:
        label = "Slightly elevated for this time of day"
    else:
        label = "Better than usual for this time of day"

    return {
        "label": label,
        "confidence": confidence,
        "delta_pct": delta,
        "sample_count": count,
        "latest": latest,
    }


def attribution(wan: list[SeriesPoint], lan: list[SeriesPoint], end: dt.datetime | None) -> dict[str, str]:
    if end is None:
        return {"label": "No recent data", "confidence": "Low", "why": "No telemetry was available."}

    cut = end - dt.timedelta(minutes=policy.ATTRIBUTION_MINUTES)
    recent_wan = [p for p in wan if p.ts >= cut]
    recent_lan = [p for p in lan if p.ts >= cut]
    recent_buckets = [b for b in classify_buckets(wan) if b["end"] >= cut]

    wan_bad = any(p.sustained_bad for p in recent_wan)
    wan_turbulence = any(b["is_turbulence"] for b in recent_buckets)
    lan_elevated = [p for p in recent_lan if p.p95_ms > policy.LAN_ELEVATED_P95_MS]
    lan_rate = len(lan_elevated) / len(recent_lan) if recent_lan else 0.0
    lan_bad = len(lan_elevated) >= 3 and lan_rate > 0.2

    if not recent_wan and not recent_lan:
        return {
            "label": "No recent data",
            "confidence": "Low",
            "why": "No LAN or WAN samples were present in the last 15 minutes of the export.",
        }
    if lan_bad and wan_bad:
        return {
            "label": "Likely local LAN / Wi-Fi",
            "confidence": "Medium",
            "why": f"LAN was elevated in {len(lan_elevated)}/{len(recent_lan)} recent samples while WAN also degraded.",
        }
    if lan_bad and not wan_bad and not wan_turbulence:
        return {
            "label": "Likely local LAN / Wi-Fi",
            "confidence": "High",
            "why": f"LAN was elevated in {len(lan_elevated)}/{len(recent_lan)} recent samples while WAN stayed stable.",
        }
    if not lan_bad and (wan_bad or wan_turbulence):
        return {
            "label": "Likely upstream ISP / path",
            "confidence": "High" if wan_bad else "Medium",
            "why": f"WAN showed {'sustained degradation' if wan_bad else 'turbulence'} while LAN stayed below the local threshold.",
        }
    return {
        "label": "No recent issue detected",
        "confidence": "Recent window",
        "why": "LAN and WAN both looked stable in the last 15 minutes of the export.",
    }


def report_attribution(
    wan: list[SeriesPoint],
    lan: list[SeriesPoint],
    buckets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attribute the event-level evidence that a morning brief summarizes."""
    wan_degraded = any(p.sustained_bad for p in wan) or any(b["is_turbulence"] for b in buckets)
    lan_elevated = [p for p in lan if p.p95_ms > policy.LAN_ELEVATED_P95_MS]
    lan_elevated_rate = len(lan_elevated) / len(lan) if lan else 0.0
    lan_degraded = len(lan_elevated) >= 3 and lan_elevated_rate > 0.2

    if not wan and not lan:
        return {
            "label": "No clear source identified",
            "confidence": "Low",
            "why": "No usable local or internet telemetry was available.",
            "lan_elevated": 0,
            "lan_samples": 0,
        }
    if lan_degraded:
        return {
            "label": "Likely local LAN / Wi-Fi",
            "confidence": "Medium" if wan_degraded else "High",
            "why": f"Local gateway evidence was persistently elevated ({len(lan_elevated)}/{len(lan)} samples).",
            "lan_elevated": len(lan_elevated),
            "lan_samples": len(lan),
        }
    if wan_degraded:
        return {
            "label": "Likely upstream ISP / path",
            "confidence": "High",
            "why": f"Internet-side degradation was detected while local gateway evidence stayed stable ({len(lan_elevated)}/{len(lan) or 0} elevated local samples).",
            "lan_elevated": len(lan_elevated),
            "lan_samples": len(lan),
        }
    return {
        "label": "No issue detected",
        "confidence": "High",
        "why": "No sustained internet degradation and no persistent local gateway degradation were detected.",
        "lan_elevated": len(lan_elevated),
        "lan_samples": len(lan),
        "source": "core_signal_fallback",
    }


def with_fallback_source(attribution_result: dict[str, Any]) -> dict[str, Any]:
    out = dict(attribution_result)
    out.setdefault("source", "core_signal_fallback")
    return out


def normalize_attribution_label(raw_label: str, raw_status: str) -> str:
    label_key = raw_label.lower()
    status_key = raw_status.lower()
    if "local" in label_key or "wi-fi" in label_key or "wifi" in label_key or "lan" in label_key or status_key == "likely_local":
        return "Likely local LAN / Wi-Fi"
    if "upstream" in label_key or "isp" in label_key or "path" in label_key or status_key == "likely_upstream":
        return "Likely upstream ISP / path"
    if status_key in {"no_network_issue_detected", "no_issue_detected"} or "no network issue" in label_key:
        return "No issue detected"
    return "No clear source identified"


def normalize_prime_observer_attribution_entry(entry: dict[str, Any], source: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    raw_label = str(entry.get("label") or entry.get("attribution_label") or "").strip()
    raw_status = str(entry.get("status") or entry.get("attribution_status") or "").strip().lower()
    confidence = str(entry.get("confidence") or entry.get("attribution_confidence") or "Unknown").strip()
    evidence = entry.get("evidence")
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    if isinstance(evidence, list):
        summary = "; ".join(str(item) for item in evidence if item)
    elif isinstance(entry.get("attribution_evidence"), dict):
        summary = str(entry["attribution_evidence"].get("summary") or raw_label or "Prime Observer attribution export was present.").strip()
        metrics = entry["attribution_evidence"]
    else:
        summary = str(entry.get("why") or raw_label or "Prime Observer attribution export was present.").strip()
    return {
        "label": normalize_attribution_label(raw_label, raw_status),
        "confidence": confidence or "Unknown",
        "why": summary,
        "source": source,
        "raw_status": raw_status,
        "raw_label": raw_label,
        "generated_at": entry.get("generated_at"),
        "evidence": evidence,
        "metrics": metrics,
    }


def select_incident_attribution(incidents: Any) -> dict[str, Any] | None:
    if not isinstance(incidents, list) or not incidents:
        return None
    normalized = [
        item
        for item in (
            normalize_prime_observer_attribution_entry(incident, "prime_observer_incident")
            for incident in incidents
        )
        if item is not None
    ]
    if not normalized:
        return None
    for label in ("Likely upstream ISP / path", "Likely local LAN / Wi-Fi"):
        matches = [item for item in normalized if item["label"] == label]
        if matches:
            chosen = dict(matches[0])
            chosen["incident_count"] = len(normalized)
            return chosen
    chosen = dict(normalized[0])
    chosen["incident_count"] = len(normalized)
    return chosen


def normalize_exported_attribution(
    exported: dict[str, Any] | None,
    sustained_count: int = 0,
) -> dict[str, Any] | None:
    if not isinstance(exported, dict):
        return None

    if sustained_count:
        incident = select_incident_attribution(exported.get("incidents"))
        if incident is not None:
            return incident

    window = normalize_prime_observer_attribution_entry(
        exported.get("window_attribution"),
        "prime_observer_window",
    )
    if window is not None:
        return window

    current = normalize_prime_observer_attribution_entry(
        exported.get("current_attribution"),
        "prime_observer_current",
    )
    if current is not None:
        return current

    legacy = {
        "attribution_label": exported.get("attribution_label"),
        "attribution_status": exported.get("attribution_status"),
        "attribution_confidence": exported.get("attribution_confidence"),
        "attribution_evidence": exported.get("attribution_evidence"),
        "generated_at": exported.get("generated_at"),
    }
    return normalize_prime_observer_attribution_entry(legacy, "prime_observer_current")


def dns_context(dns: dict[str, Any] | None, end: dt.datetime | None) -> dict[str, Any]:
    if not dns or dns.get("status") != "ok" or not isinstance(dns.get("summary"), dict):
        return {"available": False, "summary": "DNS/security context unavailable."}

    summary = dns["summary"]
    generated_at = parse_timestamp(str(dns.get("generated_at") or ""))
    age_hours = None
    stale = False
    if generated_at and end:
        age_hours = max((end.astimezone(dt.timezone.utc) - generated_at.astimezone(dt.timezone.utc)).total_seconds() / 3600.0, 0.0)
        stale = age_hours > 6

    top_reasons = summary.get("top_reasons") if isinstance(summary.get("top_reasons"), list) else []
    return {
        "available": True,
        "stale": stale,
        "age_hours": age_hours,
        "total_queries": summary.get("total_queries"),
        "blocked_queries": summary.get("blocked_queries"),
        "block_rate_pct": summary.get("block_rate_pct"),
        "encrypted_rate_pct": summary.get("encrypted_rate_pct"),
        "top_reasons": top_reasons[:3],
        "window": dns.get("window"),
    }


def analyze(
    observations: list[Observation],
    dns: dict[str, Any] | None = None,
    exported_attribution: dict[str, Any] | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    warnings: list[str] | None = None,
    ignored_hosts: dict[str, int] | None = None,
) -> dict[str, Any]:
    if observations and (start is None or end is None):
        end = max(o.ts for o in observations)
        start = min(o.ts for o in observations)

    lan, wan = collapse_series(observations)
    buckets = classify_buckets(wan)
    by_phase: dict[str, list[SeriesPoint]] = {}
    for point in wan:
        by_phase.setdefault(point.phase, []).append(point)

    sustained_buckets = [b for b in buckets if b["is_sustained"]]
    turbulence_buckets = [b for b in buckets if b["is_turbulence"]]
    raw_spikes = [p for p in wan if p.raw_bad and not p.sustained_bad]
    fallback_report_attribution = with_fallback_source(report_attribution(wan, lan, buckets))
    exported_report_attribution = normalize_exported_attribution(exported_attribution, len(sustained_buckets))

    return {
        "window_start": start,
        "window_end": end,
        "sample_counts": {
            "rows": len(observations),
            "lan_points": len(lan),
            "wan_points": len(wan),
        },
        "wan_health": stats_for(wan),
        "wan_by_phase": {phase: stats_for(points) for phase, points in sorted(by_phase.items())},
        "pattern": pattern_context(wan),
        "attribution": attribution(wan, lan, end),
        "report_attribution": exported_report_attribution or fallback_report_attribution,
        "fallback_report_attribution": fallback_report_attribution,
        "buckets": buckets,
        "sustained_buckets": sustained_buckets,
        "turbulence_buckets": turbulence_buckets,
        "raw_spikes": raw_spikes,
        "dns": dns_context(dns, end),
        "warnings": list(warnings or []),
        "ignored_hosts": dict(ignored_hosts or {}),
    }
