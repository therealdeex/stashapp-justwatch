# Continuing programming — remediation handoff

2026-09-21. Implements docs/CONTINUING-PROGRAMMING-REMEDIATION-PLAN.md against
the findings in docs/CONTINUING-PROGRAMMING-REVIEW.md. Baseline: plugin
`9a5d902`, TV `992119f7` (the review's reproduction evidence is preserved in
`analysis/continuing-review/results.json`; the post-fix probe observations are
in `analysis/continuing-review/results-fixed.json`, produced by
`analysis/continuing-review/reproduce_fixed.py`).

**Status: implementation complete; dev verification complete on Stash :9998;
device flows partially verified (see "Remaining device checks"). Production
was NOT touched.**

## Design summary (one coherent state machine, not patched counts)

- **One exactly-once transition** (`apply_airing`): live scheduling,
  reservation replay, and schema-2 migration all advance pass/deck/counts/
  recency/credits/pending-arrivals by the same function, exactly once per
  airing. Selection (`pick_next`) is pure — it can no longer spend credits or
  mutate the deck.
- **Durable ledger = `checkpoint`**: consumption state at the
  actually-aired boundary. An independent wall-clock cursor (`airedThrough`)
  folds every completed airing exactly once, every hour (R4), deep-copying
  aired history (input purity).
- **Committed future = reservations**: every build re-derives durable state
  from checkpoint + committed (protected) airings. A deletion cut therefore
  releases canceled reservations by construction — a canceled eligible scene
  simply never entered the checkpoint, so it is still in the pass deck (R1).
  `committedThrough` is derived from the retained timeline, never carried.
- **Policy edits** (spacing/repeatHours/newShare) keep the whole 24-h
  protected prefix and the entire ledger; only the flexible future is
  re-dealt (R9). The contradicting test was explicitly rewritten.
- **Schema 3** publications carry `checkpoint`, `airedThrough`,
  `encoreBlock`, `generation` (a token over programs + ledger + cursors +
  signatures — the commit guard compares THIS, not `digest(programs)`, and
  revalidates rollout activation under the lock) (R12). Strict reader
  validation: a corrupt checkpoint raises, never resets. Schema-2 dev files
  migrate on the next prepare with a `.v2.bak` backup.
- **Freshness**: arrivals require a genuine `createdAt` at/after the index
  baseline (missing timestamps count as new; re-eligible old content only
  joins the queue); any first exposure satisfies a pending arrival and only
  arrival-flagged slots spend a credit; `newShare` is a bounded authored
  policy (0.05–0.30, default 0.15) (R2/R3).
- **Variety**: strict LRU replaced by three freshness tiers — never-aired,
  stale (12 h buckets capped at 7 days, so the per-pass shuffle stays
  meaningful), and an immediate-repeat guard (6 h, exact recency) so the
  just-aired scene is truly last without imposing a permanent order (R3).
- **Bounds**: short-clip caps add a warning via the set (no crash) and
  coverage honestly reports the shortfall (R5). Empty sources are cached
  like any other index; the budget cursor advances by serviced channels
  within urgency classes; a failing source backs off after 3 consecutive
  failures (named `backoff`); corrupt publications are isolated in ordering,
  builds, and status writes (R6).
- **Truthful ops**: status stores durable facts only; coverage/expiry/
  readiness are computed at READ time against the reader's clock.
  `--verify` generates a `runId`, passes it into the task, waits for THAT
  job by id, and requires OUR run with a `finishedAt` in the manifest; an
  unrelated old successful run can never verify. Deferred/budget/backoff
  outcomes are named states, distinct from success and failure (R7).
- **Outage recovery**: `encoreBlock` (the exact fallback the read surface
  serves) is stored outside the retention window, so recovery reproduces the
  airing clients already see even after 240 h dark, reconciling removed
  scenes (R10).
- **TV recovery**: a stale generation after ANY suspension returns without
  dispatching; the lookup anchors at the FAILED airing's end (not wall
  clock); one recovery job per tune generation, cancelled on tune; a repeated
  failure callback never starts a second lookup; the bounded fallback path is
  unchanged (R11).

## Finding → fix → test matrix

