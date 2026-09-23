# Channel curation GUI — implementation plan

Date: 2026-09-23. Status: ready for implementation; frontend selection pending.
Companion execution prompt: `docs/CHANNEL-CURATION-HANDOFF.md`.

## 1. Outcome and confirmed decisions

Give the owner a browser GUI for curating the entire Just Watch lineup, starting
with the **513-network v4-final proposal plus existing custom channels**. The
same channel groups must appear in the browser and Android TV guide. Existing
networks become editable channels rather than read-only cards.

Decisions explicitly confirmed by the owner:

- Use `data/proposed_channels_final.csv` / the 513-channel proposal as the
  starting network catalog, not the current 795-network catalog.
- Preserve existing custom channels alongside that catalog.
- Each channel belongs to **exactly one group**.
- Groups organize channels in **both the GUI and TV guide**.
- Changes require **Apply**. No mutation autosaves to the server.
- Build **five genuinely different, interactive frontend prototypes** with
  mock data. UX is the priority; the owner chooses a direction or a combination.
- The **only planned user-review blocker** is review of those interfaces and
  incorporation of the owner's feedback. Continue other independent work while
  waiting; after feedback, finish the implementation without another design gate.

The assignment is implementation across both repositories, migration tooling,
validation, a working dev installation, and a release-ready package/runbook.
Production deployment is outside this assignment; do not treat a production
approval as another prerequisite to completion. This plan itself changes no live
catalog, rollout file, server, device, or application code.

The owner's decisions supersede the old AGENTS.md requirements for read-only
networks, CSV authority after initialization, and editor autosave. Update those
specific instructions during implementation. The remaining strict-loading,
identity, source, concurrency, and scheduling safeguards continue to apply.

## 2. Evidence and repository boundaries

Plugin repository: `/home/shahram/dev/stash-justwatch`.
TV repository: `/home/shahram/dev/StashAppAndroidTV`.
Recon baseline: plugin `5b33aa1`; TV `61f92187`. Re-read actual HEADs and working
changes on execution; these are provenance, not instructions to reset either repo.

| Surface | Current implementation | Implication |
| --- | --- | --- |
| Browser UI | `ui/index.js`, `ui/styles.css`, Stash PluginApi React | Extend the existing integration; retain `/plugins/stash-justwatch` |
| Custom storage | `justwatch/catalog.py`, schema 1, numbers 1–99 | Strict loading, immutable IDs/seeds, revision locking already exist |
| Network storage | `justwatch/networks.json`, compiled CSV | Currently read-only, installed with code; unsuitable for owner edits |
| Source projection | `justwatch/lineup.py` | Reuse the proven membership and bounded-rotation engine |
| Dispatcher | `justwatch/main.py`, `justwatch/contract.py` | Add negotiated editing operations, preserve old playback shapes |
| Counts/status | `snapshots.py`, `programming.py`, `continuing.py` | Separate pool/rotation counts from schedule readiness |
| TV protocol | `features/justwatch/JustWatchPluginClient.kt` | Currently restricts network sections to three known strings |
| TV model | `features/justwatch/ChannelKey.kt`, `ChannelDirectory` | Fixed section enum/order currently drives grouping and navigation |
| TV UI/effects | `ui/pages/justwatch/JustWatchViewModel.kt`, guide/explore/landing pages | Must consume owner-defined groups and refresh after edits |

Source artifacts to inspect:

- `analysis/just-watch-final/README.md`, `validation_report.md`,
  `migration_map.csv`, `final_channels.csv`, `networks.preview.json`.
- `tools/import_channels.py`, `tools/recount_channels.py`.
- Relevant tests for catalog, lineup, networks, operations, snapshots,
  programming, continuing, and TV directory/guide/recovery behavior.
- `docs/CONTINUING-PROGRAMMING-REVIEW-2.md` and its reproduction files.
  This newer local review records unresolved defects; older handoff claims of
  completion are not sufficient evidence. Preserve these working-tree files.

Confirmed local artifact facts:

- Current compiled tier: 795 networks.
- Selected proposal: 513 networks = 209 General + 115 Studios + 189 Performers.
- Migration: 292 retained identities; 221 new identities reuse retired numbers;
  another 282 numbers become vacant. Thus 503 old identities retire in total.
