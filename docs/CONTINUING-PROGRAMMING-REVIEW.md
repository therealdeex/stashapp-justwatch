# Independent review of continuing programming

2026-09-21. Reviewed plugin HEAD `9a5d902` and TV HEAD `992119f7`.

**Recommendation: hold production activation.** The integration is useful, but
the consumption ledger, freshness path, operational verification and simulation
have reproducible defects. Passing existing tests does not establish the promised
behavior. Fix and independently validate these before running the production pilot.

## Scope and evidence

Reviewed `continuing.py`, its integration in main/networks/programming, the task
CLI, the simulation, relevant Python tests, the TV implementation commit, schedule
and ViewModel consumers, and the implementation/pilot reports.

Reran the plugin suite: **217 passed in 11.30s**. Added no product fixes. Independent
offline probes are in `analysis/continuing-review/reproduce.py`; their captured
results are in `analysis/continuing-review/results.json`. Run:

```sh
python3 analysis/continuing-review/reproduce.py
```

The probes use synthetic entries, temporary publication directories, and mocked
dependencies. They make no network requests and do not touch the dev rollout.
They report observations rather than assert that today's broken outputs are correct.

TV source findings below are static control-flow findings; no production/device
interaction was performed. Reran **28 targeted JVM tests**, all passing:
JustWatchPluginClientTest (9), NetworkTierTest (10), PublishedScheduleTest (9).
Gradle reported BUILD SUCCESSFUL in 29s. These do not exercise the ViewModel
recovery race identified below. The 30-day simulation was inspected, not rerun: its
measurement defects must be fixed before using another run as evidence.

P1 means fix before production pilot. P2 means fix or explicitly resolve before
claiming conformance to the original plan; it does not mean the behavior is correct.
Line references below apply to the reviewed commits.

## Findings

### R1 — P1: deletion replanning does not restore the consumption checkpoint

`justwatch/continuing.py:486–506,590–593`.

When an invalid future airing lies inside the protected prefix, the implementation
decrements some counts but leaves canceled eligible scenes out of the deck and
leaves their last-scheduled/recent state behind. Its comment explicitly says those
scenes wait for the next pass. This does not release reservations as promised.
The decrement loop also includes newly committed candidates that have not actually
been folded; it does not select only previously durable reservations.

More seriously, `committedThrough` retains its old maximum even when the kept
prefix was cut earlier. New replacement airings before that stale timestamp will
be skipped by subsequent durable folds.

Reproduction: after a deletion at hour 12, **25 canceled eligible IDs remain absent
from the durable deck**. `committedThrough` says hour **24.5**, while the last
actually retained committed airing starts at **11.5**. This can silently skip
videos in a pass and make durable state diverge from published playback.

Required fix: restore/replay a real checkpoint, not partial count subtraction.
Recover every part of selection state, distinguish reserved from aired exposure,
and derive the commit cursor from the resulting timeline. Test multiple consecutive
deletions, newly protected regions, repeat appearances, and process restarts.

### R2 — P1: new-arrival selection spends state twice and bypasses pass membership

`justwatch/continuing.py:365–379,278–303,514–517,563–572`.

`_pick_next` spends a credit and removes the arrival from the deck; `fold_airing`
then spends another credit and consumes it again. Retained-airing replay only
calls fold, so replay does not reproduce the original live selection transitions.
With two initial credits, one selected arrival leaves **0.15 instead of 1.15**.
If arrival removal empties a deck before fold, fold can advance/refill a pass early.

Pending arrivals are filtered by eligibility but not remaining-deck membership.
A scene picked normally remains pending because only `arrival=True` clears it.
It can therefore be chosen again as an arrival within the same pass. The probe
shows the selector choosing `a` from a deck containing only `b`.

Also, all newly eligible IDs are treated as new arrivals even if their `createdAt`
is old; the queried creation timestamp and arrival baseline do not classify them.
The 15% share is a constant, not the bounded configurable policy in the plan.

Required fix: selection must be side-effect-free or have one shared transition
applied exactly once in both construction and replay. First ordinary exposure
must satisfy a pending arrival. Keep eligibility, membership changes and genuine
library creation separate; define and test the missing-timestamp fallback.

### R3 — P1: identical pass ordering is not confined to tiny libraries

`justwatch/continuing.py:400–408`.

The tie-break ranks exact last-scheduled time before shuffled deck position. When
other preferences tie, least-recently-scheduled order becomes the prior pass order.
The comment/report calls this an inherent single-digit-library limitation. It is
neither inherently necessary nor confined to that size.

Reproduction: **100 eligible half-hour videos**, with no studio/performer metadata,
produce identical first and second 50-hour passes. Eight videos also reproduce it.
The existing pass-variation test includes diverse metadata, masking this branch.

