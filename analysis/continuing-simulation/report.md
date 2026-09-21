# Continuing-programming simulation — 30 days, offline, clock-injected

Window crosses America/Toronto spring-forward (2026-03-08). Hourly
preparation with per-run file round-trips (restart-equivalent), daily
trickle arrivals, a 200-scene bulk import (day 10), rolling deletions
(including a protected-zone deletion on day 5), a 72h scheduler outage
(days 15-17), and a rolling created-at source.

## Continuing engine vs fixed-50 baseline

| Channel | Engine unique / eligible | Baseline unique | Engine evening repeat | Baseline evening repeat | Engine same-window recur | Baseline |
| --- | --- | --- | --- | --- | --- | --- |
| Small Mix | 58 / 58 (100.0%) | 50 (86.2%) | 0.256 | 0.273 | 0.621 | 0.711 |
| Large Mix | 700 / 700 (100.0%) | 50 (7.1%) | 0.0 | 0.734 | 0.138 | 0.767 |
| Huge Mix | 2121 / 5000 (42.4%) | 50 (1.0%) | 0.0 | 0.0 | 0.0 | 0.773 |
| Overlap A | 481 / 481 (100.0%) | 50 (10.4%) | 0.0 | 0.041 | 0.0 | 0.556 |
| Overlap B | 500 / 500 (100.0%) | 50 (10.0%) | 0.0 | 0.041 | 0.0 | 0.556 |
| Short Clips | 199 / 199 (100.0%) | 50 (25.1%) | 0.255 | 0.985 | 0.87 | 0.976 |
| Long Form | 60 / 60 (100.0%) | 50 (83.3%) | 0.0 | 0.0 | 0.0 | 0.0 |
| Recent Window | 179 / 180 (99.4%) | 50 (27.8%) | 0.471 | 0.443 | 0.448 | 0.231 |

`evening repeat` is the Jaccard similarity of consecutive evenings'
scene sets (local 18:00-24:00): 1.0 = the same evening lineup every day
(today's complaint). Lower is better. `same-window recur` is the share of
airings whose scene also aired in the same local 3h window in the prior
7 days.

## Arrivals

```json
{
  "smallTrickle": {
    "count": 28,
    "medianFirstAirHours": 28.3,
    "maxFirstAirHours": 72.2,
    "feasibleMaxFirstAirHours": 32.2,
    "outageAffectedCount": 3
  },
  "largeBulkImport200": {
    "count": 138,
    "medianFirstAirHours": 254.6,
    "oldestPendingAtEndHours": 475.0
  }
}
```

## Gates

```json
{
  "unexpectedProtectedAiringChanges": 0,
  "membershipRemovalDrivenChanges": 445,
  "ineligibleFutureAirings": 0,
  "largeFixtureUniqueScheduled": 700,
  "largeFixtureBeyond50": true,
  "feasibleArrivalsWithin72h": true,
  "duplicateConsumptionWithinPass": 0,
  "encoreWindowsDuringOutage": 60,
  "horizonNeverBelow24hHealthy": true
}
```

## Resources

```json
{
  "prepareCalls": 5288,
  "avgPrepareSeconds": 0.0256,
  "p95PrepareSeconds": 0.0781,
  "maxPrepareSeconds": 0.427,
  "graphqlQueries": 2119,
  "peakMemoryMBAtDay20": 22.3,
  "largestPublicationKB": 1470.8
}
```

Repeat-interval detail (median hours between consecutive airings of a
scene) is in results.json per channel.

## Honest limitations (measured, not hidden)

- **Thin libraries must repeat.** Small Mix (23h of content) improves only
  marginally on evening repetition (0.273 → 0.256): a 23h pass airs the
  whole channel daily no matter the policy. Short Clips (10h) stays high
  (0.985 → 0.255 evening, 0.87 same-window) — the engine relaxes cooldown
  and window preferences rather than inventing filler.
- **A shrinking dynamic source can regress window diversity.** Recent
  Window's rolling 30-day membership thins from 150 to ~30 scenes; its
  same-window recurrence (0.448) then exceeds the baseline's (0.231),
  which sampled 50 fixed scenes. Variety follows eligible duration.
- **Bulk imports drain gradually by design.** A 200-scene import at a 15%
  share needs weeks: 138 of 200 first-aired within 30 days (median 255h),
  the oldest pending waited 475h. The engine reports this honestly via
  the pending-arrivals warning instead of promising a deadline.
- **Pass-boundary adjacency persists at small scale.** Occasional
  same-scene airings a few hours apart at pass boundaries on the
  thinnest channels (min gap 0.4h on Small Mix) — everyone is
  cooldown-violating at a boundary; LRU tie-breaking mitigates but
  cannot eliminate it without blocking playback.
- **Huge Mix covers 42.4% in 30 days** — exactly correct: 5000 x 20min
  is a 1667-hour pass; 720 hours cannot air it all. No 50-item cap is
  involved; the remainder airs in ~28 more days.