- All 513 proposal CSV rows have stable keys. The current 795-row CSV has none.
- Proposal counts describe the September 8 extraction, not today's library.
- The local dev catalog had four custom channels at revision 21 during recon.
  Read the target catalog at migration time; never hard-code this observation.

Some checked-in guidance is stale. In particular, the TV repo's current root
`AGENTS.md` designates `.105` / USB `G072JN0734330EBH` for testing; older plugin
notes name `.196`. Follow the actual TV policy. Never run, inspect, screenshot,
or interactively test the production `.169` TV.

## 3. Scope and product defaults

These defaults resolve routine choices without requiring more owner questions.
Frontend feedback can refine presentation and interaction details.

### Channels

Support create, rename, number/swap, duplicate, move to group, edit source,
change color/glyph, pause/resume, archive/restore, and programming preferences.
Keep IDs and seeds immutable through edits. A duplicate receives a new ID and
seed, copies the source/policy, and starts as an unapplied draft.

The server assigns final IDs/seeds when creation is committed and returns a
temporary-to-final ID mapping in the receipt. Reject attempts to mutate an
existing identity; never regenerate it from a display name. Replaying the same
creation request returns the same identity. Store a stable key for new networks
independent of naming and numbering, preserving imported keys verbatim.

Keep current number bands initially: `ch_` channels use 1–99; `net_` channels use
100–899. They have the same editing experience and may share groups. The bands
are compatibility constraints, not group identities. Preserve sparse numbering.
Offer the next available valid number and explicit atomic swaps within a band;
never silently renumber neighbors. New channels default to a free 100–899 slot,
with the low-band option available. Full bands yield a useful validation message.

Archive removes a channel from playback without destroying its definition,
identity, or history. Archived channels reserve their numbers until deliberately
renumbered or restored through a reviewed swap. Pause keeps the definition and
number but removes it from the active playback directory. Clarify both actions.
Do not include irreversible deletion in the initial GUI.

### Groups

A group has an immutable ID, editable name, and an explicit position. Every
channel has one valid `groupId`, including paused and archived channels.
Seed My Channels, General, Studios, Performers from current definitions.
Allow create, rename, reorder, and atomic bulk channel moves. Names must be
nonempty and unique after trimming/case folding. Do not permit removal of the
last group. Removing a populated group requires choosing a destination for all
members; the reassignment and removal are one Apply transaction.

Group order drives browser/TV browsing order; channels within a group sort by
channel number. Changing groups never renumbers channels. Channel up/down follows
the displayed active guide order and wraps; number entry still tunes directly.
Groups do not add, inherit, or subtract content rules. No nested groups or
multiple memberships in this release. Group metadata includes a legacy section
fallback solely for old clients; the new interface does not expose that mapping.

### Content-pool editing

Support the current baseline losslessly, with a visual rule builder:

- Tags: match ANY, match ALL, exclude ANY; explicit descendant inclusion.
- Performers and studios: match ANY or ALL, plus explicit exclusions where
  supported by the actual Stash schema. Studio descendants remain explicit.
- Scene date range, duration minimum/maximum, added-within-days, and text search.
- Existing saved scene searches remain linked sources, used verbatim including
  their `q`. Show that edits to the saved search in Stash affect membership.
- Different rule rows combine with AND. ANY/ALL applies within its row. Multiple
  rows over the same field must be conjoined correctly, not overwrite each other.
- Represent complex existing saved searches without flattening or rewriting them.
  Keep advanced saved searches as an escape hatch; a general nested boolean-tree
  authoring interface is not required for this release.

Use searchable entity pickers with names, selected items, descendant settings,
and clear missing-entity states. Large entity sets (the proposal includes thousands
of performer IDs) need a summary and searchable membership list, not thousands of
chips rendered at once. Do not cap or truncate stored memberships to fit the UI.

Read-only preview shows a plain-language rule summary, current matching-pool
count, a bounded sample, and fixed-rotation size separately. Continuing channels
also show their actual published schedule/status separately. Do not label the
first item in a content sample as NOW. On mock screens mark all counts/previews as
simulated; use neutral placeholder art rather than fetching scene media.

### Programming and effect timing