Required fix: use bounded recency/adjacency penalties or recency buckets, with the
per-pass shuffle resolving acceptable candidates. Test missing metadata,
single-studio/single-performer sources and several library sizes. Repetition on a
thin source is unavoidable; identical permutations generally are not.

### R4 — P1: normal hourly preparation almost never records completed airings

`justwatch/continuing.py:334–343,495–511`.

`record_aired` only runs when an airing newly enters the protected/durable prefix.
That normally happens about 24 hours before playback, so `end > now` prevents it
from being recorded. When it finally ends, it is already beyond the fold cursor
and is never recorded again. The builder needs an independent actual-airing cursor.

Reproduction: after 48 hourly updates, **96 completed retained airings but only
2 history intervals**. The time-of-day preference consequently lacks its claimed
multi-airing history and mostly relies on the most recent scheduled occurrence.

Required fix: advance actual-airing history independently and idempotently. Test
history contents against an external wall-clock timeline, retention and outages.
Use deep copies for nested historical lists; the current shallow `dict` copy can
mutate prior history when record_aired appends to existing scene lists.

### R5 — P1: reaching the short-video cap crashes instead of publishing a warning

`justwatch/continuing.py:432,576–578`.

`warnings` is a set, but the cap branch calls `warnings.append(...)`.
Two eligible ten-second videos reproduce
`AttributeError: 'set' object has no attribute 'append'` during the initial build.

Required fix: correct the type use and validate the intended resource behavior.
20,000 ten-second airings provide only about 55.6 hours, not seven days. Report
partial coverage accurately and prove subsequent runs can progress within bounds.

### R6 — P1: fairness and per-channel failure isolation break in preparation

`justwatch/continuing.py:657–665,682–691,700–702,728,790–811`.

The saved cursor advances by `len(outcomes)`, which normally equals the entire
channel count. Modulo that count, it returns to the same position. Empty indexes
are always due; they can consume the budget every run. With **three empty sources
and budget two**, all three runs service the same two and indefinitely defer the
third. Rotating the entire coverage-sorted list would also discard its priority.

Coverage sorting reads publication files before the per-channel try/except. One
malformed JSON publication aborts the entire network preparation. Status writing
also reads all publications without per-channel error isolation, so even a caught
preparation error can prevent durable last-run diagnostics from being published.

Required fix: stable fair ordering within priority classes, cached empty results,
bounded retry policy, and isolation for sorting/status reads as well as builds.
Do not silently reset a corrupt channel. Preserve diagnostics and continue peers.

### R7 — P1: the operational surface can falsely claim successful healthy scheduling

`tools/prepare_programming.py:51–79`; `justwatch/main.py:564–583`;
`justwatch/continuing.py:745–779`.

`--verify` never correlates the returned job ID with the completed run. It polls by
description and then accepts any lastRun with no non-ready outcomes; missing
outcomes also pass vacuously. The offline mocked probe supplies an unrelated old
successful run after queueing a new job: **the command still prints verified success**.
The recorded `startedAt` is assigned after preparation finishes.

`coverageHours`, `expiring` and `degraded` are stored at manifest-write time and
returned unchanged by ProgrammingStatus/Directory. If the scheduler stops, these
can continue reporting seven-day coverage even after the publication expires.
Empty publications are also `ready: true`, while the verification command ignores
their zero coverage. Recovery/budget deferral are indiscriminately treated as task
failures instead of separately named operational states.

Required fix: request/run correlation and genuine start/finish/error lifecycle;
per-channel error isolation; derive age/coverage/expiry at read time from compact
timestamps without writing or opening all schedules. Distinguish successful empty
indexing from ready-to-air coverage. Test absent, stale, unrelated and partial runs.

### R8 — P1: simulation output is not an independent acceptance oracle

`tools/simulate_continuing.py:174–182,336–340,387–390,400–434,522–550`.

- `repeat_gaps` stores timestamps and gaps in the same list; from the third airing
  it subtracts a prior gap from an epoch timestamp. Four exactly daily airings
  produce a **48-hour median and 500,024-hour p90**, rather than 24/24.
- `duplicateConsumptionWithinPass` is literally assigned **0**, never measured,
  and is not checked by gates_ok. The assertion cannot detect R2.
- Any index removal excuses every protected-airing change on that step, including
  unrelated changes before the first invalid airing. This is too broad.
- The outage skips 60 hours, shorter than the 168-hour horizon. Its counter counts
  skipped scheduler steps, not observed encore playback, and the simulator stops
  collecting playback during the outage. It does not exercise horizon exhaustion.
- Running prepare once per channel bypasses multi-channel scheduling, fairness
  and shared source-cache behavior. It cannot validate the tier-level resource path.
