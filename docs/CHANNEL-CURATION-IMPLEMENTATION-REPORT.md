# Channel curation — implementation report

Date: 2026-09-23. Repos: `stash-justwatch` @ master, `StashAppAndroidTV`
(dev worktree). Owner decisions and scope per
`docs/CHANNEL-CURATION-HANDOFF.md`; technical spec per
`docs/CHANNEL-CURATION-IMPLEMENTATION-PLAN.md`.

## 0. TL;DR

* The **five prototypes are live** at `http://localhost:8462/` (tailscale:
  `https://dev-lab2.manx-teeth.ts.net:8462/`) — start with
  `python3 tools/serve_prototypes.py`. **The production frontend is not
  chosen yet**: that is the single owner review gate
  (`docs/CHANNEL-CURATION-DESIGN-DECISION.md` is created and PENDING).
* The independent foundations are implemented and tested end to end:
  authoritative editable library, transactions with durable idempotent
  receipts, dynamic single-membership groups, the visual-rule criteria model,
  the editing API surface, incremental refresh with stale-worker rejection,
  the 513-network migration tool (rehearsed on disposable data), and the
  continuing-engine baseline fixes (S1–S4) the review demanded.
* Full plugin suite: **323 passed** (262 baseline + 61 new).
* Dev end-to-end validation and the TV integration are covered in §6; the
  production release is NOT performed (runbook: `docs/CHANNEL-CURATION-RELEASE.md`).

## 1. Changed files / commits (plugin repo)

| Commit | Content |
| --- | --- |
| `7ae2e76` | Five frontend prototypes + landing + server + fixture generator |
| `1f1fad5` | `library.py`, `criteria.py`, `channel_service.py`, `channel_ops.py`, `refresh.py`; `main.py` runtime routing; `contract.py` additive capabilities; continuing S1/S2/S4 fixes + service-routed commit guards |
| `baedf51` | Test suites (review regressions S1–S4, library, ops/rules, migration) + migration-tool fixes |
| `(post-baedf51)` | Authoritative-empty `networks` block; legacy-SaveCatalog truncation guard; compat test matrix |
| `b028ed6` | Docs: API additions, migration runbook, release runbook, design-decision placeholder, AGENTS.md supersessions |
| `2996e68` | README: the editable channel library |

New plugin modules: `justwatch/library.py` (authoritative store:
strict schema-1 loading, process-safe lock, touched-record transactions,
receipts committed atomically with the document, bounded history),
`justwatch/criteria.py` (rule model: legacy shapes verbatim, composite
`filter`, new `criteria` ANY/ALL-per-facet + exclusions + metadata rows),
`justwatch/channel_service.py` (one resolved view for every consumer),
`justwatch/channel_ops.py` (editing surface, 8 additive operations),
`justwatch/refresh.py` (durable pending-refresh journal, stale-worker
rejection).

## 2. Prototype review gate (the one planned blocker)

Variants A–E (Channel Studio, Group Organizer, Channel Library, Visual
Guide, Guided Curator) are genuinely different workflows over the same
shared mock data — the real 513-network v4-final proposal plus synthetic
customs and fixture channels (empty pool, deleted entity, paused, archived,
a 3,569-performer rule row, a 60-char name). Every count, preview, schedule
and artwork pixel is labeled simulated; nothing touches a live server.

Verified in a real browser (Playwright) against the plan's shared review
script: search, rename+group change with visible draft state, ANY/ALL +
exclusion rule editing with plain-language summary, pool-vs-rotation
preview, Apply progress/acknowledgment, Discard, draft survival across
navigation (and reload within the tab), bulk move into a NEW group with one
Apply, conflict/lost-response/validation/slow scenarios with the draft
always intact, keyboard-only core flow, and the ≤760 px layouts.