Preserve the two existing scheduling implementations rather than merging engines
in this project. Existing custom fixed/explore/discovery preferences remain;
network continuing preferences respect the server rollout gate and fixed pins.
Show desired preferences and actual scheduling availability truthfully. Saving a
preference must never activate continuing mode for all 513 networks or bypass the
operator kill switch. Disabled capabilities receive a concise explanation.

- Name, group, glyph, and color changes do not reset rotations or schedules.
- Pool/sort changes invalidate only the affected channel's membership/rotation.
- Ordinary scheduling preference edits keep the existing protected-airing policy.
- Pool changes cannot continue serving newly ineligible future scenes simply
  because an old publication exists. A currently playing scene may finish; future
  tuning must use the applied source. Return an explicit preparing state for a
  source-incompatible publication and use the current bounded Lineup as the
  negotiated fallback until a compatible publication is ready. Never serve an
  incompatible old encore. Readers remain read-only.
- UI states distinguish Applied, Preparing programming, Ready, Empty pool, and
  Unavailable. A persisted edit remains successful if background refresh fails;
  provide retry/status separately instead of resubmitting the edit.

### Useful management features

Include channel search/filter, group filtering, bulk move and pause/resume,
archive/restore, duplicate, draft discard, definition export, and revision history
with restore-as-a-new-revision. History is bounded and restores definitions, never
rewinds the actually-aired ledger. Backup/restore tools cover full operational data.
User-facing import/merging of arbitrary external catalogs, recommendations that
redesign the catalog, and automatic re-selection of the proposal are out of scope.

## 4. Frontend prototype stage and the sole review gate

Create an isolated prototype area, suggested `prototypes/channel-curation/`, with
one landing page linking all five working variants. Provide one documented local
start command and copyable browser URLs. The owner must not need to assemble or
build five unrelated projects. Keep prototype assets separate from the deployed
Stash route. Use a shared mock adapter and local fixture data; no production calls.

| Variant | Distinct workflow to demonstrate |
| --- | --- |
| A — Channel Studio | Searchable dial/list and focused side-by-side editor |
| B — Group Organizer | Group-first navigation, channel management within the selected group |
| C — Channel Library | Dense filterable table, selection and efficient bulk operations |
| D — Visual Guide | Guide-shaped browsing, opening a contextual editor while retaining place |
| E — Guided Curator | Progressive editing through identity, group, pool, programming, and review |

These are interaction alternatives, not five themes over identical markup. Match
production constraints where possible: semantic HTML, responsive browser layout,
keyboard access, reusable controls, and no second React runtime in final integration.
Existing master-detail/no-tabs guidance describes the old editor; user-directed
prototype exploration may vary navigation. Keep the selected implementation coherent.

Use the real proposal's 513 channel definitions plus synthetic custom channels;
synthetic media/counts are labeled. Include fixtures for an empty pool, deleted
entity, long names, thousands of selected entities, paused/archived channels,
validation error, slow Apply, conflict, failed request, and successful retry.
A reset control restores the same starting fixture across variants.

All variants must support the same review script:

1. Find a known channel among 513 using search, then browse by group.
2. Rename it and change its group; observe unsaved changes without mutation.
3. Add ANY/ALL/exclusion rules and understand the plain-language summary.
4. Inspect a simulated count/sample; distinguish source pool from rotation.
5. Apply, observe progress and acknowledgment; also try Discard.
6. Switch away with a dirty draft, then return without losing it.
7. Create a group and bulk move selected channels with explicit Apply.
8. Try the error/conflict fixtures; confirm the draft survives.
9. Operate the core flow with keyboard only and at a narrow browser width.

Deliver a concise comparison describing strengths/tradeoffs and provide the URLs.
Ask for preferred variant or combination and specific friction points in one
feedback request. Capture the answer in
`docs/CHANNEL-CURATION-DESIGN-DECISION.md`, with chosen flow, requested changes,
and acceptance observations. This file is created during implementation, not
pre-filled with an invented selection.

**Gate:** do not select a production frontend by elapsed time, lack of response,
a scoring formula, or implementer preference. Await actual owner feedback before
final production frontend integration. Continue backend, migrations, contract,
TV data integration, and tests while the review is pending. If independent work
is exhausted, report precisely what is complete and wait at this gate. After the
owner responds, integrate the feedback and continue; do not invent a second
mandatory prototype-approval cycle.

