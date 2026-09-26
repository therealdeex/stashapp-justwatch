# Channel editing audit and implementation plan — 2026-09-24

Baseline: `6726bc2` (`cb2da5d` is the latest runtime change). Audit only: no runtime fixes or deployments were made. All transaction probes wrote to temporary directories. Dev Stash validation/preview on :9998 was read-only; production was not accessed.

## Outcome and the reported 60 → 80 minute error

A clean minimum-duration change from 3600 to 4800 seconds works on this checkout:

- The unmodified production React bundle submitted `duration.min: 4800`, with no browser exception, and handled a mocked committed receipt.
- The Python transaction committed the change against a temporary copy of the actual dev library, retaining channel identity.
- Actual dev Stash validation and preview of that draft succeeded. Preview returned zero matching scenes, zero rotation, and `rotationComplete: true`.
- The owner subsequently identified the channel as **Movies**, but does not recall the error. The dev library has no exact or partial Movies name match. Its only duration-bearing channel is `net_4a1078b4`, **Feature Length (60+ Min)**, number 258, with `min: 3600` and exclusion tag 9320. This is not an identified reproduction of Movies; do not assume the same identity/deployment.

**The exact reported incident remains unconfirmed.** The channel name is known (Movies); the error text, channel id, affected deployment, minimum versus maximum field, interface used, and any earlier edits remain unavailable. There is no evidence for a 60-minute limit or an 80-minute conversion bug. Do not claim the following related reproductions explain the incident without matching its payload/error.

A related ordinary editing sequence DOES fail: remove the final tag, then change 60 to 80 minutes. The browser submits `tags: []` and `tagsAny: []`; Python rejects both with `bad_source_ids`, despite the remaining valid duration criterion (E1). A channel can also be misreported as missing its source when a tighter duration produces an empty pool (E7).

## Confirmed findings

P1 = fix before release because owner intent, refresh recovery, or durable task outcomes are violated. P2 = correctness/diagnostic regression to include in the same follow-up.

### E1 — P1: normal rule edits generate payloads the server rejects

Locations: `ui/index.js:2117` (ANY/ALL toggle), `:2189` (remove final entity), `:2207` (remove exclusion), `:1715` (raw draft submission); `justwatch/criteria.py:257`–`:274`.

The UI retains empty facet arrays, while `criteria.validate` rejects any present empty array. Switching ANY/ALL always leaves the unused side as `[]`; removing the last entry has the same problem. Client validation does not report this mismatch. Browser reproduction captured the exact duration-edit payload; feeding it to Python produced `source.tags` and `source.tagsAny` errors.

Fix: use one explicit draft-to-wire serializer for preview, validation, Apply, and retry identity. Omit genuinely empty facet arrays; retain invalid non-empty values for typed validation instead of silently filtering them away. Keep comparison canonicalization separate: `canonicalSource` deliberately fills empty keys and is unsuitable as the wire serializer. Define whether the additive draft API also accepts empty arrays as unset without weakening strict stored-file validation. Never alter seeded membership/exclusions.

Acceptance: every facet's ANY↔ALL switch, last inclusion/exclusion removal, and last-rule removal has the correct outcome; duration-only and exclusion-only criteria stay valid. Preview and Apply receive the same rules. Capture a real browser payload and validate it with Python rather than always mocking `valid: true`.

### E2 — P1: failed health refreshes are acknowledged and forgotten

Locations: `justwatch/snapshots.py:205`; `justwatch/refresh.py:213`–`:254`.

`_channel_health` converts transport/query failures into `healthStatus: unavailable`. The worker writes that result, acknowledges the journal generation, and reports `refreshed`. Probe: unavailable health → `remaining: 0`, empty pending journal, outcome `refreshed`. This loses the intended retry. Passing `{}` as previous health also drops last-known counts, despite the stale-health contract.

Fix: treat unavailable health as a retryable stage failure. Preserve last-known values and pending generation; record a bounded diagnostic and retry/backoff state. A programming failure after successful health must remain independently retryable: the next pass must not skip programming merely because health is now current. Keep unrelated channels progressing.

Acceptance: injected Stash failure preserves counts and pending work; recovery drains it; programming failure retries after health succeeds; concurrent enqueues survive; a failing channel cannot starve siblings.

### E3 — P1: refresh freshness and health publication ignore order/policy/state changes

Locations: `justwatch/refresh.py:147`–`:156`, `:191`–`:202`, `:225`–`:230`.

The journal is populated for sort, policy, and playable-state changes, but `_health_is_current` tests only source signature. A sort change with matching source health is acknowledged without computing anything. Probe: zero health calls, old rotation version retained, empty journal. During an active computation, the health commit guard also checks only source; changing sort during the build still publishes the old result under the newer library revision. The later programming publication guard does not protect this earlier health write.

