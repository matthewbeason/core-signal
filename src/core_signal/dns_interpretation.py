from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any


DEFAULT_DNS_HISTORY = Path("data/dns_observations.jsonl")
TOTAL_ACTIVITY_SHARE_THRESHOLD = 20.0
TOTAL_ACTIVITY_DOMINANCE_THRESHOLD = 5.0
BLOCKED_DOMAIN_SHARE_THRESHOLD = 35.0
BLOCKED_REASON_SHARE_THRESHOLD = 50.0
EXPECTED_BLOCK_REASON_SHARE_THRESHOLD = 85.0
MIN_HISTORY_FOR_COMPARISON = 3

EXPECTED_BLOCK_REASON_NAMES = {
    "adguard",
    "easylist",
    "hagezi",
    "ha gezi",
    "oisd",
}


@dataclass(frozen=True)
class DNSObservation:
    source_generated_at: str | None
    window: str | None
    status: str
    total_queries: int
    blocked_queries: int
    dns_block_rate: float | None
    dns_encrypted_rate: float | None
    top_total_label: str | None
    top_total_type: str | None
    top_total_name_redacted: bool
    top_total_share: float | None
    top_total_dominance_ratio: float | None
    top_blocked_label: str | None
    top_blocked_name_redacted: bool
    top_blocked_share_of_blocked: float | None
    top_blocked_share_of_total: float | None
    top_blocked_dominance_ratio: float | None
    top_reason_label: str | None
    top_reason_share_of_blocked: float | None
    top_reason_dominance_ratio: float | None


@dataclass(frozen=True)
class DNSFinding:
    title: str
    status: str
    confidence: str
    finding_type: str
    why_it_matters: str
    recommendation: str
    evidence: list[str]


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


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}x"


def observation_key(observation: DNSObservation) -> tuple[str | None, str | None]:
    return (observation.source_generated_at, observation.window)


def safe_label(name: str | None, redacted: bool, fallback: str | None) -> str | None:
    if redacted:
        return fallback
    return name or fallback


def domain_name(summary: dict[str, Any], key: str) -> tuple[str | None, bool]:
    redacted = bool(summary.get(f"{key}_redacted"))
    raw_name = str(summary.get(key) or "").strip()
    return (None if redacted or not raw_name else raw_name, redacted)


def first_top_entity(summary: dict[str, Any]) -> dict[str, Any]:
    rows = summary.get("top_entities")
    if not isinstance(rows, list):
        return {}
    return next((row for row in rows if isinstance(row, dict)), {})


def top_entity_label(summary: dict[str, Any], count: int | None = None) -> str:
    rows = summary.get("top_entities")
    if not isinstance(rows, list):
        return "entity_1"
    for row in rows:
        if not isinstance(row, dict):
            continue
        if count is not None and int_value(row.get("count")) != count:
            continue
        label = str(row.get("label") or "").strip()
        if label:
            return label
    return "entity_1"


def top_entity_dominance(summary: dict[str, Any], count: int | None = None) -> float | None:
    rows = summary.get("top_entities")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if count is not None and int_value(row.get("count")) != count:
                continue
            dominance = float_value(row.get("dominance_ratio"))
            if dominance is not None:
                return dominance
    return float_value(summary.get("top_entity_dominance_ratio"))


def blocked_domain_dominance(summary: dict[str, Any], count: int | None = None) -> float | None:
    for key in ("top_blocked_domains", "blocked_top_domains"):
        rows = summary.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        first = rows[0] if isinstance(rows[0], dict) else {}
        if count is not None and int_value(first.get("count") or first.get("query_count")) != count:
            continue
        dominance = float_value(first.get("dominance_ratio"))
        if dominance is not None:
            return dominance
        if len(rows) > 1 and isinstance(rows[1], dict):
            first_count = int_value(first.get("count") or first.get("query_count"))
            second_count = int_value(rows[1].get("count") or rows[1].get("query_count"))
            if first_count > 0 and second_count > 0:
                return first_count / second_count
    return None


