# Changelog

## 0.6.0 (2026-09-09)

Stable network identities + the source semantics the v4-final catalog needs.
Contract stays v1 (all additive); the current production CSV compiles
byte-identically through the upgraded importer (golden test). Ships the
proposed **just-watch-v4-final** catalog package (513 networks) at
`analysis/just-watch-final/` + `data/proposed_channels_final.csv` — NOT
deployed; the live networks.json and production CSV are untouched.

- **stable_key** (optional authoring column): decouples network identity
  from display naming. Blank keeps the exact legacy id/seed derivation; a
  present key becomes the identity basis. Survivors seed it
  `"<old number>|<old production name>"` and keep id/seed/rotation
  byte-for-byte; new channels get semantic keys (`jw:v1:tagpair:7978:8027`).
  Immutable once in production.
- **ANY-of criteria**: `include_performer_logic=ANY` → `performersAny`
  (union), multi-studio `include_studio_logic=ANY` → `studiosAny` (union of
  subtrees, INCLUDES depth -1). Single-studio rows keep the legacy
  `studios` shape — no source-identity churn. Enables the aggregate
  discovery networks without synthetic tags.
- **Metadata criteria**: scene-date range, minimum duration (inclusive
  BETWEEN — Stash has no ≥ modifier), and created-at recency
  (`created_within_days`, resolved to a UTC-midnight cutoff at query time).
  New Arrivals stays dynamic and its rotation version carries the resolved
  cutoff epoch (daily invalidation); static sources' versions are unchanged.
- Importer hardening: families enumerated exactly (9 normal + 7 specials),
  duplicate stable keys / derived ids rejected, metadata validated at
  import, `--csv`/`--out` flags, ANY-list/metadata labels summarized.
- `networks.load(path=...)` so validation compiles a preview artifact and
  can never load the deployed file by accident; loader accepts the new
  source keys strictly.
- v4-final package: corrected tag-pair band (≤2 same-namespace pairs total,
  explicit counting), locked 15 triples / 4 include-exclude / 16 duos, eight
  discovery networks as real filters, migration map against the 795-row
  production CSV (292 survivors byte-identical, 221 new in freed slots, 282
  numbers freed), 24/24 validation checks incl. 513/513 exact membership
  reproduction and 100.00% post-JAV coverage. See
  `analysis/just-watch-final/README.md`.

## 0.5.0 (2026-09-06)

JAV confinement in the network tier + the recount loop that keeps the CSV's
counts honest. No plugin runtime changes — the composite filter projection
already carried any-of exclusions end to end; this release is policy (CSV),
tooling, and truth maintenance. Contract stays v1.

- Tag policy: 796 of the 800 network rows now exclude tag `9320` ("JAV",
  434 scenes) — JAV airs only where the owner puts it (today: the custom
  "JAV" channel 1 plus the four exempt all-JAV studios), never on another
  network. Applied and recounted in one
  pass; ids/seeds unchanged, rotations re-versioned through the canonical
  filter hash.
- `tools/recount_channels.py`: recounts `exact_scene_count` +
  `library_share_pct` per row against the owning library, projecting each
  row exactly as the runtime does (importer `build_source` ->
  `lineup.build_scene_filter`), so counts and aired lineups cannot disagree
  about membership. `--check` is the re-runnable validation sweep (exits
  non-zero on drift); `--exclude-tag ID=NAME` applies the nowhere-but-its-
  own-channel policy idempotently before recounting; `--write` persists.
  First full sweep: 800/800 exact (the 2026-09-05 sample was 54/54; the
  library had drifted past it — 15 rows recounted upward, e.g. New
  Sensations Vault 413 -> 1046, independent of the JAV policy).
- Importer polish: `sourceLabel` joins multiple excluded tag names with
  ", " instead of the raw pipe (rows now carry two).