## 5. Apply, drafts, concurrency, and recovery

Use separate acknowledged server state and local draft state. Local draft retention
is allowed; it is never a server save. Provide one clear Apply action for the
current channel, and separate explicitly scoped Apply actions for group management
and a selected bulk operation. A channel Apply must not publish another channel's
unfinished draft. Explain the affected channel count for bulk actions and swaps.

Recommended state machine:

`clean -> dirty -> validating -> applying -> applied`

Validation errors, transport errors, and revision conflicts branch to recoverable
states that retain the draft. Editing while a request is in flight creates a newer
local draft. Acknowledgment applies only to the immutable submitted snapshot; newer
edits remain dirty and require another explicit Apply. Remove timer-driven saves,
post-save auto-flushes, and legacy mutation shortcuts from all production edit paths.

- Navigation preserves per-channel drafts; show dirty indicators. Offer Apply,
  Discard, or Keep draft when appropriate, without repeatedly interrupting normal
  browsing. Warn on tab close while dirty. If persisting drafts locally, scope them
  to server/user/library identity and revision, version them, and clear on logout or
  acknowledged discard. Reload never applies them automatically.
- Apply is disabled for an unchanged/invalid draft. Inline errors link to fields.
- Validation checks structure and references relevant to the submitted edit. A
  renamed channel must not fail because an unrelated channel has a deleted entity.
  Existing unresolved sources can retain metadata edits; new/changed references
  must be valid. Honest zero-result rules are valid, with an empty-pool notice.
- Each transaction supplies `expectedRevision` and a unique `requestId`. Commit
  under a process-safe lock, increment revision once, and return field errors or
  `revision_conflict` with the current revision without partial writes.
- Deduplicate retries by request ID plus payload digest. Persist commit identity
  atomically with the library document so a crash after commit cannot produce a
  duplicate channel/revision when retried. A reused ID with different content is
  rejected. Define bounded receipt retention and explicit expired/unknown results.
- An old tab cannot overwrite unrelated newer edits with a whole stale catalog.
  Rebase nonoverlapping changes against freshly loaded records; show true conflicts
  and retain both versions. Any deliberate replacement is limited to the reviewed
  touched records and still checks a freshly observed revision.
- Never replace a failed/corrupt library load with an editable empty catalog.
- A queued task ID is not success. Wait for the correlated durable Apply receipt.
  A lost response is resolved by receipt lookup, not a blind new transaction.

## 6. Storage and ownership architecture

Introduce one authoritative owner-editable library, suggested
`<data>/channel-library.json`, outside the plugin installation. It holds both
channel namespaces and groups. The compiled CSV/JSON becomes an immutable seed
and provenance artifact; deployments/reimports never overwrite an initialized
owner library. Avoid a permanently writable base-plus-overrides system and avoid
two independent writable stores for the same channel.

Suggested components (adjust filenames to fit implementation):

- `justwatch/library.py`: strict storage, validation, locking, transactions,
  migration markers, receipts, history/restore.
- `justwatch/channel_service.py`: one resolved channel/group view consumed by
  playback, editor, scheduler, status, and compatibility adapters.
- `justwatch/criteria.py`: canonical rule validation, summaries/signatures, and
  projection, sharing existing `lineup.py` behavior.
- Existing `catalog.py` / `networks.py`: retain legacy loaders/seed validation;
  route runtime consumers through the resolved view after activation.

Suggested library envelope (storage version independent of API contract version):

```json
{
  "schemaVersion": 1,
  "libraryId": "immutable generated identifier",
  "revision": 1,
  "migration": {
    "seedCatalog": "just-watch-v4-final",
    "seedDigest": "verified artifact digest",
    "legacyCatalogRevision": 21
  },
  "groups": [
    {"id": "grp_general", "name": "General", "position": 1,
     "legacySection": "general"}
  ],
  "channels": [],
  "settings": {},
  "recentRequests": []
}
```