def reason_dominance(summary: dict[str, Any]) -> float | None:
    rows = summary.get("top_reasons")
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    first = rows[0] if isinstance(rows[0], dict) else {}
    second = rows[1] if isinstance(rows[1], dict) else {}
    first_count = int_value(first.get("queries"))
    second_count = int_value(second.get("queries"))
    if first_count <= 0 or second_count <= 0:
        return None
    return first_count / second_count


def normalize_dns_summary(dns: dict[str, Any] | None) -> DNSObservation | None:
    if not dns or not isinstance(dns.get("summary"), dict):
        return None

    summary = dns["summary"]
    status = str(dns.get("status") or "unknown").strip() or "unknown"
    total_queries = int_value(summary.get("total_queries"))
    blocked_queries = int_value(summary.get("blocked_queries"))

    queried_count = int_value(summary.get("top_queried_domain_count"))
    queried_name, queried_redacted = domain_name(summary, "top_queried_domain")
    queried_share = share_to_pct(summary.get("top_queried_domain_share"))

    resolved_count = int_value(summary.get("top_resolved_domain_count"))
    resolved_name, resolved_redacted = domain_name(summary, "top_resolved_domain")
    resolved_share = share_to_pct(summary.get("top_resolved_domain_share_of_total"))
    if resolved_share is None:
        resolved_share = share_to_pct(summary.get("top_resolved_domain_share"))

    entity = first_top_entity(summary)
    entity_count = int_value(entity.get("count"))
    entity_label = str(entity.get("label") or "entity_1").strip() or "entity_1"
    entity_redacted = bool(entity.get("name_redacted"))
    entity_name = None if entity_redacted else str(entity.get("name") or "").strip() or None
    entity_share = share_to_pct(entity.get("share_of_total"))
    if entity_share is None:
        entity_share = share_to_pct(summary.get("top_entity_share"))
    entity_dominance = float_value(entity.get("dominance_ratio"))
    if entity_dominance is None:
        entity_dominance = float_value(summary.get("top_entity_dominance_ratio"))

    total_candidates = [
        (
            safe_label(queried_name, queried_redacted, top_entity_label(summary, queried_count)),
            "DNS domain",
            queried_redacted,
            queried_share,
            top_entity_dominance(summary, queried_count),
        ),
        (
            safe_label(resolved_name, resolved_redacted, top_entity_label(summary, resolved_count)),
            "DNS domain",
            resolved_redacted,
            resolved_share,
            top_entity_dominance(summary, resolved_count),
        ),
        (
            safe_label(entity_name, entity_redacted, entity_label),
            "DNS entity",
            entity_redacted,
            entity_share,
            entity_dominance,
        ),
    ]
    total_candidates = [candidate for candidate in total_candidates if candidate[0] and candidate[3] is not None]
    top_total = max(total_candidates, key=lambda item: item[3] or 0.0, default=(None, None, False, None, None))

    blocked_count = int_value(summary.get("top_blocked_domain_count"))
    blocked_name, blocked_redacted = domain_name(summary, "top_blocked_domain")
    reason_label = str(summary.get("top_blocked_reason") or "").strip() or None
    reason_queries = int_value(summary.get("top_blocked_reason_queries"))
    reasons = summary.get("top_reasons")
    if (not reason_label or reason_queries <= 0) and isinstance(reasons, list) and reasons:
        first_reason = reasons[0] if isinstance(reasons[0], dict) else {}
        reason_label = str(first_reason.get("name") or "").strip() or reason_label
        reason_queries = int_value(first_reason.get("queries")) or reason_queries

    return DNSObservation(
        source_generated_at=str(dns.get("generated_at") or "").strip() or None,
        window=str(dns.get("window") or "").strip() or None,
        status=status,
        total_queries=total_queries,
        blocked_queries=blocked_queries,
        dns_block_rate=share_to_pct(summary.get("dns_block_rate")),
        dns_encrypted_rate=share_to_pct(summary.get("dns_encrypted_rate")),
        top_total_label=top_total[0],
        top_total_type=top_total[1],
        top_total_name_redacted=bool(top_total[2]),
        top_total_share=top_total[3],
        top_total_dominance_ratio=top_total[4],
        top_blocked_label=safe_label(blocked_name, blocked_redacted, top_entity_label(summary, blocked_count)),
        top_blocked_name_redacted=blocked_redacted,
        top_blocked_share_of_blocked=share_to_pct(
            summary.get("top_blocked_domain_share_of_blocked", summary.get("top_blocked_domain_share"))
        ),
        top_blocked_share_of_total=share_to_pct(summary.get("top_blocked_domain_share_of_total")),
        top_blocked_dominance_ratio=blocked_domain_dominance(summary, blocked_count),
        top_reason_label=reason_label,
        top_reason_share_of_blocked=(
            min(100.0, 100.0 * reason_queries / blocked_queries)
            if reason_queries > 0 and blocked_queries > 0
            else None
        ),
        top_reason_dominance_ratio=reason_dominance(summary),
    )


