# Independent remediation review — 2026-09-22

Reviewed plugin HEAD `5b33aa1` and TV HEAD `61f92187`.

**Recommendation: keep production continuing activation off.** The remediation
addresses several earlier defects, but the ledger still double-counts history in
selection replay, thin channels immediately repeat videos, and the release evidence
does not establish the claimed no-duplicate invariant. Additional task/migration
regressions also need correction.

The current handoff records a separately authorized production plugin/APK deploy
with no continuing activation. This review made no deployment, rollout, server or
device changes and did not independently check the current production state.

## Verification performed

- `python3 -m pytest -q`: **262 passed in 45.81s**.
- Targeted TV JVM tests: **15 executed, 1 failed**, detailed below. Command:
  `./gradlew testDebugUnitTest --tests '*JustWatchRecoveryTest' --tests '*PublishedScheduleTest' --console=plain`.
- Independent offline reproductions:
  `python3 analysis/continuing-review/recheck_v071.py`.
- Captured observations: `analysis/continuing-review/results-v071.json`.
- Inspected the corrected simulator and regression tests. Did not rerun the
  full 30-day simulation because its duplicate gate remains insufficient.

The probes only use synthetic data, local temporary directories and mocked task
dependencies. Product code and existing tests were not modified.

## Findings, ordered by release relevance

### S1 — P1: completed history is applied again when rebuilding selection state

Location: `justwatch/continuing.py:624–629,717–719,728–730`.

The actual-history loop correctly folds newly completed airings into `checkpoint`.
However, `programs` still contains up to 48 hours of completed history. The next
loop replays **all** `programs[:committed]` onto a copy of that checkpoint, including
those already completed airings. The arrival-rebuild branch repeats the same mistake.

Independent probe: a 500-scene fixture initially publishes 336 half-hour airings.
At the first hourly extension, two have completed. Before selecting the first new
slot, the working state has **338 consumed exposures instead of 336**. The checkpoint
itself is not what this probe shows inflated; the temporary state driving future
selection is inflated. This distinction is why actual-history tests alone miss it.

Impact: selection sees incorrect counts, recency, quota credits and potentially
pass progression. Construction and replay still do not implement exactly-once
consumption. Correct actual-history logging does not repair this second replay.

Fix: replay only reservations not already included in the checkpoint/actual-airing
cursor, including the currently airing scene if it has not completed. Keep past
programs for readers/history without replaying their consumption. Test selection
state against checkpoint plus independently enumerated future reservations, across
retention, pass boundaries, deletions, arrivals and repeated restarts.

### S2 — P1: the immediate-repeat preference sorts recent candidates backwards

Location: `justwatch/continuing.py:555–558`.

When all choices fall within REPEAT_GUARD, `freshness = (2, age)` is minimized.
That chooses the **smallest age, i.e. the most recently played video**, contradicting
the comment “most recent last.” With thin sources all higher-priority terms can tie.

Independent probe with missing studio/performer metadata and half-hour scenes:

| Eligible videos | Immediate repeated pairs in the seven-day publication |
| --- | --- |
| 2 | 167 |
| 8 | 41 |
| 30 | 0 |
| 100 | 0 |

For the eight-scene channel the first pass ends in scene 2 and the next starts
with scene 2. Limited libraries must repeat eventually, but replaying the just-ended
video while seven alternatives exist is avoidable and undermines the original goal.

Fix the direction of recency ranking within the guard without restoring a permanent
strict-LRU order. Assert actual adjacent scene identities for small homogeneous and
single-source libraries, not just whether each pass is internally unique.

### S3 — P1: custom-channel and rollout task failures no longer fail the task

Location: `justwatch/main.py:595–597`.

The failure comprehension evaluates `value != "ready"` for non-network keys,
producing a boolean, then compares it to the string `"failure"`. Neither True nor
False equals that string, so custom-channel errors and the special `rollout` error
are excluded from the failure set.

Independent probe: mocked custom preparation returns
`{"ch_12345678": "index failed"}`; `_op_prepare_programming` returns normally
instead of raising GraphQLClientError. The result payload contains the error, but
Stash task success is misleading. The correlated CLI's own failure detection does
not fix scheduler callers that do not use `--verify`.

Fix by classifying both branches into the same explicit outcome vocabulary or by
using separate predicates. Add op-level tests for custom failure, malformed rollout,
network failure, legitimate deferral, empty indexing and recovery. Preserve durable
run diagnostics even when returning task failure.

### S4 — P1: schema-2 migration discards the old publication before calling build

Location: `justwatch/continuing.py:977–979`.

`build()` has a timeline-based schema-2 migration branch, but `prepare()` passes
`None` unless `prior.schema == SCHEMA` (3). Thus the actual production task path
never gives the migration code its old timeline. A backup is created, followed by
a fresh schedule and checkpoint.

Independent probe uses a legacy publication with a current airing lasting another
hour. Preparation reports ready but **does not retain that current airing**. The
existing migration regression uses an empty `programs` list, so it cannot detect
this interruption or prove preservation of consumption history.

