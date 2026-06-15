from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from statistics import median
from typing import Any
from urllib.parse import urlencode

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


TARGET_GROUP_LABELS = {
    "1.1.1.1": "Cloudflare",
    "9.9.9.9": "Quad9",
    "45.90.28.134": "NextDNS primary",
    "45.90.30.134": "NextDNS secondary",
    policy.GATEWAY_HOST: "local gateway",
}

TARGET_GROUP_NAMES = {
    "internet_probe": "Internet probes",
    "resolver_probe": "Resolver probes",
    "gateway_probe": "Gateway probe",
}


def compact_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def target_labels(group: dict[str, Any]) -> list[str]:
    counts = as_mapping(group.get("host_counts"))
    labels = [TARGET_GROUP_LABELS.get(str(host), str(host)) for host in counts if str(host)]
    return sorted(dict.fromkeys(labels))


def group_counts(group: dict[str, Any]) -> dict[str, int]:
    return {
        "sample_count": int_value(group.get("sample_count")),
        "raw_bad": int_value(group.get("raw_bad_samples", group.get("raw_bad_count"))),
        "sustained_bad": int_value(group.get("sustained_bad_samples", group.get("sustained_bad_count"))),
        "turbulence_buckets": int_value(group.get("turbulence_buckets")),
    }


def group_state(group: dict[str, Any]) -> str:
    counts = group_counts(group)
    if counts["sustained_bad"] > 0 or counts["turbulence_buckets"] > 0 or counts["raw_bad"] > 0:
        return "degraded"
    if counts["sample_count"] > 0:
        return "healthy"
    return "unknown"


def target_group_context(*sources: dict[str, Any]) -> dict[str, Any] | None:
    groups: dict[str, dict[str, Any]] = {}
    facts: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for fact in source.get("target_group_facts") or []:
            fact_text = str(fact).strip()
            if fact_text:
                facts.append(fact_text)
        source_groups = as_mapping(source.get("target_groups"))
        for group_name, group in source_groups.items():
            if isinstance(group, dict):
                groups[str(group_name)] = group
        for group_name in ("internet_probe", "resolver_probe", "gateway_probe"):
            summary = source.get(f"{group_name}_summary")
            if isinstance(summary, dict):
                groups[group_name] = summary

    metrics = next((source for source in sources if isinstance(source, dict) and "lan_bad" in source), {})
    if isinstance(metrics, dict) and "gateway_probe" not in groups and int_value(metrics.get("lan_recent_samples")):
        lan_bad = bool(metrics.get("lan_bad"))
        groups["gateway_probe"] = {
            "sample_count": metrics.get("lan_recent_samples"),
            "raw_bad_samples": metrics.get("lan_elevated_samples") if lan_bad else 0,
            "sustained_bad_samples": metrics.get("lan_elevated_samples") if lan_bad else 0,
            "host_counts": {policy.GATEWAY_HOST: metrics.get("lan_recent_samples")},
        }

    if not groups and not facts:
        return None

    states = {name: group_state(group) for name, group in groups.items()}
    labels = {name: target_labels(group) for name, group in groups.items()}
    sparse = {
        name: group_counts(group)["sample_count"] > 0 and group_counts(group)["sample_count"] < 16
        for name, group in groups.items()
    }
    return {
        "facts": list(dict.fromkeys(facts)),
        "groups": groups,
        "states": states,
        "labels": labels,
        "sparse": sparse,
    }


def merge_target_group_contexts(*contexts: dict[str, Any] | None) -> dict[str, Any] | None:
    groups: dict[str, dict[str, Any]] = {}
    facts: list[str] = []
    for context in contexts:
        if not isinstance(context, dict):
            continue
        groups.update(as_mapping(context.get("groups")))
        facts.extend(str(fact) for fact in context.get("facts") or [] if str(fact).strip())
    if not groups and not facts:
        return None
    return target_group_context({"target_groups": groups, "target_group_facts": list(dict.fromkeys(facts))})


