# Library Survey & Channel Revamp Proposal — 2026-09-28

**Server:** prod Stash `192.168.8.40:9999` (v0.31.1), read-only GraphQL.
**Method:** full mirror of the library into `library.db` (this directory), all
slicing done offline, every proposed channel's count **validated against the
live server with the runtime's own projection** (importer `build_source` →
`lineup.build_scene_filter`): **234/234 channels, 0 drift**.

## The library at a glance

| Metric | Value |
| --- | --- |
| Scenes | 20,921 |
| Studios | 2,810 (566 scenes have none) |
| Performers | 5,824 (4,320 F / 1,165 M / 339 other-or-blank) |
| Tags | 1,363 — of which ~480 belong to a systematic curated taxonomy |
| Scene–tag links | 398,431 (avg ~19 tags/scene) |
| Birthdates | 4,800/5,824 performers (82%) — real age math is possible |
| Scene dates | essentially 2003→2026 (modern library, 2011+ dominates) |
| Avg scene length | ~40 min |

### The curated taxonomy is the spine

The library is deeply curated with prefixed tag families — this is what makes
clean theme channels possible where raw Stash tags would be noise:

| Family | Tags | What it gives us |
| --- | --- | --- |
| `ACT:` | 24 | acts (anal 3,931 · squirting 1,089 · DP 582 · bukkake 277 · fisting 181) |
| `KINK:` | 25 | BDSM fine-grain (spanking 4,114 · dom/sub 2,508 · humiliation 2,297 · bondage 2,132 · pegging 657 · CBT 507 …) |
| `THEME:` | 28 | narratives (married-IRL 3,867 · roleplay 2,419 · cheating 1,449 · step-family 707 · cuckolding 363 …) |
| `CAST:` | 8 | structure (1M1F 12,435 · solo-F 2,116 · FFM 1,957 · group 1,293 · **2F = the lesbian pool, 974** · MMF 787) |
| `DEMO:` | 13 | scene-level demographics (interracial 7,065 · Latin-F 2,701 · Asian-F 717 · Black-F 617 · Mixed-F 748) |
| `AGE:` | 10 | age bands at filming (18-22F 6,228 · 30-39F 4,796 · 40-49F 996 · 50-59M 820 …) |
| `BODY:` / `WARD:` / `SET:` / `PROD:` | 117/16/20/14 | looks, wardrobe, settings, production style |

Key structural discoveries:

- **There is no usable "Lesbian" tag (10 scenes).** `CAST: 2F` is the lesbian
  pool — all 974 scenes with a 2F cast have **zero** male performers. Solo,
  FFM, MMF and group pools are equally clean.
- **Ethnicity exists twice**: scene-level (`DEMO:` tags, curator-assigned) and
  performer-level (`ethnicity` field — needs case normalization:
  `Caucasian`/`CAUCASIAN` etc.). Scene-level is the sharper tool; performer
  ANY-lists cover the thin slices (Indian 70, Middle Eastern 54 scenes).
- **Age-difference is computable from real birthdates**: 8,603 dated scenes
  have a ≥10y cast age span; **4,644 have the man ≥15y older**; 2,536 ≥20y;
  499 have the woman ≥10y older. The `AGE:` band pairs are the tag-expressible
  proxy (e.g. 18-22F + 40-49M = 1,083) and they track the real math closely.
- **Studio families are real**: Kink (31 child studios, 1,942 scenes — the BDSM
  backbone), Vixen Media Group 1,996, Brazzers 1,869, Naughty America 820…
  Stash studio criteria are **hierarchical** — a parent-studio channel airs
  the whole family (New Sensations = 1,058 scenes, not 421).
- **Data quality**: `CURATOR:` flags ~3.3k scenes `Has Unmapped Tags` and
  ~3.3k `Needs Review`; 591 scenes have no performers. Not channel blockers,
  worth knowing.

## The JAV universe (isolated, per policy)

**553 scenes across 82 studios** (533 carry tag `9320`; the rest sit in
studios whose catalog is JAV). 98% of JAV scenes carry their DVD code in the
scene `code` field (JUQ-334, JUR-590 …). Long-tailed: Madonna alone is 197
(221 with child studios), then Nagae Style 44, Hunter 31, JET Eizo 25, Otona
No Drama 23, Ure Comi 22. JAV themes from its own tags: wife-sharing 204,
cheating 162, married-woman 141, solo 200, featured-actress 126,
big-breasts 105, humiliation 91, mature 82.

Every non-JAV channel in the proposal excludes tag `9320`; the JAV block
(850–872) is entirely JAV-only. The old tier's four sanctioned all-JAV rows
are superseded by this block.

## The proposal — 234 channels (`channel-proposals.csv`)

