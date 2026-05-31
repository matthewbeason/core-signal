# Core Signal Morning Brief - 2026-05-30

Observation window: 2026-05-29 17:02 UTC-07:00 to 2026-05-30 17:02 UTC-07:00

## Summary

WAN had sustained degradation in 1 15-minute bucket(s), while the most recent attribution state is no network issue detected.

- WAN samples analyzed: 1413 from 4170 export rows.
- WAN p95 median: 34.7 ms; p95-of-p95: 47.8 ms; jitter 95th: 11.9 ms.
- Sustained bad rate: 0.1%; bad minutes/hour: 0.1.

## What Happened?

The latest 24-hour export was interpreted using Prime Observer's WAN thresholds: p95 latency above 140 ms, jitter above 50 ms, or packet loss above 1%. A sustained bad moment requires two consecutive raw bad WAN samples.

Core Signal found 1 sustained degradation bucket(s). These are the intervals most likely to have been noticeable.
First sustained interval(s): 2026-05-30 13:00 UTC-07:00-13:15.

## What Was Unusual?

Latest baseline comparison: Better than usual for this time of day (-48.5% vs baseline, high confidence from 1455 samples).
There were also 5 isolated raw bad sample(s) outside sustained streaks.

## What Deserves Attention?

Recent attribution: No network issue detected (High confidence). LAN and WAN both looked stable in the last 15 minutes of the export.
Review the sustained degradation buckets first, especially if they overlap with user-impacting work, calls, streaming, or gaming.

## DNS / Security Context

NextDNS context is available: 2.1% blocked across 175979 queries; encrypted DNS rate 5.4%.
Top block reasons: OISD (3622), Native Tracking (Sonos) (138), Native Tracking (Amazon Alexa) (132).

## What Can Be Safely Ignored?

Calm buckets and isolated raw spikes outside sustained intervals can be ignored unless users reported symptoms at those exact times.
The latest baseline comparison was better than usual, so elevated-baseline concern can be ignored for this run.

## Evidence

- FIBER: 1413 samples; median p95 34.7 ms; p95-of-p95 47.8 ms; sustained bad 0.1%.
- Turbulence buckets: 0; sustained degradation buckets: 1.
- Runtime is deterministic and uses only local Prime Observer exports.
