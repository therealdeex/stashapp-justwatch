# stash-justwatch — project knowledge

**Status:** v0.8.0-dev, contract version 1 (additive channel-library
surface). Companion plugin for the TV app's Just Watch feature (repo
`~/dev/StashAppAndroidTV`). Since v0.8.0 the plugin carries the
CHANNEL-LIBRARY feature: an owner-editable library
(`<data>/channel-library.json`) seeded from the v4-final 513-network proposal
plus customs, dynamic single-membership groups, explicit-Apply editing with
durable receipts, and incremental refresh. See
docs/CHANNEL-CURATION-API.md, docs/CHANNEL-CURATION-MIGRATION.md and
docs/CHANNEL-CURATION-HANDOFF.md. Until a deployment runs the migration
(tool: `tools/migrate_channel_library.py`), every behavior below that
mentions the library is dormant and the legacy stores rule.

## Architecture

Python raw-interface plugin (stdin/stdout JSON envelope; stdout = exactly one
`{"output": ...}` or `{"error": ...}`; diagnostics on stderr). No runtime deps
beyond stdlib (+ optional PyYAML for the API-key fallback). Data flow:

- `justwatch/main.py` — dispatcher. Sync ops answer `runPluginOperation`;
  writes (`SaveCatalog`, `RefreshData`) are task-only (async job) so the
  health-snapshot regeneration never blocks a GraphQL connection.
- `justwatch/contract.py` — the v1 client contract: plugin id, operation map,
  sorts→server-sort projection, shared FA glyph set (the TV dial's brand
  glyphs; the web editor offers exactly these so glyphs render identically on
  TV), channel-number band 1–99, and the ROTATION policy (`ROTATION_SIZE` 50,
  `ROTATION_SCAN_LIMIT` 1000 — advertised in Capabilities `features.rotation`).
- `justwatch/catalog.py` — catalog load/save + validation. Stored files load
  STRICTLY (`<Dir>/stash-justwatch-data/catalog.json`): broken shapes, future
  schema versions, or invalid channel records raise `CatalogError` — the file
  is never silently normalized into an empty editable catalog. New drafts
  normalize tolerantly; validation reports malformed fields as
  `{path, code, message}`. Saves take a process-safe `catalog_lock` around the
  revision re-read/check/write and use unique temp files.
- `justwatch/library.py` — the AUTHORITATIVE owner-editable library
  (schema 1; both ch_/net_ namespaces + groups with immutable ids and exactly
  one membership per channel; number bands 1–99 / 100–899). Strict loading
  like the catalog; one write path: touched-record transactions with
  expectedRevision + requestId, receipts committed atomically WITH the
  document (idempotent replay; digest mismatch rejected — REJECTED receipts
  carry their digest too, so identical retries replay exactly), bounded
  history snapshots, process-safe `.library.lock`. Transactions are staged
  on a deep candidate: a rejection leaves definitions and revision
  byte-identical. Identity (id/seed/kind/provenance) is enforced on the
  FINAL candidate through every opcode; `channels.patch` is allow-listed to
  real-boolean enabled/paused/archived. Programming modes are
  namespace-validated on change (`ch`: fixed/explore/discovery; `net`:
  fixed/continuing — `bad_mode_for_kind`), stored losslessly
  (`normalize_programming` keeps continuing newShare through cosmetic
  edits), and a `pre_commit` hook runs inside the lock before the document
  saves (the refresh intent-journal write is ordered BEFORE the commit).
- `justwatch/criteria.py` — the canonical rule model AND the ONE projector:
  legacy shapes pass through verbatim; the composite `filter` shape; the
  `criteria` shape (per-facet ANY/ALL + explicit exclusions +
  date/duration/recency/text + DYNAMIC entity selection:
  `studioSceneCount`/`performerSceneCount` thresholds — "studios with fewer
  than 2 scenes" — projected to Stash's nested `studios_filter`/
  `performers_filter` so membership resolves server-side per query; needs a
  Stash with the nested `*_filter` fields, probed live at
  validation/preview with a typed unsupported-rule error).
  `lineup.build_scene_filter` DELEGATES here — preview, Lineup, health, and
  both indexers share one implementation (audit C2). Authored `q` rides
  every query path (`criteria.text_query_of`). Dynamic thresholds: min
  inclusive, max EXCLUSIVE ("fewer than"); a combined range converts max to
  max-1 inside Stash's inclusive BETWEEN. A facet set to both ANY and ALL
  is a validation error; non-numeric ids and impossible calendar dates are
  typed errors (never silently dropped); exclude-only pools are the
  historical "everything except" shape and stay valid for `criteria`
  (legacy `filter` needs one positive criterion, as always).
