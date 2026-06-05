from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from core_signal.analyze import analyze
from core_signal.brief import render_brief, report_date, write_reports
from core_signal.ingest import (
    CsvSchemaError,
    Observation,
    latest_window,
    load_pattern_history,
    read_dns_summary,
    read_observations,
)
from core_signal.patterns import (
    analyze_concentration,
    analyze_patterns,
    confidence_from_inputs,
    render_pattern_report,
    write_pattern_reports,
)


UTC = dt.timezone.utc


def obs(
    minute: int,
    host: str,
    p95: float,
    jitter: float = 5.0,
    loss: float = 0.0,
    baseline_delta: float | None = 0.0,
) -> Observation:
    return Observation(
        ts=dt.datetime(2026, 5, 30, 8, minute, tzinfo=UTC),
        phase="FIBER",
        host=host,
        p95_ms=p95,
        jitter_ms=jitter,
        loss_pct=loss,
        baseline_p95=50.0 if baseline_delta is not None else None,
        baseline_delta_pct=baseline_delta,
        baseline_sample_count=40 if baseline_delta is not None else None,
    )


def worth_knowing_items(markdown: str) -> list[str]:
    section = markdown.split("Worth knowing:", 1)[1].split("Technical Evidence:", 1)[0]
    return [line.removeprefix("- ").strip() for line in section.splitlines() if line.startswith("- ")]


def pattern_obs(day: int, hour: int, minute: int, host: str, p95: float, jitter: float = 5.0) -> Observation:
    return Observation(
        ts=dt.datetime(2026, 5, day, hour, minute, tzinfo=UTC),
        phase="FIBER",
        host=host,
        p95_ms=p95,
        jitter_ms=jitter,
        loss_pct=0.0,
    )


def write_history_file(directory: Path, date: str, rows: list[tuple[str, str, float]]) -> Path:
    path = directory / f"bakeoff_{date}.csv"
    lines = ["ts,phase_label,host,p95_ms,jitter_ms,loss_pct\n"]
    for timestamp, host, p95 in rows:
        lines.append(f"{timestamp},FIBER,{host},{p95},5,0\n")
    path.write_text("".join(lines))
    return path


def section(markdown: str, heading: str) -> str:
    body = markdown.split(heading, 1)[1]
    next_heading = body.find("\n## ")
    return body if next_heading == -1 else body[:next_heading]