Importer-schema CSV (same columns the compiled tier uses, plus
`proposal_block`, `survey_evidence`, and the importer's optional
`scene_date_from/to`). Numbering is a proposal layout, not a final assignment.

| Block | Numbers | Count | Highlights |
| --- | --- | --- | --- |
| Themes | 100–116 | 17 | Real Couples 3,867 · Cheating 1,449 · Cuckold 363 · Step-family 707 |
| Kink | 120–137 | 18 | Impact 4,114 · Dom/Sub 2,508 · Humiliation 2,297 · Bondage 2,132 · Pegging 657 · Watersports 96 |
| Cast | 140–153 | 14 | Girl-Girl 974 · Teen Lesbians 445 · Solo 2,116 · FFM 1,957 · Interracial Orgies 812 |
| Age & Age-Gap | 155–165 | 11 | Teens 6,228 · MILF 4,796 · Teen+40s Man 1,083 · Teen+50s Man 165 · MILF+Younger Men 208 |
| Ethnicity | 170–181 | 12 | Interracial Nation 7,065 (the library's defining cut, 34%) · IR Teens 2,028 · Latinas 2,701 · Asian (non-JAV) 717 · Indian & Middle East (performer list) 103 |
| Body | 185–203 | 19 | Busty 5,336 · Alt & Tattooed 6,145 · Petite 2,047 · Hairy 1,988 · Tan Lines 249 |
| Look / Setting | 205–216 | 12 | Lingerie 2,661 · Stockings 1,630 · Outdoors 1,222 · Dungeon 140 |
| Production | 218–226 | 9 | Narrative 1,769 · Amateur 1,368 · POV 1,125 · **Retro Vault (date ≤2009) 734** |
| Performers | 230–288 | 59 | 22 anchor men (Mick Blue 575 … Isiah Maxwell 168) + 37 women (Anissa Kate 320 … Kylie Rocket 80), JAV-excluded |
| Studios | 290–336 | 41 | Tushy 589 · Vixen 534 · Jules Jordan 569 · New Sensations **1,058 (hierarchical)** · the 7 Kink.com sites (Upper Floor 389 … Training of O 143) |
| **JAV** | 850–872 | 22 | 10 per-studio channels (Madonna 221 · Nagae 44 · Hunter 31 · … ≥10 scenes) + 11 themed (Shared Wives 204 · Affairs 162 · Solo Idols 200 · …) + Studio Deep Cuts 191 (70+ sub-10-scene studios pooled) |

## Caveats discovered while validating

1. **Studio criteria are hierarchical** (`INCLUDES depth -1`): parent-studio
   channels include child studios. Counts for Digital Playground (210), New
   Sensations (1,058), Evil Angel (332), Madonna (221), Deep Cuts (191) are
   the hierarchical live counts — that is what would air.
2. **`tools/import_channels.py` does not project `exclude_studio_ids_any`**
   (the library `criteria` shape does). A "JAV: everything else" channel
   (tag + theme-tag exclusions + studio exclusions) is therefore not
   expressible through the compiled-tier importer — it was dropped from the
   proposal (the honest remainder was only ~28–69 scenes anyway). If wanted,
   import via the channel-library criteria shape, or extend `build_source`.
3. Thin pools flagged: Fabulous 50s (126), Cougar Prep (85), Watersports (96),
   Freckled (75), BBW (63), JAV Mature Woman (50), JAV Creampie (44),
   JAV Drama (40), JAV Cuckold (37), Deep Cuts (191 but wide) — all well under
   the 50-scene rotation, expect frequent loops.
4. `THEME: Hotwife` (1 scene) and `THEME: CFNM`/`Parody` (0) are broken/empty
   tags — hotwife content actually lives in `THEME: Wife sharing` +
   `THEME: Cuckolding` + the HotwifeXXX/Touch My Wife studios (studio block).

## Files

- `channel-proposals.csv` — the deliverable (234 rows, live-validated counts)
- `fetch_library.py` → `library.db` — the read-only mirror (re-run anytime)
- `slice_library.py` → `survey_report.txt` — the full cross-section survey
- `build_channel_proposals.py` → `proposals-debug.txt` — proposal generator
- `validate_channels.py` — live re-validation sweep (exit 0 on 0 drift)

## Next steps (when you're ready to revamp)

1. Review/prune the proposal CSV (family + block columns make filtering easy).
2. Either feed it to `tools/import_channels.py` as a new authoring CSV, or —
   better, post-migration — apply it to the channel **library** via
   ApplyChannelChanges where the richer `criteria` shape (tag ANY-of) can
   merge e.g. all wife-themed JAV tags into one channel.
3. Re-run `validate_channels.py` after any edit — it's the drift gate.