| Finding | Fix (where) | Tests (all passing) |
| --- | --- | --- |
| R1 deletion cuts consume scenes + stale commit cursor | checkpoint restore + derived `committedThrough` (`continuing.py` build) | `test_r1_deletion_restores_checkpoint_and_commit_boundary`, `test_r1_consecutive_deletions_and_restart_stay_consistent`, `test_deletion_replans_from_earliest_affected_and_releases_reservations` |
| R2 arrival double-spend; out-of-deck re-pick; replay mismatch; fake arrivals | pure `pick_next` + single `apply_airing`; arrivals ride the remaining deck; any exposure satisfies pending; createdAt/baseline classification; bounded `newShare` | `test_r2_arrival_costs_exactly_one_credit`, `test_r2_arrival_never_picked_outside_the_deck`, `test_r2_normal_exposure_satisfies_a_pending_arrival`, `test_r2_old_content_is_not_labeled_a_new_arrival`, `test_r2_replay_matches_construction_via_one_transition` |
| R3 identical permutations; strict LRU | bucketed freshness tiers + 6 h immediate-repeat guard (`pick_next`) | `test_r3_homogeneous_library_does_not_repeat_pass_permutations[8/30/100/500]`, `test_r3_immediate_repeats_still_avoided_without_permanent_order`, `test_no_repeats_within_a_pass_and_pass_order_varies` |
| R4 completed airings never recorded; shallow-copy mutation | independent `airedThrough` cursor; deep-copied `aired` | `test_r4_every_completed_airing_recorded_exactly_once`, `test_r4_time_of_day_history_is_real` |
| R5 `warnings.append` crash; dishonest coverage | set add; cap warning + honest partial coverage | `test_r5_short_clip_cap_warns_and_reports_partial_coverage`, `test_r5_twenty_thousand_short_airings_stay_bounded` |
| R6 budget cursor stuck; empty sources monopolize; corrupt file aborts tier/status | class-wise fair cursors, cached empty indexes, bounded backoff, per-channel isolation everywhere (`prepare`, `write_status`) | `test_r6_budget_rotates_so_the_starved_channel_progresses`, `test_r6_corrupt_publication_does_not_abort_peers_or_status`, `tests/test_tier_resources.py` (45 channels, budget 6, backoff, TTLs, shared sources) |
| R7 `--verify` accepts stale run; frozen coverage | runId correlation end-to-end; read-time `status_view` | `test_r7_verify_rejects_stale_unrelated_run`, `test_r7_verify_passes_on_the_correlated_run`, `test_r7_status_ages_and_empty_is_not_ready`, `test_status_manifest_round_trip` |
| R8 simulation not an oracle | corrected simulator: independent ledger (duplicate oracle: pass-third slot threshold clamped below the 6 h guard, release-exempt), earliest-invalid deletion exceptions, 240 h outage observed via the read surface against a frozen-file oracle, batched tier prepare, injected time everywhere, arrival deadlines for every arrival, sampled memory; adverse fixtures prove gates fail | `tests/test_simulation_metrics.py` (11), corrected run `analysis/continuing-simulation-v2/` |
| R9 policy edit violates 24 h protection | policy/source signatures split; only the flexible future rebuilds | `test_policy_edit_keeps_protection_prefix_and_durable_ledger` (replaces the contract-contradicting test, see below) |
| R10 long outage loses the encore | stored unpruned `encoreBlock`; recovery works past horizon+retention; reconciles removed scenes; `programming.schedule` serves the same block | `test_r10_outage_beyond_horizon_and_retention_preserves_current_encore`, `test_r10_outage_reconciles_deleted_encore_items`, `test_long_outage_finishes_current_encore_then_resumes` |
| R11 TV stale failure dispatch; wall-clock anchor; concurrent recoveries | stale guard before every dispatch; failed-end anchor; single cancelled-on-tune recovery job (`JustWatchViewModel.onPlayerFailed`, `tuneInternal`) | `JustWatchRecoveryTest` (6 interleaving tests incl. suspended lookups) |
| R12 empty-state reset; weak commit guard; premature activation | strict `validate_checkpoint` + reader; full-state `generation` guard with lock-internal activation revalidation; staged rollout (`stage: prepare\|active`, boolean-validated) | `test_r12_corrupt_checkpoint_raises_never_resets`, `test_r12_generation_guard_rejects_same_programs_different_state`, `test_r12_schema2_publication_migrates_with_backup`, `test_rollout_string_false_is_loud_not_true`, `test_rollout_prepare_stage_builds_without_advertising` |

