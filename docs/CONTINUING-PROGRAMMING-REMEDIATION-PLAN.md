# Continuing programming — remediation implementation plan

2026-09-21. This follow-up supplements the original design plan with the concrete
findings in `CONTINUING-PROGRAMMING-REVIEW.md`. Preserve the original product goals
and project invariants. Deliver corrections and dev verification, not production
deployment. Review baseline: plugin `9a5d902`, TV `992119f7`.

Review validation baseline: all 217 plugin tests and 28 targeted TV JVM tests
(JustWatchPluginClientTest, NetworkTierTest, PublishedScheduleTest) pass. Independent
probes still reproduce the defects; green baseline suites are not release clearance.

## 1. Establish failing regressions and trustworthy oracles

Run `analysis/continuing-review/reproduce.py`. Preserve results.json as the historical
review baseline; write fixed-run observations to a separate file. Turn the probes
into normal regression tests asserting the desired behavior, not current outputs.
Do not change existing tests merely to accept a defect. Update the policy-edit test
explicitly because it contradicts the agreed 24-hour protection requirement.

Add hand-calculated tests for metric helpers: one scene at four daily times must
have 24h median/min/p90; identity/order changes must be detectable independently.
Define a test ledger independent of engine fold/selection code. It records each
airing's pass/reservation, actual start/end, membership at scheduling time and
cancellation. Use it to compare consumption after replans and restarts.

The simulator must fail when supplied an intentional duplicate, moved protected
airing before an allowed deletion boundary, never-aired overdue arrival, or
ineligible future item. Expected failures in these tests are evidence the checks
work. Avoid reusing the production helper under test as its own expected result.

## 2. Repair the state machine before selection tuning (R1, R2, R4, R9, R12)

Design one explicit transition representation:

- Actual history through a wall-clock completed-airing cursor, folded once.
- Durable reservations for the protected future, with a recoverable checkpoint.
- Provisional flexible reservations regenerated from that checkpoint.
- Pass/deck, counts, recency, pending arrivals and quota credit all advanced by the
  same exactly-once transition in build and replay.

Make candidate selection pure, or consolidate its mutations into the transition;
never mutate quota/deck in both selection and fold. Use explicit pass/airing
identity to replay; do not infer a pass solely from a deck being empty after a
prior mutation. Normal first exposure clears pending arrival status too.

For deletion inside a protected region, restore the checkpoint before the earliest
invalid airing and replay only retained valid reservations. Return canceled eligible
scenes to the appropriate pass without inventing earlier exposure. Recompute the
commit boundary, lastScheduled, recent metadata and credits together. Tests must
show published replacement airings all fold correctly on subsequent hours.

Preserve ordinary policy edits through the next 24-hour whole-airing boundary.
Do not reset lifetime consumption or rebaseline arrivals for a spacing preference.
Define behavior for source changes, actual removals and duration corrections.

Use a monotonic storage generation or full-state token under the lock, separate
from a display/program version. Reject stale publication/state/status writes and
revalidate policy/activation generation. Validate all state fields on read and
reject corrupt state instead of defaulting to an empty pass. If storage schema
changes, support current dev schema-2 files with a backup/migration path. Never
attempt to reconstruct unreliable historical data as if it were known exactly.

Acceptance: deletion probe yields a consistent boundary and every canceled valid
reservation is recoverable; one arrival spends one credit; no out-of-deck arrival
repeat; actual history contains every completed synthetic airing exactly once;
input objects remain unchanged; restart/replay and concurrent interleavings agree.

## 3. Deliver observable variety and genuine freshness (R2, R3)

Replace strict LRU ordering as the final tie-break with bounded recency/adjacency
penalties, leaving seeded pass order meaningful among acceptable candidates.
Prioritize no immediate repeat when alternatives exist without recreating the
entire previous order. Use explicit tests across 1, 2, 8, 30, 100, 500 scenes,
missing metadata, identical metadata, different durations and multiple seeds.
One-scene sources are an explicit exception; a two-scene source cannot satisfy
every combination of permutation change and boundary-adjacency avoidance.

Classify creation vs newly eligible old content using createdAt plus indexed
baseline/first-seen state. Define missing creation timestamps and bulk imports.
Implement the bounded configurable arrival share or document an explicit product
decision before changing that requirement. Keep authored policies in the sanctioned
CSV/importer or documented operator-policy surface, never hand-edited networks.json.

Acceptance: the 100-scene homogeneous probe varies successive orders; cooldown
and adjacency metrics remain sensible; feasible arrivals first-air within 72h;
normal-deck first exposure also satisfies freshness; backlog age is based on actual
first exposure, not only specially flagged slots. Evaluate actual share over long
windows and confirm retained-airing replay produces the same quota balance.

## 4. Bound work without losing liveness (R5, R6, R10)

Correct the set/list crash, then test an index of ten-second clips. If publication
caps prevent a full seven days, expose partial coverage and continue predictably;
consider segmented storage only if measured costs justify it. Never claim seven
days when capped at less.

