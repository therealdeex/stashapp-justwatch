# Channel curation implementation audit

2026-09-23. Audited plugin `2536613` and TV `46c23ad3`.
**Assessment: not ready to call complete or release.** The production editor cannot
finish startup. Independent probes also reproduce lost edits, rejected transactions
that still change data, content-rule/playback disagreement, stale schedule serving,
and unsafe migration/restore behavior. Passing existing suites does not cover these
paths.

Product code, dev library, rollout files, servers, and devices were not changed by
this audit. New files are this review, a remediation plan/handoff, and independent
reproductions. Existing unrelated working files were preserved.

## Evidence and scope

- `python3 -m pytest tests/ -q`: **329 passed in 41.73 s**.
- TV focused JVM tests: **44 passed, 0 failures/errors**, Gradle BUILD SUCCESSFUL:
  ChannelLibraryTest 20, JustWatchPluginClientTest 9, PublishedScheduleTest 9,
  JustWatchRecoveryTest 6. Command:
  `./gradlew :app:testDebugUnitTest --tests '*ChannelLibraryTest' --tests '*JustWatchPluginClientTest' --tests '*JustWatchRecoveryTest' --tests '*PublishedScheduleTest' --console=plain`.
- Independent Python probes: `analysis/channel-curation-audit/reproduce.py` and
  `results.json`. All use temporary directories and synthetic clients; the
  migration subprocesses also target disposable data only.
- Browser probes: `analysis/channel-curation-audit/browser-reproduce.cjs` and
  `browser-results.json`, Chromium with React 18 and mocked GraphQL. They load the
  actual production `ui/index.js`, not a prototype. The unmodified file demonstrates
  C1. To examine interactions behind that blocker, subsequent probes add only the
  missing draft-store `count()` method to an in-memory source copy; no product file
  is patched. Findings behind that shim are explicitly identified below.
- Code inspection covers the cross-module playback/scheduling paths, migration,
  explicit Apply, source projection, TV group parsing, and cache invalidation.
- No new APK was built or installed, no physical-TV verification was attempted,
  and no production/development GraphQL mutations were made. The browser's API is
  entirely synthetic. These results are not a live-device acceptance claim.

Priorities: P1 = required before releasing the affected feature; P2 = correctness
or acceptance gap to complete in the remediation. Numbers are stable finding IDs,
not an instruction to fix each in isolation.

## C1 — P1: production Channel Studio fails during boot

Location: `ui/index.js:583` and `ui/index.js:2464`.

`makeDraftStore()` supplies get/put/drop/ids/clear/subscribe, but no `count()`.
App initialization calls `draftsRef.current.count()` immediately after the first
successful GetChannelLibrary response. The caught TypeError becomes the screen:

> The channel library is not available on this deployment: draftsRef.current.count is not a function

Browser evidence: `production_boot` uses the unmodified shipped file. The backend
response is valid and the error is entirely in the production frontend. The same
missing method is used by the draft subscription and beforeunload handler.

Required: repair the store contract, provide genuine in-memory fallback when
sessionStorage is unavailable, and add an integration smoke test that mounts the
production route against real response shapes. Prototype screenshots do not test
this integration.

## C2 — P1: authored content rules do not reach playback consistently

Locations: `justwatch/channel_ops.py:240`, `justwatch/main.py:384`,
`justwatch/lineup.py:84`, `justwatch/lineup.py:167`,
`justwatch/programming.py:74`, `justwatch/continuing.py:299`.

PreviewChannelPool uses the new `criteria.build_scene_filter`, while Lineup and
both scheduling indexers still use `lineup.build_scene_filter`. The latter does
not accept `type: criteria`.

Independent evidence:

- `rules_playback`: a valid criteria/tag source previews successfully. The same
  stored source fails Lineup, custom indexing, and continuing indexing with
  `ValueError: unknown source type: 'criteria'`.