The illustrative legacy revision is not a default. Each channel keeps its exact
`id`, `seed`, `number`, `name`, `glyph`, `color`, `sort`, `source`, programming
preferences, and enabled state, with new `groupId` and archive state. Retain seed
provenance including stable key and original legacy section. Keep historical seed
counts separate from current derived counts; do not present them as fresh health.
Validate IDs, real booleans, uniqueness, references, finite numeric values, supported
schema versions, and all supported sources strictly. Unknown future storage versions
fail loudly; never discard unknown fields by passing new records through the old
custom-only normalizer.

Migrate existing source objects byte-for-byte where possible. Merely renaming a
channel must not transform its source or change its membership/rotation signature.
For newly authored complex rules, an explicit versioned source representation may
be added, e.g. `type: criteria` with an AND list of typed ANY/ALL/exclusion/range
clauses. Validate the complete AST before projection. Never accept arbitrary
GraphQL text. Preserve exclusion semantics directly in Stash criteria (not an
incorrect NOT wrapper), hierarchy behavior, text search, and UTC recency cutoff.

Revision domains:

- Library revision changes for committed definition/group changes.
- Channel definition/membership signatures identify what requires invalidation.
- A cosmetic/group edit changes directory presentation but not the rotation or
  scheduling ledger. Preserve the legacy v1 response behavior where necessary;
  enhanced clients use additive signatures to avoid pointless playback resets.
- Health/status updates have their own timestamps/versions and do not churn
  library definition revision or cause false edit conflicts.

History is a bounded append-only revision archive under the data directory. Write
history safely around atomic primary publication; ensure a committed revision can
be recovered even if auxiliary receipt/history mirroring fails. Keep the primary
receipt journal authoritative. Exports omit secrets and are explicitly definition
exports, not a substitute for scheduler-state backup.

## 7. Migration to the selected proposal

Build an explicit migration command/task with dry-run output and backup/restore
support. No read-only operation, startup, or page visit silently migrates state.
The implementation assignment authorizes applying and rehearsing it on dev.

1. Read the actual target legacy custom catalog, compiled networks, rollout file,
   publications, and relevant revision digests. Record a manifest and immutable
   backup outside install paths. Preserve all user data and original artifacts.
2. Compile `data/proposed_channels_final.csv` through the real importer to a
   staging artifact. Validate against `networks.preview.json` and migration data.
   Use the compiled names, not `final_channels.csv`'s alternate `proposed_name`.
3. Verify exactly 513 networks, valid unique IDs/numbers/keys, source semantics,
   209/115/189 initial network groups, and 292 byte-identical survivor IDs/seeds.
   Preserve all existing custom definitions/IDs/seeds and assign My Channels.
4. Cross-check migration expectations against the actual target baseline. If
   definitions have drifted, emit a precise reconciliation report and preserve the
   unexpected records in backups/history; do not silently overwrite unrecognized
   custom data or pretend a divergent deployment matches the recorded baseline.
5. Preserve proposal exclusions exactly, including its five sanctioned exceptions.
   Do not run the old 795-row four-exception recount recipe over this proposal.
   Group changes never affect exclusions. Duplicates copy explicit source rules;
   new channels expose applicable network exclusions as visible authored defaults.
6. Preserve special sources: large materialized ANY performer/studio sets stay
   materialized; New Arrivals remains a dynamic date cutoff. The 2020s Vault upper
   date is currently 2026-12-31; show the real range, do not silently reinterpret it.
   This project must not regenerate the research selection or rename the proposal.
7. Re-read revision/digests under the transaction lock before publishing. Commit
   the fully validated library atomically with a migration marker; crash/retry
   cannot partially switch readers or import twice. A completed migration is a
   no-op on rerun and never resets subsequent owner edits.
8. Preserve retained publications, IDs, seeds, and aired history. A new channel at
   an old number never inherits the retired channel's identity/publication. Keep
   retired publications in the backup/archive and exclude them from scheduling.
   Preserve rollout settings for retained IDs; do not transfer activation by
   number. Record retired rollout entries and skip them explicitly.
9. Validate real membership against a target Stash only where IDs match that
   library. The tiny dev library will legitimately yield many empty sources;
   structural/golden fixtures, not fabricated live counts, verify seed fidelity.
10. Rehearse restore with disposable data. Restore the complete coherent snapshot
    (definitions, activation state, publications, scheduler metadata), never mix
    pre-migration definitions with incompatible future publications. Document that
    rollback is an operator recovery tool, not a channel-edit undo operation.

