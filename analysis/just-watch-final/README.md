# Just Watch v4 Final — the 513-channel network catalog

**Catalog `just-watch-v4-final`** · 513 channels · generated from the
2026-09-08 extraction (20,299 scenes). This package is the proposed
replacement for the current 795-row production network tier. Nothing here is
deployed: the production CSV and the live `justwatch/networks.json` are
untouched, and the catalog compiles to `networks.preview.json` (in this
folder) for validation only.

## Inputs and provenance

| Input | Value |
| --- | --- |
| Production baseline | `data/proposed_channels_new_taxonomy_scene_validated.csv` (795 rows) |
| Metadata extraction | `/home/shahram/dev/stash-extract` — 20,299 scenes, 2026-09-08 |
| Candidate universe | the corrected v3 analysis package (selection/registry pickles) |
| Analysis rules | v3 corrected selection + v4 locked bands (pairs re-selected with explicit same-namespace semantics; triples/include-exclude/duos locked) |
| Stable-key contract | v1 (see below) |

## Composition

| Family | Count | | Section | Count |
| --- | ---: | --- | --- | ---: |
| Performer spotlights | 158 | | General | 209 |
| Studio/network spotlights (hierarchical) | 100 | | Studios | 115 |
| Direct canonical tag channels | 142 | | Performers | 189 |
| Tag pairs | 40 | | **Total** | **513** |
| Tag triples | 15 | | | |
| Tag include/exclude | 4 | | | |
| Performer + tag specialties | 15 | | | |
| Studio + tag signatures | 15 | | | |
| Performer duos | 16 | | | |
| Discovery/meta | 8 | | | |
| **Total** | **513** | | | |

The eight discovery/meta networks: One-Scene Performers (1,797 scenes) ·
Rare Performers <5 (5,212) · Prolific Performers 100+ (10,596) · Fringe
Studios (2,035) · 2010s Vault (10,789) · 2020s Vault (7,831) · Feature
Length 60+ min (1,566) · New Arrivals last 180 days (4,620 generation-time
reference). "Quick Fixes" is not included.

## Stable-key contract (v1)

Legacy identity derives from display text: `id = net_+sha1("<number>|<name>")[:8]`,
`seed = int(sha1("seed|<number>|<name>")[:8], 16) % 2^31`. From v4, an
optional `stable_key` column decouples identity from naming:

- blank/missing → exact legacy derivation (the 795-row production CSV keeps
  compiling byte-identically; proven by a golden test)
- present → the key is the identity basis (`id = net_+sha1(<stable_key>)[:8]`,
  `seed` from `"seed|"+stable_key`)

Every survivor seeds `stable_key = "<old number>|<old production name>"`,
which reproduces its existing id and seed **byte-for-byte** — all 292
survivors keep their number, name, network id, rotation seed, and rotation
ordering. New channels use semantic keys over canonical ids
(`jw:v1:tagpair:7978:8027`, `jw:v1:special:new-arrivals`, …), never display
text, so future renames and renumbers can no longer reshuffle rotations.

> `stable_key` is immutable once a channel reaches production. Respelling it
> is creating a new network identity.

## Migration (against the 795-row production CSV)

- **292 keep** — concept survives in place; number, name, and identity
  preserved byte-exact. Zero count drift against the extraction.
- **221 replace** — retired slots repurposed by new channels (lowest freed
  numbers first; no survivor renumbered).
- **282 retire** — numbers left unused (the dial stays sparse; compacting
  would churn identities for nothing).
- Row 835 (Madonna studio_tag) retires as the duplicate Madonna identity;
  the unique studio/network concept survives (studio 819, slot 407).

See `migration_map.csv` (all 795 current rows → disposition) and
`retired_channels.csv`.

## Semantics you should know

- **Rarity and prolificacy are defined over network-eligible post-JAV
  scenes**, not raw library cardinality: "One-Scene Performer" = exactly one
  post-JAV scene. The aggregates (One-Scene/Rare/Prolific/Fringe) are
  **materialized entity-set snapshots** of the extraction, expressed as real
  union filters (`performersAny`/`studiosAny`) — no synthetic Stash tags.
  They refresh when this pipeline re-runs.