- `legacy_filter_rule_divergence`: editing an existing `filter` source is subtler.
  Preview includes studio exclusion, duration maximum 900, and dynamic studio
  scene-count selection. Playback silently drops the studio exclusion/dynamic
  selection and projects duration 0..2147483647 instead of 0..900.
- Different criteria sources collapse to the same legacy `source_key` tuple
  `('criteria', '')`. Fixing only the projection branch leaves source identity,
  publication reuse, and cache deduplication incorrect.

The UI preserves `filter` for seeded channels and creates `criteria` for new
channels, so both failures are normal user paths. This defeats the central promise
that the content pool being edited is the content that airs.

Required: one canonical, cycle-safe projection/query/signature path used by all
consumers, including health and both schedulers. Preserve legacy source semantics;
include q, all supported criteria, order, and dynamic epochs in the appropriate
identities. Verify through public operations and query arguments, not only the new
criteria helper.

## C3 — P1: a rejected transaction can still mutate the library

Locations: `justwatch/library.py:438`, `justwatch/library.py:471`,
`justwatch/library.py:660`.

`_apply_ops` builds maps of references to the live loaded document. Patch/move/swap
operations mutate those channel dictionaries before final whole-transaction checks.
If a later operation raises `_OpError`, `apply_transaction` calls `_finish` with
the already-mutated document and writes a rejected receipt alongside those edits.

`rejected_partial` submits a pause followed by two creations competing for the
same initially-free number. It returns `rejected/duplicate_number`, but the existing
channel is paused on disk and revision stays **1**. The two creations are absent;
the transaction is partially applied and invisible to revision-based invalidation.

Related retry defect: rejected receipts do not get the digest passed to `_finish`.
An identical retry of this rejected request produces
`request_replayed_with_different_content` instead of replaying its receipt.

Required: stage all operations on an independent candidate; validate the complete
result; publish definitions only on success. A rejected receipt must be committed
against the untouched current document, with the same digest discipline as a
successful receipt. Every rejection path must have this invariant.

## C4 — P1: bulk patch bypasses immutable identity and allowed-field checks

Location: `justwatch/library.py:593`.

The second validation loop handles a `channels.patch` operation with the initial
`if`, so its `elif kind == 'channels.patch'` field/type validation is unreachable
for that operation. `kind` is also stale from the prior loop. `_apply_ops` then
updates channel dictionaries with arbitrary patch keys.

`patch_identity` submits `{seed: 9876}` through channels.patch. It receives a
committed receipt and changes the server-owned seed. Validation of channel.put
alone therefore does not establish identity immutability.

Required: validate operation shape/allowed fields/types for every operation path;
independently enforce immutable id/seed/kind/provenance on the final candidate.
Handle malformed lists, unknown IDs, duplicate temporary IDs, and unknown fields
as typed rejections with no document mutation.

## C5 — P1: committed refresh work and health results can be lost

Locations: `justwatch/channel_ops.py:319`, `justwatch/refresh.py:51`,
`justwatch/refresh.py:120`, `justwatch/refresh.py:168`,
`justwatch/refresh.py:209`.

Three independently reproduced problems:

1. **Commit/journal crash window.** Apply commits the library/receipt, then writes
   a separate pending file. `crash_window` injects failure before enqueue. The
   source is committed but no refresh entry exists. Retrying the identical request
   compares the already-changed document to itself, calls it metadata-only, and
   still queues nothing. Draining the journal hourly cannot recover absent work.
2. **Concurrent work deletion.** The pending file is read/modified/written without
   a common transactional lock. `lost_enqueued_work` adds work for channel 2 while
   a worker handles channel 1; the worker's final write replaces the queue with
   its old snapshot and erases channel 2's unprocessed entry.
3. **Batch health clobbering.** Each channel's snapshot merges into the same
   `previous_snapshot` captured before the batch. `lost_health` processes two
   channels, reports both refreshed, and leaves only channel 2 in the final health
   snapshot. Concurrent batches can overwrite each other similarly.

