from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from . import policy
from .analyze import SeriesPoint, classify_buckets, collapse_series, quantile
from .ingest import Observation


BUSINESS_START = 8 * 60
BUSINESS_END = 18 * 60
MORNING_START = 8 * 60 + 30
MORNING_END = 11 * 60 + 30
AFTERNOON_START = 15 * 60
AFTERNOON_END = 17 * 60 + 30
HALF_HOUR = 30


@dataclass(frozen=True)
class PatternFinding:
    name: str
    evidence: list[str]
    first_observed: dt.datetime | None
    last_observed: dt.datetime | None
    observation_count: int
    confidence: str
    possible_explanations: list[str]
    confidence_inputs: dict[str, Any]
    approaching_signature: bool


@dataclass(frozen=True)
class ConcentrationSignal:
    signal_kind: str
    entity_label: str
    entity_type: str
    name: str | None
    name_redacted: bool
    count: int
    total: int
    share_pct: float
    dominance_ratio: float | None
    share_label: str
    observation: str
    review: str
    persistence: str
    confidence: str


def minute_of_day(ts: dt.datetime) -> int:
    return ts.hour * 60 + ts.minute


def in_window(point: SeriesPoint, start_minute: int, end_minute: int) -> bool:
    minute = minute_of_day(point.ts)
    return start_minute <= minute < end_minute


def is_weekday(point: SeriesPoint) -> bool:
    return point.ts.weekday() < 5


def is_business_hour(point: SeriesPoint) -> bool:
    return is_weekday(point) and in_window(point, BUSINESS_START, BUSINESS_END)


