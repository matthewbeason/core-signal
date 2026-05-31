from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from core_signal.analyze import analyze
from core_signal.brief import render_brief, report_date, write_reports
from core_signal.ingest import Observation, latest_window, read_dns_summary


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
        markdown = render_brief(result)
        with tempfile.TemporaryDirectory() as tmp:
            dated, latest = write_reports(markdown, Path(tmp), report_date(result))
            self.assertTrue(dated.exists())
            self.assertTrue(latest.exists())
            self.assertIn("Core Signal Morning Brief", latest.read_text())


if __name__ == "__main__":
    unittest.main()