**Awaiting: the owner's choice and feedback** (recorded verbatim in
`docs/CHANNEL-CURATION-DESIGN-DECISION.md` when it arrives). Production UI
integration (Phase D) starts only after that response; it will replace the
mock adapter with the real operations on the PluginApi React runtime at
`/plugins/stash-justwatch`.

## 3. Baseline: continuing review (docs/CONTINUING-PROGRAMMING-REVIEW-2.md)

All six plugin-side findings were first REPRODUCED at `5b33aa1` with the
review's own probes (`analysis/continuing-review/recheck_v071.py`), then
fixed; `tests/test_review2_regressions.py` locks the fixed behavior:

* **S1 (P1)** — selection replay re-consumed completed history (338 vs 336
  exposures in the review's probe). Fixed by starting the reservation replay
  at the actual-history cursor, in both replay sites. Tests assert the
  consumption total and cover the arrival-rebuild branch.
* **S2 (P1)** — the repeat guard ranked most-recent FIRST (167/41 immediate
  adjacent repeats on 2/8-scene fixtures). Now the most-recently-aired scene
  ranks strictly last within the guard while the rest follows the current
  pass's shuffle: 0 adjacent repeats at 2/8/30/100 scenes, and consecutive
  passes still vary (no strict-LRU).
* **S3 (P1)** — custom-channel and rollout task failures never failed the
  task (boolean-vs-"failure" comparison). One explicit outcome vocabulary now
  classifies both groups; deferrals still succeed.
* **S4 (P1)** — the task path handed `build()` a `None` prior for schema-2
  publications, dropping a live airing on migration. The real prior is now
  passed at every schema; a current airing survives (test with a
  nonempty legacy publication).
* **S5 (P2)** — simulator duplicate gate remains a proximity heuristic
  (unresolved). Not depended on: the exact-once invariant is covered by the
  new ledger-adjacent tests instead, and continuing activation is NOT
  expanded by this project. Recorded as a known limitation.
* **S6 (P2)** — expired/encore status flags (unresolved, baseline). Recorded
  as a known limitation; no consumer added by this project relies on it.
* **S7 (P2)** — the wall-clock-dependent TV recovery test: deterministic
  fixture (see §6).

## 4. Acceptance matrix evidence

| Area | Evidence |
| --- | --- |
| Seed fidelity | `tests/test_migration_tool.py`: 513 networks byte-equal to the preview (id/number/seed/source/name), section totals 209/115/189, all 292 keep-rows' stable keys exact, five JAV exceptions (225/252/407/461/480) confirmed un-excluded |
| Custom preservation | same file: ids/seeds/sources/policies preserved incl. `savedFilter` and disabled channels; metadata-only edits proven to not reindex (`test_metadata_apply_does_not_enqueue_reindex…`) |
| Migration safety | no-op rerun after an owner edit (byte-equal document); backup written; restore reinstates legacy state; drift (a "keep" slot renamed on the deployment) reported with exit code, nothing written |
| Library integrity | corrupt/future-schema raise and are never reset; unknown fields rejected; duplicate numbers/ids rejected; dangling group refs rejected; booleans strict (`tests/test_channel_library.py`) |
| Apply isolation | applying one channel leaves others untouched; in-flight edits stay dirty (prototype state machine; production mirrors it via snapshots); ops apply only submitted ops |
| Concurrency | two-process commit race → exactly one committed + one revision_conflict; same-id retry replays the receipt even with stale expectedRevision; changed-payload same-id rejected |
| Error UX | conflict/transport/validation scenarios keep the draft (prototype, verified in browser); lost response resolves via receipt lookup; `status: unknown` documented |
| Rules | ANY/ALL fields, AND rows, exclusion riding criteria, exclude-only pools, depth -1, range boundaries, UTC recency cutoff, signature stability, conflicting ANY+ALL rejected (`tests/test_channel_ops.py`) |
| Counts/performance | Directory/GetChannelLibrary do zero scene queries; metadata Apply performs no reindex (journal empty); content Apply touches exactly the changed channel |
| Background recovery | crash-after-commit recovered by the journal (drained by PrepareProgramming); stale worker re-queues against the current signature under the lock (`test_stale_worker_publication_is_requeued…`) |
| Scheduling | cosmetic/group edits retain the ledger; pool-change preparing/fallback semantics via journal + signatures; policy edits preserve protected airings (continuing suite unchanged) |
| Compatibility | `tests/test_library_compat.py`: legacy Directory shapes on a migrated deployment; owner group names never enter `section`; intentional-empty tier = present-but-empty block; fully-paused library = empty playable surface; legacy SaveCatalog maps to a customs-only Apply and refuses to truncate composite rules; Capabilities stay v1-additive |
| Browser accessibility | keyboard-only core flow, visible focus rings, Esc/Tab handling, non-color status chips, narrow layouts (screenshots in `prototypes/channel-curation/screenshots/`) |
| Persistence/restore | Apply receipts are part of the atomic document (restart-safe by construction + dev restart check in §6); disposable full-backup restore tested |

### Measurements (documented environment: dev-lab2 VM, Python 3.14, Chromium via Playwright)

Server-side, real 513+4-channel library document (p50 / max over 5–7 runs):
`GetChannelLibrary` full 517 rows 14.5 / 17.0 ms; searched query 8.2 ms;
Apply metadata-only on one channel 58.3 / 61.9 ms; Apply rule edit on the
3,569-id performer source 65.7 / 87.2 ms; `GetChannelDefinition` of the
3,569-id channel 11.4 ms; `PreviewChannelPool` = one bounded count+page
query (3.7 ms against the in-process fake; network RTT dominates live).

Browser-side (prototype C, 519 rows): search/feedback ≈ 0–1 ms after the
140 ms debounce; full table re-render 89 ms — inside the ~100 ms
interaction target. Entity picker with 3,569 selected stays windowed
(≈5–30 DOM rows; storage keeps every id).

## 5. Known limitations / baseline gaps (unresolved by this project)

1. **Continuing S5** — the simulator's duplicate gate remains a proximity
   heuristic; the 30-day simulation is superseded as evidence. Continuing
   activation was not expanded, so this does not gate the library feature.
2. **Continuing S6** — expired publications do not flag `expiring`/`encore`
   truthfully at read time (baseline). The library's pool-status surface
   carries its own freshness and does not consume those flags.
3. **Generation guard digest** — review's design check (digest omits some
   index metadata; policy-signature revalidation) remains as documented;
   the refresh journal adds signature revalidation around NEW publications
   (tested), but the pre-existing continuing generation token is unchanged.
