# Continuing programming — remediation SUPERSEDES the 2026-09-21 simulation

The `results.json` / `report.md` in this directory were produced by the
original `tools/simulate_continuing.py` against plugin `9a5d902`. The
independent review (docs/CONTINUING-PROGRAMMING-REVIEW.md, finding **R8**)
found its measurements defective, and its "all gates pass" output was **not**
acceptance evidence:

- `repeat_gaps` mixed epoch timestamps and gaps in one list (four exactly
  daily airings reported a 48 h median and a 500,024 h p90).
- `duplicateConsumptionWithinPass` was assigned the literal `0`, never
  measured, and never checked by the gate.
- Any index removal excused EVERY protected-airing change on the step.
- The outage skipped only 60 h (shorter than the 168 h horizon) and counted
  skipped scheduler steps, not observed encore playback.
- Preparation ran once per channel, bypassing tier-level budget/fairness.
- The engine's dynamic-source epoch used the real wall clock while the
  simulator used simulated time.

**Superseded by** `analysis/continuing-simulation-v2/` (corrected simulator,
schema-3 engine), whose gates are computed by an independent ledger with
adverse-input tests in `tests/test_simulation_metrics.py`. Keep this
directory only as the historical record of the invalid evidence.