Required: an atomic commit/outbox or equivalent recoverable protocol; lock-protected
merge/claim/acknowledge by work generation; and merging into the current health map
at publication time. A failed/unknown health computation must remain retryable,
not be silently acknowledged as a successful refresh. Scope expensive work to the
changed channels and retain bounded/fair background processing.

## C6 — P1: changed pools can serve old schedules; stale builders can publish

Locations: `justwatch/main.py:849`, `justwatch/programming.py:254`,
`justwatch/refresh.py:213`, `justwatch/programming.py:238`,
`justwatch/continuing.py:1016`.

`_op_schedule` finds the current channel but serves its stored publication without
comparing membership/configuration. `stale_schedule` commits a source change from
tag 5 to tag 999; Schedule still returns `ready` with
`scene-from-removed-pool` from the old publication. The same unconditional read
permits obsolete encore contents after expiry.

Separately, `_prepare_one` revalidates only source identity. `stale_worker_policy`
changes sort to newest while an older shuffle build is in flight. The worker
returns `ready` and publishes the old shuffle result. The main custom/continuing
builders also use `catalog_lock`, while library Apply uses `.library.lock`, so
those checks are not mutually exclusive with the writer they purport to guard.
Pause/archive/mode/policy and full publication generation need guard coverage too.

Required: source-compatible read gating and explicit preparing/current-Lineup
fallback; preserve a running airing according to the documented boundary policy.
Unify the relevant commit locking and validate every input used by selection,
including effective mode and the publication generation. Superseded work must be
requeued/retained, not counted as ready. Do not reset aired history.

## C7 — P1: programming modes and fixed pins disagree across consumers

Locations: `justwatch/channel_service.py:188`,
`justwatch/channel_ops.py:105`, `justwatch/library.py:767`,
`justwatch/continuing.py:199`, `justwatch/main.py:849`.

The legacy network adapter drops the `programming` object, yet
`continuing.authored_mode` reads that object to enforce a fixed pin and preferences.
The enhanced directory advertises the stored mode directly, without rollout
resolution. The legacy directory/Schedule resolve through the lossy adapter.

`mode_disagreement` creates a fixed-pinned network on an active rollout:

- GetChannelDirectory reports fixed.
- Legacy Directory reports continuing.
- Schedule returns preparing (tries scheduled playback), rather than fixed.
- A metadata-only rename also strips its stored `newShare` policy.

The generic `_finalize_channel` normalizes every policy through the custom engine's
`policy`, which has no continuing mode/newShare. A continuing preference can become
fixed merely through a rename. In code inspection, the GUI also offers explore and
discovery on networks, but the network Schedule resolver only chooses continuing
or fixed; the periodic custom engine processes only `ch_` channels. A one-off
network explore build is not a coherently served/refreshed programming mode.

Required: lossless engine-aware policy validation and one effective-mode resolver
shared by editor status, both directories, scheduling selection, and playback.
Honor the existing operational gate and fixed pin. Either support each advertised
mode end-to-end for that namespace or clearly disable it; do not claim a setting
works simply because it can be stored.

## C8 — P1: production draft handling loses edits and overwrites newer records

Locations: `ui/index.js:1354`, `ui/index.js:1493`,
`ui/index.js:1539`, `ui/index.js:2755`.

Browser evidence **after the isolated C1 count shim**:

- `newer_inflight_edit_lost_after_navigation`: submit a name, type a newer name
  while applying, switch channels, let the receipt arrive, return. The newer name
  is gone. `edit()` deliberately skips the draft store while applying.
- `newer_inflight_edit_lost_even_without_leaving_until_receipt`: waiting on the
  channel until its receipt arrives initially leaves the newer edit visible, but
  navigating away/back restores the submitted snapshot. `finalize()` never stores
  the newer dirty draft either.
- `stale_draft_overwrites_external_edit`: a local rename is based on revision 1.
  Another editor changes color at revision 2. Reload updates the global revision
  but keeps the old full-record draft. Apply sends expectedRevision 2 and restores
  the old color, silently overwriting a field the local user never edited.