Legacy `catalog.json` becomes a backed-up input after migration, not an independent
writer. Legacy GetCatalog/SaveCatalog calls adapt to the authoritative library and
can touch only their custom subset, preserving all networks/groups. They use the
same global revision discipline. If a legacy source shape cannot represent an
edited custom channel, explicitly report unsupported editing to that old client
rather than silently truncate its rules. Playback compatibility is mandatory;
perfect backward editing support for every new source type is not.

## 8. API and runtime integration

Keep contract version 1 playback shapes compatible. Advertise additive feature
flags (for example `channelLibrary`, `channelGroups`, `explicitApply`, `poolPreview`)
and operation names through Capabilities. New clients discover operations there.
Suggested operations and contracts:

| Operation | Execution | Responsibilities |
| --- | --- | --- |
| GetChannelLibrary | Sync, bounded read | Revision, groups, lightweight channels, capabilities; page/search support |
| GetChannelDirectory | Sync, bounded read | Complete lightweight playback directory with ordered groups and signatures |
| GetChannelDefinition | Sync read | Full editable source/policy, labels, provenance, current revisions |
| ValidateChannelChanges | Sync, bounded | Typed transaction errors and effect summary, no writes |
| PreviewChannelPool | Sync, bounded | Draft-signature-correlated count/sample; no publication or mutation |
| ApplyChannelChanges | Task | Atomic touched-record transaction with expectedRevision/requestId |
| GetChannelApplyResult | Sync read | Correlated queued/committed/rejected state and durable receipt |
| GetChannelHistory | Sync, paged | Bounded definition revisions; restoration submits a new Apply |

Existing Directory, FullDirectory, Lineup, Schedule, ProgrammingStatus, and desk
operations use the same authoritative resolved channel service. Migrate direct
`networks.load/get` and `catalog.load` runtime call sites, especially scheduling
commit guards, so editing the GUI cannot disagree with what airs.

The enhanced directory carries ordered groups plus per-channel `groupId`, channel
signatures, and current count freshness. Continue supplying
legacy `channels` and `networks` fields, legal legacy section names, and historical
number bands in the existing Directory operation. Do not put arbitrary new group
names into the old `section` field. A new TV client uses GetChannelDirectory when
negotiated and the legacy Directory/sections otherwise. Both resolve the same
library revision; do not maintain separate manually synchronized definitions.

Represent an intentionally empty owner library distinctly from an absent plugin
or missing network seed. All channels paused/archived must yield an empty guide;
it must not resurrect 795 compiled or client-generated channels. Compatibility
adapters must return an authoritative empty network block where appropriate.

Large source definitions are loaded on demand in the new operations, not embedded
in every enhanced directory refresh. Preserve fields in the old Directory response
for compatibility, including existing full source fields; do not silently remove
them to optimize the old contract. Page/search editor list results and aggregate labels without N entity requests
per list row. Debounce/cancel preview requests; ignore stale responses by draft
signature and resolved time epoch. Bound response counts and scan work. A preview
request cannot launch full 513-channel indexing.

No GraphQL connection performs Apply's background publication work. Resolve changed
references and validate before the short commit lock; recheck revision and identity
inside it. After commit, schedule incremental count/index/publication work for only
affected sources. Cosmetic edits, group moves, and number changes do not index
content. Share identical-source caches and preserve bounded scheduler fairness.

Durably record pending refresh work with the applied revision/signature so a crash
between commit and task dispatch is recoverable by the next scheduler run. A stale
worker may not publish counts/schedules for superseded definitions. Revalidate
channel existence, effective mode, source/policy signatures, and publication
generation under the commit guard. Keep the existing exactly-once ledger contract.

Applied empty pools, deleted sources, transport failures, and stale historical
counts are distinct states. No global legacy tuning settings silently alter owner
sources. New user edits may intentionally change explicit exclusions; never hide
those changes inside a group move or import.

Unknown/pending counts are not zero and must not hide a channel as off air. Enhanced
TV clients consume explicit pool status/freshness; compatibility payloads retain
their documented numeric count semantics and last-known values where available.
Test this distinction for newly created channels and source edits while refresh
is pending. Advertise the larger editable-library limits separately from the old
custom-channel limit of 99; do not silently change what legacy limits describe.

