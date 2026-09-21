# Continuing-programming simulation — 30 days, offline, clock-injected

Window crosses America/Toronto spring-forward (2026-03-08). Hourly
preparation of ALL channels in one batched tier-level call, with
per-run file round-trips (restart-equivalent), daily trickle arrivals,
a 200-scene bulk import (day 4), an old-content re-eligibility probe
(day 6), rolling deletions, a protected-zone deletion (day 5), and a
240-hour scheduler outage (days 10-20) — longer than the 168h horizon
plus 48h retention, so retention cannot be what saves recovery. Encore
playback during the outage is observed through the read surface
(programming.schedule), not inferred from skipped worker steps.

Gates are computed by an independent ledger over PUBLISHED programs
(no engine fold/selection helpers); duplicate consumption within a
pass is MEASURED, not asserted constant. Adverse fixtures proving the
gates fail on corrupted input live in tests/test_simulation_metrics.py.

## Continuing engine vs fixed-50 baseline (approximate ordering)

| Channel | Engine unique / eligible | Baseline unique | Engine evening repeat | Baseline evening repeat | Engine same-window recur | Baseline |
| --- | --- | --- | --- | --- | --- | --- |
| Small Mix | 58 / 58 (100.0%) | 50 (86.2%) | 0.155 | 0.268 | 0.303 | 0.704 |
| Large Mix | 678 / 700 (96.9%) | 50 (7.1%) | 0.003 | 0.85 | 0.0 | 0.911 |
| Huge Mix | 1437 / 5000 (28.7%) | 50 (1.0%) | 0.0 | 0.0 | 0.0 | 0.773 |
| Overlap A | 504 / 504 (100.0%) | 50 (9.9%) | 0.0 | 0.041 | 0.0 | 0.556 |
| Overlap B | 500 / 500 (100.0%) | 50 (10.0%) | 0.0 | 0.041 | 0.0 | 0.556 |
| Short Clips | 199 / 199 (100.0%) | 50 (25.1%) | 0.408 | 0.985 | 0.682 | 0.976 |
| Long Form | 60 / 60 (100.0%) | 50 (83.3%) | 0.0 | 0.0 | 0.03 | 0.0 |
| Recent Window | 29 / 30 (96.7%) | 30 (100.0%) | 0.371 | 0.445 | 0.328 | 0.871 |

`evening repeat` is the Jaccard similarity of consecutive evenings'
scene sets (local 18:00-24:00): 1.0 = the same evening lineup every day
(today's complaint). Lower is better. `same-window recur` is the share of
airings whose scene also aired in the same local 3h window in the prior
7 days. The baseline's seeded order is a digest-based APPROXIMATION of
Stash's random_<seed> sort, not a validated exact reproduction.

## Arrivals

```json
{
  "smallTrickle": {
    "count": 28,
    "medianFirstAirHours": 30.2,
    "maxFirstAirHours": 228.3,
    "feasibleMaxFirstAirHours": 32.4,
    "outageAffectedCount": 11
  },
  "largeBulkImport200": {
    "count": 187,
    "medianFirstAirHours": 197.4,
    "firstAired": 187,
    "overdueNeverAired": 13,
    "oldestPendingAtEndHours": 0
  },
  "dynamicChannel": {
    "firstAired": 0,
    "overdueNeverAired": 0
  },
  "oldContentReeligibility": {
    "added": 20,
    "wronglyFlaggedAsArrival": 0,
    "firstAired": 8
  }
}
```

## Gates

```json
{
  "unexpectedProtectedAiringChanges": 0,
  "removalDrivenProtectedChanges": 311,
  "ineligibleFutureAirings": 0,
  "duplicateConsumptionEvents": 0,
  "largeFixtureUniqueScheduled": 678,
  "largeFixtureBeyond50": true,
  "feasibleArrivalsWithin72h": true,
  "oldContentNeverFlaggedAsArrival": true,
  "encoreBoundaryBreaksDuringOutage": 0,
  "encoreServedThroughoutOutage": true,
  "horizonNeverBelow24hHealthy": true
}
```

## Resources

```json
{
  "prepareCalls": 481,
  "avgBatchPrepareSeconds": 0.2998,
  "p95BatchPrepareSeconds": 0.374,
  "maxBatchPrepareSeconds": 1.7048,
  "graphqlQueries": 1592,
  "peakMemoryMBSampled": 21.3,
  "largestPublicationKB": 1588.9
}
```

Repeat-interval detail (median hours between consecutive airings of a
scene) is in results.json per channel.

## Honest limitations (measured, not hidden)

- **Thin libraries must repeat.** Small Mix (12.5h of content) airs its
  whole library more than once a day no matter the policy; Short Clips
  (10h) likewise. The engine relaxes cooldown and window preferences
  rather than inventing filler; identical-taste passes on a thin source
  are bounded by content, not by the scheduler.
- **A shrinking dynamic source can regress window diversity.** Recent
  Window's rolling 30-day membership thins over the run; variety follows
  eligible duration.
- **Bulk imports drain gradually by design.** A 200-scene import at the
  configurable share cannot all first-air within 30 days; the oldest
  pending age and the drain are reported, never promised as a deadline.
- **Huge Mix coverage is horizon-bound**: 5000 x 20min is a 1667-hour
  pass; the simulation window cannot air it all. No 50-item cap is
  involved; the remainder airs in later runs.
- The fixed baseline uses a digest-based approximation of the server's
  seeded shuffle; its figures are comparative, not exact.

