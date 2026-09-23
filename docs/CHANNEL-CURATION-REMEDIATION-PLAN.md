# Channel curation — remediation implementation plan

2026-09-23. Based on `docs/CHANNEL-CURATION-AUDIT.md`, findings C1–C12.
Audited baseline: plugin `2536613`; TV `46c23ad3`. This is a corrective plan,
not a new channel-catalog or visual-design proposal.

## Objective and preserved decisions

Finish the selected Option A Channel Studio experience reliably: edits made through
Apply must survive, govern the content that airs, and remain consistent across the
browser, storage, background workers, and TV. Start from the current implementation
and actual owner data; never reseed an existing editable library to erase problems.

Preserve the 513-network starting proposal plus customs, current IDs/seeds,
owner-applied changes, one group per channel, browser/TV groups, explicit Apply,
and the recorded Option A feedback. Preserve dynamic studio/performer selection
as a supported feature, with accurate count semantics and capability handling.
The prototype selection gate has already been satisfied. No new design approval
gate is needed for this remediation.

Scope: plugin/backend, selected production frontend, Android TV consumers,
non-destructive migration/upgrade/recovery tools, regression evidence, and dev
verification. Production deployment is not part of this assignment. Keep the
continuing activation file/kill switch independent; no new broad activation.

## 1. Establish evidence and protect existing work

1. Read applicable current AGENTS.md files in both repositories, the audit, the
   original plan, API/migration docs, and the recorded design decision. Record
   actual HEADs and local changes. Preserve untracked migration/review artifacts.
2. Run the supplied Python and browser reproductions and retain their original
   JSON as pre-fix evidence. The browser first tests unmodified code and then
   inserts one count() shim only to expose other bugs. That shim must not remain
   necessary once C1 is fixed.
3. Establish baseline Python and focused TV results. The audit observed 329/44
   passes; do not infer acceptance from those counts alone. Add end-to-end failure
   assertions for every reproduced issue before treating it as fixed.
4. Use disposable data for fault injection and migration/rollback rehearsals.
   Read and back up the actual dev library/rollout/publications before dev changes.
   Do not touch production services or TVs. Follow the TV repo's current device
   policy, rather than older contradictory device addresses in plugin notes.

## 2. Repair the production frontend foundation (C1)

Implement a coherent draft-store interface and exercise it through the actual
registered production component. count/ids/get/put/drop/subscribe must agree after
create, update, discard, acknowledgment, navigation, and reload. Keep a real
in-memory map when sessionStorage is unavailable/full; swallowing an exception
and re-reading empty storage is not a fallback.

Tests mount `ui/index.js` with PluginApi React and valid GetChannelLibrary/
GetChannelDefinition responses. Assert a usable editor, not merely absence of a
console exception. Cover zero channels, populated channels, persistent drafts,
storage exceptions, malformed library responses, and network failures. Keep
backend failures distinct from frontend programming errors in user-facing text.

This small fix unblocks deeper production-UI tests; do not present it as completing
the frontend remediation.

## 3. Make transactions atomic and identity-safe (C3, C4)

Separate transaction parsing, static validation, candidate construction, final
validation, and commit:

1. Parse/validate every operation type and its allowed keys/value types.
2. Load the current document under the correct writer lock and check replay by
   requestId plus exact payload digest.
3. Stage all mutations on an independent deep candidate, never live document
   references. Support a group created within the same transaction as a move.
4. Validate the final candidate's identity, numbering, groups, source/policy
   shapes, channel existence, count limits, and temporary-ID uniqueness.
5. Commit the candidate and receipt only on success. A rejection may add a receipt
   to the original current document, but cannot alter definitions or their revision.

Enforce id/seed/kind/provenance ownership on the final candidate independent of
which opcode produced it. Restrict bulk flags to their explicit allowlist and real
booleans. Unknown channel IDs, malformed arrays, duplicate temp IDs, bad swaps,
invalid final groups, and unsupported fields yield typed rejections.

Apply the digest to rejected as well as committed receipts. Replay returns the
same outcome, even after later commits. Define receipt expiration honestly: an
unknown result is not proof of failure. Never resubmit a creation under a fresh
identity while its original outcome is unresolved. Do not expose a dangerous
"overwrite everything at current revision" shortcut.

Acceptance:

- Pause + conflicting creates yields rejection with byte-identical definitions,
  unchanged revision, and exact replay of that rejected receipt.
- Immutable identity changes fail through every opcode, not just channel.put.
- Multiple creations, group create+move, and number swaps either fully commit or
  leave all definitions unchanged.
- Two-process revision races, lost-response retries, malformed payloads, and
  post-commit acknowledgment crashes preserve exactly one commit identity.

