# Core Signal Handoff

Core Signal is archived and preserved for reference.

If and when its capabilities are merged into Prime Observer, start with these
areas:

- `src/core_signal/analyze.py` for deterministic event interpretation
- `src/core_signal/patterns.py` for recurring-pattern detection and confidence
  scoring
- `src/core_signal/dns_interpretation.py` for privacy-safe DNS interpretation
- `src/core_signal/brief.py` for the current morning-brief narrative contract
- `tests/test_core_signal.py` for the behavior that should remain explicit and
  reviewable during extraction

Parts that are historical only:

- standalone repo packaging
- local report directories
- LaunchAgent installation scripts
- separate-release documentation surfaces
