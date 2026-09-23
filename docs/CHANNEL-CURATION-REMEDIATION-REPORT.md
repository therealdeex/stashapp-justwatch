# Channel curation — remediation report (C1–C12)

2026-09-23. This report closes the corrective assignment against
`docs/CHANNEL-CURATION-AUDIT.md` (independent audit, 2026-09-23) following
`docs/CHANNEL-CURATION-REMEDIATION-PLAN.md`. The audit's pre-fix evidence
(`analysis/channel-curation-audit/reproduce.py` → `results.json`,
`browser-reproduce.cjs` → `browser-results.json`) is preserved verbatim as
provenance; every fix below is backed by NEW post-fix assertions.

Tested commits:

* Plugin `stash-justwatch`: **`cb2da5d`** (`fix(curation): remediate audit
  findings C1-C12`) on master, audit baseline was `2536613`.
* TV `StashAppAndroidTV`: **`5ed36fd4`** (`justwatch: per-channel
  invalidation from library content identities (C12)`) on main, audit
  baseline was `46c23ad3`.

Preserved owner decisions and data: the dev library was migrated ONCE
before this work and kept authoritative throughout (revision moved 14 → 16
only through the verification applies below); no reseed, no tier revert, no
rollout/kill-switch change, no scheduling-history reset, no content-policy
change. Option A remains the frontend; dynamic performer/studio selection
remains a supported feature with corrected, documented semantics.

## Fix/test matrix