Cache successful empty indexes with an expiry/backoff. Fix fair work progression
within urgency classes; preserve near-expiry priority and avoid starving newly
activated sources. Budget real expensive indexing separately from reused/deduped
work. Ensure one failed source cannot bypass work bounds indefinitely through
repeated exceptions. Decide when safe cached membership may extend a schedule
despite an indexing backlog; do not re-air known removed content.

Catch malformed publications in ordering/status as per-channel failures and keep
servicing valid peers. Bound readers and error payloads. Add 41+ channels, empty
sources, failures and heterogeneous TTLs to the batch scheduler tests. Inject the
same time into dynamic cutoffs/epochs and the builder; do not mix wall clocks.

Recover encore from the old unpruned schedule even after retention empties. Test
recovery after horizon+32h, horizon+72h and longer, with removals during the outage.
Keep currently valid fallback boundaries stable and document invalid-file behavior.

Acceptance: third channel in budget-two fixture eventually progresses; corrupt
channel does not abort peers or status; caps report shortfalls without crashing;
long-outage recovery matches the read surface's current encore when eligible.

## 5. Make readiness, run verification and activation truthful (R7, R12)

Add an opaque request/run ID propagated from CLI task arguments into status.
If the installed Stash job API supports reliable exact job-ID correlation, use
it as well. Never infer completion from absence of jobs matching a description.
Capture startedAt before work, finishedAt afterward, and outcome even on failure.
Do not conflate deferred work, empty sources, recovered encore and fatal failure.

Compute current coverage/expiry/staleness from manifest timestamps on sync reads.
Reads remain lightweight/read-only. An empty publication can be successfully
indexed while not ready to air. `--verify` must clearly distinguish job completion,
per-channel preparation outcome and requested pilot readiness, and exit nonzero
when the requested readiness requirement is unmet. Keep credentials private.

Add staged desired-policy/preparation vs effective-activation state. Prepare a
pilot without advertising continuing playback, then publish readiness and an
effective whole-airing cutover time. Define upgraded-TV behavior across cutover
and rollback. Preserve old-client fixed playback throughout. Default deployment
still activates nothing. Rollout-file parsing must validate booleans, not treat
the string "false" as true.

Acceptance: the stale-run probe fails verification; missing/partial unrelated
reports cannot pass; status ages to expiring/encore without a scheduler write;
fresh pilot activation cannot put TVs onto a missing publication; rollback timing
is documented and matches client behavior.

## 6. Repair and test TV recovery (R11)

After any suspension, stale generation means return without any dispatch. Track
one recovery job per tune generation and cancel/ignore it on tune changes. Anchor
recovery to the failed airing end rather than wall-clock current airing when the
viewer has skipped ahead. Retain a bounded attempt policy and cancellation safety.

Add ViewModel tests with a controllably suspended schedule dependency: fail A,
tune B, release A's lookup, assert B is untouched; repeated callbacks trigger one
recovery; skip ahead then fail advances beyond the failed slot; all-dead/empty
channels reach the intended bounded fallback; thrown/canceled lookups behave.

Audit normal published tune position vs the existing joinOffsetMs override and
explicit time-shift semantics. This is an unresolved design check, not a proven
new regression from the integration commit. Ensure actual UI claims about shared
broadcast timing match the chosen behavior. Check guide, next/previous, resume,
mode transitions and cache invalidation against real client plumbing.

## 7. Rebuild evidence, then dev verification

Repair simulation measurement before rerunning it. Batch real preparation across
channels, give distinct fixture sources real distinct membership semantics, and
exercise deduplication separately. Inject time through all epoch/cutoff paths.
Continue observing playback while the scheduler is off. Include an outage longer
than the full publication horizon plus retention. Measure encore from Schedule,
not the number of skipped worker iterations. Track every pending arrival deadline.

Derive protected exceptions from the earliest actually invalid airing; unrelated
protected changes must fail. Track pass duplicates independently, not as a constant.
Use a time-correct baseline for arrivals/deletions, or clearly label a deliberately
static approximation. Label synthetic ordering as an approximation unless validated
against actual Stash ordering. Measure peak memory/build costs over all relevant
events, not just one sampled day. State coverage/resource limitations honestly.

Run all plugin tests, relevant full TV JVM tests, debug build, corrected 30-day
simulation and a multi-channel resource benchmark. Keep reports tied to commits
and fixture/config versions; supersede the earlier invalid evidence explicitly.

Then verify dev Stash :9998 and a headed session on the allowed dev stick .105 / USB
G072JN0734330EBH. Re-read its AGENTS.md. Test actual tune/skip/guide/deletion recovery,
outage state and activation/rollback. If hardware is unavailable, complete the
independent work and record the exact remaining device checks. Do not claim a clean
app launch is an end-to-end programming test.

## Review gates and deliverables

Deliver reviewable commits in dependency order, a finding-to-test matrix for R1–R12,
fixed-run probe results, corrected simulation output, updated status/rollout docs,
and migration/rollback commands for current schema-2 dev publications. Distinguish
measured facts, static reasoning, approximations and remaining device limitations.

Do not deploy to production or activate its pilot. The separate 513-network catalog
remains untouched. Once corrections and dev evidence are reviewable, a production
pilot can be separately authorized and observed for a week before expansion.