## 4. Commit refresh intent durably and process it without lost work (C5)

Record pending work atomically with the definition/receipt commit. Recommended:
add an outbox to a versioned library document, with a small explicit, backed-up
schema upgrade for existing schema-1 libraries. Alternatively demonstrate an
actual recoverable transactional protocol; a subsequent file write is insufficient.
Do not reconstruct pending work only from a retention-capped receipt list.

Each pending entry needs channel identity, the relevant source/order/policy/state
signature, and a work generation. Worker protocol:

1. Claim/snapshot bounded work under a shared lock.
2. Compute outside the lock.
3. Reload the current definition and pending work inside the commit lock.
4. Publish only if every computation input and generation remains valid.
5. Acknowledge only the generation processed; preserve newly enqueued work for
   the same or other channels. Requeue superseded work explicitly.

Use one lock/order discipline across Apply, all schedulers, and any legacy adapter
that writes into the library. Document lock acquisition order and test it so the
fix cannot introduce deadlocks or hold global locks over network calls.

At health publication, merge the result into the current snapshot, not the batch's
initial snapshot. Store signature/freshness per channel. An older computation may
not label itself current by borrowing the latest global library revision. Preserve
unrelated health records. Transport failure, missing source, empty pool, partial
index, and success remain distinct; retryable failures retain pending work.

Expose pending/working/failed/current refresh state to the UI. An eight-second
"pending" toast disappearing is not evidence that preparation finished. Keep
persistence success separate from preparation state and provide retry for work,
not re-Apply of a successfully committed definition.

Acceptance includes crash after definition commit/before dispatch, concurrent
Apply during a worker, newer work for the same channel, two-channel health merge,
worker restart, retryable transport failure, and policy/pause changes mid-build.
Assert query counts: cosmetic Apply performs no indexing, and a content edit does
not cause a full 513-channel recomputation.

## 5. Unify content query and identity semantics (C2, C11)

Refactor to one query-building/validation interface used by preview, Lineup,
health, custom indexing, continuing indexing, and source-cache identity. Resolve
current circular dependencies explicitly rather than monkey-patching functions.
Legacy public wrappers can delegate, preserving old byte-stable signatures where
needed for unchanged legacy definitions.

The shared result should carry canonical membership identity, scene_filter,
find_filter/text q, hierarchy choices, and resolved dynamic epoch. Include every
supported key: ANY/ALL/exclusions on all facets, date bounds, min/max duration,
recency, text, and dynamic studio/performer rules. Do not retain two independent
implementations of filter semantics that can drift again.

- A criteria source must work through the real Lineup and both real indexers.
- A modified legacy filter must not silently omit new fields at playback.
- Distinct criteria sources must have distinct source-cache/signature keys.
- Saved-search q remains verbatim; criteria q must reach FindFilterType too.
- Playback-order signatures include sort/seed; membership signatures should not
  include presentation. Dynamic epochs must be consumed by downstream caches,
  not merely computed and returned by a helper.
- Reference/date/ID validation must reject malformed values instead of silently
  dropping them. Validate actual changed references; allow metadata-only edits to
  an existing broken source so it remains recoverable.

Preview must obey the playable-rotation contract. Use the shared bounded
fetch_rotation (up to 50 playable rows, at most 1000 scanned), or report pool
samples without asserting an uncomputed rotation size/completion value. Prefer
using the shared machinery so preview and playback agree. Correlate responses with
source, order, seed, and epoch; a stale response must not overwrite a newer draft.

Dynamic count decisions for this remediation:

- Preserve the UI meaning "at least min, fewer than max": min inclusive, max
  exclusive. When combining bounds using inclusive BETWEEN, convert max to max-1
  and validate the resulting interval. Test exact boundary values.
- Detect nested-field support on the actual Stash instance; cache capabilities
  appropriately and provide typed unsupported-rule feedback. Do not silently
  ignore a criterion or discover incompatibility only during playback.
- Describe current nested scene_count as total-library entity activity. It is not
  equivalent to the proposal's post-JAV eligible counts or hierarchical fringe
  calculations. Keep seeded materialized sources intact unless explicitly edited.
  Do not claim converting a list to a raw count is membership-preserving.
- No implicit research-catalog regeneration or 513-channel source rewrite. A
  future eligible-pool-count predicate is a separate semantic capability, not an
  excuse to label raw counts incorrectly.

Acceptance matrix covers actual query variables and returned membership across
all consumers, invalid/missing entities, q, unplayable leading pages, bounds,
large ANY sets, excluded studios/performers, unsupported Stash capabilities, and
membership changes from dynamic activity with no authored-rule change.