- **Fringe Studios** carries the 794 maximal studios whose hierarchical
  post-JAV pool is under 10 scenes (descendants of included studios pruned;
  union verified bit-identical).
- **Global JAV exclusion** applies to every channel except the five
  sanctioned exceptions (performers 8926, 1661; studios 819, 1599, 2733).
- **Metadata filters are real criteria**, not tags: `date BETWEEN`,
  `duration >= 3600s` (inclusive, via BETWEEN — Stash has no ≥ modifier),
  `created_at` recency. **2020s Vault is dated 2020-01-01 through
  2026-12-31** (analysis semantics preserved); that fixed upper bound needs
  annual regeneration — from 2027-01-01 the name misleads until re-run.
- **New Arrivals is dynamic**: `created_within_days = 180` resolves its
  cutoff at query time (180 calendar days at UTC midnight). Its
  `exact_scene_count` is a generation-time reference that legitimately
  drifts, and its rotation version carries the resolved cutoff epoch so
  clients invalidate daily.
- **Tag pairs**: 40 channels, anchor cap 3, Jaccard rejection gate 0.60,
  reservation floors for SET/THEME/PROD/CAST/KINK/WARD, **exactly 2
  same-namespace pairs total**, at most 2 per cross-namespace combination
  (the v3 `same_ns_cap` bug — namespace sets collapsing `ACT+ACT` to
  `{ACT}` and bypassing the counter — is fixed by counting explicitly).
  The full band is printed in `validation_report.md`.
- **Naming**: survivors keep production names byte-exact; new channels carry
  plain descriptive names. A separate human naming/branding review follows.

## Artifacts

| File | Contents |
| --- | --- |
| `final_channels.csv` / `.json` | the full catalog (id/number/name/disposition/counts/quality/reason) |
| `migration_map.csv` | every current row → keep/replace/retire with final channel and counts |
| `retired_channels.csv` | the 503 retired/replaced current rows with notes |
| `final_tag_pairs.csv` · `final_tag_triples.csv` · `final_performer_duos.csv` · `final_special_channels.csv` | the banded selections in detail |
| `networks.preview.json` | compiled through the REAL importer (`tools/import_channels.py --csv data/proposed_channels_final.csv --out …`) |
| `validation_report.md` | all checks, statistics, and the 40-pair table |
| `implementation_changes.md` | the plugin-side changes this required (v0.6.0) |
| `../scripts/finalize.py` · `validate.py` | regenerate / revalidate everything |

The authoring CSV itself is **`data/proposed_channels_final.csv`** (the
production 26-column format plus five appended optional columns:
`stable_key`, `scene_date_from`, `scene_date_to`, `duration_min_seconds`,
`created_within_days`).

## Regenerating

```sh
python3 analysis/just-watch-final/scripts/finalize.py [--timestamp <fixed>]
python3 analysis/just-watch-final/scripts/validate.py
```

Both are deterministic (identical inputs → byte-identical artifacts; proven
by check R2). Paths to the v3 package and extraction are env-overridable
(`JW_V3_SCRIPTS`, `JW_STAGE`, `JW_EXTRACT`).

## Validation summary

24/24 checks passed (see `validation_report.md`): 513 unique definitions,
no duplicate numbers/keys/ids, no empty channel, **513/513 channels
reproduce their counts exactly** from a fresh independent extraction read,
aggregates match their recomputed concepts, metadata filters match
references, JAV policy exact (5 exceptions), **100.00% post-JAV coverage**,
band compositions exact, CSV widths exact, JSON/CSV/preview agreement,
importer + strict-loader compile, 172 plugin tests green (including the
golden production-compile regression), finalizer determinism, and the
operational large-filter check (28 KB `performersAny` accepted by Stash
v0.31.1 in 50 ms; production timing skipped — no local prod credentials).

## Deploying (later, explicitly)

This package is review-ready but **not deployed**. When approved: re-run
recount-style validation against production, then point the importer at
`data/proposed_channels_final.csv` and reload the plugin. Deployment is
safe either way against the current TV APK (an old APK ignores what it
doesn't know; a new APK falls back gracefully).