| # | Audit finding | Fix (commit) | Post-fix evidence |
| --- | --- | --- | --- |
| C1 | Production Channel Studio throws at boot (`draftsRef.current.count is not a function`) | `cb2da5d` `ui/index.js`: real in-memory map as the store's source of truth (sessionStorage is a write-through mirror; storage failures degrade to memory, never to empty re-reads); `count()` implemented | `browser-verify.cjs` `c1_production_boot` (unmodified bundle, zero page errors) + `c1_storage_failure_memory_fallback` (drafts survive a throwing sessionStorage); live boot on dev Stash below |
| C2 | Preview uses the new projector; Lineup + both indexers use the old one; criteria sources throw at playback; edited filter rows silently dropped; distinct criteria collapse to one key | `cb2da5d`: ONE projector — `criteria.build_scene_filter` handles all source types; `lineup.build_scene_filter` delegates; preview/Lineup/health/custom-index/continuing-index all consume it; authored `q` rides every path (`criteria.text_query_of`); `lineup.source_key` distinguishes criteria sources and covers filter exclusions/duration-max/scene-counts/q | `test_curation_remediation.py::TestSourcePlaybackParity` (criteria through real Lineup + both indexers; editor==playback projection equality; distinct keys; q in every query) + dev live check below |
| C3 | Rejected transaction persists staged ops without a revision bump; rejected receipts lack digests so retries don't replay | `cb2da5d` `library.py`: ops staged on a deep candidate; every rejection commits ONLY its receipt against the untouched document; rejected receipts carry the payload digest | `TestTransactionAtomicity` (pause + conflicting creates: byte-identical channels, revision unchanged, exact replay) |
| C4 | `channels.patch` validation unreachable (stale `kind`); arbitrary seed patch commits | `cb2da5d`: patch branch validated in the main loop (list shape, allow-list, real booleans); `_enforce_identity` on the final candidate through every opcode | `TestIdentityEnforcement` (seed/id/kind/provenance/mixed/non-boolean/unknown-key patches all rejected, document unchanged; final-candidate enforcement unit) |
| C5 | Commit→enqueue crash window loses work; concurrent enqueues deleted by worker's journal rewrite; two-channel batches keep only the last health record | `cb2da5d` `refresh.py`: intent journaled by a `pre_commit` hook INSIDE the transaction/lock (no window); worker acknowledges only the generation it processed; health merges into the CURRENT snapshot per channel under the writer lock; entries whose stored health matches the current signature drop as current | `TestDurableRefreshWork` (simulated loss aborts with nothing committed; retry journals + drains; concurrent enqueue survives; both channels' health present; cosmetic Apply does zero scene queries) |
| C6 | Schedule serves source-incompatible publications/encore; stale workers publish (sort changed mid-build); commit guards under the wrong lock | `cb2da5d`: `_op_schedule` read-gates publication compatibility (stale pool serves only the running airing, `status: preparing` + `stalePublication`); custom publications gain a `generation` commit token; publication commits run under `channel_service.writer_lock` (`.library.lock` on library deployments) with source+sort+seed+policy+state+generation guards | `TestScheduleAndBuildGuards` (stale pool not served, running airing preserved, future empty; sort-edit worker returns `changed_during_build` and publishes nothing) |
| C7 | Modes disagree across consumers; rename strips continuing policy; GUI offers unservable modes | `cb2da5d`: `channel_service.effective_programming` is the ONE resolver (both directories, Schedule, editor, schedulers); network adapter passes `programming` through losslessly; `library.normalize_programming` preserves continuing knobs; mode CHANGES validated per namespace (`bad_mode_for_kind`); UI offers only namespace-servable modes | `TestProgrammingModeParity` (pin beats rollout everywhere identically; `newShare` survives rename; `ch`+continuing rejected; group-create+move atomic) + dev live check below |
| C8 | Edits typed while applying are lost; stale full-record draft overwrites external edits via a borrowed revision | `cb2da5d` `ui/index.js`: drafts always persist (including mid-Apply); each draft pins its acknowledged `base` and its pending request identity; three-way rebase (`rebaseDraft`) on init and on every library refresh — untouched fields take server values, touched fields keep local values, same-field conflicts are NAMED for review; creations remap newer drafts to the final id | `browser-verify.cjs` `c8_newer_draft_survives_navigation`, `c8_stale_draft_rebase` (external color kept + local rename kept), `c8_transport_retry_same_request` |
| C9 | Groups autosave on blur; group+move is two writes; transport failure announced as "Nothing changed" | `cb2da5d`: Groups dialog is a staged draft with Apply/Discard (zero tasks on blur/Enter/reorder/create); bulk move builds ONE `[group.put, channels.move]` transaction (server validates same-transaction groups); `submitOps` persists the request identity and reuses it for identical retries; transport failure is reported as UNKNOWN, never "nothing changed" | `browser-verify.cjs` `c9_group_rename_stages` (0 tasks before Apply), `c9_group_create_move_atomic` (single task, 2 ops) |
| C10 | Drift reported but exit 0 + library written anyway; restore overlays newer files; legacy inputs read loosely; compiled artifact/scheduler state not backed up | `cb2da5d` `tools/migrate_channel_library.py`: fail-closed drift (exit 2, dry-run AND apply, before any backup/commit); keep-row identity reconciliation (id+seed, not just names); strict legacy loaders; commit-time legacy digest recheck under the lock; hash-manifested backups incl. the compiled artifact when external, pending journal, history tree, and recorded ABSENCES in collision-safe dirs; exact resumable manifest-driven restore (hash verification, tree replacement, absence recreation) | `test_migration_tool.py` (drift → exit 2 both modes, no library, no backup; exact restore removes post-backup rollout/publications/library) + audit probes now return the intended values |
| C11 | Preview ignores `q`, estimates rotation from count, claims completion; missing entities/dates accepted; dynamic max semantics inconsistent; capability check unimplemented; docs claimed raw scene_count ≡ proposal counts | `cb2da5d`: preview runs the shared `fetch_rotation` (measured `rotationSize`/`rotationComplete`); `criteria.validate` rejects non-numeric ids and impossible calendar dates; `ValidateChannelChanges` checks changed references against Stash (bounded 60, metadata-only edits of broken sources stay recoverable) and probes nested-filter support with a typed error; dynamic thresholds defined once (min inclusive, max EXCLUSIVE; BETWEEN max−1); design-decision doc corrected (raw scene_count = total-library activity, NOT the proposal's post-JAV eligible counts; seed definitions untouched) | `TestPreviewTruth` + `TestSourcePlaybackParity::test_dynamic_max_is_exclusive_including_combined_bounds`; dev live checks below |
| C12 | TV invalidates ALL channels on any revision bump (cosmetic edits included); per-channel identities unparsed; global revision prefixed onto Lineup versions | `5ed36fd4` (TV): `LibraryChannel` parses membershipSignature/sort/seed; refresh diffs per-channel `contentIdentity` — cosmetic changes invalidate nothing, content edits invalidate only that channel's timeline + guide row; legacy path unchanged; plugin drops the global-revision prefix on library Lineup versions (`cb2da5d`) | `JustWatchRefreshInvalidationTest` (4 ViewModel-level tests: cosmetic → zero invalidations + zero refetches; source edit / removal / mode change → only the affected channel) + `test_library_compat` (rotationVersion stable across a cosmetic revision) |

## Commands run (all green)

```sh
# plugin (python3.14, stdlib only)
python3 -m pytest tests/ -q                 # 361 passed (329 baseline + 32 new)

# plugin browser verification — REAL production bundle, NO shims
npm install --prefix /tmp/jw-curation-audit-browser --no-audit --no-fund react@18 react-dom@18 playwright-core
JW_AUDIT_PLAYWRIGHT=/tmp/stash-verify/node_modules/playwright-core \
JW_AUDIT_REACT_ROOT=/tmp/jw-curation-audit-browser/node_modules \
JW_AUDIT_CHROME=/opt/google/chrome/chrome \
node analysis/channel-curation-audit/browser-verify.cjs   # PASS: true (7 cases)

# TV (focused + full JVM)
cd ~/dev/StashAppAndroidTV
./gradlew :app:testDebugUnitTest --console=plain              # BUILD SUCCESSFUL
./gradlew :app:testDebugUnitTest --tests '*JustWatchRefreshInvalidationTest' \
  --tests '*ChannelLibraryTest' --tests '*JustWatchPluginClientTest' \
  --tests '*JustWatchRecoveryTest' --tests '*PublishedScheduleTest'   # 48/48
./gradlew :app:assembleDebug --console=plain                  # BUILD SUCCESSFUL
```

Audit-probe cross-check: `python3 analysis/channel-curation-audit/reproduce.py`
against the remediated code now yields the intended outcomes for every case
it covers (criteria through Lineup/indexers, `patch_identity` rejected,
`rejected_partial` byte-identical + exact replay, `crash_window` recoverable,
`lost_enqueued_work` preserved, `lost_health` both channels,
`stale_schedule` preparing + stalePublication, `mode_disagreement` fixed
everywhere with `newShare` preserved, `stale_worker_policy` refuses to
publish, `preview_truth` measured rotation with `q`, `migration_drift` exit
2 with no write, `rollback_overlay` exact). The original `results.json` /
`browser-results.json` remain as pre-fix evidence.

## Dev verification (real Stash on :9998, post-fix code via the live symlink)

Backup first: `/opt/stash-dev/stash-justwatch-data-backup-remediation-20260923-182408`
(full data-dir copy, taken before any verification write).

* `reloadPlugins` → `Capabilities` advertises `features.channelLibrary`
  version 1 (0.12 s).
* `GetChannelLibrary`: **518 channels** (513 proposal + customs + owner
  edits), first page 0.15 s, full list in one additional page — the
  513-channel list is fast and bounded. Per-channel `membershipSignature`,
  `sort`, `programmingMode` all present for the TV.
* `PreviewChannelPool` with a dynamic criteria source
  (`tagsAny + studioSceneCount max`) on the REAL dev Stash: ok in 0.2 s,
  pool 1 / rotation 1 / complete — the nested-filter path works live. A `q`
  preview returned pool 0 for a non-matching term (the text filter provably
  reached the query).
* `ValidateChannelChanges` with a nonexistent studio id → `missing_entity`
  (plus `bad_kind` for the deliberately-kindless probe record).
* **Real Apply flow**: created `net_2555bca3` "ZZ remediation-verify
  (archived)" #881 from a criteria source (committed r15 in 1.6 s, receipt
  replayed exactly), `Lineup` for the criteria channel returned real scenes
  ("The Lost Pilot") — the C2 fix live — then archived it (r16). The pending
  journal drained to `[]`. The verification channel remains in the library,
  archived, clearly named for the owner to delete at will.
