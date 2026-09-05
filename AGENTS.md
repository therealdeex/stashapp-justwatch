# stash-justwatch — project knowledge

**Status:** v0.2.0, contract version 1. Companion plugin for the TV app's
Just Watch feature (repo `~/dev/StashAppAndroidTV`).

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
- `justwatch/lineup.py` — source → `SceneFilterType` projection (saved filters
  used verbatim INCLUDING their text search `q`; tags/studios hierarchical
  INCLUDES depth -1; performers flat). `fetch_rotation` assembles the channel's
  ACTIVE ROTATION: up to 50 playable scenes in the channel's own order, paging
  past unplayable rows, bounded at 1000 raw rows; returns `rotationVersion`
  (ordering hash + catalog revision), `sourceTotal`, `rotationComplete`.
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

## Invariants (do not break)

- **Custom channels are exactly what the owner authored.** Global section
  include/exclude tags from settings apply ONLY to the TV app's auto channels
  (201+); never apply them to custom channel lineups.
- **Optimistic concurrency, process-safe for writes:** saves must carry
  `expectedRevision`; mismatch is `revision_conflict` with `currentRevision`.
  Revision increments by 1 per save. The check+write section runs under
  `catalog_lock` (flock/msvcrt), so concurrent processes cannot both pass it.
- **Stable identity:** channel `id` (`ch_` + 8 hex) and `seed` are immutable
  across edits — SaveCatalog preserves the stored seed of every existing id;
  a changed or omitted draft seed cannot reseed it. Renumbering is the only
  ordering mechanism (no drag order).
- **Rotation policy:** TV playback, editor previews, and health all describe
  the same bounded rotation (`fetch_rotation`), never an unbounded library
  fetch; `total` = rotation length, `sourceTotal` = library behind it.
- **Contract, not version:** clients negotiate via `capabilities`
  (`contractVersion` 1, operation whitelist). Adding ops/fields = contract v1
  compatible; changing existing shapes = bump the contract.
- **Sync vs task split:** never make `SaveCatalog`/`RefreshData` sync ops
  (snapshot regen is N Stash queries); never make Lineup a task (TV needs
  sync pages).
- Corruption of an existing catalog file raises (`CatalogError`) — never
  silently reset; the owner's channels would be wiped.
- Settings clamps mirror the TV app: `solo >= 3`, `group >= 2`, `group <
  solo`; `launchMode in {last, random}`. These settings shape the PLUGIN's own
  computed directory (FullDirectory) and the editor rail only — the TV app
  tunes from its own ServerPreferences and never reads them; the UI says so.

## UX contract (Channel Studio page) — decided with design review

Master–detail, **no tabs**: the left rail is the dial (guide-row anatomy:
number, glyph tile in brand color, name + summary; selected row gets a
brand-color left edge). Editor pane: network card (glyph + name + number
stepper with **Swap** conflict resolution), Programming (source chip + six
play-order chips), Appearance (glyph grid + swatches), On Air strip (10 thumbs
+ loop-length line), Remove. Create/change-source share one sheet: grouped
results always visible (Custom lineups → Studios → Tags → Performers), search
narrows, inline prefilled name, lowest-free auto-number. **Autosave** with a
muted Saved indicator — no Save button. ONE save coordinator backs every edit
(create/edit/relink/renumber/delete/settings): saves submit against the last
server-acknowledged revision, edits made mid-save serialize cleanly, an
external revision conflict keeps the draft and offers reload vs deliberate
overwrite, and failed saves leave a kept draft with a Retry action — a newer
unsent draft is never labeled "Saved". Health: off-air (0), thin (<10),
missing-source relink states. Language is the TV fiction: lineup, airing
from, play order, on air, off air, loops. TV side: "MY CHANNELS" banner,
brand-tinted number cells, flip order wraps 99 → 101.

## Ops quick reference

## Deployment (this machine, dev-lab2)

- Plugin deploys by symlink: `/opt/stash-dev/plugins/stash-justwatch` → this
  repo; apply changes with Stash **Settings → Plugins → reload** (or the
  `reloadPlugins` mutation). Dev Stash listens on **:9998** (all interfaces) —
  LAN `192.168.8.123:9998`, tailscale `100.99.132.40:9998`; the API key file is
  `/opt/stash-dev/API_KEY`. Use port 9998 consistently for this server.
- Programming scheduler: systemd user units `stash-justwatch-programming.{service,timer}`
  (hourly), env at `~/.config/stash-justwatch/scheduler.env` (`STASH_URL`,
  `STASH_API_KEY_FILE`). Install steps are in README.
- TV app: build debug APK in `~/dev/StashAppAndroidTV` (`./gradlew assembleDebug`);
  emulator AVD `@stash-tv-api36`. Fire TV sticks: **192.168.8.196:5555 = dev/
  testing**, **192.168.8.169:5555 = production** (see that repo's AGENTS.md).
- Dev Stash web login is interactive-only (no stored creds); API access uses
  the key file above.

| Op | Mode token | Sync? | Purpose |
| --- | --- | --- | --- |
| Capabilities | `Capabilities` | yes | handshake (advertises rotation policy) |
| Directory | `Directory` | yes | TV: enabled channels + counts + revision |
| Lineup | `Lineup` | yes | TV: the channel's rotation `{id,title,duration,…}` |
| FullDirectory | `FullDirectory` | yes | editor rail: every auto-section with previews |
| PreviewLineup | `PreviewLineup` | yes | editor: draft channel rotation + preview paths |
| GetCatalog | `GetCatalog` | yes | editor: full catalog + health |
| ValidateCatalog | `ValidateCatalog` | yes | validate + resolve, no write |
| Schedule | `Schedule` | yes | TV: the channel's published airings at a wall-clock time |
| PreviewProgramming | `PreviewProgramming` | yes | editor: dry-run a draft programming policy |
| ProgrammingDesk | `ProgrammingDesk` | yes | editor: publication status + overlap stats |
| SaveCatalog | `SaveCatalog` | task | lock → validate → write → snapshot → per-request result |
| RefreshData | `RefreshData` | task | recompute health snapshots |
| PrepareProgramming | `PrepareProgramming` | task | publish the next 72 h (hourly timer; only changed channels) |