def normalize_prime_observer_attribution_entry(
    entry: dict[str, Any],
    source: str,
    root_target_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
    normalized = {
        "label": normalize_attribution_label(raw_label, raw_status),
        "confidence": confidence or "Unknown",
        "why": summary,
        "source": source,
        "raw_status": raw_status,
        "raw_label": raw_label,
        "id": entry.get("id") or entry.get("event_id") or entry.get("incident_id"),
        "start": entry.get("start"),
        "end": entry.get("end"),
        "generated_at": entry.get("generated_at"),
        "evidence": evidence,
        "metrics": metrics,
    }
    context = merge_target_group_contexts(
        root_target_context,
        target_group_context(entry, metrics),
    )
    if context is not None:
        normalized["target_group_context"] = context
    return normalized


def select_incident_attribution(incidents: Any, root_target_context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(incidents, list) or not incidents:
        return None
    normalized = [
        item
        for item in (
            normalize_prime_observer_attribution_entry(incident, "prime_observer_incident", root_target_context)
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

    root_target_context = target_group_context(exported)
    if sustained_count:
        incident = select_incident_attribution(exported.get("incidents"), root_target_context)
        if incident is not None:
            return incident

    window = normalize_prime_observer_attribution_entry(
        exported.get("window_attribution"),
        "prime_observer_window",
        root_target_context,
    )
    if window is not None:
        return window

    current = normalize_prime_observer_attribution_entry(
        exported.get("current_attribution"),
        "prime_observer_current",
        root_target_context,
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
    return normalize_prime_observer_attribution_entry(legacy, "prime_observer_current", root_target_context)


def severity_for_status(status: str) -> str:
    return {"Healthy": "none", "Watch": "watch", "Attention": "attention"}.get(status, "unknown")


def normalize_confidence(value: Any) -> str:
    key = str(value or "Unknown").strip().lower()
    if key == "high":
        return "High"
    if key == "medium":
        return "Medium"
    if key == "low":
        return "Low"
    if key in {"none", "unknown", ""}:
        return "Unknown"
    return str(value).strip() or "Unknown"


def iso_value(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value or "")


def stable_event_id(kind: str, window_start: Any, window_end: Any, attribution_source: str, reference_id: Any = None) -> str:
    identity = "|".join(
        [
            kind,
            iso_value(window_start),
            iso_value(window_end),
            attribution_source,
            str(reference_id or ""),
        ]
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"core-signal-{kind}-{digest}"


def prime_observer_reference(
    window_start: dt.datetime | None,
    window_end: dt.datetime | None,
    attribution_result: dict[str, Any],
) -> dict[str, Any] | None:
    if window_start is None or window_end is None:
        return None
    ref_start = str(attribution_result.get("start") or window_start.isoformat())
    ref_end = str(attribution_result.get("end") or window_end.isoformat())
    query = urlencode({"start": ref_start, "end": ref_end})
    ref_type = "event" if attribution_result.get("source") == "prime_observer_incident" else "window"
    return {
        "type": ref_type,
        "id": attribution_result.get("id"),
        "path": "viz/investigate.html",
        "url": f"viz/investigate.html?{query}",
        "window_start": ref_start,
        "window_end": ref_end,
        "build_command_args": ["--start", ref_start, "--end", ref_end],
    }


def evidence_window(window_start: dt.datetime | None, window_end: dt.datetime | None) -> dict[str, Any] | None:
    if window_start is None or window_end is None:
        return None
    timezone = window_start.tzname() or "unknown"
    return {
        "source": "prime_observer",
        "window_start": window_start,
        "window_end": window_end,
        "timezone": timezone,
        "granularity": f"{policy.TURBULENCE_BUCKET_MINUTES}-minute bucket",
    }


def source_label(source: str) -> str:
    if source.startswith("prime_observer"):
        return "Prime Observer investigation reference"
    if source == "core_signal_fallback":
        return "network attribution observation"
    return source or "telemetry observation"


def confidence_reason(confidence: str, attribution_result: dict[str, Any], status: str, kind: str) -> str:
    source = attribution_result.get("source", "core_signal_fallback")
    why = str(attribution_result.get("why") or "").strip()
    if source == "prime_observer_current" and status == "Attention":
        return (
            "Medium confidence because Core Signal detected an actionable historical event, "
            "but the available Prime Observer attribution was current-state rather than event-specific."
        )
    if confidence == "High":
        return f"High confidence because {why}" if why else "High confidence because multiple relevant observations agreed."
    if confidence == "Medium":
        return f"Medium confidence because {why}" if why else "Medium confidence because the event was meaningful but the source attribution was limited."
    if confidence == "Low":
        return f"Low confidence because {why}" if why else "Low confidence because usable evidence was limited."
    if kind == "baseline_slowdown":
        return "Confidence follows the historical baseline sample count and recurrence context."
    return "Confidence was limited because Core Signal could not find enough supporting evidence for a stronger judgment."


def attribution_candidate(label: str) -> str | None:
    if label == "Likely upstream ISP / path":
        return "upstream"
    if label == "Likely local LAN / Wi-Fi":
        return "local"
    if label in {"No issue detected", "No recent issue detected"}:
        return "none"
    if label == "No clear source identified":
        return "unknown"
    return None


def target_group_pattern(context: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(context, dict):
        return None
    states = as_mapping(context.get("states"))
    internet = states.get("internet_probe", "unknown")
    resolver = states.get("resolver_probe", "unknown")
    gateway = states.get("gateway_probe", "unknown")
    resolver_sparse = bool(as_mapping(context.get("sparse")).get("resolver_probe"))

    if gateway == "degraded" and (internet == "degraded" or resolver == "degraded"):
        return {
            "candidate": "local",
            "confidence": "medium",
            "reason": (
                "Gateway probe degraded alongside WAN target groups, so local gateway/LAN involvement is likely; "
                "upstream involvement may also be present."
            ),
        }
    if internet == "degraded" and resolver == "healthy" and gateway == "healthy":
        return {
            "candidate": "upstream",
            "confidence": "high" if not resolver_sparse else "medium",
            "reason": (
                "Internet probes degraded while resolver and gateway probes remained healthy, "
                "pointing to general internet/path degradation rather than the resolver path."
            ),
        }
    if resolver == "degraded" and internet == "healthy" and gateway == "healthy":
        return {
            "candidate": "resolver_path",
            "confidence": "medium" if resolver_sparse else "high",
            "reason": (
                "Resolver probes degraded while internet and gateway probes remained healthy, "
                "making a resolver-path or DNS provider path issue more likely."
            ),
        }
    if internet == "degraded" and resolver == "degraded" and gateway == "healthy":
        return {
            "candidate": "upstream",
            "confidence": "medium" if resolver_sparse else "high",
            "reason": (
                "Internet and resolver probes both degraded while the gateway probe remained healthy, "
                "pointing to a broader upstream/WAN path issue without isolating DNS."
            ),
        }
    if internet == "degraded" and gateway == "healthy":
        return {
            "candidate": "upstream",
            "confidence": "medium",
            "reason": "Internet probes degraded while gateway evidence remained healthy.",
        }
    if resolver == "degraded" and gateway == "healthy":
        return {
            "candidate": "resolver_path",
            "confidence": "low" if resolver_sparse else "medium",
            "reason": "Resolver probes degraded while gateway evidence remained healthy.",
        }
    return None


def attribution_assessment(
    status: str,
    attribution_result: dict[str, Any],
) -> dict[str, str] | None:
    if status == "Healthy":
        return None
    group_pattern = target_group_pattern(attribution_result.get("target_group_context"))
    if group_pattern is not None:
        return group_pattern
    label = str(attribution_result.get("label") or "")
    candidate = attribution_candidate(label)
    if candidate is None or candidate == "none":
        return None

    confidence = normalize_confidence(attribution_result.get("confidence"))
    if confidence == "Unknown":
        confidence = "Low"
    reason = str(attribution_result.get("why") or "").strip()
    if not reason:
        if candidate == "upstream":
            reason = "WAN degraded while LAN remained healthy."
        elif candidate == "local":
            reason = "Local gateway evidence was elevated."
        else:
            reason = "Available local-vs-internet evidence did not clearly identify a source."
    return {
        "candidate": candidate,
        "confidence": confidence.lower(),
        "reason": reason,
    }


def uncertainty_notes(
    kind: str,
    status: str,
    attribution_result: dict[str, Any],
) -> list[str]:
    target_context = attribution_result.get("target_group_context")
    if status == "Healthy" or (kind == "watch" and not target_context):
        return []

    notes: list[str] = []
    label = str(attribution_result.get("label") or "")
    source = str(attribution_result.get("source") or "")
    if kind == "watch" and target_context:
        pass
    elif label == "Likely upstream ISP / path":
        notes.append("Unable to distinguish ISP congestion from transient routing issues using the available telemetry.")
    elif label == "Likely local LAN / Wi-Fi":
        notes.append("Unable to identify the specific local device, Wi-Fi segment, or router condition from the available telemetry.")
    elif label in {"No clear source identified", ""}:
        notes.append("Available local-vs-internet evidence does not clearly identify whether the source was local or upstream.")

    if kind in {"turbulence", "isolated_instability"}:
        notes.append("Brief instability did not persist long enough to estimate user impact with high confidence.")
    if source == "prime_observer_current" and status == "Attention":
        notes.append("Prime Observer attribution describes current state rather than the historical event window.")
    notes.extend(target_group_uncertainty_notes(attribution_result.get("target_group_context")))
    return notes


def target_group_uncertainty_notes(context: dict[str, Any] | None) -> list[str]:
    if not isinstance(context, dict):
        return []
    states = as_mapping(context.get("states"))
    sparse = as_mapping(context.get("sparse"))
    notes: list[str] = []
    if sparse.get("resolver_probe"):
        notes.append("Resolver probes do not yet have enough history to establish a stable baseline.")
    if states.get("internet_probe") == "degraded" and states.get("resolver_probe") == "degraded":
        notes.append("Both internet and resolver probes degraded, so the evidence does not isolate DNS from broader WAN path.")
    if states.get("gateway_probe") == "degraded":
        notes.append("Gateway probe degradation means local involvement cannot be ruled out.")
    return list(dict.fromkeys(notes))


def rating_index(rating: str) -> int:
    return {"limited": 0, "moderate": 1, "strong": 2}.get(rating, 0)


def rating_from_index(index: int) -> str:
    return ["limited", "moderate", "strong"][max(0, min(index, 2))]


def adjust_evidence_strength_for_target_groups(
    strength: dict[str, str] | None,
    context: dict[str, Any] | None,
) -> dict[str, str] | None:
    if strength is None or not isinstance(context, dict):
        return strength
    states = as_mapping(context.get("states"))
    labels = as_mapping(context.get("labels"))
    sparse = as_mapping(context.get("sparse"))
    rating = rating_index(strength.get("rating", "limited"))
    reasons: list[str] = []

    degraded_group_count = sum(1 for state in states.values() if state == "degraded")
    multi_target_group = any(
        states.get(group_name) == "degraded" and len(labels.get(group_name) or []) >= 2
        for group_name in ("internet_probe", "resolver_probe")
    )
    if multi_target_group:
        rating += 1
        reasons.append("multiple targets in the same probe class degraded")
    if states.get("internet_probe") == "degraded" and states.get("resolver_probe") == "degraded" and states.get("gateway_probe") == "healthy":
        rating += 1
        reasons.append("internet and resolver probes agreed while the gateway stayed healthy")
    elif degraded_group_count >= 2 and states.get("gateway_probe") != "degraded":
        rating += 1
        reasons.append("multiple probe classes agreed")
    if states.get("gateway_probe") == "degraded":
        rating -= 1
        reasons.append("gateway degradation adds local-path ambiguity")
    if sparse.get("resolver_probe") and states.get("resolver_probe") == "degraded":
        rating -= 1
        reasons.append("resolver probe history is limited")

    if not reasons:
        return strength
    return {
        "rating": rating_from_index(rating),
        "reason": f"{strength.get('reason', '').rstrip('.')} with target-group context: {', '.join(reasons)}.",
    }


def evidence_strength(
    kind: str,
    sustained_buckets: list[dict[str, Any]],
    turbulence_buckets: list[dict[str, Any]],
    raw_spikes: list[SeriesPoint],
    pattern: dict[str, Any],
    supporting_facts: list[dict[str, Any]],
    target_context: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    if kind == "watch":
        if target_context is None:
            return None
        return adjust_evidence_strength_for_target_groups(
            {
                "rating": "limited",
                "reason": "Target-group evidence was present, but the event did not meet stronger Core Signal persistence thresholds.",
            },
            target_context,
        )
    strength: dict[str, str] | None = None
    if kind == "sustained_slowdown":
        if len(sustained_buckets) >= 2:
            strength = {
                "rating": "strong",
                "reason": f"{len(sustained_buckets)} sustained WAN period(s) were observed.",
            }
        else:
            strength = {
                "rating": "moderate",
                "reason": "A sustained WAN period was observed with supporting attribution context.",
            }
    elif kind == "turbulence":
        strength = {
            "rating": "limited",
            "reason": "Repeated WAN instability was observed, but it did not become sustained.",
        }
    elif kind == "isolated_instability":
        strength = {
            "rating": "limited",
            "reason": f"{len(raw_spikes)} raw WAN threshold crossing(s) were observed without sustained persistence.",
        }
    elif kind == "baseline_slowdown":
        confidence = normalize_confidence(pattern.get("confidence"))
        sample_count = pattern.get("sample_count")
        rating = "moderate" if confidence in {"Medium", "High"} else "limited"
        strength = {
            "rating": rating,
            "reason": f"Historical baseline comparison used {sample_count} comparable sample(s) with {confidence.lower()} confidence.",
        }
    elif supporting_facts:
        strength = {
            "rating": "limited",
            "reason": "Core Signal found supporting facts, but not enough structure for stronger evidence quality.",
        }
    return adjust_evidence_strength_for_target_groups(strength, target_context)


def event_confidence(status: str, kind: str, attribution_result: dict[str, Any], pattern: dict[str, Any]) -> str:
    if attribution_result.get("source") == "prime_observer_current" and status == "Attention":
        return "Medium"
    if kind == "baseline_slowdown":
        return normalize_confidence(pattern.get("confidence"))
    return normalize_confidence(attribution_result.get("confidence") or pattern.get("confidence"))


def fact_reference_url(reference: dict[str, Any] | None) -> str | None:
    if not reference:
        return None
    return reference.get("url") or reference.get("path")


def target_group_supporting_facts(context: dict[str, Any] | None, reference: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    states = as_mapping(context.get("states"))
    labels = as_mapping(context.get("labels"))
    sparse = as_mapping(context.get("sparse"))
    facts: list[dict[str, Any]] = []
    for group_name in ("internet_probe", "resolver_probe"):
        if states.get(group_name) != "degraded":
            continue
        target_list = compact_join(list(labels.get(group_name) or []))
        if not target_list:
            continue
        facts.append(
            {
                "id": f"fact-{group_name.replace('_', '-')}",
                "kind": "target_group_evidence",
                "summary": f"{TARGET_GROUP_NAMES[group_name]} degraded: {target_list}.",
                "source": "Prime Observer target grouping",
                "reference": fact_reference_url(reference),
            }
        )
    if states.get("gateway_probe") == "healthy":
        facts.append(
            {
                "id": "fact-gateway-probe",
                "kind": "target_group_evidence",
                "summary": "Gateway probe remained below local threshold.",
                "source": "Prime Observer target grouping",
                "reference": fact_reference_url(reference),
            }
        )
    elif states.get("gateway_probe") == "degraded":
        facts.append(
            {
                "id": "fact-gateway-probe",
                "kind": "target_group_evidence",
                "summary": "Gateway probe also degraded.",
                "source": "Prime Observer target grouping",
                "reference": fact_reference_url(reference),
            }
        )
    if sparse.get("resolver_probe"):
        facts.append(
            {
                "id": "fact-resolver-history",
                "kind": "target_group_evidence",
                "summary": "Resolver probe history is limited.",
                "source": "Prime Observer target grouping",
                "reference": fact_reference_url(reference),
            }
        )
    return facts


def build_supporting_facts(
    kind: str,
    summary: str,
    affected_start: dt.datetime | None,
    affected_end: dt.datetime | None,
    wan_health: dict[str, Any],
    raw_spikes: list[SeriesPoint],
    report_attribution_result: dict[str, Any],
    reference: dict[str, Any] | None,
    pattern: dict[str, Any],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    window = evidence_window(affected_start, affected_end)
    if window is not None:
        facts.append(
            {
                "id": "fact-telemetry-window",
                "kind": "telemetry_window",
                "summary": summary,
                "source": "telemetry observation",
                "reference": fact_reference_url(reference),
                "window": window,
            }
        )

    attribution_why = str(report_attribution_result.get("why") or "").strip()
    if attribution_why:
        facts.append(
            {
                "id": "fact-attribution",
                "kind": "network_attribution",
                "summary": attribution_why,
                "source": source_label(str(report_attribution_result.get("source") or "")),
                "reference": fact_reference_url(reference),
                "observed_at": report_attribution_result.get("generated_at"),
                "window": {
                    "window_start": report_attribution_result.get("start") or affected_start,
                    "window_end": report_attribution_result.get("end") or affected_end,
                },
            }
        )

    facts.extend(target_group_supporting_facts(report_attribution_result.get("target_group_context"), reference))

    if raw_spikes and kind in {"turbulence", "isolated_instability"}:
        facts.append(
            {
                "id": "fact-raw-instability",
                "kind": "telemetry_observation",
                "summary": f"{len(raw_spikes)} raw WAN threshold crossing(s) were present without enough persistence for a stronger event.",
                "source": "telemetry observation",
                "reference": fact_reference_url(reference),
                "window": window,
            }
        )

    if kind == "baseline_slowdown":
        facts.append(
            {
                "id": "fact-historical-baseline",
                "kind": "historical_baseline",
                "summary": (
                    f"{pattern.get('label')} with {pattern.get('confidence')} baseline confidence "
                    f"from {pattern.get('sample_count')} comparable sample(s)."
                ),
                "source": "telemetry observation",
                "reference": None,
                "window": window,
            }
        )

    if not facts and wan_health.get("samples", 0) == 0:
        facts.append(
            {
                "id": "fact-missing-telemetry",
                "kind": "telemetry_observation",
                "summary": "No usable internet telemetry was available for the latest export window.",
                "source": "telemetry observation",
                "reference": None,
                "window": window,
            }
        )
    return facts


def event_status_from_inputs(
    wan_health: dict[str, Any],
    sustained_buckets: list[dict[str, Any]],
    turbulence_buckets: list[dict[str, Any]],
    pattern: dict[str, Any],
    report_attribution_result: dict[str, Any],
    raw_spikes: list[SeriesPoint],
) -> str:
    if wan_health.get("samples", 0) == 0:
        return "Watch"
    if sustained_buckets:
        return "Attention"
    if (
        report_attribution_result.get("label") == "Likely local LAN / Wi-Fi"
        and report_attribution_result.get("confidence") == "High"
    ):
        return "Attention"
    if report_attribution_result.get("label") in {"Likely local LAN / Wi-Fi", "Likely upstream ISP / path"}:
        return "Watch"
    if target_group_pattern(report_attribution_result.get("target_group_context")) is not None:
        return "Watch"
    if turbulence_buckets:
        return "Watch"
    if len(raw_spikes) >= policy.TURBULENCE_MIN_RAW_BAD:
        return "Watch"
    if pattern.get("label") == "Highly elevated for this time of day" and pattern.get("confidence") in {"Medium", "High"}:
        return "Watch"
    return "Healthy"


def event_issue_location(status: str, attribution_result: dict[str, Any]) -> str:
    if status == "Healthy":
        return ""
    label = attribution_result.get("label", "")
    if label == "Likely local LAN / Wi-Fi":
        return "Likely local Wi-Fi/router issue"
    if label == "Likely upstream ISP / path":
        return "Likely upstream/ISP issue"
    return "No clear source identified"


def event_recommended_action(status: str, location: str) -> str:
    if status == "Healthy":
        return "None."
    if status == "Watch":
        return "No action unless people noticed slow calls, buffering, gaming lag, or dropped connections."
    if location == "Likely local Wi-Fi/router issue":
        return "Check the router or Wi-Fi if people noticed symptoms during the affected time."
    if location == "Likely upstream/ISP issue":
        return "No home-network change is suggested. Check provider status or contact the ISP only if symptoms matched the affected time."
    return "Check whether symptoms matched the affected time. If this repeats, compare it with what people were doing."


def event_window_from_buckets(buckets: list[dict[str, Any]]) -> tuple[dt.datetime | None, dt.datetime | None]:
    if not buckets:
        return None, None
    return min(b["start"] for b in buckets), max(b["end"] for b in buckets)


def build_event_metadata(
    wan_health: dict[str, Any],
    sustained_buckets: list[dict[str, Any]],
    turbulence_buckets: list[dict[str, Any]],
    raw_spikes: list[SeriesPoint],
    pattern: dict[str, Any],
    report_attribution_result: dict[str, Any],
    window_start: dt.datetime | None,
    window_end: dt.datetime | None,
) -> list[dict[str, Any]]:
    status = event_status_from_inputs(
        wan_health,
        sustained_buckets,
        turbulence_buckets,
        pattern,
        report_attribution_result,
        raw_spikes,
    )
    if status == "Healthy":
        return []

    if sustained_buckets:
        kind = "sustained_slowdown"
        affected_start, affected_end = event_window_from_buckets(sustained_buckets)
        summary = f"{len(sustained_buckets)} sustained slowdown period(s) were found."
        why = "Sustained slowdown was detected, which means user impact was possible."
    elif turbulence_buckets:
        kind = "turbulence"
        affected_start, affected_end = event_window_from_buckets(turbulence_buckets)
        summary = "Brief instability repeated enough to be worth noting."
        why = "Brief instability repeated enough to be noteworthy, but it was not actionable because no sustained slowdown was detected."
    elif len(raw_spikes) >= policy.TURBULENCE_MIN_RAW_BAD:
        kind = "isolated_instability"
        affected_start = min(p.ts for p in raw_spikes) if raw_spikes else window_start
        affected_end = max(p.ts for p in raw_spikes) if raw_spikes else window_end
        summary = f"{len(raw_spikes)} brief unstable moments were found."
        why = "Brief instability repeated enough to be noteworthy, but the events did not continue long enough to suggest user impact."
    elif pattern.get("label") == "Highly elevated for this time of day" and pattern.get("confidence") in {"Medium", "High"}:
        kind = "baseline_slowdown"
        latest = pattern.get("latest")
        affected_start = latest.ts if isinstance(latest, SeriesPoint) else window_start
        affected_end = latest.ts if isinstance(latest, SeriesPoint) else window_end
        summary = "Performance was unusually slow compared with the normal pattern for that time of day."
        why = "Performance was noticeably different from historical norms, but no sustained instability or user-impacting issue was detected."
    else:
        kind = "watch"
        affected_start, affected_end = window_start, window_end
        summary = "Something notable appeared in the latest telemetry."
        why = "Something was notable enough to watch, but no sustained instability or user-impacting issue was detected."

    location = event_issue_location(status, report_attribution_result)
    reference = prime_observer_reference(affected_start, affected_end, report_attribution_result)
    reference_id = reference.get("id") or reference.get("url") if reference else None
    confidence = event_confidence(status, kind, report_attribution_result, pattern)
    supporting_facts = build_supporting_facts(
        kind,
        summary,
        affected_start,
        affected_end,
        wan_health,
        raw_spikes,
        report_attribution_result,
        reference,
        pattern,
    )
    event_id = stable_event_id(kind, affected_start, affected_end, report_attribution_result.get("source", ""), reference_id)
    reason = confidence_reason(confidence, report_attribution_result, status, kind)
    event = {
        "id": event_id,
        "kind": kind,
        "status": status,
        "severity": severity_for_status(status),
        "confidence": confidence,
        "confidence_reason": reason,
        "window_start": affected_start,
        "window_end": affected_end,
        "summary": summary,
        "why": why,
        "supporting_facts": supporting_facts,
        "recommended_action": event_recommended_action(status, location),
        "recommendation_trace": {
            "event_id": event_id,
            "supporting_fact_ids": [fact["id"] for fact in supporting_facts],
            "confidence": confidence,
            "confidence_reason": reason,
        },
        "issue_location": location,
        "interpretation_source": "core_signal",
        "attribution_source": report_attribution_result.get("source", "core_signal_fallback"),
        "prime_observer_reference": reference,
        "evidence_window": evidence_window(affected_start, affected_end),
        "related_events": [],
    }
    uncertainties = uncertainty_notes(kind, status, report_attribution_result)
    assessment = None if kind == "watch" and "target_group_context" not in report_attribution_result else attribution_assessment(status, report_attribution_result)
    strength = evidence_strength(
        kind,
        sustained_buckets,
        turbulence_buckets,
        raw_spikes,
        pattern,
        supporting_facts,
        report_attribution_result.get("target_group_context"),
    )
    if uncertainties:
        event["uncertainties"] = uncertainties
    if assessment is not None:
        event["attribution_assessment"] = assessment
    if strength is not None:
        event["evidence_strength"] = strength
    return [event]


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
    report_attribution_result = exported_report_attribution or fallback_report_attribution
    wan_health = stats_for(wan)
    pattern = pattern_context(wan)

    return {
        "window_start": start,
        "window_end": end,
        "sample_counts": {
            "rows": len(observations),
            "lan_points": len(lan),
            "wan_points": len(wan),
        },
        "wan_health": wan_health,
        "wan_by_phase": {phase: stats_for(points) for phase, points in sorted(by_phase.items())},
        "pattern": pattern,
        "attribution": attribution(wan, lan, end),
        "report_attribution": report_attribution_result,
        "fallback_report_attribution": fallback_report_attribution,
        "buckets": buckets,
        "sustained_buckets": sustained_buckets,
        "turbulence_buckets": turbulence_buckets,
        "raw_spikes": raw_spikes,
        "events": build_event_metadata(
            wan_health,
            sustained_buckets,
            turbulence_buckets,
            raw_spikes,
            pattern,
            report_attribution_result,
            start,
            end,
        ),
        "dns": dns_context(dns, end),
        "warnings": list(warnings or []),
        "ignored_hosts": dict(ignored_hosts or {}),
    }