- `justwatch/channel_service.py` — the ONE resolved channel view (library if
  present, else legacy stores); Directory/FullDirectory/Lineup/Schedule/
  programming/continuing read through it so what the GUI edits is what airs.
  `effective_programming` is the ONE effective-mode resolver (authored /
  effective / scheduled) used by both directories, Schedule, the editor,
  and both schedulers; `writer_lock` gives library deployments `.library.lock`
  for every publication commit guard. The network adapter passes
  `programming` through losslessly (the continuing fixed pin + newShare
  survive every hop).
- `justwatch/channel_ops.py` — the editing surface (GetChannelLibrary,
  GetChannelDirectory, GetChannelDefinition, ValidateChannelChanges,
  PreviewChannelPool, ApplyChannelChanges task, GetChannelApplyResult,
  GetChannelHistory). Additive v1; playback shapes untouched.
  ValidateChannelChanges checks CHANGED entity references against Stash
  (bounded 60 lookups; metadata-only edits of a broken source stay
  recoverable); PreviewChannelPool reports the actual bounded playable
  rotation via the shared `fetch_rotation` (measured rotationSize/
  rotationComplete, never estimated from count).
- `justwatch/refresh.py` — the durable pending-refresh INTENT journal:
  membership changes journal per-channel work (signature + generation)
  INSIDE the Apply transaction (pre-commit, under the lock) — there is no
  crash window between "definitions committed" and "work enqueued".
  Workers compute outside the lock, then commit under it: revalidate the
  channel, merge health into the CURRENT snapshot per channel (two-channel
  batches never clobber siblings), acknowledge ONLY the journal generation
  processed (concurrent enqueues survive), and drop entries whose stored
  health already matches the current membership signature. Stale workers
  (source/sort/seed/policy/state changed mid-build) publish nothing and
  requeue. Cosmetic edits never reindex. Drained inline by Apply and by
  every PrepareProgramming run.
- `justwatch/lineup.py` — source → `SceneFilterType` projection (saved filters
  used verbatim INCLUDING their text search `q`; tags/studios hierarchical
  INCLUDES depth -1; performers flat; the networks' composite `filter` type:
  INCLUDES_ALL per criterion, `excludes` riding the tags criterion).
  `build_scene_filter` delegates to `criteria` (one projector everywhere).
  `fetch_rotation` assembles the channel's
  ACTIVE ROTATION: up to 50 playable scenes in the channel's own order, paging
  past unplayable rows, bounded at 1000 raw rows; returns `rotationVersion`
  (the channel's OWN ordering hash — membership + order + epoch, NOT the
  global revision, so cosmetic applies never churn cached lineups),
  `sourceTotal`, `rotationComplete`. `source_key` distinguishes criteria
  sources by full canonical rules; `_canonical_filter` covers
  excludePerformers/Studios, duration max, scene-count rows and q when
  present (legacy sources canonicalize to their exact historical tuples).
- `justwatch/networks.py` — the LEGACY compiled network tier (100–899,
  currently 795 networks) compiled from
  `data/proposed_channels_new_taxonomy_scene_validated.csv` by
  `tools/import_channels.py` into `justwatch/networks.json` (strict loader,
  `NetworksError` on malformed; a MISSING file is an empty tier). Since v0.6.0
  the composite source also supports ANY-of performers/studios
  (`performersAny`/`studiosAny`), metadata criteria (scene-date range,
  min-duration, created-at recency with a dynamic rotation epoch), and the
  authoring CSV's optional `stable_key` decouples network identity from
  display naming (immutable once in production). `Directory` carries it as
  the `networks` block; `Lineup` serves `net_` ids through the same rotation;
  `FullDirectory` sections come from it with zero Stash queries. Replaces the
  retired autodir tiering (the TV app no longer generates sections client-side
  when the block is present; `specs.json`/`extract_specs.py` are gone).
  **Superseded on migrated deployments** (see the library bullet): the
  v4-final 513-network proposal is the seed of the owner library; the
  compiled artifact becomes a backed-up provenance input. Re-running
  `tools/import_channels.py` against a migrated deployment does NOT change
  what airs — the library does.