4. **Prototype scope** — mock data; simulated counts everywhere; the
   prototype adapter is not the production data source (Phase D replaces it).
5. **Dev library emptiness** — the 10-scene dev Stash yields mostly empty
   pools after migration; seed counts are historical and clearly labeled.

## 6. Dev end-to-end validation and TV integration (executed 2026-09-23)

**Dev deployment migrated** (`/opt/stash-dev/stash-justwatch-data`):

* dry run clean (4 customs + 513 networks, reconciliation 292/221/282, no drift);
* `--apply` committed `lib_ee2370f095` revision 1 with an automatic backup
  (`/opt/stash-dev/migration-backup-20260923-122640` containing catalog.json,
  networks.json, continuing-networks.json, programming/, snapshots/);
* **restore rehearsed on the live dev data**: restored, verified
  `channel-library.json` gone + legacy files back, then re-applied;
* retired rollout id recorded: `retiredRolloutIds: ["net_8091d3ea"]`; the two
  surviving rollout ids stay active; no activation transferred by number.

**Plugin reloaded on dev** (`reloadPlugins`), then verified over GraphQL:

* `Capabilities` advertises `features.channelLibrary`;
* `GetChannelLibrary`: revision 1, 517 channels, groups
  My Channels / General / Studios / Performers;
