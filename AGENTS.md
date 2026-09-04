# stash-justwatch — project knowledge

**Status:** v0.1.0, contract version 1. Companion plugin for the TV app's
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
  TV), channel-number band 1–99.
- `justwatch/catalog.py` — catalog load/save (atomic) + validation.
  `<Dir>/stash-justwatch-data/catalog.json`. Unknown/invalid seeds are
  generated, settings are clamped, channel errors are reported as
  `{path, code, message}`.
- `justwatch/lineup.py` — source → `SceneFilterType` projection (saved filters
  used verbatim; tags/studios hierarchical INCLUDES depth -1; performers
  flat). Unplayable scenes (no file duration) are dropped from pages.
- `justwatch/snapshots.py` — health snapshots (count, loop seconds,
  source-missing) + `save_result.json` side-channel, dual-written to
  `<Dir>/stash-justwatch-data/snapshots/` and `<PluginDir>/assets/snapshots/`
  (the UI reads the assets mirror with session-cookie auth).
- `justwatch/stash_client.py` — urllib GraphQL client; SessionCookie +
  ApiKey/config.yml fallback.

## Invariants (do not break)

- **Custom channels are exactly what the owner authored.** Global section
  include/exclude tags from settings apply ONLY to the TV app's auto channels
  (201+); never apply them to custom channel lineups.
- **Optimistic concurrency:** saves must carry `expectedRevision`; mismatch is
  `revision_conflict` with `currentRevision`. Revision increments by 1 per
  save.
- **Stable identity:** channel `id` (`ch_` + 8 hex) and `seed` are immutable
  across edits; the schedule's determinism rests on them. Renumbering is the
  only ordering mechanism (no drag order).
- **Contract, not version:** clients negotiate via `capabilities`
  (`contractVersion` 1, operation whitelist). Adding ops = contract v1
  compatible if you add to the map; changing shapes = bump the contract.
- **Sync vs task split:** never make `SaveCatalog`/`RefreshData` sync ops
  (snapshot regen is N Stash queries); never make Lineup a task (TV needs
  sync pages).
- Corruption of an existing catalog file raises (`CatalogError`) — never
  silently reset; the owner's channels would be wiped.
- Settings clamps mirror the TV app: `solo >= 3`, `group >= 2`, `group <
  solo`; `launchMode in {last, random}`.

## UX contract (Channel Studio page) — decided with design review

Master–detail, **no tabs**: the left rail is the dial (guide-row anatomy:
number, glyph tile in brand color, name + summary; selected row gets a
brand-color left edge). Editor pane: network card (glyph + name + number
stepper with **Swap** conflict resolution), Programming (source chip + six
play-order chips), Appearance (glyph grid + swatches), On Air strip (10 thumbs
+ loop-length line), Remove. Create/change-source share one sheet: grouped
results always visible (Custom lineups → Studios → Tags → Performers), search
narrows, inline prefilled name, lowest-free auto-number. **Autosave** with a
muted Saved indicator — no Save button. Health: off-air (0), thin (<10),
missing-source relink states. Language is the TV fiction: lineup, airing
from, play order, on air, off air, loops. TV side: "MY CHANNELS" banner,
brand-tinted number cells, flip order wraps 99 → 101.

## Ops quick reference

| Op | Mode token | Sync? | Purpose |
| --- | --- | --- | --- |
| Capabilities | `Capabilities` | yes | handshake |
| Directory | `Directory` | yes | TV: enabled channels + counts |
| Lineup | `Lineup` | yes | TV: one page `{id,title,duration}` |
| PreviewLineup | `PreviewLineup` | yes | editor: draft channel + preview paths |
| GetCatalog | `GetCatalog` | yes | editor: full catalog + health |
| ValidateCatalog | `ValidateCatalog` | yes | validate + resolve, no write |
| SaveCatalog | `SaveCatalog` | task | validate → write → snapshot |
| RefreshData | `RefreshData` | task | recompute health snapshots |