## 6. Resolve programming once and guard scheduling consistently (C6, C7)

Introduce a single effective-programming resolution result consumed by both
Directory operations, the editor, Schedule, custom/continuing preparation, and
status. It must distinguish authored preference, effective playback mode,
availability, fixed pin, rollout stage, and preparation status.

Preserve programming objects losslessly on cosmetic Apply. Validate with the
appropriate engine; do not normalize network continuing/newShare through the
custom fixed/explore/discovery policy. Pass the complete authored policy through
adapters. Archive/pause must affect scheduler eligibility as well as playback.

For each GUI-advertised mode, implement coherent read, periodic preparation,
index/selection, and failure fallback for that channel namespace. If a network
mode is unsupported, disable it clearly rather than persist a setting that is
never served. Do not enable broad continuing activation; respect operator rollout
and authored fixed pins on every path.

Publication commit checks must cover source/order/epoch, all relevant policy,
enabled/paused/archived state, effective mode, and complete prior-publication
generation under the actual library writer lock. Detect and preserve newer
scheduler writes; a source-only comparison cannot establish safe publication.

Schedule reads compare publication compatibility to the current applied channel.
A source-incompatible publication/encore must not supply excluded future scenes.
Return preparing and use the negotiated current bounded Lineup fallback until a
compatible publication is ready. Readers remain read-only. Preserve the existing
running-airing and ordinary policy-edit protection rules; membership exclusions
must not inherit blanket 24-hour protection for invalid future content. Preserve
the actually-aired checkpoint; do not solve invalidation by erasing history.

Acceptance: rollout active/prepare/off × fixed pin/unpinned × GUI mode; cosmetic
rename preserves policy/airings; concurrent sort/policy/pause/source edits reject
stale builds; source-changed Schedule cannot serve the old current/future/encore
pool to a new tune; periodic preparation renews every advertised scheduled mode.

## 7. One production draft/Apply coordinator (C8, C9)

Use one shared implementation for channels, groups, bulk actions, swaps, archive,
restore, and programming. Preserve the selected Option A layout; fix behavior.

Persist separately:

- last acknowledged base definition and revision/signature;
- current editable draft and touched-field intent;
- immutable submitted snapshot, request identity, and expected revision;
- a newer draft typed while that request is in flight;
- durable receipt and background-work state.

Every edit updates the local store even during validation/Apply. Navigation, a
receipt, or a remount cannot replace a newer draft with the submitted snapshot.
When a temporary creation commits, move any newer draft to the final channel ID
instead of dropping it. Persist request identity through reload/unmount and query
its receipt before attempting another submit. Unknown outcome means unknown.

Do not update a draft's base revision just because GetChannelLibrary was reloaded.
On new server state, use the acknowledged base for a three-way comparison:
nonoverlapping edits can be rebased explicitly; same-field changes need resolution.
Never silently apply a stale full record against a freshly copied global revision.
An unrelated channel/group Apply must not authorize that overwrite either.

Group manager becomes a true local draft with Apply/Discard. Blur, Enter in a
name input, reorder, and create stage intent; no task runs until Apply. Group
create+move is one atomic transaction using a temporary/stable reference supported
by the server candidate validator. Inputs survive failed submissions. Present the
affected channel count and number-swap scope accurately.

Use the production bundle in browser tests. Required cases:

- Zero GraphQL task calls before Apply for every mutation path.
- Submitted A, then typed B, navigate before or after receipt: B survives and still
  requires Apply. Repeat for a newly created temporary channel.
- Two-tab changes to different fields merge without loss; same-field conflict
  retains both choices. Reload/unrelated Apply does not bypass this protection.
- Lost submit response, receipt timeout, server rejection, reload, and retry keep
  the original request and draft. A committed creation cannot duplicate.
- Atomic group create+move; no partial group left behind on invalid move.
- Program refresh status follows actual backend work rather than a fixed timeout.
- Keyboard, dialog focus/return, narrow layout, long lists, and large rule sets.

## 8. Correct TV invalidation without dropping membership changes (C12)

Parse the enhanced per-channel identities, order, effective mode, and dynamic epoch
needed to determine timeline validity. Compare them by stable channel ID. Separate
presentation/group navigation updates from membership/scheduling changes.

- Rename/color/glyph/group changes update the displayed directory and guide
  without invalidating unrelated playback caches or resetting the active timeline.
- Changed source, order, eligibility epoch, or effective programming mode
  invalidates only the affected channel's relevant cache.
- Continue to handle removed/paused channels, intentionally empty libraries,
  retired identities, favorites, global number lookup, and dynamic group ordering.