Fix the task-path migration handoff and test it with nonempty current, protected,
past and flexible airings. Check the backup, the new cursor/checkpoint and stable
airing identities. Document limits of reconstructing history from old schema-2
files without silently discarding the timeline that is still available.

### S5 — P1: the replacement duplicate gate still does not prove pass uniqueness

Location: `tools/simulate_continuing.py:204–257`, especially `246–247`.

The new oracle flags repeats only when their distance is less than a threshold
capped near the six-hour guard window. This is a repeat-proximity heuristic, not
an independent pass-consumption ledger. It can miss duplicates far apart within
one pass, and legitimate cross-pass rearrangement makes proximity alone ambiguous.

Independent adverse fixture: a known first pass over 100 unchanged eligible IDs
has slot 50 replaced with ID 0. It now has **99 unique IDs in 100 slots**, but
`duplicate_airings(...)` returns **[]**. There are no membership changes, releases,
outages or ambiguous boundaries in this fixture.

The regression-test Ledger uses a different half-pass-distance heuristic, which
also does not establish the exact invariant. The function comments describing a
minimum number of intervening distinct scenes across arbitrary pass boundaries
are not generally true when each pass shuffles differently.

Fix: for unchanged full-library fixtures, assert exact uniqueness/membership in
each independently known pass starting at bootstrap. For dynamic cases expose
auditable pass/reservation facts and validate them with an independent reference
ledger. Keep proximity as a separate quality metric. Add adverse fixtures with
widely spaced duplicates, dropped scenes and invalid pass transitions. Rerun and
supersede the simulation only once those gates detect the injected violations.

### S6 — P2: expired scheduling is not reported as encore, and its alert clears

Location: `justwatch/continuing.py:1072–1076`.

Coverage correctly decreases at read time now. However, `expiring` is gated by
`ready`, so it becomes false once coverage reaches zero. `encore` only copies the
stored `degraded` flag, which normally becomes true after recovery, not when an
unattended publication first expires.

Independent probe at hour 200 after an ordinary seven-day publication returns:
`coverageHours: 0, ready: false, expiring: false, encore: false`.
The Schedule read path would already be looping its emergency encore.

Fix: separate expired/encore playback state from recovered-from-outage history.
Define sustained alerting below the coverage threshold, including zero, and empty
source behavior. Test manifest aging against Schedule at the same times without
any scheduler writes. Consumers should not need to infer outage state from a
combination of contradictory flags.

### S7 — P2: the new TV recovery test is wall-clock dependent and fails this run

Location: TV
`app/src/test/java/com/github/damontecres/stashapp/ui/pages/justwatch/JustWatchRecoveryTest.kt:51–58,313`.

Observed JVM failure:
`recovery anchors at the failed airing end and hops beyond it` →
`AssertionError: hopped to the next airing` at line 313.

The fixture rounds the real clock down to ten minutes and starts six programs
25 minutes before that. It always expects recovery to load `-scene-3`, although
the currently airing scene depends on which half of the real ten-minute interval
the test runs in. In the latter half it is scene 3 already, so the correct next
scene is 4. This failure is **not evidence by itself that product recovery is
broken**. The fixture must stop depending on execution time.

Fix: inject the same Clock into the ViewModel and schedule fixtures, assert the
successor of the captured failed airing, and test both sides of a boundary. The
other five recovery tests and all nine PublishedSchedule tests passed in this run.
The source change does address the old stale-generation dispatch path.

## Remaining design checks, distinct from the reproduced defects

- **Activation:** `stage: prepare` is implemented, but active mode is still decided
  from the rollout file alone. There is no automatic readiness check/shared effective
  cutover timestamp in resolved_mode. Flipping the operator file is not proof that
  every channel/client changes at a whole-airing boundary. Either implement the
  original guarantee or explicitly document this as an operator-controlled immediate
  cutover limitation; do not claim it cannot expose a missing schedule.
- **Generation guard:** its digest omits indexedAt and some index metadata, and the
  locked revalidation compares source signature but not policy signature. Treat
  concurrent policy/index-only changes as unresolved until controlled interleaving
  tests cover the actual producer of generation tokens, not an artificially changed
  `generation` string in a mocked read.
- **Time injection:** build/due checks accept simulated time, but index_source still
  calls build_scene_filter/effective_epoch without it. Dynamic-source simulation
  needs one time basis all the way through actual query projection.

## Improvements confirmed in source/tests

Pure arrival choice and a single explicit apply function replace the previous double
credit spend; actual history now has a separate completion cursor; ordinary policy
edits retain the protected prefix; short-cap warnings use a set correctly; empty
index caching and per-channel read isolation exist; runId verification and live
coverage arithmetic replace the earlier stale-run/frozen-coverage implementation;
the TV stale-generation completion returns without dispatch.

Those are meaningful corrections, but do not close S1–S7. Prioritize S1–S5 and the
TV deterministic test fix, then refresh the evidence and resolve the status/cutover
limitations before enabling the continuing pilot. No further product edits were
made during this review.