* `Schedule` on rollout-listed `net_461ab3e3` → `fixed` — correct: its
  stored authored pin is `{"mode": "fixed"}` and the pin wins everywhere now
  (this is precisely the C7 disagreement the audit demonstrated).
* Production UI (`/plugins/stash-justwatch` on the real server, logged-in
  browser): boots with **zero page errors**, renders the 518-channel dial at
  r16, and a no-Apply edit stages exactly one draft pill ("1 unsaved draft")
  — C1 + the no-write-before-Apply contract on the live deployment.
* **TV (dev stick `G072JN0734330EBH` / .105, per the device policy)**:
  installed the remediation APK, launched, navigated to Just Watch →
  the landing page showed a real library channel ("CH 4 · Studio Showcase",
  NOW PLAYING with time remaining); the guide rendered the owner's groups
  (My Channels / General / Studios / Performers) with per-channel airings
  (e.g. "CH 3 · Discovery Lab": The Lost Pilot 6:07–6:42 PM, Monster Fish of
  Thailand 6:42–7:28 PM); tuning from the guide started playback (video
  surface live). No screenshots were possible on this stick (screencap
  returns empty); verification is via UI dumps, logcat, and the plugin
  surface. `.169` was NOT touched.

APK: `~/dev/StashAppAndroidTV/app/build/outputs/apk/debug/StashAppAndroidTV-debug-0.9.1-127-g5ed36fd4-106-armeabi-v7a.apk`
(the stick is armeabi-v7a; arm64-v8a and fat APKs are alongside it).