Fix: define explicit identities for rotation health and programming readiness. Rotation health must include source semantics, ordering, seed, relevant dynamic epoch, and playable state. Programming work must also compare effective mode/policy and publication needs. Under `.library.lock`, revalidate the complete identity used by that stage before publishing. Drop stale results and preserve/requeue work. In `commit_hook`, derive the before-map from its locked `original` argument, not the outside-lock captured map. Do not make cosmetic edits churn rotation identity.

Acceptance: sort-only edits update health/rotation; policy-only changes run required programming even with current health; pause/archive during computation prevents stale publication; concurrent source/sort/policy updates survive; cosmetic edits remain no-reindex.

### E4 — P1: Apply bypasses Stash reference validation; exclusions are not checked

Locations: `justwatch/channel_ops.py:210`–`:278`, `:405`–`:428`; `ui/index.js:1750`.

`ValidateChannelChanges` runs `_reference_errors`; `ApplyChannelChanges` never does. The UI intentionally continues if preflight throws. Probe: validation reports `missing_entity` for a newly selected tag, while direct Apply commits the same draft. Additionally, reference validation iterates only positive facets, so a newly authored nonexistent exclusion passes both paths. Legacy cross-kind single-id comparisons also need care: the same numeric id in two entity types is not the same reference.

Fix: make changed-source reference and dynamic-capability validation part of the authoritative Apply workflow, including exclusions. Keep remote calls outside the writer lock; validate against an acknowledged revision, then recheck the revision inside the lock before committing. Ensure receipt replay is checked before repeating remote work, and record deterministic validation rejections durably. A transient Stash failure must not become permission to commit unchecked rules or a permanent unsupported-feature classification. Preserve metadata-only recovery for unchanged broken sources. Keep lookup bounds, with honest typed capped outcomes.

Acceptance: missing positive and excluded entities fail consistently in Validate/Apply; a caller bypassing preflight cannot bypass the checks; cross-kind equal ids are checked; metadata-only edits of broken sources work; receipt replay works while Stash is unavailable; revision races do not commit against a different validation basis.

### E5 — P1: some rejected drafts crash tasks without a durable receipt

Locations: `justwatch/library.py:538`–`:549`, `:654`–`:717`, `:752`, `:494`–`:504`; `justwatch/channel_ops.py:204`–`:208`.

Draft checks do not cover all constraints enforced at save time. An otherwise valid put containing an unknown top-level field validates successfully, then `save` raises `LibraryError` (“corrupt … unknown fields”) before a receipt is written. A put for a nonexistent channel id using a free number reaches an unhandled `KeyError`. Other malformed values can also crash `_reference_errors` or `_effect_summary` after structural errors have already been found. The actual stored library is not corrupted by these probes; the diagnostic wrongly describes the rejected candidate as corruption.

Fix: validate input shape, id existence, supported fields, flag nullability, and policy types before semantic/effect processing. Validate the finalized candidate against storage invariants before the refresh intent hook. Convert expected invalid-draft failures to typed durable rejection receipts against the unchanged original document; preserve digest/replay behavior. Distinguish an invalid candidate from corruption of existing storage. Do not catch arbitrary programming/I/O failures and report fictional rejection or success; log a correlation id and preserve truthful unknown outcomes if receipt storage itself fails.

Acceptance: malformed payload matrix returns typed errors with no source/revision mutation; deterministic task rejections have replayable receipts; unknown id/field and invalid policy shapes never crash; no refresh intent is enqueued for a structurally invalid candidate; actual stored corruption still fails closed.

### E6 — P2: text visible at Apply time can miss the submitted snapshot

Locations: `ui/index.js:1614`–`:1623`, `:1737`, `:2316`–`:2320`.

Text search updates `qLocal` immediately but updates the draft after 300 ms. Reproduction: rename a channel, type a text query, immediately Apply. Submitted source omits `q`; the visible text remains as a newer unsaved draft. It is not lost in this reproduction, but the first Apply does not commit what was already visible when clicked.

Fix: update the authoring draft synchronously; debounce preview/network work only. Alternatively flush all pending field edits before validating and snapshotting, with channel-scoped cancellation on unmount. Ensure navigation cannot apply an old debounced callback to another channel.

Acceptance: immediate Apply includes visible text; a text-only edit enables Apply immediately; navigation/reload preserves the latest text; edits actually made after submission remain a separate newer draft.

### E7 — P2: empty rule pools are labeled missing sources

Locations: `justwatch/snapshots.py:222`–`:228`; `justwatch/lineup.py:380`–`:418`.