* `Directory` keeps the legacy shape: 4 customs + 513 networks with legal
  legacy `section` names — old TVs are unaffected in shape.

**Browser-path edit → Apply → persisted definition → directory refresh**
(driven through the exact GraphQL calls the production UI will make):

1. `ApplyChannelChanges` (task, `channel.create` tempId `temp-e2e`) →
   receipt `committed` rev 2 with `idMap {temp-e2e: net_5e7b27c4}`;
2. `GetChannelApplyResult` by requestId returns the durable receipt;
3. `GetChannelDefinition` shows the persisted record + plain-language summary
   ("1. Scenes tagged with any of: #9320 (sub-tags included).");
4. `Directory` (old-TV view) lists the new channel at #880;
5. idempotent retry with the SAME requestId replayed the same receipt (no
   duplicate channel, revision unchanged);
6. archive via `channels.patch` → committed rev 3; the channel remains listed
   in the library (`archived: true`) and disappears from the playback
   Directory; 513 networks remain.
   (The archived "Curation E2E Probe" #880 / net_5e7b27c4 is left in the dev
   library as a visible test artifact.)
7. A rejected probe (referencing a non-existent channel id) produced a typed
   `rejected` receipt and changed nothing.

Findings fixed during the e2e: `runPluginTask` needs a manifest task entry
(added: "Apply Channel Changes"), and archived/paused nets now leave the
legacy playback surfaces (`f0db590`).

**TV integration** (repo `StashAppAndroidTV`, commit `46c23ad3` on top of
`61f92187`): dynamic owner groups implemented per the plan (capability-gated
`GetChannelDirectory` parsing with `grp_other` recovery, dynamic ordering =
group position + owner number with exactly-once membership, unchanged
`ChannelKey.Custom/Network` identities so favorites/history survive,
owner-numbers-only pad ranges, guide group tabs/subtitles/CH-jumps from the
loaded group list, focus preservation by group id, present-but-empty library
= empty dial with no generated fallback, legacy enum path byte-for-byte for
old plugins, revision + structural-fingerprint refresh detection so cosmetic
changes never retune). The wall-clock-dependent `JustWatchRecoveryTest`
fixture (review S7) is now deterministic and stable across reruns.

* `:app:testDebugUnitTest`: **859 tests, 0 failures** (justwatch suites:
  189); `./gradlew :app:assembleDebug` BUILD SUCCESSFUL;
* APK (dev-install artifact):
  `~/dev/StashAppAndroidTV/app/build/outputs/apk/debug/StashAppAndroidTV-debug-0.9.1-125-g61f92187-106-armeabi-v7a.apk`
  (+ arm64 + universal);
* Device check on the permitted dev stick `.105` (USB serial
  G072JN0734330EBH): APK installed (`Streamed Install: Success`), app
  launches and navigates to Just Watch with zero crashes; the landing and
  tune surfaces serve MIGRATED-library channels (CH 120, 259, 260, 262, 264
  with live program/`min left` data from the real dev Stash); playback
  started normally. NOT driven on-device: the guide's group-tab UI (Compose
  focus could not be driven reliably via adb within this session); the
  dynamic-group UI ordering is covered by the 20 new JVM tests
  (`ChannelLibraryTest.kt`) and available for hands-on review on the stick.
  Production `.169` was not touched.

## 7. Deliverable map

* Prototypes + review gate: `prototypes/channel-curation/` (+README),
  `docs/CHANNEL-CURATION-DESIGN-DECISION.md` (PENDING).
* Selected production UI: **blocked only on the owner's feedback** (Phase D).
* Backend/API: `docs/CHANNEL-CURATION-API.md`; code map in AGENTS.md.
* Migration/rollback: `docs/CHANNEL-CURATION-MIGRATION.md`; production
  release steps: `docs/CHANNEL-CURATION-RELEASE.md` (not executed).
* TV changes + APK: §6.