## Test / build results (actual runs)

- Plugin: `python3 -m pytest tests/ -q` → **261 passed** (was 217; +44
  remediation/oracle/tier/metric tests; existing tests updated only where
  they referenced the old schema shape or contradicted the plan — below).
- TV: `gradlew :app:testDebugUnitTest` (features.justwatch.* +
  ui.pages.justwatch.*) → **BUILD SUCCESSFUL**, including 6 new
  `JustWatchRecoveryTest` interleaving tests alongside the existing
  JustWatchPluginClientTest / NetworkTierTest / PublishedScheduleTest /
  BroadcastScheduleTest / JustWatchReducerTest suites.
- TV debug build: `gradlew :app:assembleDebug` → BUILD SUCCESSFUL
  (`StashAppAndroidTV-debug-0.9.1-124-g992119f7-106-armeabi-v7a.apk`).
- Corrected 30-day simulation: `analysis/continuing-simulation-v2/`
  (results.json + report.md, tied to this commit's engine and fixtures).
- Fixed-run probes: `analysis/continuing-review/results-fixed.json`.

## Test changes, explicitly justified

- `test_policy_change_keeps_notice_boundary_and_restarts_state` expected a
  ~2-minute notice boundary WITH a full state restart — that contradicted the
  plan's 24-hour whole-airing protection (the review's R9 called this out and
  instructed not to redefine the plan to match the test). Replaced by
  `test_policy_edit_keeps_protection_prefix_and_durable_ledger`.
- Tests referencing schema-2's derived `state` dict were moved to the durable
  `checkpoint` (the derived mirror no longer exists — the file stores ONE
  ledger). Behavior assertions were kept equivalent or tightened.
- The duplicate oracle counts SLOTS between a scene's consecutive airings
  against a pass-third threshold clamped below the 6 h guard window: pass
  boundaries are not externally observable while a pending-arrival backlog
  legitimately delays closures, and an extreme pass-order reversal is not a
  duplicate. The threshold still catches double-spends and out-of-deck
  re-picks (adversarial fixtures prove it), and sanctioned reservation
  releases are exempt.

## Migration / rollback (current dev schema-2 publications)

- Forward: nothing to run manually. The next `PrepareProgramming` task backs
  each schema-2 publication up to `<channel>.json.v2.bak` and writes the
  schema-3 publication, rebuilding the ledger from the client-visible
  timeline (stored schema-2 counters were NOT trustworthy per R1/R2/R4 — the
  migration replays what actually aired instead of trusting them; arrival
  baselines are re-stamped at migration time, a documented conservative
  choice).
- Backward (plugin): `rsync` the previous build back; the old engine reads
  schema-2 files. To return a migrated channel to its pre-remediation file:
  `python3 tools/rollback_continuing.py --data-dir <data> --restore-v2-backups`
  (preview with `--dry-run`).
- Operational kill switch (works from any build):
  `python3 tools/rollback_continuing.py --data-dir <data> --deactivate`
  (or delete `continuing-networks.json`). Old clients always kept the fixed
  Lineup contract; upgraded TVs fall back on their next Directory poll, and
  the currently playing scene simply plays out to the next valid airing.

## Measured resources (corrected simulation, 30 days, 8 channels)

- Batched tier-level prepare: avg 0.27 s / p95 0.33 s per hourly run
  (all 8 channels, including the 5000-scene source); peak sampled memory
  21.3 MB; largest publication ~1.6 MB (the 5000-scene channel: index +
  ledger + 48 h of airings).
- Tier resource tests (`tests/test_tier_resources.py`): 45 channels,
  budget 6, 5 empty + 5 identical sources + a failing source: fair rotation
  reaches the whole tier, shared sources query once, empty indexes are not
  re-queried within the TTL, backoff stops hammering a failing source while
  peers progress.
- Honest limits: a 7-day horizon of 10-second clips cannot exist under
  MAX_PROGRAMS = 20,000 (~55.6 h); the engine warns and continues rather
  than claiming coverage. Thin libraries repeat (content-bound).

## Dev verification (Stash :9998) — performed 2026-09-21