For an empty rotation, health calls `resolve_source`; that function handles linked entity/saved-filter sources, not `filter`/`criteria`. Those shapes lack a single `id`, so it returns false. Probe: valid `criteria.duration.min: 4800` with a successful zero-result query yields `missingSource: true` rather than off air. A failed existence lookup is also converted to “missing” instead of unavailable.

Fix: a successful empty query for a valid rule source means `offAir`. Use source-specific missing-reference handling only when established by an actual lookup; transport errors remain unavailable. Keep zero matching scenes distinct from zero playable scenes in useful status details.

Acceptance: filter/criteria empty pools, nonplayable pools, genuinely deleted linked entities, and failed entity lookups have distinct correct health outcomes. Raising duration past all eligible scenes is valid and remains saved.

### E8 — P2: explicit zero duration bounds are silently erased

Locations: `justwatch/criteria.py:97`–`:107`, `:290`–`:305`, `:432`–`:439`; `ui/index.js:365`–`:370`, `:2246`–`:2299`.

Validation permits nonnegative bounds, but normalization preserves only values greater than zero; projection treats max=0 as absent and uses INT_MAX. Probe: `{duration:{max:0}}` validates and commits while dropping the duration condition entirely. This can broaden the authored pool. The editor advertises `min: 0` for both inputs. Minute fields also round stored seconds and truncate entered decimals, so the precision policy needs to be explicit.

Fix: preserve explicit zero using presence/None checks end-to-end; distinguish blank from zero. Reject malformed/overflowing values with typed errors, and ensure normalization never turns an accepted constrained criteria source into an invalid empty one. Keep untouched seconds exact; define supported minute input precision and validate rather than silently truncate.

Acceptance: absent/blank/zero min/max, zero-only criteria, min>max, 60→80 minutes, decimals, negative values, and GraphQL integer overflow have stable documented outcomes; valid authored bounds project identically before and after persistence.

### E9 — P2: calendar test fails after its hard-coded date

Location: `tests/test_channel_ops.py:78`–`:82`.

The test computes one cutoff from fixed 2026-09-23 but compares it with a projector using actual UTC today. On 2026-09-24 expected March 27 differs from actual March 28. This is a test bug, not demonstrated runtime recency drift.

Fix: freeze/inject the same UTC clock for both paths. Test UTC day rollover deterministically, including a non-UTC local timezone. Do not freeze production behavior to the old date.

## Logging and error tracking required in the follow-up

Current diagnostics are insufficient: `main.py` prints mode and outer exceptions to stderr, but caught health errors are discarded; refresh outcomes exist only in a task return; polling errors and best-effort preflight failures are swallowed. The durable committed receipt does not contain the task's later refresh map. UI refresh messaging depends on preflight effects and clears after eight seconds regardless of readiness.

Implement logging early so subsequent fixes are diagnosable:

1. **Python structured events:** stdlib helper, UTC timestamp, severity, event name, plugin/build version, operation, invocation/error id, requestId, channelId, libraryId, expected/current revision, worker generation/runId, elapsedMs, outcome/error code, retryability. Event-specific allowlists; no blanket serialization of args or context.
2. **Persistent destination:** bounded JSONL at `<data>/logs/channel-studio.jsonl` plus concise stderr diagnostics. Use a separate process-safe append/rotation lock, bounded lock timeout, and no library-lock inversion. Suggested retention: 5 files × 2 MiB. A multiprocess `RotatingFileHandler` alone is not sufficient. Logging failures must degrade to safe stderr without affecting transaction outcome. Document location/retention and retrieval.
3. **Event coverage:** validation start/result, task submit/dispatch, idempotent replay, conflict/rejection, commit success, refresh enqueue/start/stage outcome/retry/supersede/complete, storage failure and unexpected exception. Receipt polling should aggregate repeated failures rather than log every poll as an error. Record the failure stage so “invalid draft”, “committed but refresh failed”, and “outcome unknown” are distinguishable.
4. **Correlation:** generate the browser action id before preflight; retain the immutable requestId through retries and pending-draft recovery. Return additive error ids in validation/rejection details and safe ids in legacy string error messages without changing existing envelope types. Keep stdout exactly one JSON output/error envelope.
5. **Browser diagnostics:** a bounded event buffer, structured console errors for caught failures, and listeners for unexpected errors/unhandled rejections. Capture operation, HTTP status, timing, stage, safe code and correlation ids. Check HTTP responses and malformed GraphQL envelopes explicitly. Offer “Copy error details”/“Download diagnostics” so failures before reaching the server can be investigated. Export must work when server access is down.
6. **Data minimization:** never record API keys, cookies, authorization headers, query-string credentials, full GraphQL variables, source/search text, channel/scene names, media paths, or raw request bodies. Record field paths/opcodes and digests/counts. Error messages and tracebacks can contain those values: sanitize and cap them too. Keep logs outside the assets mirror. Test with synthetic secret canaries.
7. **Truthful readiness:** preserve immutable commit receipts; expose evolving refresh status separately with additive fields/operation keyed to channel/request/generation. Persist safe failure code/id and last-attempt/retry state. The UI should keep “Applied; refresh failed/pending” visible until actual state changes, rather than clearing it on a timer. Remounts and retries must recover that state.
8. **Verification:** subprocess-level stdout envelope check; concurrent writer/rotation stress; unwritable/full logs do not change commit success; redaction tests across nested exceptions and URLs; browser transport/validation/receipt-timeout scenarios; correlate a failure from copied browser details to its server event and retained refresh work.