- Preserve old plugin/old TV fallback contracts and deterministic schedule behavior.

Add ViewModel-level refresh tests with observable invalidation calls and a fixed
clock. Test real parsed API payloads; the existing helper fingerprint test does not
prove the refresh path. A rename followed by source edit must produce different
cache effects. Verify dynamic-rule membership refresh even without a library
revision change. Run focused JVM suites and assembleDebug, then dev-device checks
under the actual repository policy.

## 9. Fail-closed migration and exact recovery (C10)

Keep already-migrated owner libraries authoritative. A repeated seed migration is
a no-op and cannot recompile away later edits. A remediation schema upgrade must
preserve channel IDs, seeds, custom channels, policies, sources, groups, and history.

For legacy-to-library migration:

- Use strict legacy readers; reject malformed/future schemas and inconsistent
  source definitions. Reconcile actual retained IDs/seeds/sources and unexplained
  records, not just some display names.
- Reported drift means nonzero exit and no authoritative state write. Dry-run must
  also return nonzero on drift, matching documentation. Staging artifacts can be
  generated, but distinguish them from data changes.
- Read/recheck legacy revisions and digests while coordinated with the actual
  legacy writers. Use a documented migration/scheduler maintenance lock protocol
  so a concurrent save/build cannot be lost between snapshot and commit.
- Back up the actual compiled artifact path, definitions, rollout, publications,
  status, scheduler/index metadata, refresh journal, history, and schema markers.
  Record file hashes and meaningful absences. Use collision-safe backup directories.

Restore must recreate the backed-up state exactly within its managed scope, not
merge it with newer files. Validate the manifest/hashes before changing anything.
Remove artifacts recorded as absent, replace managed publication trees, preserve
unrelated operator data, and avoid an interval where readers see mixed snapshots.
Use staging/atomic switches or a documented recoverable multi-step protocol with
workers stopped/locked. A restore interruption must be resumable and diagnosable.

Acceptance uses temporary directories for drift rejection, changed source/seed,
concurrent custom save, interrupted migration, same-second backup attempts, missing
rollout restoration, newer publication cleanup, pending/outbox cleanup, tampered
backup rejection, and crash/retry during restore. Then rehearse on disposable copies
of actual dev data; do not use the live installation as the fault-injection target.

## 10. Final verification and reporting

Order work to avoid isolated cosmetic fixes: establish failing behavior tests;
repair frontend boot and backend transaction/outbox fundamentals; unify content and
mode/read/commit guards; implement robust frontend drafts/group Apply; repair TV
invalidation and migration recovery; run integrated validation.

Required evidence:

| Findings | Gate before claiming completion |
| --- | --- |
| C1 | Unmodified production route boots and remains usable with storage failures |
| C2/C11 | Same authored source yields correct membership/query semantics in preview, Lineup, health, both indexers |
| C3/C4 | Rejects do not alter definitions; no opcode bypasses server identity ownership; receipts replay exactly |
| C5 | Commit/crash/concurrent enqueue cannot lose work; health merges keep every channel; stale results rejected |
| C6/C7 | Applied source/policy/state governs served and published programming, with gate/pin parity and history preserved |
| C8/C9 | Browser reproductions pass WITHOUT a shim; no task before Apply; drafts/retries/rebases lose no intent |
| C10 | Drift blocks; backup/restore is coherent, exact, crash-recoverable, and preserves actual owner edits |
| C12 | TV refresh tests prove cosmetic continuity and affected-only source invalidation |

Run Python, production-browser, and relevant TV suites; assemble the debug APK.
Verify the real selected UI against dev Stash :9998, then the permitted dev TV or
allowed fallback. Read sources/counts/status through APIs; do not fabricate results
because the dev library is small. Measure the 513-channel list and large dynamic/
materialized sources; keep read operations bounded and avoid full-tier work per edit.

Update API, migration, design-decision clarifications, applicable AGENTS.md, release
runbook, and implementation report to describe actual verified behavior. Keep
historical pre-fix reports as provenance, clearly superseded. Provide a new
`docs/CHANNEL-CURATION-REMEDIATION-REPORT.md` with finding-to-fix/test mapping,
actual commands/counts, tested commits, dev evidence, APK path, remaining baseline
limitations, and recovery commands.

Do not repeat prototype selection or request permission for routine fixes. Report
an actual unavailable dependency honestly and complete all independent work. Do
not mark an acceptance case passed when it was only inferred from source or tested
against prototype code. Completion requires closing C1–C12 or identifying a precise,
agreed scope change; simply documenting the reproduced bugs as limitations is not
completion of this corrective assignment.
