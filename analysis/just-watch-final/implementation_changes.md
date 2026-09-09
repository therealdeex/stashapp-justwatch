# Implementation changes — plugin v0.6.0

The v4-final catalog required extending the plugin's network source contract.
Everything is additive (contract stays v1) and the current production catalog
compiles byte-identically through the upgraded importer (golden test +
validation check R1). Version bumped 0.5.0 → 0.6.0.

## `tools/import_channels.py`

- **`stable_key`** (optional CSV column): blank → exact legacy
  `id`/`seed` derivation; present → the key is the identity basis. Duplicate
  non-blank keys and derived-id collisions are rejected before writing.
- **ANY semantics** per the logic columns, with the compatibility matrix
  (single-studio rows keep the legacy `studios` source shape so all 795
  production rows keep their source identity and rotation versions):

  ```
  performer ALL      -> performers       -> INCLUDES_ALL (intersection)
  performer ANY      -> performersAny    -> INCLUDES      (union)
  studio  ANY, 1 id  -> studios          -> legacy behavior
  studio  ANY, 2+ id -> studiosAny       -> INCLUDES depth -1 (union of subtrees)
  studio  ALL        -> studios          -> INCLUDES_ALL depth -1
  ```

  Multi-studio rows require an explicit `include_studio_logic`.
- **Metadata columns** `scene_date_from`/`scene_date_to`
  (`date: {from,to}`), `duration_min_seconds` (`duration: {min}`),
  `created_within_days` (`createdAt: {withinDays}`); a metadata-only row is
  a valid channel. Dates validated as `YYYY-MM-DD`; duration/days positive
  integers.
- **Families enumerated exactly**: the nine normal network families plus the
  seven `special_*` families (→ general section); no prefix matching.
- `source_label` summarizes large ANY lists ("3,569 performers (any)") and
  metadata criteria ("2010–2019", "60+ min", "last 180 days") instead of
  dumping thousands of names.
- CLI: optional `--csv`/`--out` (positional csv still honored; defaults
  unchanged — the live `justwatch/networks.json` is never touched by the
  finalization pipeline).

## `justwatch/networks.py`

- `load(path=None)`: explicit path override so validation/tests can never
  accidentally load the deployed file.
- `_require_channel` accepts and validates the new source keys
  (`performersAny`, `studiosAny`, `date`, `duration`, `createdAt`);
  malformed metadata criteria raise (strict loader unchanged in spirit); the
  include-criteria check now also accepts metadata-only sources.

## `justwatch/lineup.py`

- `build_scene_filter` projects the new criteria: `performersAny` →
  `performers INCLUDES`, `studiosAny` → `studios INCLUDES depth -1`, `date`
  → `BETWEEN from/to`, `duration` → `BETWEEN min..2147483647` (inclusive ≥;
  Stash has no GREATER_THAN_EQUALS modifier), `createdAt` →
  `created_at GREATER_THAN <cutoff>` where the cutoff is resolved at query
  time as 180-calendars-days-back at UTC midnight (bare date parses as
  midnight → the boundary day is included, matching the extraction's
  `created_at[:10] >=` reference semantics).
- **Dynamic rotation epoch**: `effective_epoch(source)` returns the resolved
  cutoff date for `createdAt` sources ("" otherwise) and is folded into
  `rotation_version`, so New Arrivals' version moves daily while static
  sources keep their exact historical version bytes.
- `_canonical_filter` includes the new criteria — but only when non-empty,
  so a legacy source canonicalizes identically and no existing rotation
  version churns.

## Tests (`tests/test_networks.py`, +43 → 172 total)

- The five stable-key proofs (legacy identity reproduced; survivor key
  reproduces it byte-exact; rename/renumber with fixed key keeps id+seed;
  double-import byte-determinism) plus duplicate-key/id rejection.
- The ANY matrix incl. the single-studio legacy shape; metadata import
  (source criteria, labels, JAV exclusion riding empty-value tags criterion,
  malformed rejection); metadata projection shapes; the created-cutoff
  anchor (2026-09-08 − 180d = 2026-03-12, matching the extraction); epoch
  behavior and the unchanged legacy rotation-version formula.
- **Golden regression**: compiling the production CSV with the upgraded
  importer equals the live `justwatch/networks.json` byte-for-byte.
- Loader accept/reject matrix for the new keys; `load(path=...)`.

## Not changed

Contract version (1), capabilities, custom-channel catalog/editor behavior,
ops surface, sync/task split, and the production data files
(`data/proposed_channels_new_taxonomy_scene_validated.csv` remains
`DEFAULT_CSV`; `justwatch/networks.json` untouched). The finalization
scripts live under `analysis/just-watch-final/scripts/` and read the v3
analysis package + extraction via env-overridable absolute paths.
