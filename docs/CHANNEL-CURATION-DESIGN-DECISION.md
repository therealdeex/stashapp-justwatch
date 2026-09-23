# Channel curation — design decision (owner feedback)

Status: **DECIDED 2026-09-23** — owner response recorded below; the
production frontend is Option A. One follow-up requirement (dynamic
performer/studio selection) investigated the same day; findings and the
implemented answer are in the next section.

## Owner response (2026-09-23, verbatim)

> I like 1. Option A - Channel Studio.
>
> But i want more options for choosing performers and studios dynamically.
> Such as 'Studios with less than 2 scenes' is not possible here i think.
> Same with performer. Is our list going to be dynamic? For example, Fringe
> studios is named one by one, not dynamically. Can you please help me
> clarify this and if we can bring those in with dynamic settings instead of
> each studio one by one which would make maintenacen much harder. For some
> i understand doing it manually and i'm fine with that, but we should make
> the effort to make things dynamic where we can. Is this clear? Can you
> please investigate?

## Decision

* **Chosen frontend: Option A — Channel Studio** (searchable dial + focused
  side-by-side editor). Integrated into Stash's `/plugins/stash-justwatch`
  route with the real operations (no autosave; explicit Apply with durable
  receipts).
* **Requirement added by the owner**: dynamic (predicate-based) performer and
  studio selection wherever possible; manual lists acceptable only where
  dynamic is genuinely impossible.

## Investigation: dynamic performer/studio selection (2026-09-23)

**The owner's observation is correct.** The proposal channels "Fringe
Studios" (794 studio ids) and "Rare Performers (<5 Scenes)" (3,569 performer
ids) are materialized snapshots from the 2026-09-08 extraction: new small
studios never join, and studios that grow stay in the list until a re-extract.

**Investigation result (checked against the dev Stash's live GraphQL
schema, not assumptions):**

* Modern Stash exposes **nested relational scene filters**:
  `SceneFilterType.studios_filter: StudioFilterType` and
  `performers_filter: PerformerFilterType`, and both studio/performer filter
  types carry `scene_count` (an int criterion). Verified live on dev:
  `findScenes(scene_filter: {studios_filter: {scene_count: {value: 2,
  modifier: LESS_THAN}}})` returns "all scenes from studios with fewer than
  2 scenes" — exactly the owner's example — with zero id lists, composable
  with the existing JAV exclusion.
* Therefore **"studios/performers by scene count" is genuinely dynamic**:
  membership resolves server-side on every query, needs no refresh job for
  correctness, and can never go stale.
* Pre-requisite check: this requires a Stash version with the nested
  `*_filter` fields. The plugin validates support and reports a clear error
  instead of silently failing (per deployment, at validation/preview time).

**Implemented answer (backend):** the criteria rule model gains two dynamic
rows — `studioSceneCount` and `performerSceneCount` (each optional `min` /
`max` scene thresholds). They project to the nested `*_filter` criteria
directly; they compose with every other row (AND) and with id-based
includes/excludes. Plain-language summaries and previews cover them. The
production Channel Studio rule editor exposes them as "match by activity"
controls on the performer and studio rows.

**What stays materialized (deliberately):** the seeded proposal channels are
kept byte-for-byte as authored — the plan forbids silently regenerating the
research selection, and the owner said manual lists are fine for some. The
owner can, at any time, edit e.g. "Fringe Studios" in the Channel Studio,
replace its 794-id list with the dynamic "studios with fewer than 2 scenes"
row, and Apply — that is a normal, reviewed, one-Apply edit with history.
New dynamic channels are equally one creation away.

**Known limits (honest, corrected 2026-09-23 remediation):** `scene_count`
counts ALL scenes of the performer/studio in the WHOLE Stash library — this
is **total-library entity activity**, NOT the proposal extraction's
**network-eligible post-JAV scene count** (which also applied hierarchical
studio treatment and the per-channel exclusion rules when materializing
"Fringe Studios"/"Rare Performers"). A dynamic `scene_count` row is therefore
a related-but-different membership predicate, not a maintenance-free
conversion of those materialized lists. The seeded proposal channels keep
their materialized id lists byte-for-byte until an explicit content edit
chooses the dynamic semantics; converting a list to a count rule is a
reviewed, one-Apply content decision, never an automatic rewrite. The nested
filters need the newer Stash runtime; on an older server, validation and
preview report "dynamic performer/studio rules are not supported by this
Stash version" instead of failing mysteriously.