- JAV's only network homes are the four all-JAV studios, kept on the air by
  owner decision as the sanctioned exception (407 Madonna Selects, 461 Nagae
  Style Showcase, 480 Hunter Vault, 835 Madonna: Wrong Side of the Bed
  Nights — exemption noted in each row's rationale). Every other row
  excludes JAV. `--except-row N` exempts rows from `--exclude-tag` and fails
  on unknown numbers, so re-applying the policy can never silently re-exclude
  the exempt studios.

## 0.4.0 (2026-09-05)

The network tier: the owner's curated channel list (800 networks, 100–899)
compiled from `data/proposed_channels_scene_validated.csv` now replaces the TV
app's client-generated General/Studios/Performers sections — the dial becomes
1–99 My Channels plus the curated 100–899 band. Contract stays v1; every
change is additive and deploy-order safe (an old plugin or app falls back to
the legacy generation).

- `tools/import_channels.py`: CSV → `justwatch/networks.json`. Deterministic
  (ids/seeds hash the row identity — re-import never reshuffles rotations),
  strict (family, logic flags, name length, duplicate numbers, singleton
  studios). Section + brand glyph/color per family.
- New `filter` source type (network tier only): ALL-of tags / performers /
  studios plus any-of tag exclusions, projected in `build_scene_filter` to
  `INCLUDES_ALL` / `excludes` / `depth -1` — one rotation code path for every
  tier. `source_key`/`rotation_version` hash the canonical filter.
- `Directory` gains a `networks` block (revision + channels with
  owner numbers, brand, section, validated counts); `Lineup` serves `net_`
  channels through the same bounded rotation; `FullDirectory` sections now
  come from the tier with zero Stash queries (the autodir library tiering is
  retired along with `specs.json`). Capabilities advertises
  `features.networks`.
- Network channels are read-only: never in catalog.json, never snapshot
  computed (CSV counts are the health), fixed programming mode.

## 0.3.0 (2026-09-04)
- Multi-tag sources: a tag channel can air from a SET of tags — a scene
  matching ANY of them airs (`tags INCLUDES depth -1`, union semantics).
  Sources carry canonical `source.ids` (sorted, with `id` mirroring the first);
  old single-id catalogs load unchanged. `rotation_version` now hashes the
  whole tag set; `resolve_source` labels sets "kids, comedy +1" and reports
  exists-when-any-exists. Additive: contract stays v1, the TV app is unaffected.
- Channel Studio: tag channels show their tags as removable pills in the editor
  ("Airing from"), with a dashed "+ Add tag" pill opening a multi-pick tag
  sheet; the rail summarizes sets ("Tags · kids, comedy +1") and flags deleted
  tags ("1 tag missing"); switching a multi-tag channel to another lineup kind
  asks first. Single-tag channels render as a one-pill set — no migration.

## 0.2.0 (2026-09-05)
Published programming plus the reliability pass from the 2026-09-04 integration
audit (docs/AUDIT-2026-09-04.md). Contract v1 unchanged; all response additions
are additive fields/ops.

- Published programming: a channel can air a published 72-hour schedule instead
  of a raw rotation — fixed/explore/discovery modes, soft spacing, a repeat
  cooldown, and weekly studio/performer spotlight blocks. The `PrepareProgramming`
  task builds each publication (bounded index paging, only changed channels
  rebuilt); `Schedule`/`PreviewProgramming`/`ProgrammingDesk` serve it to the TV
  and the Channel Studio trial UI; `tools/systemd/` ships an hourly timer to
  keep schedules fresh.
- Autosave: one save coordinator for every edit (create/edit/relink/renumber/
  delete/settings). Saves submit against the last server-acknowledged revision, so
  edits made while a save is in flight serialize instead of conflicting; an external
  revision conflict keeps the local draft and offers reload vs deliberate overwrite.
- Save results: stored per requestId (retention-capped) instead of one overwritten
  file, job polling is bounded, request ids are UUIDs, and every handled save
  failure — including internal errors — publishes a correlated result. Failed saves
  leave a kept draft + Retry action.
- Catalog: stored files load strictly — broken shapes, future schema versions, or
  invalid channel records raise CatalogError instead of silently normalizing away
  (and being wiped by the next save). Draft validation reports malformed fields
  (numeric name, string source, null tag lists) as structured errors.
- Saves: the revision check + write now run under a process-safe catalog lock with
  unique temp files; concurrent saves yield one success and one revision_conflict.
- Seeds: existing channels keep their stored seed through SaveCatalog (changed or
  omitted drafts cannot reseed); only new channels take a fresh seed.
- Rotation policy (documented in Capabilities `features.rotation`): a channel airs a
  bounded, ordered, playable loop (size 50, scan ≤ 1000 raw rows) assembled
  server-side. Lineup, PreviewLineup, and health counts/loop lengths all derive from
  that same rotation; `rotationVersion` (ordering hash + catalog revision) lets
  clients drop stale lineups.
- Saved searches: the saved filter's text search (`q`) is now carried through
  Lineup, PreviewLineup, and health — a text-only saved search no longer becomes an
  unrestricted lineup.
- Cookie auth: reads the server's `SessionCookie.Name`/`Value` fields (Go
  http.Cookie marshaling) instead of lowercase `value`.
- Health: per-channel failure boundaries; distinguishes missing source (relink),
  off air, and temporarily unavailable (stale numbers kept, flagged); sources of
  every type are existence-checked; snapshot publication is revision-aware (older
  computations never replace newer).
- Settings honesty: the Tuning sheet now states these thresholds shape THIS page's
  automatic sections only — the TV tunes from its own settings — and the launch-mode
  control was removed from the web.
- Auto-tier numbering: ordinary studios/performers stop at base+97 so the spillover
  pin (399/499) never collides at larger directory sizes.

## 0.1.0
- Initial release: v1 contract, catalog + health snapshots, Channel Studio page, custom channels 1-99.