Draft storage contains no base revision/base definition, and production commits
are whole channel.put records. Advancing the global revision is not a rebase.
Transport request identities are only retained in component/in-flight memory and
are also lost across reload; the bulk path always generates a new request ID.

Required: persist acknowledged base, touched-field intent, current draft, and
pending immutable request separately. Always retain in-flight newer edits. Rebase
only demonstrably nonoverlapping edits; conflicting fields require explicit
resolution against a freshly loaded record. A reload or unrelated commit cannot
silently authorize an old full-record replacement.

## C9 — P1: group editing still autosaves; bulk actions lack reliable recovery

Locations: `ui/index.js:890`, `ui/index.js:980`,
`ui/index.js:2511`, `ui/index.js:2693`.

Group rename dispatches a write on blur/Enter; reorder writes on arrow click;
create writes immediately and clears the input. The dialog says its changes apply
immediately and has only Done, not an Apply/Discard transaction. This contradicts
the owner's explicit Apply requirement. Browser reproduction
`group_rename_without_apply` (after C1 shim) exercises blur without pressing Apply.

Creating a group and moving selected channels uses two separate writes despite
promising one Apply. A failure after the first leaves partial intent committed.
`submitOps` always creates a new request identity; on transport error it tells the
owner “Nothing changed” without checking whether the task committed. It neither
retains a durable retry transaction nor reliably refreshes a stale revision after
conflict. These are separate from the main editor's partial receipt handling.

Required: one shared explicit-Apply transaction coordinator for every mutation;
group editing stages locally; group creation plus move is one atomic operation set;
unknown outcomes stay unknown until correlated receipt resolution. Do not clear
inputs/drafts or announce no change on a mere transport failure.

## C10 — P1: migration ignores reported drift; restore overlays rather than restores

Locations: `tools/migrate_channel_library.py:284`,
`tools/migrate_channel_library.py:310`, `tools/migrate_channel_library.py:337`.

`migration_drift` changes the name of a keep-row in disposable deployment data.
The tool prints DRIFT but returns **0** for dry-run and **0** for --apply, creating
the library anyway. There is no drift-abort before backup/commit. The documented
nonzero/no-write guarantee and implementation report are incorrect.

`rollback_overlay` backs up a deployment without a rollout, adds a newer publication,
rollout, and pending journal, then restores. All three newer artifacts survive.
`copytree(..., dirs_exist_ok=True)` merges publication trees; it does not restore
the exact prior snapshot. Missing-at-backup files are not removed (except the main
library document). Restored legacy definitions can therefore coexist with new
activation or publications.

Inspection also finds that legacy files are read before the library lock without
a revision/digest recheck against legacy writers. A concurrent SaveCatalog can be
lost. Backup does not capture the installed compiled artifact when it lives outside
the data directory, and the whole scheduler metadata set is not inventoried.
Drift checking compares only selected names/numbers, not retained source/seed/ID
changes; legacy catalog parsing bypasses its strict loader.

Required: fail closed on recognized drift, validate target snapshots strictly,
coordinate with actual legacy/new writers and schedulers, recheck inputs at commit,
and implement an exact manifest-based restore including recorded absences and
scheduler state. Verify hashes and restore atomically or with a recoverable staged
switch. Rehearse on disposable data before dev or any future release.

## C11 — P2: previews and dynamic-rule validation overstate correctness

Locations: `justwatch/channel_ops.py:176`, `justwatch/channel_ops.py:240`,
`justwatch/criteria.py:124`, `justwatch/criteria.py:327`,
`docs/CHANNEL-CURATION-DESIGN-DECISION.md`.

Independent evidence:

- `preview_truth`: a criteria source with q sends no q in the query. All sample
  rows are unplayable, but the API reports a 50-scene rotation from count=100 and
  claims rotationComplete. It calculates `min(50, sourceTotal)` rather than using
  the shared playable rotation, and its completion flag is unrelated to actual
  scans. The editor's pool preview can therefore be factually wrong.