def read_dns_history(path: Path) -> list[DNSObservation]:
    if not path.exists():
        return []
    observations: list[DNSObservation] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            observations.append(DNSObservation(**payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return observations


def append_dns_observation(path: Path, observation: DNSObservation, prior: list[DNSObservation]) -> bool:
    key = observation_key(observation)
    if key[0] and key in {observation_key(item) for item in prior}:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(asdict(observation), sort_keys=True) + "\n")
    return True


def observation_history_event(dns: dict[str, Any] | None, history_path: Path) -> tuple[DNSObservation | None, list[DNSObservation], bool]:
    current = normalize_dns_summary(dns)
    prior = read_dns_history(history_path)
    appended = False
    if current is not None:
        appended = append_dns_observation(history_path, current, prior)
    return current, prior, appended


def expected_block_reason(name: str | None) -> bool:
    normalized = (name or "").lower()
    return any(marker in normalized for marker in EXPECTED_BLOCK_REASON_NAMES)


def history_occurrences(prior: list[DNSObservation], field: str, label: str | None) -> int:
    if not label:
        return 0
    return sum(1 for item in prior if getattr(item, field) == label)


def history_shares(prior: list[DNSObservation], field: str, label: str | None) -> list[float]:
    if not label:
        return []
    label_fields = {
        "top_total_share": "top_total_label",
        "top_blocked_share_of_blocked": "top_blocked_label",
        "top_reason_share_of_blocked": "top_reason_label",
    }
    shares: list[float] = []
    label_field = label_fields.get(field)
    if label_field is None:
        return shares
    for item in prior:
        if getattr(item, label_field, None) != label:
            continue
        value = getattr(item, field, None)
        if isinstance(value, (int, float)):
            shares.append(float(value))
    return shares


def confidence_from_history(prior_count: int, occurrences: int, current_share: float | None, dominance: float | None) -> str:
    if prior_count < MIN_HISTORY_FOR_COMPARISON:
        return "Low"
    if occurrences >= 2 and (
        (current_share is not None and current_share >= TOTAL_ACTIVITY_SHARE_THRESHOLD)
        or (dominance is not None and dominance >= TOTAL_ACTIVITY_DOMINANCE_THRESHOLD)
    ):
        return "Medium"
    return "Low"


def total_activity_finding(current: DNSObservation, prior: list[DNSObservation]) -> DNSFinding | None:
    if not current.top_total_label:
        return None
    share = current.top_total_share
    dominance = current.top_total_dominance_ratio
    if not (
        (share is not None and share >= TOTAL_ACTIVITY_SHARE_THRESHOLD)
        or (dominance is not None and dominance >= TOTAL_ACTIVITY_DOMINANCE_THRESHOLD)
    ):
        return None

    prior_count = len(prior)
    occurrences = history_occurrences(prior, "top_total_label", current.top_total_label)
    shares = history_shares(prior, "top_total_share", current.top_total_label)
    status = "observed"
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        status = "recurring" if occurrences >= 2 else "new in local history"
    if current.top_total_name_redacted:
        why = "One redacted DNS entity accounted for a large share of total DNS activity."
        recommendation = "Review locally if this concentration is unexpected; Prime Observer redacted the name by privacy settings."
    else:
        why = "One DNS domain or entity accounted for a large share of total DNS activity."
        recommendation = "Review locally to determine whether this concentration is intentional."

    evidence = [
        f"Current top total-activity label: {current.top_total_label}",
        f"Share of total DNS activity: {pct(share)}",
        f"Dominance ratio vs next peer: {ratio(dominance)}",
        f"Prior local DNS observations available: {prior_count}",
    ]
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        evidence.append(f"Same label appeared in {occurrences} prior observation(s).")
        if shares:
            evidence.append(f"Median prior share for this label: {pct(median(shares))}")
    else:
        evidence.append("Local DNS history is insufficient for recurrence comparison.")

    return DNSFinding(
        title=f"Total DNS concentration: {current.top_total_label}",
        status=status,
        confidence=confidence_from_history(prior_count, occurrences, share, dominance),
        finding_type="total_activity_concentration",
        why_it_matters=why,
        recommendation=recommendation,
        evidence=evidence,
    )


def blocked_domain_finding(current: DNSObservation, prior: list[DNSObservation]) -> DNSFinding | None:
    share = current.top_blocked_share_of_blocked
    dominance = current.top_blocked_dominance_ratio
    if not current.top_blocked_label:
        return None
    if not (
        (share is not None and share >= BLOCKED_DOMAIN_SHARE_THRESHOLD)
        or (dominance is not None and dominance >= TOTAL_ACTIVITY_DOMINANCE_THRESHOLD)
    ):
        return None

    prior_count = len(prior)
    occurrences = history_occurrences(prior, "top_blocked_label", current.top_blocked_label)
    status = "observed"
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        status = "recurring" if occurrences >= 2 else "new in local history"
    why = "One blocked DNS domain accounted for a large share of blocked DNS activity."
    recommendation = "Review locally if this blocked-domain concentration is unexpected."
    if current.top_blocked_name_redacted:
        why = "One redacted blocked DNS domain accounted for a large share of blocked DNS activity."
        recommendation = "Review locally if this concentration is unexpected; Prime Observer redacted the name by privacy settings."

    evidence = [
        f"Current top blocked-domain label: {current.top_blocked_label}",
        f"Share of blocked DNS activity: {pct(share)}",
        f"Dominance ratio vs next peer: {ratio(dominance)}",
        f"Prior local DNS observations available: {prior_count}",
    ]
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        evidence.append(f"Same blocked-domain label appeared in {occurrences} prior observation(s).")
    else:
        evidence.append("Local DNS history is insufficient for recurrence comparison.")

    return DNSFinding(
        title=f"Blocked DNS concentration: {current.top_blocked_label}",
        status=status,
        confidence=confidence_from_history(prior_count, occurrences, share, dominance),
        finding_type="blocked_domain_concentration",
        why_it_matters=why,
        recommendation=recommendation,
        evidence=evidence,
    )


def blocked_reason_finding(current: DNSObservation, prior: list[DNSObservation]) -> DNSFinding | None:
    share = current.top_reason_share_of_blocked
    dominance = current.top_reason_dominance_ratio
    if not current.top_reason_label:
        return None
    if share is None or share < BLOCKED_REASON_SHARE_THRESHOLD:
        return None
    if expected_block_reason(current.top_reason_label) and share < EXPECTED_BLOCK_REASON_SHARE_THRESHOLD and (
        dominance is None or dominance < TOTAL_ACTIVITY_DOMINANCE_THRESHOLD
    ):
        return None

    prior_count = len(prior)
    occurrences = history_occurrences(prior, "top_reason_label", current.top_reason_label)
    status = "observed"
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        status = "recurring" if occurrences >= 2 else "new in local history"
    evidence = [
        f"Current top blocked reason: {current.top_reason_label}",
        f"Share of blocked DNS activity: {pct(share)}",
        f"Dominance ratio vs next peer: {ratio(dominance)}",
        f"Prior local DNS observations available: {prior_count}",
    ]
    if prior_count >= MIN_HISTORY_FOR_COMPARISON:
        evidence.append(f"Same blocked reason appeared in {occurrences} prior observation(s).")
    else:
        evidence.append("Local DNS history is insufficient for recurrence comparison.")

    return DNSFinding(
        title=f"Blocked DNS reason concentration: {current.top_reason_label}",
        status=status,
        confidence=confidence_from_history(prior_count, occurrences, share, dominance),
        finding_type="blocked_reason_concentration",
        why_it_matters="One blocked DNS reason accounted for a large share of blocked DNS activity.",
        recommendation="Review locally only if this concentration is unexpected.",
        evidence=evidence,
    )


def history_context_finding(current: DNSObservation | None, prior: list[DNSObservation], appended: bool) -> DNSFinding:
    if current is None:
        return DNSFinding(
            title="DNS history context",
            status="not evaluated",
            confidence="Low",
            finding_type="history_context",
            why_it_matters="Core Signal did not receive a usable Prime Observer DNS summary for this pattern-report run.",
            recommendation="Keep the Prime Observer DNS summary export available if DNS interpretation is desired.",
            evidence=["No privacy-safe DNS observation was appended."],
        )
    if appended:
        append_note = "Current observation appended to local DNS history."
    elif observation_key(current) in {observation_key(item) for item in prior}:
        append_note = "Current observation was already present in local DNS history."
    else:
        append_note = "Current observation was not appended by this render path."

    evidence = [
        f"Current DNS summary generated at: {current.source_generated_at or 'unknown'}",
        f"Current DNS summary window: {current.window or 'unknown'}",
        f"Prior local DNS observations available: {len(prior)}",
        append_note,
    ]
    return DNSFinding(
        title="DNS history context",
        status="insufficient history" if len(prior) < MIN_HISTORY_FOR_COMPARISON else "history available",
        confidence="Low" if len(prior) < MIN_HISTORY_FOR_COMPARISON else "Medium",
        finding_type="history_context",
        why_it_matters="DNS interpretation needs repeated local observations before recurrence can be evaluated.",
        recommendation=(
            "Continue weekly pattern-report runs to build local DNS history."
            if len(prior) < MIN_HISTORY_FOR_COMPARISON
            else "Use local history comparison as context, not as a root-cause claim."
        ),
        evidence=evidence,
    )


def analyze_dns_interpretation(
    current: DNSObservation | None,
    prior: list[DNSObservation],
    appended: bool = False,
) -> dict[str, Any]:
    findings = [history_context_finding(current, prior, appended)]
    if current is None or current.status != "ok":
        return {"findings": findings}

    specific_finding = (
        total_activity_finding(current, prior)
        or blocked_domain_finding(current, prior)
        or blocked_reason_finding(current, prior)
    )
    if specific_finding is not None:
        findings.append(specific_finding)
    else:
        evidence = [
            f"Top total-activity share: {pct(current.top_total_share)}",
            f"Top total-activity dominance ratio: {ratio(current.top_total_dominance_ratio)}",
            f"Top blocked-domain share of blocked activity: {pct(current.top_blocked_share_of_blocked)}",
            f"Top blocked-reason share of blocked activity: {pct(current.top_reason_share_of_blocked)}",
        ]
        findings.append(
            DNSFinding(
                title="DNS concentration check",
                status="no current concentration signal",
                confidence="Low" if len(prior) < MIN_HISTORY_FOR_COMPARISON else "Medium",
                finding_type="threshold_check",
                why_it_matters="Core Signal checked the current privacy-safe DNS summary without treating one summary as a stable pattern.",
                recommendation="No DNS-specific action is suggested from this weekly report.",
                evidence=evidence,
            )
        )
    return {"findings": findings}


def render_dns_interpretation_section(interpretation: dict[str, Any]) -> list[str]:
    lines = ["## DNS Interpretation", ""]
    findings: list[DNSFinding] = interpretation.get("findings") or []
    if not findings:
        lines.extend(
            [
                "No DNS interpretation findings were evaluated because no usable privacy-safe DNS summary was available.",
                "",
            ]
        )
        return lines

    for finding in findings:
        lines.extend(
            [
                f"### {finding.title}",
                "",
                f"- Status: {finding.status}",
                f"- Finding type: {finding.finding_type}",
                f"- Confidence: {finding.confidence}",
                f"- Why it matters: {finding.why_it_matters}",
                f"- Recommendation: {finding.recommendation}",
                "- Evidence:",
            ]
        )
        lines.extend(f"  - {item}" for item in finding.evidence)
        lines.append("")
    return lines