## 9. Android TV integration

Add a dynamic group model with stable string IDs and ordered channel membership;
retain the legacy enum for plugin-absent/old-server fallback. Avoid adding a Kotlin
enum entry for each user group. Keep existing ChannelKey.Custom/Network keys so
favorites, history, deep links, and last-channel state retain surviving identities.

Update parsing, ChannelDirectory, ViewModel refresh, guide headings/filter controls,
explore/browse, landing surfaces that show groups, number pad legends, and any
section-dependent navigation. Search all references to `JustWatchSection`, fixed
section strings, network section allowlists, and section-based `all` ordering.

Required behavior:

- Every enabled channel appears exactly once in its group; archived/paused
  channels are omitted. Group rename/reorder/move is reflected after refresh.
- Preserve selected group/focus across refresh when its ID survives; choose a
  deterministic nearby group/channel if deleted. Renaming does not lose focus.
- Do not interrupt playback for cosmetic metadata changes. Membership changes
  invalidate affected future lineups only; handle source-incompatible schedule
  fallback explicitly and finish an already playing valid stream when possible.
- Group order and number-sorted membership agree with surfing order. Pad lookup
  remains global and unique even if displayed groups contain sparse numbers.
- Surviving favorites/history retain identity. Retired IDs cannot retune to new
  unrelated channels that reused their numbers. Retired favorites are pruned or
  marked unavailable; stored last-channel fallback is deterministic.
- Define unknown/malformed group handling without losing otherwise valid channels:
  use a stable recovery group for malformed wire membership, emit diagnostics,
  and keep strict server-side rejection for invalid stored definitions.
- Support new plugin/new TV, new plugin/old TV, old plugin/new TV, and no plugin.
  Test a present-but-empty authoritative library separately from plugin absence.

Run focused JVM tests and build a debug APK. Device verification uses only the
permitted dev stick (or emulator fallback under the current TV instructions).
Exercise guide navigation, groups, pad, favorites, refresh, and in-progress playback.
Dev browser access uses port 9998. Never print API keys into logs or reports.

## 10. Work sequence and dependencies

### Phase A — establish baseline and prototype fixture

Read current instructions/status in both repos; preserve unrelated working files.
Record actual versions, runnable test commands, and baseline failures. Inspect the
latest continuing review and separate reproduced defects from stale claims.
Compile/validate the selected seed into a temporary artifact; create shared mock
fixtures and small deterministic behavioral fixtures. Do not change live networks.

### Phase B — implement and present five prototypes

Build all variants and verify the shared review script. Provide working URLs,
launch instructions, and short comparisons. Request owner feedback once and mark
the design selection pending. Prototypes are not the production UI deliverable.

### Phase C — independent foundations while feedback is pending

Implement strict library storage, transactions/receipts, history, groups, migration,
source validation/projection, and compatibility adapters. Add backend operations,
incremental refresh, scheduler resolution/commit guards, and TV dynamic-group data
support with tests. These are authorized while UI selection is pending. Dev
migration rehearsal is also independent of frontend selection.

### Phase D — integrate the selected production frontend

After actual feedback, record the design decision and build that experience into
Stash's registered route. Reuse appropriate prototype components, replace the mock
adapter with real capabilities/operations, and implement full draft/Apply/error
behavior. Retain useful prototype assets for review but ship only the chosen UI.

### Phase E — end-to-end dev rollout and regression validation

Back up dev data, execute the explicit migration, reload the dev plugin, and
exercise browser → Apply → directory → TV group/lineup refresh. Build/install only
on an allowed test target. Verify restart persistence, idempotent migration,
concurrent editing, and rollback on disposable data. Complete relevant baseline
fixes needed for new behavior; do not hide failures by changing assertions.

### Phase F — delivery

Update README, applicable AGENTS.md, API/schema/migration documentation, tests, and
operator runbook. Provide implementation report, test evidence, artifact locations,
known limitations, and production release/restore commands with prerequisites.
Do not leave selected frontend integration, tests, or dev verification as a future
suggestion after the sole design-review gate has been satisfied.

## 11. Verification and acceptance matrix