- Dynamic membership is computed by the fake client using simulated time, while
  production `effective_epoch()` inside the engine uses actual wall-clock date.
- First-air latency only counts specially flagged arrival slots and the feasible
  gate omits pending arrivals that never aired. Normal first exposure and never-aired
  deadline violations need independent accounting.
- Fixed baseline uses the final library retroactively over the whole simulation,
  and a Python digest order rather than the actual Stash seeded-random order. It
  is an approximation, not the claimed exact production baseline.

Required fix: independently derive facts from eligibility events and broadcast
intervals; test metric functions on hand-calculated fixtures; include adverse
synthetic output to prove gates actually fail. Invalidate and regenerate the old
report after engine fixes. Do not selectively quote unaffected-looking figures
from it as a release gate.

### R9 — P2: an ordinary policy edit violates 24-hour protection and resets history

`justwatch/continuing.py:435–446`; `tests/test_continuing.py:216`.

Any configuration-signature change cuts the schedule at roughly two minutes and
restarts consumption state. A spacing-only edit changes **44 of 46 protected
airings** in the probe. This is not an eligibility-removal exception. The existing
test expressly expects the shorter notice boundary, contradicting the original plan.

Required fix: distinguish policy from eligibility changes; keep the protected
prefix and durable pass/history for ordinary preferences. Reconcile source edits
explicitly; do not redefine the plan to match the current test.

### R10 — P2: a sufficiently long outage loses the current encore

`justwatch/continuing.py:430–431,522–540`.

If all old programs have aged out of 48-hour retention, `programs` is empty and
the encore-recovery block never runs, despite Schedule still looping the stored
last 50 airings. At hour 240 after a 168-hour initial publication, the probe
observes **different current airings before/after preparation** and degraded=false.
The existing hour-200 test retains enough old material and does not cover this.

Required fix: derive the currently airing fallback from the unpruned old publication
independently of retained history. Test both sides of the 48-hour cutoff, very long
outages and reconciliation of no-longer-eligible encore items.

### R11 — P1: TV recovery can apply an old failure to a newly tuned channel

TV `JustWatchViewModel.kt:1235–1262`.

There is a generation check before starting the coroutine, but after suspended
`upcomingAt`, a generation mismatch goes into the `else` branch and dispatches
`PlayerFailed(reason)` to the current state. If the user changed channels while
the lookup was pending, the old failure can disrupt the new channel.

Also, next-program lookup is anchored at wall-clock now instead of the failed
program's end. After the viewer skips forward, this can select an earlier program
or the same failed program, then trigger channel-skip rather than progress. The
hop token is only set after lookup; concurrent failure callbacks can start multiple
recoveries. These are static findings, not claims of observed device behavior.

Required fix: ignore stale completions before either success or failure dispatch;
serialize/cancel recovery per tune generation; anchor to failed airing identity/end;
keep a bounded recovery policy. Add suspended-coroutine ViewModel tests, not just
BroadcastSchedule fake-dependency tests. Verify actual behavior on dev hardware.

### R12 — P2: publication/activation lifecycle is incomplete

`justwatch/continuing.py:139–174,710–724`; `docs/PILOT-MANIFEST.md` activation steps.

Static design gaps requiring explicit resolution:

- Rollout activation is visible immediately, before preparation, so compatible TVs
  can switch to `preparing`/empty schedules. There is no prepare-then-activate
  staging or shared whole-airing cutover as required by the plan.
- The commit guard compares `version = digest(programs)`, not the whole durable
  generation. Two builds can have identical programs but different indexedAt,
  actual history or consumption state. The guard cannot detect those stale writes.
  Rollout activation is not revalidated inside the commit lock either.
- Schema-2 read validation checks only that programs is a list and state a dict;
  an empty state is accepted, then `old.get('state') or empty_state(now)` can reset
  it. Strong internal state validation is necessary to uphold no-silent-reset.

Required fix: an atomic generation/CAS protocol covering state and publication,
strict state validation, and a staged activation/readiness/cutover contract. Add
controlled interleaving tests. Keep editorial network revision separate from
publication and activation generations; keep fixed Lineup unchanged.

## What remains useful

The network/custom separation, opt-in rollout concept, full-source indexing,
lightweight manifest approach and additive TV capability plumbing are worth
retaining. This review does not recommend replacing the entire integration.
However, partial fixes to individual count fields will not repair the ledger:
R1/R2/R4 need one coherent state-transition model and an independent oracle.

## Release decision

Do not activate the production pilot based on the current simulation or verifier.
Complete the remediation plan, rerun meaningful tests and corrected simulation,
then perform a visible dev-TV session. Leave production deployment and the pending
513-network catalog outside this review/remediation authorization.
