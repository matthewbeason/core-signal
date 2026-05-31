from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from core_signal.analyze import analyze
from core_signal.brief import render_brief, report_date, write_reports
from core_signal.ingest import CsvSchemaError, Observation, latest_window, read_dns_summary, read_observations


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
        self.assertIn("Recommended Action: No action unless", markdown)

    def test_no_action_report_does_not_lead_with_metrics(self) -> None:
        result = analyze([obs(0, "1.1.1.1", 20), obs(0, "192.168.1.1", 10)], None)
        markdown = render_brief(result)
        before_technical = markdown.split("Technical Evidence:", 1)[0]
        for term in ("p95", "jitter", "packet loss", "WAN", "LAN", "baseline delta"):
            self.assertNotIn(term, before_technical)

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
        self.assertIn("Issue Location: Unclear source", render_brief(unclear))


if __name__ == "__main__":
    unittest.main()