Use behavior tests, not tests that mirror helper implementation. Existing Python
suite: `python3 -m pytest tests/ -q`. Discover actual Gradle task/flavor names in
this checkout; focused examples are `./gradlew :app:testDebugUnitTest` with relevant
JustWatch test filters, followed by `./gradlew :app:assembleDebug`. Add a reproducible
browser test harness if absent; dev dependencies must not become Python runtime deps.

| Area | Required evidence |
| --- | --- |
| Seed fidelity | 513 networks; exact survivor identities; expected group totals; source equivalence incl. large ANY sets and exclusions |
| Custom preservation | All actual custom records/IDs/seeds/preferences preserved; metadata-only edits preserve source signatures |
| Migration safety | Repeat after owner edit is no-op; interrupted commit/backup recovery; baseline drift detected; no number-based state inheritance |
| Library integrity | Corrupt/future schema rejected; missing seed distinct from intentional empty; group/number/ID constraints strict |
| Apply isolation | No write before Apply; apply one channel cannot publish another draft; later in-flight edits remain dirty |
| Concurrency | Two-process revision race; same-ID retries before/after lost response; changed-payload rejection; no lost unrelated edits |
| Error UX | Missing source, zero pool, transport failure, stale preview, apply conflict, receipt timeout, and retry preserve draft truthfully |
| Rules | ANY vs ALL, AND between rows, exclusion precedence, descendants, range boundaries, text q, UTC recency, deleted entities |
| Counts/performance | Directory requires no scene fetches; metadata Apply performs no full-tier refresh; content Apply targets changed sources |
| Background recovery | Crash after commit still refreshes; stale worker cannot overwrite current counts/schedule; status aging agrees with playback |
| Scheduling | Cosmetic/group edits retain ledger; pool removal cannot serve invalid future/encore; policy edits preserve protected airings |
| TV groups | Dynamic names/order; exactly-once membership; pad/surf order; focus preservation; surviving favorites and retired identities |
| Compatibility | Four client/server combinations; empty authoritative library does not restore generated channels |
| Browser accessibility | Keyboard, labels, visible focus, modal focus return, non-color status, narrow layout, large lists |
| Persistence/restore | Server restart retains all Apply edits; export/history restore; disposable full-backup rollback |

Measure list/search rendering and preview/apply timings on a documented environment
with all 513 networks plus customs. Target responsive local interactions (around
100 ms for search/filter feedback), with immediate progress feedback for network
work. Report actual p50/p95 where useful; do not invent timings or impose a brittle
network latency assertion. Test thousands-of-entity sources without truncation or
an unbounded DOM. Use pagination/windowing when measurement warrants it.

The newer scheduler review is a baseline risk, not a request for another user
approval. Reproduce relevant findings before relying on their surfaces. Fix task
failure propagation, status truthfulness, migration preservation, and stale commit
issues when necessary for this feature. Do not advertise an exactly-once/continuous
playback guarantee based on the old proximity-only simulation. Any continuing
behavior changed by this project needs independent ledger/airing evidence. Do not
expand production activation to evade test gaps. Record unrelated unresolved
baseline defects accurately, with reproductions and their effect on supported use.

## 12. Deliverables and definition of done

- Five runnable, comparable interactive prototypes and the owner's recorded choice.
- The selected functional Channel Studio UI integrated into Stash.
- An owner-editable library seeded from the 513 proposal plus existing customs.
- Dynamic single-membership groups in both browser and TV.
- Explicit Apply with durable receipts, optimistic concurrency, preserved drafts,
  correct previews, incremental refresh, and honest status.
- Source editing that round-trips the baseline and preserves identity/state.
- Migration, definition export/history restore, complete backup/rollback tooling,
  and compatibility coverage.
- Passing relevant Python/browser/TV checks; working dev integration and APK.
- `docs/CHANNEL-CURATION-IMPLEMENTATION-REPORT.md` with actual evidence,
  `docs/CHANNEL-CURATION-DESIGN-DECISION.md`, updated user/operator documentation,
  and an executable release runbook (production deployment not performed here).

Report limitations precisely. If a required test or device is unavailable, use
available independent checks and disclose the remaining evidence gap; do not mark
unperformed verification as passed. The only planned design decision awaiting the
owner is the prototype review. Routine technical decisions belong to the implementer.