- Read the dev rollout first (`continuing-networks.json`: enabled with
  net_8091d3ea / net_461ab3e3 / net_04685144 per AGENTS.md) and the three
  existing schema-2 publications before touching anything.
- The hourly dev programming timer migrated the schema-2 publications to
  schema 3 while the remediated engine was being developed (`.v2.bak` backups
  present for all three); the final engine strict-reads them, and the next
  run self-healed the ledger (net_8091d3ea: committed 70, airedThrough
  advancing, checkpoint deck/counts/aired populated).
- `reloadPlugins` + `tools/prepare_programming.py --verify --timeout 240`
  against :9998: job queued with runId `cli-c6460e20735a`, verified by runId
  match — `verified: run cli-c6460e20735a completed; 5 channel(s) prepared,
  0 deferred, 2 not ready` (the not-ready set is the honest readiness
  surface: the zero-coverage example channel and a custom channel report
  `ready: false, coverageHours: 0` instead of fake health).
- `Schedule` on an activated network: `status: ready`, 50 programs,
  sourceTotal 1 (the 10-scene dev library — the tier airs its true
  membership). `Schedule` on a FIXED network (net_84c4e02e): `{"status":
  "fixed", "programs": []}` — the legacy contract is untouched.
- `Directory`: the networks block advertises exactly the three rollout ids as
  `continuing`; unknown channel ids error cleanly.
- `ProgrammingStatus`: runId/finishedAt present; per-channel live coverage
  (168.1–168.2 h, expiring false; empty channel not ready).
- Rollback rehearsal: `tools/rollback_continuing.py --data-dir
  /opt/stash-dev/stash-justwatch-data --restore-v2-backups --dry-run` →
  3 backups found, nothing modified.

## Device verification (dev stick .105 / USB G072JN0734330EBH) — partially performed

The authorized dev stick was docked and reachable over USB; the debug APK
(`0.9.1-124-g992119f7-106-armeabi-v7a`, same debug keystore) installed with
`adb install -r` (Success) and the session ran against dev Stash :9998
(the stick's configured server, confirmed in its prefs).

Verified on hardware:

1. Install, launch, and the Just Watch entry render with plugin data: the
   landing page resolves custom channels' now/next from publications
   ("CH 3 · Discovery Lab — NOW PLAYING … 41 min left", "CH 4 · Studio
   Showcase").
2. The TV guide renders the plugin's network tier (My Channels / General /
   Studios / Performers bands) and channel 229's row shows its PUBLISHED
   continuing schedule as five program cells with whole-airing boundaries
   (3:12–3:47, 3:47–4:22, 4:22–4:58, 4:58–5:33, 5:33–6:08) — the schema-3
   publication is served and displayed.
3. The player pipeline works on hardware: Watch Now on a channel with a
   published schedule starts playback (active HEVC hardware decode observed
   in MediaCodec logs over time) and the OSD shows the publication's
   "Up next" airing; the OSD menu (Options / TV guide / Your dial /
   Previous channel / Back to live / channel-number pad) is functional.
4. Honest off-air: tuning networks whose sources match nothing in the dev
   library lands on the bounded "Channel off the air" fallback with
   recoverable actions — no crash, no invented content (the tier airs empty
   against a foreign library, as documented).
5. No runtime crashes in logcat across the whole session.

Remaining device checks (need a guided session — blind adb focus control in
the live guide proved unreliable for these):

1. Tune DIRECTLY into a continuing network's playback in the player (guide
   focus lands on 229's row and renders its schedule; the player path itself
   is proven via CH 3's published schedule — the pipeline is channel-kind
   agnostic — but an explicit net_-channel playback was not captured).
2. Skip-to-next-program and failed-airing recovery with an actually deleted
   scene under the player (requires deliberately removing dev library
   content — not done without separate approval).
3. Activation staging observed by a TV: `stage: prepare` (TVs stay fixed) →
   `active` at a whole-airing boundary → `--deactivate` (return to fixed).
4. Schedule-expiry encore labeling across a real >48 h scheduler outage on
   hardware.

## Production actions NOT performed

- No deploy to 192.168.8.40, no rollout modification there, no production
  TV contact (192.168.8.169 untouched), no pilot activation, and the
  513-network proposal remains untouched. Production pilot observation
  remains a separately authorized step.