- `justwatch/snapshots.py` — health snapshots + `save_result.json` side-channel,
  dual-written to `<Dir>/stash-justwatch-data/snapshots/` and
  `<PluginDir>/assets/snapshots/` (the UI reads the assets mirror with
  session-cookie auth). Health is per-channel failure-isolated and
  distinguishes `ok` / `offAir` / `missingSource` / `unavailable` (stale
  numbers kept). Snapshot publication is revision-aware (older computations
  never replace newer). Save results are stored per `requestId`
  (newest-first, retention-capped) so concurrent tabs each find their own.
- `justwatch/programming.py` — published programming: per-channel policy
  (fixed/explore/discovery, spacing, repeat cooldown, weekly studio/performer
  spotlight). Only the `PrepareProgramming` task builds; it writes a
  deterministic 72-h publication per channel (`programming/<id>.json`, bounded
  index paging, already-published airings preserved). `schedule`, `preview`,
  and `desk` are read-only and never mutate a publication.
- `justwatch/stash_client.py` — urllib GraphQL client; SessionCookie (Go
  `http.Cookie`: `Name`/`Value` fields) + ApiKey/config.yml fallback.
- `justwatch/continuing.py` — the CONTINUING engine for network channels
  (v0.7.1, rollout-gated): full-eligible-library broadcasts with a durable
  ledger (`checkpoint` at the actually-aired boundary + `airedThrough`
  cursor), ONE exactly-once consumption transition (`apply_airing`) shared by
  live scheduling and replay, a rolling 7-day publication re-derived each
  build from checkpoint + committed reservations, 24-h whole-airing
  protection that survives ordinary policy edits, ~15% new-arrival slots
  (bounded configurable `newShare`; genuine createdAt-based arrivals only;
  24–72 h first-air target), soft cooldown/immediate-repeat-guard/
  time-of-day/spacing preferences with bucketed freshness, deterministic
  encore recovery from a stored unpruned `encoreBlock`, staggered/
  deduplicated indexing with cached empty sources, fair budget rotation with
  a bounded failure backoff, and the lightweight `status.json` manifest
  (durable facts only — coverage/expiry are derived at READ time; runs are
  correlated by `runId` from `tools/prepare_programming.py --verify`).
  Publications are schema 3 with a `generation` token over the whole durable
  state used as the commit guard; schema-2 dev files migrate on the next
  prepare with a `.v2.bak` backup (`tools/rollback_continuing.py` restores
  them / flips the kill switch). Activation is ONLY the operator rollout file
  `<data>/continuing-networks.json` (default absent = everything fixed;
  `enabled` must be a real boolean; `"stage": "prepare"` builds publications
  without advertising them, `"stage": "active"` serves them); an authored CSV
  `programming_mode=fixed` pins a network off regardless of rollout. See
  docs/CONTINUING-PROGRAMMING-PLAN.md, docs/CONTINUING-PROGRAMMING-REMEDIATION-PLAN.md
  and docs/PILOT-MANIFEST.md; `tools/simulate_continuing.py` is the corrected
  30-day comparison against fixed-50 (independent-ledger gates; the
  pre-remediation run in analysis/continuing-simulation is SUPERSEDED).

## Invariants (do not break)

- **Custom channels are exactly what the owner authored.** Global section
  include/exclude tags from settings apply ONLY to the TV app's legacy auto
  channels; never apply them to custom channel lineups or to network
  channels.
- **Channel identity and source semantics are immutable across edits** (was:
  "the network tier is the compiled CSV, verbatim" — superseded for migrated
  deployments by the owner's explicit 2026-09 decision to make all channels
  editable; see the handoff in docs/CHANNEL-CURATION-HANDOFF.md). Ids and
  seeds are server-owned: an Apply can never change an existing channel's
  id/seed/kind; groups and cosmetic edits never touch membership. On
  migrations, seeds/sources/exclusions transfer byte-for-byte and the
  v4-final proposal's five sanctioned JAV exceptions (225, 252, 407, 461,
  480) are preserved — the OLD four-row recount recipe belongs to a
  different catalog and must never run over the proposal. Pre-migration (and
  for anything the library document does not cover) the compiled-tier rules
  still apply: `networks.json` is a build artifact, ids/seeds hash row
  identity, source ids are DATABASE ids of the validating Stash, absent file
  = absent `networks` block = clients keep their legacy generation.
  Deploying new plugin code alone NEVER activates continuing mode;
  removing/disabling the rollout file is the rollback.