class CoreSignalTests(unittest.TestCase):
    def test_missing_dns_is_optional(self) -> None:
        self.assertIsNone(read_dns_summary(Path("/tmp/does-not-exist-core-signal.json")))

    def test_missing_required_csv_header_raises_clear_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.csv"
            path.write_text("ts,host\n2026-05-30T08:00:00Z,1.1.1.1\n")
            with self.assertRaisesRegex(CsvSchemaError, "p95_ms"):
                read_observations(path)

    def test_unclassified_hosts_are_reported_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.csv"
            path.write_text(
                "ts,phase_label,host,p95_ms,jitter_ms,loss_pct\n"
                "2026-05-30T08:00:00Z,FIBER,1.1.1.1,20,1,0\n"
                "2026-05-30T08:00:00Z,FIBER,203.0.113.10,20,1,0\n"
            )
            result = read_observations(path)
            self.assertEqual(len(result.observations), 1)
            self.assertEqual(result.ignored_hosts, {"203.0.113.10": 1})

    def test_latest_window_anchors_to_newest_export_timestamp(self) -> None:
        old = Observation(dt.datetime(2026, 5, 1, tzinfo=UTC), "FIBER", "1.1.1.1", 10, 1, 0)
        recent = Observation(dt.datetime(2026, 5, 30, tzinfo=UTC), "FIBER", "1.1.1.1", 10, 1, 0)
        window, start, end = latest_window([old, recent], 24)
        self.assertEqual(window, [recent])
        self.assertEqual(end, recent.ts)
        self.assertEqual(start, recent.ts - dt.timedelta(hours=24))

    def test_pattern_history_is_bounded_by_telemetry_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_dir = Path(tmp)
            write_history_file(
                history_dir,
                "20260501",
                [
                    ("2026-05-01T11:59:00+00:00", "1.1.1.1", 10),
                    ("2026-05-01T12:30:00+00:00", "1.1.1.1", 20),
                ],
            )
            write_history_file(history_dir, "20260502", [("2026-05-02T12:00:00+00:00", "1.1.1.1", 30)])
            today = write_history_file(history_dir, "20260503", [("2026-05-03T12:00:00+00:00", "1.1.1.1", 40)])

            result = load_pattern_history(history_dir, history_days=2)

            self.assertEqual(result.window_end, dt.datetime(2026, 5, 3, 12, 0, tzinfo=UTC))
            self.assertEqual(result.window_start, dt.datetime(2026, 5, 1, 12, 30, tzinfo=UTC))
            self.assertEqual([obs.p95_ms for obs in result.ingest.observations], [20, 30, 40])
            self.assertIn(today, result.source_files)
            self.assertEqual(result.files_available, 3)

    def test_sustained_degradation_requires_two_consecutive_raw_bad_wan_points(self) -> None:
        rows = [
            obs(0, "192.168.1.1", 20),
            obs(0, "1.1.1.1", 160),
            obs(1, "192.168.1.1", 20),
            obs(1, "1.1.1.1", 170),
            obs(2, "192.168.1.1", 20),
            obs(2, "1.1.1.1", 30),
        ]
        result = analyze(rows, None)
        self.assertEqual(result["wan_health"]["raw_bad_samples"], 2)
        self.assertEqual(result["wan_health"]["sustained_bad_samples"], 1)
        self.assertEqual(result["attribution"]["label"], "Likely upstream ISP / path")

    def test_report_writes_dated_and_latest_files(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        self.assertEqual(result["attribution"]["confidence"], "Recent window")
        markdown = render_brief(result)
        with tempfile.TemporaryDirectory() as tmp:
            dated, latest = write_reports(markdown, Path(tmp), report_date(result))
            self.assertTrue(dated.exists())
            self.assertTrue(latest.exists())
            self.assertIn("Core Signal Morning Brief", latest.read_text())
            self.assertNotIn("Issue Location:", latest.read_text())

    def test_healthy_report_starts_with_clear_status_and_action(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        markdown = render_brief(result)
        lines = markdown.splitlines()
        self.assertEqual(lines[0], "# Core Signal Morning Brief - 2026-05-30")
        self.assertEqual(lines[2], "Status: Healthy")
        self.assertIn("Why This Status:\nNo meaningful instability was detected", markdown)
        self.assertIn("Recommended Action: None.", markdown)
        self.assertNotIn("Issue Location:", markdown.split("Technical Evidence:", 1)[0])

    def test_isolated_low_count_blips_remain_healthy(self) -> None:
        rows = [
            obs(0, "192.168.1.1", 20),
            obs(0, "1.1.1.1", 160),
            obs(1, "192.168.1.1", 20),
            obs(1, "1.1.1.1", 20),
            obs(2, "192.168.1.1", 20),
            obs(2, "1.1.1.1", 170),
            obs(3, "192.168.1.1", 20),
            obs(3, "1.1.1.1", 20),
            obs(4, "192.168.1.1", 20),
            obs(4, "1.1.1.1", 180),
        ]
        markdown = render_brief(analyze(rows, None))
        self.assertIn("Status: Healthy", markdown)
        self.assertIn("A few brief blips were not operationally significant.", markdown)
        self.assertNotIn("Issue Location:", markdown.split("Technical Evidence:", 1)[0])

    def test_repeated_non_sustained_instability_is_watch(self) -> None:
        rows = []
        for minute in range(8):
            rows.append(obs(minute, "192.168.1.1", 20))
            rows.append(obs(minute, "1.1.1.1", 170 if minute in {0, 2, 4, 6} else 20))
        markdown = render_brief(analyze(rows, None))
        self.assertIn("Status: Watch", markdown)
        self.assertIn("Issue Location: Likely upstream/ISP issue", markdown)
        self.assertIn("not actionable because no sustained slowdown was detected", markdown)
        self.assertIn("Recommended Action: No action unless", markdown)

    def test_report_attribution_prefers_upstream_when_internet_degraded_and_local_stable(self) -> None:
        result = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
        )
        self.assertEqual(result["report_attribution"]["label"], "Likely upstream ISP / path")
        self.assertIn("Issue Location: Likely upstream/ISP issue", render_brief(result))

    def test_report_attribution_prefers_local_when_local_is_persistently_degraded(self) -> None:
        result = analyze(
            [
                obs(0, "192.168.1.1", 150),
                obs(1, "192.168.1.1", 160),
                obs(2, "192.168.1.1", 170),
                obs(0, "1.1.1.1", 20),
                obs(1, "1.1.1.1", 20),
                obs(2, "1.1.1.1", 20),
            ],
            None,
        )
        self.assertEqual(result["report_attribution"]["label"], "Likely local LAN / Wi-Fi")
        self.assertIn("Issue Location: Likely local Wi-Fi/router issue", render_brief(result))

    def test_report_attribution_is_unclear_when_evidence_is_unavailable(self) -> None:
        result = analyze([], None)
        self.assertEqual(result["report_attribution"]["label"], "No clear source identified")
        self.assertIn("Issue Location: No clear source identified", render_brief(result))

    def test_prime_observer_incident_attribution_is_preferred_for_sustained_slowdowns(self) -> None:
        exported = {
            "current_attribution": {
                "status": "no_issue_detected",
                "label": "No network issue detected",
                "confidence": "high",
                "evidence": ["LAN and WAN both look stable now."],
            },
            "window_attribution": {
                "status": "likely_upstream",
                "label": "Likely upstream (ISP / path)",
                "confidence": "medium",
                "evidence": ["sustained internet-side incident"],
            },
            "incidents": [
                {
                    "status": "likely_upstream",
                    "label": "Likely upstream (ISP / path)",
                    "confidence": "high",
                    "evidence": ["internet-side degradation", "local gateway stable"],
                }
            ],
        }
        result = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
            exported,
        )
        self.assertEqual(result["fallback_report_attribution"]["label"], "Likely upstream ISP / path")
        self.assertEqual(result["report_attribution"]["label"], "Likely upstream ISP / path")
        self.assertEqual(result["report_attribution"]["source"], "prime_observer_incident")
        markdown = render_brief(result)
        self.assertIn("Issue Location: Likely upstream/ISP issue", markdown)
        self.assertIn("Attribution source: Prime Observer incident attribution", markdown)

    def test_prime_observer_window_attribution_is_used_when_no_incident_matches(self) -> None:
        exported = {
            "window_attribution": {
                "status": "likely_upstream",
                "label": "Likely upstream (ISP / path)",
                "confidence": "medium",
                "evidence": ["3 sustained internet-side incident(s)"],
            },
            "current_attribution": {
                "status": "no_issue_detected",
                "label": "No network issue detected",
                "confidence": "high",
                "evidence": ["LAN and WAN both look stable now."],
            },
        }
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None, exported)
        self.assertEqual(result["report_attribution"]["label"], "Likely upstream ISP / path")
        self.assertEqual(result["report_attribution"]["source"], "prime_observer_window")
        self.assertIn("Attribution source: Prime Observer window attribution", render_brief(result))

    def test_prime_observer_current_attribution_is_used_when_no_window_or_incident_exists(self) -> None:
        exported = {
            "current_attribution": {
                "status": "no_issue_detected",
                "label": "No network issue detected",
                "confidence": "high",
                "evidence": ["LAN and WAN both look stable now."],
            }
        }
        result = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
            exported,
        )
        self.assertEqual(result["report_attribution"]["label"], "No issue detected")
        self.assertEqual(result["report_attribution"]["source"], "prime_observer_current")
        markdown = render_brief(result)
        self.assertIn("Issue Location: No clear source identified", markdown)
        self.assertIn("Attribution source: Prime Observer current attribution", markdown)

    def test_missing_exported_attribution_uses_core_signal_fallback(self) -> None:
        result = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
            None,
        )
        markdown = render_brief(result)
        self.assertEqual(result["report_attribution"]["source"], "core_signal_fallback")
        self.assertIn("Issue Location: Likely upstream/ISP issue", markdown)
        self.assertIn("Attribution source: Core Signal fallback", markdown)

    def test_technical_evidence_stays_compact_by_default(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        markdown = render_brief(result)
        technical = markdown.split("Technical Evidence:", 1)[1]
        self.assertIn("- Internet samples:", technical)
        self.assertIn("- Brief instability:", technical)
        self.assertIn("- Attribution source: Core Signal fallback", technical)
        self.assertNotIn("Median p95 latency", technical)
        self.assertNotIn("Jitter 95th percentile", technical)
        self.assertNotIn("FIBER:", technical)

    def test_verbose_evidence_includes_deeper_metrics_only_when_requested(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        compact = render_brief(result)
        verbose = render_brief(result, verbose_evidence=True)
        self.assertNotIn("Verbose Evidence:", compact)
        self.assertIn("Verbose Evidence:", verbose)
        self.assertIn("Median p95 latency", verbose)
        self.assertIn("Jitter 95th percentile", verbose)

    def test_no_action_report_does_not_lead_with_metrics(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        markdown = render_brief(result)
        before_technical = markdown.split("Technical Evidence:", 1)[0]
        for term in ("p95", "jitter", "packet loss", "WAN", "LAN", "baseline delta"):
            self.assertNotIn(term, before_technical)

    def test_watch_baseline_reason_explains_not_actionable(self) -> None:
        result = analyze(
            [obs(0, "1.1.1.1", 20, baseline_delta=30), obs(0, "192.168.1.1", 10)],
            None,
        )
        markdown = render_brief(result)
        self.assertIn("Status: Watch", markdown)
        self.assertIn("Why This Status:", markdown)
        self.assertIn("noticeably different from historical norms", markdown)
        self.assertIn("not actionable because no sustained instability", markdown)
        self.assertIn("Issue Location: No clear source identified", markdown)

    def test_attention_reason_explains_user_impact(self) -> None:
        result = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
        )
        markdown = render_brief(result)
        self.assertIn("Status: Attention", markdown)
        self.assertIn("Sustained slowdown was detected", markdown)
        self.assertIn("user impact was possible", markdown)

    def test_attention_worth_knowing_orders_incident_before_performance_and_dns(self) -> None:
        exported = {
            "incidents": [
                {
                    "status": "likely_upstream",
                    "label": "Likely upstream (ISP / path)",
                    "confidence": "high",
                    "evidence": ["internet-side degradation", "local gateway stable"],
                }
            ]
        }
        result = analyze(
            [
                obs(0, "192.168.1.1", 20, baseline_delta=-30),
                obs(0, "1.1.1.1", 160, baseline_delta=-30),
                obs(1, "192.168.1.1", 20, baseline_delta=-30),
                obs(1, "1.1.1.1", 170, baseline_delta=-30),
            ],
            {"status": "ok", "summary": {"block_rate_pct": 2.3}},
            exported,
        )
        items = worth_knowing_items(render_brief(result))
        self.assertEqual(items[0], "1 sustained slowdown period(s) were found.")
        self.assertEqual(items[1], "Evidence points to an upstream/ISP issue.")
        self.assertEqual(items[2], "Outside those periods, performance was better than usual for this time of day.")
        self.assertTrue(items[-1].startswith("DNS filtering looked normal:"))

    def test_healthy_worth_knowing_keeps_performance_first_and_dns_last(self) -> None:
        result = analyze(
            [obs(0, "1.1.1.1", 20, baseline_delta=-30), obs(0, "192.168.1.1", 10)],
            {"status": "ok", "summary": {"block_rate_pct": 1.5}},
        )
        items = worth_knowing_items(render_brief(result))
        self.assertEqual(items[0], "Performance was better than usual for this time of day.")
        self.assertEqual(items[1], "No sustained slowdowns were found.")
        self.assertTrue(items[-1].startswith("DNS filtering looked normal:"))

    def test_watch_worth_knowing_leads_with_watch_reason(self) -> None:
        result = analyze(
            [obs(0, "1.1.1.1", 20, baseline_delta=30), obs(0, "192.168.1.1", 10)],
            {"status": "ok", "summary": {"block_rate_pct": 1.5}},
        )
        items = worth_knowing_items(render_brief(result))
        self.assertEqual(items[0], "Performance was slower than usual for this time of day.")
        self.assertTrue(items[-1].startswith("DNS filtering looked normal:"))

    def test_plain_language_issue_locations_are_rendered(self) -> None:
        upstream = analyze(
            [
                obs(0, "192.168.1.1", 20),
                obs(0, "1.1.1.1", 160),
                obs(1, "192.168.1.1", 20),
                obs(1, "1.1.1.1", 170),
            ],
            None,
        )
        self.assertIn("Issue Location: Likely upstream/ISP issue", render_brief(upstream))

        local = analyze(
            [
                obs(0, "192.168.1.1", 150),
                obs(1, "192.168.1.1", 160),
                obs(2, "192.168.1.1", 170),
                obs(0, "1.1.1.1", 20),
                obs(1, "1.1.1.1", 20),
                obs(2, "1.1.1.1", 20),
            ],
            None,
        )
        self.assertIn("Issue Location: Likely local Wi-Fi/router issue", render_brief(local))

        unclear = analyze([], None)
        self.assertIn("Issue Location: No clear source identified", render_brief(unclear))

    def test_pattern_confidence_requires_recurrence_and_duration(self) -> None:
        confidence, inputs = confidence_from_inputs(
            recurrence_count=1,
            timing_consistency=1.0,
            magnitude_pct=90.0,
            sample_size=2000,
            duration_days=1,
        )
        self.assertEqual(confidence, "Low")
        self.assertEqual(inputs["total_points"], 9)

    def test_pattern_confidence_is_capped_by_history_duration(self) -> None:
        short_confidence, _ = confidence_from_inputs(
            recurrence_count=10,
            timing_consistency=1.0,
            magnitude_pct=90.0,
            sample_size=2000,
            duration_days=13,
        )
        medium_confidence, _ = confidence_from_inputs(
            recurrence_count=10,
            timing_consistency=1.0,
            magnitude_pct=90.0,
            sample_size=2000,
            duration_days=20,
        )
        full_confidence, _ = confidence_from_inputs(
            recurrence_count=10,
            timing_consistency=1.0,
            magnitude_pct=90.0,
            sample_size=2000,
            duration_days=30,
        )
        self.assertEqual(short_confidence, "Low")
        self.assertEqual(medium_confidence, "Medium")
        self.assertEqual(full_confidence, "High")

    def test_pattern_report_detects_morning_ramp_without_signature_promotion(self) -> None:
        rows: list[Observation] = []
        for day in (25, 26, 27):
            rows.extend(
                [
                    pattern_obs(day, 7, 0, "1.1.1.1", 35),
                    pattern_obs(day, 7, 0, "192.168.1.1", 15),
                    pattern_obs(day, 9, 0, "1.1.1.1", 150, 55),
                    pattern_obs(day, 9, 0, "192.168.1.1", 20),
                    pattern_obs(day, 9, 5, "1.1.1.1", 155, 58),
                    pattern_obs(day, 9, 5, "192.168.1.1", 20),
                    pattern_obs(day, 9, 10, "1.1.1.1", 152, 54),
                    pattern_obs(day, 9, 10, "192.168.1.1", 20),
                    pattern_obs(day, 9, 15, "1.1.1.1", 158, 57),
                    pattern_obs(day, 9, 15, "192.168.1.1", 20),
                    pattern_obs(day, 12, 0, "1.1.1.1", 40),
                    pattern_obs(day, 12, 0, "192.168.1.1", 15),
                    pattern_obs(day, 14, 0, "1.1.1.1", 38),
                    pattern_obs(day, 14, 0, "192.168.1.1", 15),
                    pattern_obs(day, 20, 0, "1.1.1.1", 30),
                    pattern_obs(day, 20, 0, "192.168.1.1", 15),
                ]
            )

        analysis = analyze_patterns(rows)
        markdown = render_pattern_report(analysis)
        self.assertIn("Morning ramp around 08:30-11:30", markdown)
        self.assertIn("Approaching signature status: No", markdown)
        self.assertIn("not signatures or root-cause claims", markdown)

    def test_pattern_report_writes_to_pattern_directory(self) -> None:
        analysis = analyze_patterns(
            [
                pattern_obs(25, 9, 0, "1.1.1.1", 150, 55),
                pattern_obs(25, 9, 0, "192.168.1.1", 20),
                pattern_obs(25, 20, 0, "1.1.1.1", 30),
                pattern_obs(25, 20, 0, "192.168.1.1", 15),
            ]
        )
        markdown = render_pattern_report(analysis)
        with tempfile.TemporaryDirectory() as tmp:
            dated, latest = write_pattern_reports(markdown, Path(tmp) / "patterns", "2026-05-25")
            self.assertEqual(dated.name, "2026-05-25-pattern-report.md")
            self.assertEqual(latest.name, "latest.md")
            self.assertIn("Core Signal Pattern Report", latest.read_text())

    def test_pattern_report_renders_history_source_metadata(self) -> None:
        analysis = analyze_patterns(
            [pattern_obs(25, 9, 0, "1.1.1.1", 150, 55)],
            history={
                "history_dir": Path("/tmp/history"),
                "requested_days": 30,
                "files_available": 4,
                "source_files": [Path("/tmp/history/bakeoff_20260525.csv")],
                "window_start": dt.datetime(2026, 5, 25, 9, 0, tzinfo=UTC),
                "window_end": dt.datetime(2026, 5, 25, 9, 0, tzinfo=UTC),
            },
        )
        markdown = render_pattern_report(analysis)
        self.assertIn("History directory: /tmp/history", markdown)
        self.assertIn("Requested history window: 30 day(s)", markdown)
        self.assertIn("History files read: 1 of 4 available bakeoff file(s)", markdown)
        self.assertIn("Date range analyzed: 2026-05-25 09:00 UTC to 2026-05-25 09:00 UTC", markdown)

    def test_visible_top_queried_domain_concentration_is_prioritized(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "total_queries": 1000,
                    "top_queried_domain": "urldb.meetcircle-netgear.co",
                    "top_queried_domain_count": 460,
                    "top_queried_domain_share": 0.46,
                    "top_queried_domain_redacted": False,
                    "top_entities": [
                        {
                            "label": "entity_1",
                            "name": "urldb.meetcircle-netgear.co",
                            "count": 460,
                            "share_of_total": 0.46,
                            "dominance_ratio": 8.2,
                            "name_redacted": False,
                        }
                    ],
                    "top_reasons": [{"name": "Native Tracking", "queries": 90}],
                },
            }
        )
        signal = result["signals"][0]
        self.assertEqual(signal.signal_kind, "dns_queried_domain")
        self.assertEqual(signal.entity_label, "urldb.meetcircle-netgear.co")
        self.assertEqual(signal.entity_type, "DNS domain")
        self.assertEqual(signal.name, "urldb.meetcircle-netgear.co")
        self.assertFalse(signal.name_redacted)
        self.assertEqual(signal.count, 460)
        self.assertAlmostEqual(signal.share_pct, 46.0)
        self.assertAlmostEqual(signal.dominance_ratio, 8.2)

    def test_concentration_signal_detected_when_one_entity_dominates(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "total_queries": 1000,
                    "top_entity_share": 0.25,
                    "top_entity_dominance_ratio": 6.0,
                    "top_entities": [
                        {
                            "label": "entity_1",
                            "count": 250,
                            "share_of_total": 0.25,
                            "dominance_ratio": 6.0,
                            "name_redacted": True,
                        },
                    ],
                },
            }
        )
        signal = result["signals"][0]
        self.assertEqual(signal.signal_kind, "dns_entity")
        self.assertEqual(signal.entity_label, "entity_1")
        self.assertEqual(signal.entity_type, "DNS entity")
        self.assertIsNone(signal.name)
        self.assertTrue(signal.name_redacted)
        self.assertEqual(signal.count, 250)
        self.assertAlmostEqual(signal.share_pct, 25.0)
        self.assertAlmostEqual(signal.dominance_ratio, 6.0)
        self.assertEqual(signal.confidence, "Medium")

    def test_low_share_low_dominance_entities_are_suppressed(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "total_queries": 1000,
                    "top_entity_share": 0.12,
                    "top_entity_dominance_ratio": 1.5,
                    "top_entities": [
                        {
                            "label": "entity_1",
                            "count": 120,
                            "share_of_total": 0.12,
                            "dominance_ratio": 1.5,
                            "name_redacted": True,
                        },
                    ],
                },
            }
        )
        self.assertEqual(result["signals"], [])
        self.assertIn("No concentration signal met", result["message"])

    def test_top_blocked_domain_concentration_uses_blocked_activity_share(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "total_queries": 1000,
                    "blocked_queries": 100,
                    "top_queried_domain": "ordinary.example",
                    "top_queried_domain_count": 90,
                    "top_queried_domain_share": 0.09,
                    "top_queried_domain_redacted": False,
                    "top_blocked_domain": "blocked.example",
                    "top_blocked_domain_count": 40,
                    "top_blocked_domain_share_of_blocked": 0.40,
                    "top_blocked_domain_redacted": False,
                    "top_reasons": [{"name": "Native Tracking", "queries": 80}],
                },
            }
        )
        signal = result["signals"][0]
        self.assertEqual(signal.signal_kind, "dns_blocked_domain")
        self.assertEqual(signal.entity_label, "blocked.example")
        self.assertEqual(signal.entity_type, "DNS domain")
        self.assertEqual(signal.share_label, "Share of blocked DNS activity")
        self.assertAlmostEqual(signal.share_pct, 40.0)

    def test_redacted_top_entity_is_rendered_safely(self) -> None:
        markdown = render_pattern_report(
            analyze_patterns(
                [pattern_obs(25, 9, 0, "1.1.1.1", 150, 55)],
                dns={
                    "status": "ok",
                    "summary": {
                        "total_queries": 1000,
                        "top_entities": [
                            {
                                "label": "entity_1",
                                "count": 250,
                                "share_of_total": 0.25,
                                "dominance_ratio": 6.0,
                                "entity_type": "domain",
                                "name_redacted": True,
                            }
                        ],
                    },
                },
            )
        )
        concentration = section(markdown, "## Concentration Signals")
        self.assertIn("Entity label: entity_1", concentration)
        self.assertIn("Entity type: DNS entity", concentration)
        self.assertIn("Name: redacted by Prime Observer privacy settings", concentration)
        self.assertIn("One redacted DNS entity accounted for an unusually large share of DNS activity.", concentration)
        self.assertIn("Review recommended locally if this concentration is unexpected.", concentration)
        self.assertNotIn("domain represented", concentration)

    def test_entity_concentration_is_prioritized_over_top_reasons(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "total_queries": 1000,
                    "blocked_queries": 100,
                    "top_entities": [
                        {
                            "label": "entity_1",
                            "count": 250,
                            "share_of_total": 0.25,
                            "dominance_ratio": 6.0,
                            "name_redacted": True,
                        }
                    ],
                    "top_reasons": [
                        {"name": "OISD", "queries": 90},
                        {"name": "Native Tracking", "queries": 10},
                    ],
                },
            }
        )
        self.assertEqual(len(result["signals"]), 1)
        self.assertEqual(result["signals"][0].signal_kind, "dns_entity")
        self.assertEqual(result["signals"][0].entity_label, "entity_1")

    def test_top_reasons_remains_fallback_when_domain_and_entity_signals_absent(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "blocked_queries": 100,
                    "top_reasons": [
                        {"name": "Native Tracking", "queries": 72},
                        {"name": "DNS Rebinding", "queries": 18},
                        {"name": "Other", "queries": 10},
                    ],
                },
            }
        )
        signal = result["signals"][0]
        self.assertEqual(signal.signal_kind, "blocked_reason")
        self.assertEqual(signal.entity_label, "Native Tracking")
        self.assertEqual(signal.entity_type, "Blocked DNS reason")
        self.assertEqual(signal.count, 72)
        self.assertAlmostEqual(signal.share_pct, 72.0)
        self.assertAlmostEqual(signal.dominance_ratio, 4.0)

    def test_top_reasons_fallback_does_not_override_blocked_domain_signal(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "blocked_queries": 100,
                    "top_blocked_domain": "blocked.example",
                    "top_blocked_domain_count": 36,
                    "top_blocked_domain_share_of_blocked": 0.36,
                    "top_blocked_domain_redacted": False,
                    "top_reasons": [
                        {"name": "Native Tracking", "queries": 90},
                        {"name": "DNS Rebinding", "queries": 10},
                    ],
                },
            }
        )
        self.assertEqual(result["signals"][0].signal_kind, "dns_blocked_domain")

    def test_expected_blocklist_reason_is_suppressed_unless_clearly_useful(self) -> None:
        result = analyze_concentration(
            {
                "status": "ok",
                "summary": {
                    "blocked_queries": 100,
                    "top_reasons": [
                        {"name": "OISD", "queries": 72},
                        {"name": "Native Tracking", "queries": 18},
                        {"name": "DNS Rebinding", "queries": 10},
                    ],
                },
            }
        )
        self.assertEqual(result["signals"], [])

    def test_concentration_insufficient_data_message_when_no_safe_top_n_exists(self) -> None:
        markdown = render_pattern_report(
            analyze_patterns(
                [pattern_obs(25, 9, 0, "1.1.1.1", 150, 55)],
                dns={"status": "ok", "summary": {"blocked_queries": 100}},
            )
        )
        concentration = section(markdown, "## Concentration Signals")
        self.assertIn(
            "No concentration signals were evaluated because the available exported summaries do not include safe top-N entity data.",
            concentration,
        )

    def test_pattern_report_includes_concentration_section_and_avoids_alarm_language(self) -> None:
        markdown = render_pattern_report(
            analyze_patterns(
                [pattern_obs(25, 9, 0, "1.1.1.1", 150, 55)],
                dns={
                    "status": "ok",
                    "summary": {
                        "blocked_queries": 100,
                        "top_reasons": [
                            {"name": "Native Tracking", "queries": 72},
                            {"name": "DNS Rebinding", "queries": 18},
                        ],
                    },
                },
            )
        )
        concentration = section(markdown, "## Concentration Signals")
        self.assertIn("### Concentration: Native Tracking block reason", concentration)
        self.assertIn("Review recommended only if this concentration is unexpected.", concentration)
        for term in ("Problem", "Failure", "Alert", "Threat", "suspicious", "malicious"):
            self.assertNotIn(term, concentration)


if __name__ == "__main__":
    unittest.main()
