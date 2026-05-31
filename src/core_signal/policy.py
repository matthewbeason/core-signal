"""Prime Observer v0.4.1-aligned interpretation policy.

These constants intentionally mirror the public dashboard concepts observed in
Prime Observer v0.4.1. Core Signal keeps them here so drift is visible and
reviewable instead of being scattered through analysis code.
"""

GATEWAY_HOST = "192.168.1.1"
WAN_HOSTS = {"1.1.1.1", "9.9.9.9"}

WAN_BAD_P95_MS = 140.0
WAN_BAD_JITTER_MS = 50.0
WAN_BAD_LOSS_PCT = 1.0
WAN_BAD_PERSISTENCE = 2

TURBULENCE_BUCKET_MINUTES = 15
TURBULENCE_MIN_RAW_BAD = 4

LAN_ELEVATED_P95_MS = 120.0
ATTRIBUTION_MINUTES = 15

REQUIRED_CSV_HEADERS = {"ts", "host", "p95_ms"}
OPTIONAL_CSV_HEADERS = {
    "phase_label",
    "jitter_ms",
    "loss_pct",
    "baseline_p95",
    "baseline_delta_pct",
    "baseline_sample_count",
}