## Storage / upgrade notes

No schema upgrade was needed: the library document stays storage version 1
(the pending-intent journal remains a separate file, now written pre-commit
inside the transaction lock, with a `generation` field per entry — older
journal entries without a generation are handled). Existing dev data was
never at risk of a rewrite; the pre-remediation backup above is the rollback
point for the data dir.

## Remaining baseline limitations (honest)

* The audit's scoped-out baseline concerns remain documented, not re-proven
  here: continuing S5/S6 and generation-guard edge cases, and the superseded
  30-day simulation. This remediation did not rerun the simulator.
* The dynamic scene-count semantics are now documented as TOTAL-LIBRARY
  activity, deliberately different from the proposal's eligible-pool counts;
  the seeded materialized channels are untouched. An eligible-pool-count
  predicate would be a separate semantic capability.
* Reference validation checks at most 60 newly authored entity ids per
  request (reported via `reference_checks_capped` when hit) so a huge paste
  cannot turn a sync validation into a thousand queries.
* The scheduler's hourly PrepareProgramming still drains the journal and
  prepares both engines as before; publication of scheduled modes for
  networks remains rollout-gated by the operator file (unchanged by design).
* The verification channel `net_2555bca3` (archived, #881) is intentional
  debris for the owner to remove; nothing else on dev was mutated.

## Release / rollback

Release (production, when the owner chooses — outside this assignment):

```sh
# plugin
rsync -az --delete --exclude '.git' ./ shahram@192.168.8.40:/home/shahram/dev/stash-justwatch/
# then reloadPlugins over GraphQL (Apikey header; key in the prod stick's prefs)
# TV: adb -s 192.168.8.169:5555 install -r <armeabi-v7a apk>   # DISCRETE install only
```

Deployment order is safe either way (old APK ignores the new fields; new APK
falls back without them). The dev rollout file is untouched; production
rollout/kill-switch handling is the operator's standing procedure.

Rollback:

* Plugin: redeploy the audit-baseline commit (`2536613`) and `reloadPlugins`;
  the library document is readable by it (no schema change), and the pending
  journal's `generation` field is ignored harmlessly by the old reader.
* Dev data: restore
  `/opt/stash-dev/stash-justwatch-data-backup-remediation-20260923-182408`
  (a full copy taken before verification).
* Migration-era rollback (pre-migration state) remains the manifest-driven
  `--restore` path in `docs/CHANNEL-CURATION-MIGRATION.md`.
* TV: `adb install -r` the previous APK; the per-channel identity fields are
  additive and the old TV ignores them.