## Implementation order and delivery gates

### Phase 1 — diagnostics and duration reproduction

Add E9's clock fix and the logging foundation first. Convert the captured payload cases into focused regression tests. Reproduce the exact user incident if error details become available; otherwise retain the uncertainty above. Fix E1/E6/E8 with one coherent draft serialization/input policy. Add a browser-to-real-Python validation harness using temporary fixtures so protocol mismatches cannot pass a mock `valid: true` response.

Gate: duration min/max 60→80, valid empty pools, ANY/ALL/removal paths, text Apply timing, and rejection details all pass. No background work or task dispatch before explicit Apply. Snapshot and request digest remain immutable through retries.

### Phase 2 — authoritative validation and reliable receipts

Implement E4/E5 through the existing transaction path. Share structural validation and semantic reference checks, preserving metadata-only recovery and strict stored loading. Resolve deterministic errors before intent publication, and recheck revision inside the lock after remote validation. Keep contract v1 and library schema 1 unless a documented additive storage requirement makes a migration necessary.

Gate: Validate/Apply parity, unknown id/field/type matrix, exclusions and changed-kind references, idempotent replay, concurrent revision conflicts, and atomic rejection all pass. No remote calls under `.library.lock`; no loss of identity/seeds/provenance.

### Phase 3 — refresh stage recovery and health correctness

Implement E2/E3/E7 together; health success must not suppress pending programming work. Preserve per-generation acknowledgements, stage-specific identity guards, last-known counts and sibling snapshots. Add persistent readiness/error state and hook it to UI diagnostics. Existing old health entries lacking new identity fields should be treated as needing safe recomputation, not assumed fresh. Corrupt pending journals must be surfaced/logged rather than silently treated as an empty successful queue.

Gate: transient failure→retry→recovery, current health + failed programming, stale source/order/state workers, concurrent enqueue/acknowledge, unchanged cosmetics, and valid empty pools pass. Tests must assert final disk state, not only return strings.

### Phase 4 — integrated verification and handoff

Run the full Python suite once after focused checks pass; run the real production browser bundle with real temporary Python validation/Apply. Inject transport failure before and after commit and verify recovery by correlated receipt. Exercise browser reload/navigation and concurrent draft changes. Make any live dev mutation only in a backed-up, disposable verification channel with a documented cleanup; production deployment is a separate task.

Deliver runtime changes, regressions, log/readiness API documentation, exact test results, any unresolved incident details, and operational instructions for retrieving one error by its id. Never change rollout activation, migration seed data, network exceptions, or production libraries as part of these fixes.

## Evidence and scope

Artifacts: `analysis/channel-edit-audit-2026-09-24/`:

- `browser-reproduce.cjs` / `browser-results.json`: unmodified production JS with React 18/Chromium and mocked GraphQL. Demonstrates emitted payloads/state, **not** real persistence.
- `reproduce.py` / `results.json`: actual Python code, fake Stash responses/fault injection, temporary storage. Includes validation of the exact captured browser source.
- `dev-readonly-results.json`: real dev validation/preview of the 80-minute draft.

Reproduce with:

```sh
node analysis/channel-edit-audit-2026-09-24/browser-reproduce.cjs
python3 analysis/channel-edit-audit-2026-09-24/reproduce.py
python3 -m pytest tests/ -q
```

Browser harness inherits the earlier audit's configurable `JW_AUDIT_PLAYWRIGHT`, `JW_AUDIT_REACT_ROOT`, and `JW_AUDIT_CHROME` paths; defaults currently exist on this machine. Probe scripts record observed defects and do not assert that fixes pass; turn cases into intended-behavior regressions during implementation.

Test result on 2026-09-24: **360 passed, 1 failed, 49.00 seconds**, failure E9. Audited editing UI, rule validation/projection, Apply/receipts, incremental refresh/health and error paths. Full suite execution is not a new independent audit of every continuing scheduler algorithm, migration workflow, or companion Android application. No claim of a bug-free remainder.