def fmt_ts(value: dt.datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M %Z").strip()


def fmt_time_window(start_minute: int, end_minute: int) -> str:
    def one(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    return f"{one(start_minute)}-{one(end_minute)}"


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}%"


def num(value: float | None, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def day_count(points: list[SeriesPoint]) -> int:
    return len({p.ts.date() for p in points})


def span_days(points: list[SeriesPoint]) -> int:
    if not points:
        return 0
    first = min(p.ts for p in points).date()
    last = max(p.ts for p in points).date()
    return (last - first).days + 1


def point_stats(points: list[SeriesPoint]) -> dict[str, Any]:
    if not points:
        return {
            "samples": 0,
            "days": 0,
            "median_p95_ms": None,
            "median_jitter_ms": None,
            "raw_bad_rate_pct": None,
            "sustained_bad_rate_pct": None,
            "p95_of_p95_ms": None,
        }
    return {
        "samples": len(points),
        "days": day_count(points),
        "median_p95_ms": median(p.p95_ms for p in points),
        "median_jitter_ms": median(p.jitter_ms for p in points),
        "raw_bad_rate_pct": 100.0 * sum(1 for p in points if p.raw_bad) / len(points),
        "sustained_bad_rate_pct": 100.0 * sum(1 for p in points if p.sustained_bad) / len(points),
        "p95_of_p95_ms": quantile([p.p95_ms for p in points], 0.95),
    }


def relative_delta_pct(a: float | None, b: float | None) -> float:
    if a is None or b is None or b == 0:
        return 0.0
    return 100.0 * (a - b) / b


def confidence_from_inputs(
    recurrence_count: int,
    timing_consistency: float,
    magnitude_pct: float,
    sample_size: int,
    duration_days: int,
) -> tuple[str, dict[str, Any]]:
    recurrence_points = 0
    if recurrence_count >= 8:
        recurrence_points = 3
    elif recurrence_count >= 4:
        recurrence_points = 2
    elif recurrence_count >= 2:
        recurrence_points = 1

    consistency_points = 0
    if timing_consistency >= 0.70:
        consistency_points = 3
    elif timing_consistency >= 0.45:
        consistency_points = 2
    elif timing_consistency >= 0.20:
        consistency_points = 1

    magnitude = abs(magnitude_pct)
    magnitude_points = 0
    if magnitude >= 50:
        magnitude_points = 3
    elif magnitude >= 25:
        magnitude_points = 2
    elif magnitude >= 10:
        magnitude_points = 1

    sample_points = 0
    if sample_size >= 1500:
        sample_points = 3
    elif sample_size >= 500:
        sample_points = 2
    elif sample_size >= 100:
        sample_points = 1

    duration_points = 0
    if duration_days >= 28:
        duration_points = 3
    elif duration_days >= 14:
        duration_points = 2
    elif duration_days >= 7:
        duration_points = 1

    total = recurrence_points + consistency_points + magnitude_points + sample_points + duration_points
    if duration_days < 14:
        confidence = "Low"
    elif duration_days < 30:
        confidence = "Medium" if total >= 5 else "Low"
    elif recurrence_count < 2:
        confidence = "Low"
    elif total >= 10:
        confidence = "High"
    elif total >= 5:
        confidence = "Medium"
    else:
        confidence = "Low"

    return confidence, {
        "recurrence_count": recurrence_count,
        "recurrence_points": recurrence_points,
        "timing_consistency": timing_consistency,
        "timing_points": consistency_points,
        "magnitude_pct": magnitude_pct,
        "magnitude_points": magnitude_points,
        "sample_size": sample_size,
        "sample_points": sample_points,
        "duration_days": duration_days,
        "duration_points": duration_points,
        "total_points": total,
    }


def approaching_signature(confidence: str, inputs: dict[str, Any]) -> bool:
    return (
        confidence == "High"
        and inputs["recurrence_count"] >= 8
        and inputs["duration_days"] >= 28
        and inputs["timing_consistency"] >= 0.70
    )


def make_finding(
    name: str,
    points: list[SeriesPoint],
    evidence: list[str],
    recurrence_count: int,
    timing_consistency: float,
    magnitude_pct: float,
    sample_size: int,
    duration_days: int,
    possible_explanations: list[str],
) -> PatternFinding:
    confidence, inputs = confidence_from_inputs(
        recurrence_count,
        timing_consistency,
        magnitude_pct,
        sample_size,
        duration_days,
    )
    return PatternFinding(
        name=name,
        evidence=evidence,
        first_observed=min((p.ts for p in points), default=None),
        last_observed=max((p.ts for p in points), default=None),
        observation_count=recurrence_count,
        confidence=confidence,
        possible_explanations=possible_explanations,
        confidence_inputs=inputs,
        approaching_signature=approaching_signature(confidence, inputs),
    )


def timing_ratio(matching_days: int, possible_days: int) -> float:
    if possible_days <= 0:
        return 0.0
    return min(matching_days / possible_days, 1.0)


def business_hour_pattern(wan: list[SeriesPoint]) -> PatternFinding | None:
    business = [p for p in wan if is_business_hour(p)]
    off_hours = [p for p in wan if not is_business_hour(p)]
    if len(business) < 20 or len(off_hours) < 20:
        return None
    b_stats = point_stats(business)
    o_stats = point_stats(off_hours)
    delta = relative_delta_pct(b_stats["median_p95_ms"], o_stats["median_p95_ms"])
    raw_delta = (b_stats["raw_bad_rate_pct"] or 0.0) - (o_stats["raw_bad_rate_pct"] or 0.0)
    if delta < 10 and raw_delta < 2:
        return None

    days = sorted({p.ts.date() for p in business})
    matching_days = 0
    for day in days:
        day_business = [p for p in business if p.ts.date() == day]
        day_off = [p for p in off_hours if p.ts.date() == day]
        if len(day_business) >= 3 and len(day_off) >= 3:
            if relative_delta_pct(point_stats(day_business)["median_p95_ms"], point_stats(day_off)["median_p95_ms"]) >= 10:
                matching_days += 1

    return make_finding(
        "Business-hour WAN elevation",
        business,
        [
            f"Weekday business hours median WAN p95 was {num(b_stats['median_p95_ms'], ' ms')}, versus {num(o_stats['median_p95_ms'], ' ms')} outside those hours.",
            f"Raw-bad rate was {pct(b_stats['raw_bad_rate_pct'])} in business hours versus {pct(o_stats['raw_bad_rate_pct'])} outside business hours.",
            f"Sustained-bad rate was {pct(b_stats['sustained_bad_rate_pct'])} in business hours versus {pct(o_stats['sustained_bad_rate_pct'])} outside business hours.",
        ],
        matching_days or day_count(business),
        timing_ratio(matching_days or day_count(business), max(day_count(business), 1)),
        max(delta, raw_delta),
        len(business) + len(off_hours),
        span_days(wan),
        [
            "Scheduled workday traffic or provider-side congestion could be involved.",
            "Local activity, Wi-Fi contention, or router load could also contribute.",
            "The available telemetry does not establish cause or user impact.",
        ],
    )


def weekday_weekend_pattern(wan: list[SeriesPoint]) -> tuple[PatternFinding | None, str]:
    weekdays = [p for p in wan if p.ts.weekday() < 5]
    weekends = [p for p in wan if p.ts.weekday() >= 5]
    if len(weekdays) < 20 or len(weekends) < 20:
        return None, "Weekday/weekend comparison was not evaluated because one side did not have enough samples."
    w_stats = point_stats(weekdays)
    e_stats = point_stats(weekends)
    delta = relative_delta_pct(w_stats["median_p95_ms"], e_stats["median_p95_ms"])
    raw_delta = (w_stats["raw_bad_rate_pct"] or 0.0) - (e_stats["raw_bad_rate_pct"] or 0.0)
    if abs(delta) < 10 and abs(raw_delta) < 2:
        return None, "Weekday and weekend WAN behavior did not differ enough to create an observed pattern."

    worse = "Weekday" if delta >= 0 or raw_delta >= 0 else "Weekend"
    points = weekdays if worse == "Weekday" else weekends
    name = f"{worse} WAN behavior differs from comparison days"
    return make_finding(
        name,
        points,
        [
            f"Weekday median WAN p95 was {num(w_stats['median_p95_ms'], ' ms')}; weekend median WAN p95 was {num(e_stats['median_p95_ms'], ' ms')}.",
            f"Weekday raw-bad rate was {pct(w_stats['raw_bad_rate_pct'])}; weekend raw-bad rate was {pct(e_stats['raw_bad_rate_pct'])}.",
        ],
        day_count(points),
        timing_ratio(day_count(points), max(day_count(wan), 1)),
        max(abs(delta), abs(raw_delta)),
        len(wan),
        span_days(wan),
        [
            "Weekly schedules may correlate with the behavior.",
            "The difference may also reflect limited sampling if only a few days are present.",
            "The telemetry does not identify a root cause.",
        ],
    ), "Weekday/weekend comparison produced an observed candidate."


def window_elevation_pattern(
    wan: list[SeriesPoint],
    name: str,
    start_minute: int,
    end_minute: int,
) -> PatternFinding | None:
    window_points = [p for p in wan if is_weekday(p) and in_window(p, start_minute, end_minute)]
    comparison = [p for p in wan if is_weekday(p) and not in_window(p, start_minute, end_minute)]
    if len(window_points) < 12 or len(comparison) < 12:
        return None
    w_stats = point_stats(window_points)
    c_stats = point_stats(comparison)
    delta = relative_delta_pct(w_stats["median_p95_ms"], c_stats["median_p95_ms"])
    raw_delta = (w_stats["raw_bad_rate_pct"] or 0.0) - (c_stats["raw_bad_rate_pct"] or 0.0)
    jitter_delta = relative_delta_pct(w_stats["median_jitter_ms"], c_stats["median_jitter_ms"])
    if delta < 10 and raw_delta < 2 and jitter_delta < 10:
        return None

    matching_days = 0
    for day in sorted({p.ts.date() for p in window_points}):
        day_window = [p for p in window_points if p.ts.date() == day]
        day_comparison = [p for p in comparison if p.ts.date() == day]
        if len(day_window) >= 3 and len(day_comparison) >= 3:
            day_delta = relative_delta_pct(
                point_stats(day_window)["median_p95_ms"],
                point_stats(day_comparison)["median_p95_ms"],
            )
            if day_delta >= 10 or any(p.raw_bad for p in day_window):
                matching_days += 1

    return make_finding(
        f"{name} around {fmt_time_window(start_minute, end_minute)}",
        window_points,
        [
            f"Window median WAN p95 was {num(w_stats['median_p95_ms'], ' ms')}, versus {num(c_stats['median_p95_ms'], ' ms')} across other weekday samples.",
            f"Window median jitter was {num(w_stats['median_jitter_ms'], ' ms')}, versus {num(c_stats['median_jitter_ms'], ' ms')} outside the window.",
            f"Window raw-bad rate was {pct(w_stats['raw_bad_rate_pct'])}; sustained-bad rate was {pct(w_stats['sustained_bad_rate_pct'])}.",
        ],
        matching_days or day_count(window_points),
        timing_ratio(matching_days or day_count(window_points), max(day_count(window_points), 1)),
        max(delta, raw_delta, jitter_delta),
        len(window_points) + len(comparison),
        span_days(wan),
        [
            "The timing may correlate with recurring demand on the local network or upstream path.",
            "A single-day or short-duration observation should be treated as an emerging candidate only.",
            "No user impact is inferred from this pattern alone.",
        ],
    )


def lan_lookup(lan: list[SeriesPoint]) -> dict[dt.datetime, SeriesPoint]:
    return {p.ts.replace(second=0, microsecond=0): p for p in lan}


def lan_wan_coelevation_pattern(wan: list[SeriesPoint], lan: list[SeriesPoint]) -> PatternFinding | None:
    by_minute = lan_lookup(lan)
    coelevated: list[SeriesPoint] = []
    for point in wan:
        local = by_minute.get(point.ts.replace(second=0, microsecond=0))
        if local and point.raw_bad and local.p95_ms > policy.LAN_ELEVATED_P95_MS:
            coelevated.append(point)
    if len(coelevated) < 2:
        return None

    possible_days = day_count(wan)
    recurrence = day_count(coelevated)
    rate = 100.0 * len(coelevated) / max(len(wan), 1)
    windows = top_time_windows(coelevated, limit=3)
    return make_finding(
        "LAN/WAN co-elevation",
        coelevated,
        [
            f"{len(coelevated)} WAN raw-bad samples coincided with local gateway p95 above {num(policy.LAN_ELEVATED_P95_MS, ' ms')}.",
            f"Co-elevation represented {pct(rate)} of WAN samples.",
            "Most common co-elevation windows: " + ", ".join(windows) + ".",
        ],
        recurrence,
        timing_ratio(recurrence, max(possible_days, 1)),
        rate,
        len(wan) + len(lan),
        span_days(wan + lan),
        [
            "Local Wi-Fi/router contention is one possible explanation.",
            "A shared upstream disturbance could still be present.",
            "This evidence is correlative and does not establish the source.",
        ],
    )


def top_time_windows(points: list[SeriesPoint], limit: int = 3) -> list[str]:
    counts: dict[int, int] = {}
    for point in points:
        start = (minute_of_day(point.ts) // HALF_HOUR) * HALF_HOUR
        counts[start] = counts.get(start, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [f"{fmt_time_window(start, start + HALF_HOUR)} ({count})" for start, count in top]


def low_latency_pattern(wan: list[SeriesPoint]) -> PatternFinding | None:
    if len(wan) < 50:
        return None
    overall = point_stats(wan)
    overall_median = overall["median_p95_ms"]
    if overall_median is None:
        return None
    by_window: dict[int, list[SeriesPoint]] = {}
    for point in wan:
        start = (minute_of_day(point.ts) // HALF_HOUR) * HALF_HOUR
        by_window.setdefault(start, []).append(point)

    candidates: list[tuple[float, int, list[SeriesPoint], dict[str, Any]]] = []
    for start, rows in by_window.items():
        if len(rows) < 8:
            continue
        stats = point_stats(rows)
        delta = relative_delta_pct(stats["median_p95_ms"], overall_median)
        if delta <= -20 and (stats["raw_bad_rate_pct"] or 0.0) <= 1:
            candidates.append((delta, start, rows, stats))
    if not candidates:
        return None

    delta, start, rows, stats = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    window = fmt_time_window(start, start + HALF_HOUR)
    return make_finding(
        f"Recurring low-latency period near {window}",
        rows,
        [
            f"The {window} window had median WAN p95 of {num(stats['median_p95_ms'], ' ms')}, compared with overall median {num(overall_median, ' ms')}.",
            f"Raw-bad rate in that window was {pct(stats['raw_bad_rate_pct'])}.",
        ],
        day_count(rows),
        timing_ratio(day_count(rows), max(day_count(wan), 1)),
        abs(delta),
        len(wan),
        span_days(wan),
        [
            "Lower demand on the local or upstream path could be involved.",
            "The period may simply reflect the observed sample mix.",
            "Low-latency periods are useful as comparison context, not a diagnosis.",
        ],
    )


def turbulence_pattern(wan: list[SeriesPoint]) -> PatternFinding | None:
    buckets = [b for b in classify_buckets(wan) if b["is_turbulence"]]
    if not buckets:
        return None

    bucket_points = [
        point
        for point in wan
        if any(bucket["start"] <= point.ts < bucket["end"] for bucket in buckets)
    ]
    starts = [bucket["start"] for bucket in buckets]
    counts_by_window: dict[int, int] = {}
    for start in starts:
        minute = minute_of_day(start)
        counts_by_window[minute] = counts_by_window.get(minute, 0) + 1
    top = sorted(counts_by_window.items(), key=lambda item: (-item[1], item[0]))[:3]
    window_text = ", ".join(f"{fmt_time_window(start, start + policy.TURBULENCE_BUCKET_MINUTES)} ({count})" for start, count in top)
    recurrence = len({start.date() for start in starts})
    magnitude = 100.0 * len(buckets) / max(len(classify_buckets(wan)), 1)
    return make_finding(
        "Repeating turbulence windows",
        bucket_points,
        [
            f"{len(buckets)} non-sustained turbulence bucket(s) were found.",
            f"Most common turbulence windows: {window_text}.",
            "These are brief raw-bad clusters that did not meet the sustained slowdown rule.",
        ],
        recurrence,
        timing_ratio(max((count for _, count in top), default=0), max(len(buckets), 1)),
        magnitude,
        len(wan),
        span_days(wan),
        [
            "Brief congestion, transient Wi-Fi behavior, or route variability could contribute.",
            "The buckets did not persist long enough to be treated as operational incidents by themselves.",
            "Additional weeks are needed before calling this a stable rhythm.",
        ],
    )


def discovered_elevated_window(wan: list[SeriesPoint], existing_names: set[str]) -> PatternFinding | None:
    if len(wan) < 50:
        return None
    overall = point_stats(wan)
    overall_median = overall["median_p95_ms"]
    if overall_median is None:
        return None
    by_window: dict[int, list[SeriesPoint]] = {}
    for point in wan:
        start = (minute_of_day(point.ts) // HALF_HOUR) * HALF_HOUR
        if MORNING_START <= start < MORNING_END or AFTERNOON_START <= start < AFTERNOON_END:
            continue
        by_window.setdefault(start, []).append(point)

    candidates: list[tuple[float, int, list[SeriesPoint], dict[str, Any]]] = []
    for start, rows in by_window.items():
        if len(rows) < 8:
            continue
        stats = point_stats(rows)
        delta = relative_delta_pct(stats["median_p95_ms"], overall_median)
        if delta >= 25 or (stats["raw_bad_rate_pct"] or 0.0) >= 5:
            candidates.append((delta, start, rows, stats))
    if not candidates:
        return None
    delta, start, rows, stats = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    window = fmt_time_window(start, start + HALF_HOUR)
    name = f"Discovered elevated WAN window near {window}"
    if name in existing_names:
        return None
    return make_finding(
        name,
        rows,
        [
            f"The {window} window had median WAN p95 of {num(stats['median_p95_ms'], ' ms')}, compared with overall median {num(overall_median, ' ms')}.",
            f"Raw-bad rate in that window was {pct(stats['raw_bad_rate_pct'])}.",
        ],
        day_count(rows),
        timing_ratio(day_count(rows), max(day_count(wan), 1)),
        delta,
        len(wan),
        span_days(wan),
        [
            "This window was discovered from telemetry rather than pre-declared.",
            "It may reflect a recurring rhythm, a short-lived artifact, or limited sampling.",
            "More observations are needed before treating it as stable.",
        ],
    )


def int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def share_to_pct(value: Any) -> float | None:
    number = float_value(value)
    if number is None:
        return None
    if number <= 1.0:
        return number * 100.0
    return number


def concentration_confidence(share_pct: float, dominance_ratio: float | None, persistence_available: bool) -> str:
    if persistence_available and (share_pct >= 20 or (dominance_ratio is not None and dominance_ratio >= 5)):
        return "High"
    if share_pct >= 20 or (dominance_ratio is not None and dominance_ratio >= 5):
        return "Medium"
    return "Low"


def concentration_threshold_met(share_pct: float | None, dominance_ratio: float | None) -> bool:
    return (share_pct is not None and share_pct >= 20.0) or (
        dominance_ratio is not None and dominance_ratio >= 5.0
    )


def blocked_domain_threshold_met(share_pct: float | None, dominance_ratio: float | None) -> bool:
    return (share_pct is not None and share_pct >= 35.0) or (
        dominance_ratio is not None and dominance_ratio >= 5.0
    )


def entity_display_label(
    name: str | None,
    redacted: bool,
    fallback_label: str,
) -> str:
    if redacted or not name:
        return fallback_label
    return name


def top_entity_label(summary: dict[str, Any], count: int | None = None) -> str:
    top_entities = summary.get("top_entities")
    if not isinstance(top_entities, list):
        return "entity_1"

    for row in top_entities:
        if not isinstance(row, dict):
            continue
        if count is not None and int_value(row.get("count")) != count:
            continue
        label = str(row.get("label") or "").strip()
        if label:
            return label
    return "entity_1"


def top_entity_dominance(summary: dict[str, Any], count: int | None = None) -> float | None:
    top_entities = summary.get("top_entities")
    if isinstance(top_entities, list):
        for row in top_entities:
            if not isinstance(row, dict):
                continue
            if count is not None and int_value(row.get("count")) != count:
                continue
            dominance = float_value(row.get("dominance_ratio"))
            if dominance is not None:
                return dominance
    return float_value(summary.get("top_entity_dominance_ratio"))


def domain_name(summary: dict[str, Any], key: str) -> tuple[str | None, bool]:
    redacted = bool(summary.get(f"{key}_redacted"))
    raw_name = str(summary.get(key) or "").strip()
    return (None if redacted or not raw_name else raw_name, redacted)


def total_domain_signal(summary: dict[str, Any]) -> ConcentrationSignal | None:
    for field, label in (
        ("top_queried_domain", "queried"),
        ("top_resolved_domain", "resolved"),
    ):
        count = int_value(summary.get(f"{field}_count"))
        share_pct = share_to_pct(summary.get(f"{field}_share_of_total"))
        if share_pct is None:
            share_pct = share_to_pct(summary.get(f"{field}_share"))
        dominance = top_entity_dominance(summary, count=count)
        if count <= 0 or share_pct is None or not concentration_threshold_met(share_pct, dominance):
            continue

        name, redacted = domain_name(summary, field)
        entity_label = top_entity_label(summary, count=count) if redacted else (name or top_entity_label(summary, count=count))
        total = int_value(summary.get("total_queries"))
        if total <= 0 and share_pct > 0:
            total = int(round(count / (share_pct / 100.0)))

        display_name = entity_display_label(name, redacted, entity_label)
        observation = (
            "One redacted DNS domain accounted for an unusually large share of DNS activity."
            if redacted
            else "This domain accounted for an unusually large share of DNS activity."
        )
        return ConcentrationSignal(
            signal_kind=f"dns_{label}_domain",
            entity_label=entity_label,
            entity_type="DNS domain",
            name=name,
            name_redacted=redacted,
            count=count,
            total=total,
            share_pct=share_pct,
            dominance_ratio=dominance,
            share_label="Share of total DNS activity",
            observation=observation,
            review=(
                "Review recommended locally if this concentration is unexpected."
                if redacted
                else "Review recommended to determine whether this concentration is intentional."
            ),
            persistence="Not evaluated; only the current safe DNS summary is available.",
            confidence=concentration_confidence(share_pct, dominance, persistence_available=False),
        )

    return None


def blocked_domain_dominance(summary: dict[str, Any], count: int | None = None) -> float | None:
    for key in ("top_blocked_domains", "blocked_top_domains", "top_domains"):
        rows = summary.get(key)
        if not isinstance(rows, list):
            continue
        domain_rows = [row for row in rows if isinstance(row, dict)]
        if not domain_rows:
            continue
        first = domain_rows[0]
        if count is not None and int_value(first.get("count") or first.get("query_count")) != count:
            continue
        dominance = float_value(first.get("dominance_ratio"))
        if dominance is not None:
            return dominance
        if len(domain_rows) > 1:
            first_count = int_value(first.get("count") or first.get("query_count"))
            second_count = int_value(domain_rows[1].get("count") or domain_rows[1].get("query_count"))
            if first_count > 0 and second_count > 0:
                return first_count / second_count
    return None


def blocked_domain_signal(summary: dict[str, Any]) -> ConcentrationSignal | None:
    count = int_value(summary.get("top_blocked_domain_count"))
    share_pct = share_to_pct(
        summary.get("top_blocked_domain_share_of_blocked", summary.get("top_blocked_domain_share"))
    )
    dominance = blocked_domain_dominance(summary, count=count)
    if count <= 0 or share_pct is None or not blocked_domain_threshold_met(share_pct, dominance):
        return None

    name, redacted = domain_name(summary, "top_blocked_domain")
    entity_label = top_entity_label(summary, count=count) if redacted else (name or "blocked_domain_1")
    total = int_value(summary.get("blocked_queries"))
    if total <= 0 and share_pct > 0:
        total = int(round(count / (share_pct / 100.0)))

    observation = (
        "One redacted blocked DNS domain accounted for an unusually large share of blocked DNS activity."
        if redacted
        else "This domain accounted for an unusually large share of blocked DNS activity."
    )
    return ConcentrationSignal(
        signal_kind="dns_blocked_domain",
        entity_label=entity_label,
        entity_type="DNS domain",
        name=name,
        name_redacted=redacted,
        count=count,
        total=total,
        share_pct=share_pct,
        dominance_ratio=dominance,
        share_label="Share of blocked DNS activity",
        observation=observation,
        review=(
            "Review recommended locally if this concentration is unexpected."
            if redacted
            else "Review recommended to determine whether this concentration is intentional."
        ),
        persistence="Not evaluated; only the current safe DNS summary is available.",
        confidence=concentration_confidence(share_pct, dominance, persistence_available=False),
    )


def top_entity_signal(summary: dict[str, Any]) -> ConcentrationSignal | None:
    top_entities = summary.get("top_entities")
    entity_type = "DNS entity"
    if not isinstance(top_entities, list) or not top_entities:
        top_entities = summary.get("top_domains")
        entity_type = "DNS domain"
    if not isinstance(top_entities, list) or not top_entities:
        return None

    first = next((row for row in top_entities if isinstance(row, dict)), None)
    if first is None:
        return None

    label = str(first.get("label") or first.get("name") or "entity_1").strip() or "entity_1"
    redacted = bool(first.get("name_redacted"))
    raw_name = str(first.get("name") or "").strip()
    name = None if redacted or not raw_name else raw_name
    count = int_value(first.get("count"))
    share_pct = share_to_pct(first.get("share_of_total"))
    if share_pct is None:
        share_pct = share_to_pct(summary.get("top_entity_share"))
    dominance = float_value(first.get("dominance_ratio"))
    if dominance is None:
        dominance = float_value(summary.get("top_entity_dominance_ratio"))
    total = int_value(summary.get("total_queries"))
    if total <= 0 and count > 0 and share_pct and share_pct > 0:
        total = int(round(count / (share_pct / 100.0)))

    if count <= 0 or share_pct is None or not concentration_threshold_met(share_pct, dominance):
        return None

    return ConcentrationSignal(
        signal_kind="dns_entity",
        entity_label=label,
        entity_type=entity_type,
        name=name,
        name_redacted=redacted,
        count=count,
        total=total,
        share_pct=share_pct,
        dominance_ratio=dominance,
        share_label="Share of total DNS activity",
        observation=(
            "One redacted DNS entity accounted for an unusually large share of DNS activity."
            if redacted
            else "This DNS entity accounted for an unusually large share of DNS activity."
        ),
        review=(
            "Review recommended locally if this concentration is unexpected."
            if redacted
            else "Review recommended to determine whether this concentration is intentional."
        ),
        persistence="Not evaluated; only the current safe DNS summary is available.",
        confidence=concentration_confidence(share_pct, dominance, persistence_available=False),
    )


EXPECTED_BLOCK_REASON_NAMES = {
    "oisd",
    "easylist",
    "hagezi",
    "ha gezi",
    "adguard",
}


def obvious_expected_block_reason(name: str) -> bool:
    normalized = name.lower()
    return any(marker in normalized for marker in EXPECTED_BLOCK_REASON_NAMES)


def top_reason_signal(summary: dict[str, Any]) -> ConcentrationSignal | None:
    top_reasons = summary.get("top_reasons")
    if not isinstance(top_reasons, list):
        return None

    peers: list[tuple[str, int]] = []
    for row in top_reasons:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        count = int_value(row.get("queries"))
        if name and count > 0:
            peers.append((name, count))

    if not peers:
        return None

    peers.sort(key=lambda item: (-item[1], item[0]))
    top_name, top_count = peers[0]
    next_count = peers[1][1] if len(peers) > 1 else 0
    peer_total = sum(count for _, count in peers)
    blocked_total = int_value(summary.get("blocked_queries"))
    total = max(blocked_total, peer_total)
    if total <= 0:
        return None

    share = min(100.0, 100.0 * top_count / total)
    dominance = (top_count / next_count) if next_count > 0 else None
    if share < 50 or (dominance is not None and dominance < 2):
        return None
    if obvious_expected_block_reason(top_name) and share < 85 and (dominance is None or dominance < 5):
        return None

    return ConcentrationSignal(
        signal_kind="blocked_reason",
        entity_label=top_name,
        entity_type="Blocked DNS reason",
        name=top_name,
        name_redacted=False,
        count=top_count,
        total=total,
        share_pct=share,
        dominance_ratio=dominance,
        share_label="Share of available blocked-reason activity",
        observation=f"{top_name} represented {pct(share)} of available blocked-reason activity.",
        review="Review recommended only if this concentration is unexpected.",
        persistence="Not evaluated; only the current safe DNS summary is available.",
        confidence=concentration_confidence(share, dominance, persistence_available=False),
    )


def analyze_concentration(dns: dict[str, Any] | None) -> dict[str, Any]:
    if not dns or dns.get("status") != "ok" or not isinstance(dns.get("summary"), dict):
        return {
            "signals": [],
            "message": "No concentration signals were evaluated because the available exported summaries do not include safe top-N entity data.",
        }

    summary = dns["summary"]
    has_concentration_shape = any(
        [
            summary.get("top_queried_domain_count") is not None,
            summary.get("top_resolved_domain_count") is not None,
            summary.get("top_blocked_domain_count") is not None,
            isinstance(summary.get("top_entities"), list),
            isinstance(summary.get("top_domains"), list),
            isinstance(summary.get("top_reasons"), list),
        ]
    )
    if not has_concentration_shape:
        return {
            "signals": [],
            "message": "No concentration signals were evaluated because the available exported summaries do not include safe top-N entity data.",
        }

    domain = total_domain_signal(summary)
    if domain is not None:
        return {"signals": [domain], "message": None}

    blocked_domain = blocked_domain_signal(summary)
    if blocked_domain is not None:
        return {"signals": [blocked_domain], "message": None}

    entity = top_entity_signal(summary)
    if entity is not None:
        return {"signals": [entity], "message": None}

    reason = top_reason_signal(summary)
    if reason is not None:
        return {"signals": [reason], "message": None}

    return {
        "signals": [],
        "message": "No concentration signal met the deterministic threshold in the available safe exported summaries.",
    }


def analyze_patterns(
    observations: list[Observation],
    history: dict[str, Any] | None = None,
    dns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lan, wan = collapse_series(observations)
    findings: list[PatternFinding] = []
    support_notes: list[str] = []
    history = dict(history or {})

    weekday_finding, weekday_note = weekday_weekend_pattern(wan)
    support_notes.append(weekday_note)
    if weekday_finding:
        findings.append(weekday_finding)

    for candidate in (
        business_hour_pattern(wan),
        window_elevation_pattern(wan, "Morning ramp", MORNING_START, MORNING_END),
        window_elevation_pattern(wan, "Afternoon ramp", AFTERNOON_START, AFTERNOON_END),
        lan_wan_coelevation_pattern(wan, lan),
        low_latency_pattern(wan),
        turbulence_pattern(wan),
    ):
        if candidate:
            findings.append(candidate)

    discovered = discovered_elevated_window(wan, {finding.name for finding in findings})
    if discovered:
        findings.append(discovered)

    findings.sort(
        key=lambda finding: (
            {"High": 0, "Medium": 1, "Low": 2}.get(finding.confidence, 3),
            -finding.confidence_inputs["total_points"],
            finding.name,
        )
    )

    return {
        "generated_at": dt.datetime.now().astimezone(),
        "window_start": min((p.ts for p in wan + lan), default=None),
        "window_end": max((p.ts for p in wan + lan), default=None),
        "sample_counts": {
            "rows": len(observations),
            "wan_points": len(wan),
            "lan_points": len(lan),
            "days": span_days(wan + lan),
            "weekday_days": len({p.ts.date() for p in wan if p.ts.weekday() < 5}),
            "weekend_days": len({p.ts.date() for p in wan if p.ts.weekday() >= 5}),
        },
        "wan_summary": point_stats(wan),
        "lan_summary": point_stats(lan),
        "findings": findings,
        "concentration": analyze_concentration(dns),
        "support_notes": support_notes,
        "history": history,
    }


def report_date(analysis: dict[str, Any]) -> str:
    end = analysis.get("window_end")
    if isinstance(end, dt.datetime):
        return end.strftime("%Y-%m-%d")
    return dt.date.today().isoformat()


def confidence_line(finding: PatternFinding) -> str:
    inputs = finding.confidence_inputs
    return (
        f"recurrence {inputs['recurrence_points']}/3, timing {inputs['timing_points']}/3, "
        f"magnitude {inputs['magnitude_points']}/3, sample size {inputs['sample_points']}/3, "
        f"duration {inputs['duration_points']}/3; total {inputs['total_points']}/15"
    )


def render_concentration_section(concentration: dict[str, Any]) -> list[str]:
    lines = ["## Concentration Signals", ""]
    signals: list[ConcentrationSignal] = concentration.get("signals") or []
    if not signals:
        lines.extend([concentration.get("message") or "No concentration signal met the deterministic threshold.", ""])
        return lines

    for signal in signals:
        ratio = "n/a" if signal.dominance_ratio is None else f"{signal.dominance_ratio:.1f}x"
        if signal.signal_kind.startswith("dns_"):
            display_name = entity_display_label(signal.name, signal.name_redacted, signal.entity_label)
            name_line = (
                "Name: redacted by Prime Observer privacy settings"
                if signal.name_redacted
                else f"Name: {display_name}"
            )
            lines.extend(
                [
                    f"### Concentration: {display_name}",
                    "",
                    f"- Entity label: {signal.entity_label}",
                    f"- Entity type: {signal.entity_type}",
                    f"- {name_line}",
                    f"- Count: {signal.count}",
                    f"- {signal.share_label}: {pct(signal.share_pct)}",
                    f"- Dominance ratio vs next peer: {ratio}",
                    f"- Persistence: {signal.persistence}",
                    f"- Confidence: {signal.confidence}",
                    f"- {signal.observation}",
                    f"- {signal.review}",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"### Concentration: {signal.entity_label} block reason",
                    "",
                    f"- Entity type: {signal.entity_type}",
                    f"- Count: {signal.count}",
                    f"- Share of available blocked-reason activity: {pct(signal.share_pct)}",
                    f"- Dominance ratio vs next peer: {ratio}",
                    f"- Persistence: {signal.persistence}",
                    f"- Confidence: {signal.confidence}",
                    f"- {signal.observation}",
                    f"- {signal.review}",
                    "",
                ]
            )
    return lines


def render_pattern_report(analysis: dict[str, Any]) -> str:
    findings: list[PatternFinding] = analysis["findings"]
    counts = analysis["sample_counts"]
    wan = analysis["wan_summary"]
    history = analysis.get("history") or {}
    source_files = history.get("source_files") or []
    date = report_date(analysis)

    if findings:
        summary = (
            f"Core Signal found {len(findings)} observed pattern candidate(s) in "
            f"{counts['wan_points']} WAN points across {counts['days']} day(s). "
            "These are observations for evidence tracking, not signatures or root-cause claims."
        )
    else:
        summary = (
            f"Core Signal did not find enough repeated behavior to name an observed recurring pattern in "
            f"{counts['wan_points']} WAN points across {counts['days']} day(s)."
        )

    lines = [
        f"# Core Signal Pattern Report - {date}",
        "",
        "## Executive Summary",
        "",
        summary,
        "",
        "## Observed Recurring Patterns",
        "",
    ]

    if not findings:
        lines.extend(["No observed recurring patterns met the initial deterministic thresholds.", ""])
    for finding in findings:
        status = "Yes" if finding.approaching_signature else "No"
        lines.extend(
            [
                f"### {finding.name}",
                "",
                f"- First observed: {fmt_ts(finding.first_observed)}",
                f"- Last observed: {fmt_ts(finding.last_observed)}",
                f"- Observation count: {finding.observation_count}",
                f"- Confidence: {finding.confidence}",
                f"- Approaching signature status: {status}",
                "- Evidence:",
            ]
        )
        lines.extend(f"  - {item}" for item in finding.evidence)
        lines.append("- Possible explanations:")
        lines.extend(f"  - {item}" for item in finding.possible_explanations)
        lines.append("")

    lines.extend(render_concentration_section(analysis.get("concentration") or {}))

    lines.extend(
        [
            "## Supporting Evidence",
            "",
            f"- Observation window: {fmt_ts(analysis.get('window_start'))} to {fmt_ts(analysis.get('window_end'))}",
            f"- History directory: {history.get('history_dir') or 'n/a'}",
            f"- Requested history window: {history.get('requested_days') or 'n/a'} day(s)",
            f"- History files read: {len(source_files)} of {history.get('files_available', 'n/a')} available bakeoff file(s)",
            f"- Date range analyzed: {fmt_ts(history.get('window_start') or analysis.get('window_start'))} to {fmt_ts(history.get('window_end') or analysis.get('window_end'))}",
            f"- Export rows analyzed: {counts['rows']}",
            f"- WAN points analyzed: {counts['wan_points']}",
            f"- LAN points analyzed: {counts['lan_points']}",
            f"- Calendar coverage: {counts['days']} day(s), including {counts['weekday_days']} weekday day(s) and {counts['weekend_days']} weekend day(s).",
            f"- Overall WAN median p95: {num(wan['median_p95_ms'], ' ms')}",
            f"- Overall WAN p95-of-p95: {num(wan['p95_of_p95_ms'], ' ms')}",
            f"- Overall WAN raw-bad rate: {pct(wan['raw_bad_rate_pct'])}",
            f"- Overall WAN sustained-bad rate: {pct(wan['sustained_bad_rate_pct'])}",
        ]
    )
    lines.extend(f"- {note}" for note in analysis.get("support_notes", []))
    lines.extend(
        [
            "",
            "## Confidence Assessment",
            "",
            "Confidence is deterministic and based on recurrence count, timing consistency, magnitude of effect, sample size, and observation duration. Scores map to Low, Medium, or High. Fewer than 14 calendar days caps confidence at Low; 14-29 days caps confidence at Medium; 30+ days allows the normal confidence model.",
            "",
        ]
    )
    if findings:
        for finding in findings:
            lines.append(f"- {finding.name}: {finding.confidence} ({confidence_line(finding)}).")
    else:
        lines.append("- No confidence scores were produced because no observed patterns passed the initial thresholds.")

    lines.extend(
        [
            "",
            "## Questions Worth Investigating",
            "",
            "- Do the named windows continue to appear in future weekly reports?",
            "- Do LAN/WAN co-elevations recur at the same times, or were they isolated?",
            "- Do low-latency periods remain stable enough to become useful comparison baselines?",
            "- Are any recurring windows explainable by known local schedules without assuming user impact?",
            "",
            "## Recommendation",
            "",
        ]
    )
    if findings:
        lines.append(
            "Continue weekly pattern reporting and keep these as observed recurring patterns until recurrence, confidence, and timing stability are stronger. Do not promote any pattern to a signature in this release."
        )
    else:
        lines.append(
            "Continue collecting weekly reports. Current history is not sufficient to name a stable rhythm or approach signature status."
        )
    lines.append("")
    return "\n".join(lines)


def write_pattern_reports(markdown: str, patterns_dir: Path, date: str) -> tuple[Path, Path]:
    patterns_dir.mkdir(parents=True, exist_ok=True)
    dated = patterns_dir / f"{date}-pattern-report.md"
    latest = patterns_dir / "latest.md"
    dated.write_text(markdown)
    latest.write_text(markdown)
    return dated, latest