- `validation`: a newly authored nonexistent entity ID is accepted without any
  Stash lookup. Mixed valid/invalid ID lists and an impossible calendar date pass
  structural checks; normalization can silently discard invalid IDs.
- Dynamic max means LESS_THAN when alone, but BETWEEN including max when min is
  also present. Adding “at least 1” to “fewer than 2” changes the upper bound to
  include 2. UI fields retain the “fewer than” wording.
- The documented older-Stash capability check is not implemented in validation or
  preview. Unsupported nested fields fail as GraphQL errors rather than being
  negotiated/validated.

The design-decision document also incorrectly says raw Stash scene_count matches
the proposal extraction. The seed README explicitly defines rarity/fringe over
**network-eligible post-JAV scenes**, with hierarchical studio treatment. Replacing
those materialized sets with raw entity scene counts changes semantics. This must
be stated plainly, not represented as equivalent maintenance-free conversion.

Required: preserve q throughout the query pipeline; report actual playable rotation
or explicitly distinguish an estimate; strictly validate every authored field;
check changed references and dynamic schema support. Define exclusive/inclusive
thresholds once. Explain raw total-library activity vs eligible-pool activity and
preserve the seed until an explicit content edit chooses different semantics.

## C12 — P2: TV refresh still invalidates all channels for cosmetic edits

Locations in TV repo: `features/justwatch/JustWatchPluginClient.kt:62`,
`ui/pages/justwatch/JustWatchViewModel.kt:1249`.

LibraryChannel does not parse membershipSignature, sort, or seed. Refresh includes
any library revision change in `libraryChanged`, then calls
`schedule?.invalidateAll()`. A rename/group/color Apply increments the revision,
so the supposedly cosmetic update takes the same invalidation path as a source
change. Source edits likewise invalidate every channel rather than the touched one.
The global legacy Directory revision is also still prefixed onto Lineup versions.

This is code-path evidence, not a claim that a device was observed restarting an
active video. The asserted no-invalidation behavior in comments/report is false;
the focused JVM suite passes without testing this actual refresh decision.

Required: consume per-channel membership/order/dynamic-epoch/effective-mode
identities, separate presentation/navigation changes, and invalidate only affected
playback state. Add ViewModel-level tests that count invalidations and verify active
playback continuity, rather than testing only a structuralFingerprint helper.

## Documentation and completion evidence

The implementation report still says the frontend selection is pending, while
`CHANNEL-CURATION-DESIGN-DECISION.md` records Option A and HEAD adds the production
UI. Several report/API/migration guarantees contradicted above are described as
verified. Correct those documents as part of remediation and make the final report
refer to actual tested commits and paths. Retain the existing Option A decision;
there is no need for another five-prototype selection gate.

Known baseline continuing S5/S6 and generation-guard concerns remain documented.
This audit does not claim to have rerun the 30-day simulation or proven the full
continuing engine correct. The directly reproduced integration failures above are
sufficient to require remediation independently of those older concerns.

## Reproduction commands

Python (stdlib, synthetic fixtures):

```sh
python3 analysis/channel-curation-audit/reproduce.py
```

Browser dependencies can live outside the repository. The script defaults to the
local audit dependency locations; environment variables make those paths portable:

```sh
npm install --prefix /tmp/jw-curation-audit-browser --no-audit --no-fund react@18 react-dom@18 playwright-core
JW_AUDIT_PLAYWRIGHT=/tmp/jw-curation-audit-browser/node_modules/playwright-core \
JW_AUDIT_REACT_ROOT=/tmp/jw-curation-audit-browser/node_modules \
JW_AUDIT_CHROME=/opt/google/chrome/chrome \
node analysis/channel-curation-audit/browser-reproduce.cjs
```

These scripts record observations; their current outputs demonstrate defects, not
passing regression assertions. Remediation should add independent behavioral tests
that require the intended outcomes, while preserving this pre-fix evidence.