- **Nowhere-tags (v0.5.0):** every network row EXCEPT the four all-JAV
  studios (407 Madonna Selects, 461 Nagae Style Showcase, 480 Hunter Vault,
  835 Madonna: Wrong Side of the Bed Nights — the sanctioned exception, noted
  in each row's rationale) excludes tag `9320` ("JAV") — JAV airs ONLY where
  the owner puts it (custom channel 1 + those four) and never on another
  network. Policy lives in the CSV's `exclude_tag_ids_any` columns; apply new
  nowhere-tags with
  `tools/recount_channels.py --exclude-tag ID=NAME --write` (idempotent,
  recounts too), never by hand — and ALWAYS pass
  `--except-row 407 --except-row 461 --except-row 480 --except-row 835` so
  re-applying the JAV policy never re-excludes the exempt studios. Counts are
  recomputed with the same projection the runtime serves, so keep it that
  way.
- **Optimistic concurrency, process-safe for writes:** saves must carry
  `expectedRevision`; mismatch is `revision_conflict` with `currentRevision`.
  Revision increments by 1 per save. The check+write section runs under
  `catalog_lock` (flock/msvcrt), so concurrent processes cannot both pass it.
- **Stable identity:** channel `id` (`ch_` + 8 hex) and `seed` are immutable
  across edits — SaveCatalog preserves the stored seed of every existing id;
  a changed or omitted draft seed cannot reseed it. Renumbering is the only
  ordering mechanism (no drag order). Networks: `net_` + 8 hex, stable the
  same way.
- **Rotation policy:** TV playback, editor previews, and health all describe
  the same bounded rotation (`fetch_rotation`), never an unbounded library
  fetch; `total` = rotation length, `sourceTotal` = library behind it.
  Networks ride the SAME rotation machinery.
- **Contract, not version:** clients negotiate via `capabilities`
  (`contractVersion` 1, operation whitelist). Adding ops/fields = contract v1
  compatible; changing existing shapes = bump the contract.
- **Sync vs task split:** never make `SaveCatalog`/`RefreshData` sync ops
  (snapshot regen is N Stash queries); never make Lineup a task (TV needs
  sync pages).
- Corruption of an existing catalog file raises (`CatalogError`) — never
  silently reset; the owner's channels would be wiped. Malformed
  networks.json raises (`NetworksError`) for the same reason.
- Settings clamps mirror the TV app: `solo >= 3`, `group >= 2`, `group <
  solo`; `launchMode in {last, random}`. These settings shape the editor rail
  only now that FullDirectory serves networks; the TV app tunes from its own
  ServerPreferences and never reads them; the UI says so.

## UX contract (Channel Studio page) — decided with design review

Master–detail, **no tabs**: the left rail is the dial (guide-row anatomy:
number, glyph tile in brand color, name + summary; selected row gets a
brand-color left edge). Editor pane: network card (glyph + name + number
stepper with **Swap** conflict resolution), Programming (source chip + six
play-order chips), Appearance (glyph grid + swatches), On Air strip (10 thumbs
+ loop-length line), Remove. Create/change-source share one sheet: grouped
results always visible (Custom lineups → Studios → Tags → Performers), search
narrows, inline prefilled name, lowest-free auto-number.
**Superseded (owner decision, 2026-09): explicit Apply replaces autosave.**
Every mutation — create, renumber/swap, archive, group changes, bulk actions,
restores, programming preferences — commits only through Apply as an
immutable snapshot with expectedRevision + requestId; edits made while a
request is in flight stay a newer dirty draft and need another Apply.
Drafts survive navigation, validation errors, transport failures and revision
conflicts; success means the correlated durable RECEIPT
(GetChannelApplyResult), never a task id; persistence and background
programming readiness are reported separately. (The autosave-era "ONE save
coordinator" wording below describes the retired editor and is kept only for
archaeology: saves submit against the last server-acknowledged revision, an
external revision conflict keeps the draft, a newer unsent draft is never
labeled "Saved". Health: off-air (0), thin (<10),
missing-source relink states. Language is the TV fiction: lineup, airing
from, play order, on air, off air, loops. TV side: "MY CHANNELS" banner,
brand-tinted number cells; with networks present the dial is 1–99 My Channels
+ 100–899 plugin networks and the pad legend says "1–99 My Channels · 100+
Networks" (legacy legend otherwise).

## Ops quick reference

## Deployment (this machine, dev-lab2)

- Plugin deploys by symlink: `/opt/stash-dev/plugins/stash-justwatch` → this
  repo; apply changes with Stash **Settings → Plugins → reload** (or the
  `reloadPlugins` mutation). Dev Stash listens on **:9998** (all interfaces) —
  LAN `192.168.8.123:9998`, tailscale `100.99.132.40:9998`; the API key file is
  `/opt/stash-dev/API_KEY`. Use port 9998 consistently for this server.
- Continuing-networks rollout on dev: `/opt/stash-dev/stash-justwatch-data/continuing-networks.json`
  (left active 2026-09-21 with net_8091d3ea/net_461ab3e3 — the two networks
  whose filters match the 10-scene dev library — plus net_04685144 as an
  honest zero-coverage example). Delete the file to return dev to all-fixed.
- Programming scheduler: systemd user units `stash-justwatch-programming.{service,timer}`
  (hourly), env at `~/.config/stash-justwatch/scheduler.env` (`STASH_URL`,
  `STASH_API_KEY_FILE`). Install steps are in README.
- TV app: build debug APK in `~/dev/StashAppAndroidTV` (`./gradlew assembleDebug`);
  emulator AVD `@stash-tv-api36`. Fire TV sticks: **192.168.8.196:5555 = dev/
  testing**, **192.168.8.169:5555 = production** (see that repo's AGENTS.md).
- Dev Stash web login is interactive-only (no stored creds); API access uses
  the key file above.

## Production deployment

- **Prod Stash** runs on `192.168.8.40` (`docker-personal.manx-teeth.ts.net`,
  SSH as shahram) — NOT docker: systemd unit `stash.service`, working dir
  `/mnt/stash-virtiofs/stashapp`, `plugins_path: /mnt/stash-virtiofs/stashapp/plugins`.
- The prod plugin install is a plain copy (not git, not symlink):
  `/home/shahram/dev/stash-justwatch` on that host. Deploy with
  `rsync -az --delete --exclude '.git' ./ shahram@192.168.8.40:/home/shahram/dev/stash-justwatch/`
  then `reloadPlugins` over GraphQL (Apikey header; a working API key is in
  the prod stick's `run-as ... shared_prefs ... stashApiKey`).
- Prod stick **192.168.8.169:5555 runs the debug-signed APK** (same keystore
  as dev) — `adb install -r` of the armeabi-v7a APK works in place. HARD RULE
  from that repo still applies: no screenshots/UI inspection against prod
  (adult content); verify via the plugin GraphQL surface and logcat only.
- Deployment order is safe either way: an old APK ignores `Directory.networks`;
  a new APK falls back to legacy generation without the block. Deploy the
  plugin first, then the APK.
- Prod validation (read-only): the sweep is scripted —
  `python3 tools/recount_channels.py --check --url http://192.168.8.40:9999
  --api-key-file <key>` (exits non-zero on drift vs the CSV). 2026-09-06:
  800/800 exact after the JAV policy recount (the 2026-09-05 manual 54/54
  sample predated real library drift — 15 rows had grown stale).

| Op | Mode token | Sync? | Purpose |
| --- | --- | --- | --- |
| Capabilities | `Capabilities` | yes | handshake (advertises rotation policy + networks feature) |
| Directory | `Directory` | yes | TV: enabled channels + counts + revision + the `networks` block |
| Lineup | `Lineup` | yes | TV: the channel's rotation `{id,title,duration,…}` (also `net_` channels) |
| FullDirectory | `FullDirectory` | yes | editor rail: custom channels + the network sections |
| PreviewLineup | `PreviewLineup` | yes | editor: draft channel rotation + preview paths |
| GetCatalog | `GetCatalog` | yes | editor: full catalog + health |
| ValidateCatalog | `ValidateCatalog` | yes | validate + resolve, no write |
| Schedule | `Schedule` | yes | TV: the channel's published airings at a wall-clock time (custom AND activated network ids) |
| ProgrammingStatus | `ProgrammingStatus` | yes | ops: durable last-run + per-channel schedule status (a queued task id is NOT success) |
| PreviewProgramming | `PreviewProgramming` | yes | editor: dry-run a draft programming policy |
| ProgrammingDesk | `ProgrammingDesk` | yes | editor: publication status + overlap stats |
| SaveCatalog | `SaveCatalog` | task | lock → validate → write → snapshot → per-request result |
| RefreshData | `RefreshData` | task | recompute health snapshots |
| PrepareProgramming | `PrepareProgramming` | task | publish the next 72 h (hourly timer; only changed channels) |
