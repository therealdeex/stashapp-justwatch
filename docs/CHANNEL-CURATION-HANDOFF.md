# Channel curation — implementation handoff prompt

Copy the prompt below into the implementation harness with access to both local
repositories. The detailed plan is part of the assignment; this prompt does not
replace it.

---

Implement the channel curation project described in:

`/home/shahram/dev/stash-justwatch/docs/CHANNEL-CURATION-IMPLEMENTATION-PLAN.md`

Work across:

- Plugin/browser: `/home/shahram/dev/stash-justwatch`
- Android TV: `/home/shahram/dev/StashAppAndroidTV`

This is an implementation assignment. Carry it through working code, migration
tooling, tests, dev integration, documentation, and release-ready artifacts. Do
not stop at another plan or at five mockups. **There is exactly one planned user
review gate: I must review the five frontend prototypes and give feedback before
you finalize the production frontend.** Continue independent backend, migration,
contract, TV integration, and test work while waiting. After I provide feedback,
incorporate it and finish without asking for another design approval.

## Confirmed owner decisions

1. Start with the **513-channel v4-final network proposal**, plus my existing
   custom channels. Do not start with the old 795 networks or an empty catalog.
2. Make existing channels editable: name, number, group, content rules, appearance,
   enabled state, and supported programming preferences. Preserve stable identities.
3. A channel belongs to **one group only**. Groups organize both the browser GUI
   and the Android TV guide. Group membership does not secretly change content.
4. I press **Apply** to commit changes. No autosave to the server. Preserve pending
   drafts and distinguish applied changes from programming still being prepared.
5. Build **five distinct interactive frontend options**, using shared mock data,
   for me to choose from. User experience is paramount. I may combine elements.
6. Routine implementation decisions are yours. Do not introduce additional design
   approval checkpoints. Production deployment is outside this assignment; deliver
   working dev integration and the release/rollback runbook without waiting for
   production approval.

## Begin with recon and the five prototypes

Read the detailed plan and applicable AGENTS.md files in both repositories. Record
current HEADs and dirty files before editing. Do not reset or discard existing
work. Baselines during planning were plugin `5b33aa1`, TV `61f92187`; inspect the
current code rather than assuming these are still HEAD.

Existing user files at planning time include:

- `analysis/continuing-review/recheck_v071.py`
- `analysis/continuing-review/results-v071.json`
- `docs/CONTINUING-PROGRAMMING-REVIEW-2.md`

Read the newer continuing review. Do not treat older completion reports or a green
suite as proof the review's specific scheduler defects have been resolved. Verify
relevant findings and fix dependencies needed for this implementation; accurately
report unrelated baseline limitations without expanding continuing activation.

Build these five options in an isolated prototype area, with one landing page and
one documented launch command:

- **Channel Studio:** searchable dial/list with an adjacent focused editor.
- **Group Organizer:** group-first browsing and channel organization.
- **Channel Library:** dense searchable/filterable table and bulk operations.
- **Visual Guide:** guide-shaped browsing with contextual editing.
- **Guided Curator:** progressive editing from identity through rules to review.

Make workflows genuinely different, not just styling. Use the proposal's channel
definitions and synthetic custom/example states. Label simulated counts/previews.
Use neutral artwork; do not fetch production scene images for prototypes. Each
variant must support search, rename, one-group assignment, rule editing, preview,
Apply/Discard, navigation with a draft, bulk moves, and recoverable error states.
Exercise keyboard and narrow-screen behavior and large entity sets. Follow the
shared review script in section 4 of the plan.

Present all five working URLs and concise tradeoffs. Ask me for a preferred option
or combination and feedback. Keep the server reachable and provide restart
instructions. Screenshots can supplement, but cannot replace, usable prototypes.
Record my actual response in `docs/CHANNEL-CURATION-DESIGN-DECISION.md`.

**Do not infer my choice from silence or elapsed time.** Do not finalize a frontend
without my response. While I review, continue the independent work described below.
If independent work is exhausted, pause with a precise completed/remaining report
at this gate only. Do not re-ask decisions I have already made.

## Implement the independent foundations

Use the detailed plan as the technical specification, especially these boundaries:

- One authoritative editable library under the Stash data directory, separate
  from plugin installation files. CSV/compiled JSON is the initial seed, not a
  store deployments can overwrite after I edit channels.
- Strict, atomic, process-safe transactions with expected revisions and durable
  request receipts. Same-request retries are idempotent even after a lost response.
- Explicit migration from legacy storage into the selected 513-network library,
  keeping all existing custom channels. Include dry-run, backup, no-op rerun after
  later edits, drift detection, and tested restore.
- Use `data/proposed_channels_final.csv`, `analysis/just-watch-final/networks.preview.json`,
  and `migration_map.csv`. The intended migration retains 292 identities, adds
  221 new channels in reused slots, and leaves 282 other retired slots vacant.
  Do not inherit scheduling/favorite identities by reused channel number.
- Keep source rules, IDs, seeds, exclusions, and survivor scheduling history
  intact during import. Preserve the proposal's five exceptions; the older
  four-row exclusion recipe is for a different catalog. Counts in the proposal
  are historical. Do not falsify current counts on the tiny dev library.
- Groups use immutable IDs and one membership per channel. Keep sparse numbers
  independent of group order. Preserve legacy number bands and payloads for old TVs.
- One resolved channel service feeds Directory, Lineup, previews, schedulers,
  status, and editing. Replace direct runtime reads of the old network/catalog
  stores where necessary, including scheduling commit guards.
- Keep contract-v1 playback compatibility with additive capabilities/operations
  and an enhanced group surface. Arbitrary group names must not go into the old
  three-value `section` field. An intentionally empty library must not fall back
  to generated channels.
- A validated visual rules model supports ANY/ALL inclusions, exclusions, hierarchy,
  date/duration/recency, text search, and lossless existing saved searches. Keep
  large materialized entity sets intact. Reuse tested Stash filter projection.
- Preview is read-only and bounded. Match sample/count responses to the current
  draft. Show pool count, bounded rotation, and published schedule distinctly.
- Apply only touched records. Metadata/group edits do not reindex the entire
  library. Persist pending refresh work and reject stale worker publications.
- A current source-incompatible publication cannot supply newly excluded future
  scenes or an obsolete encore. Use the explicit preparing/fallback behavior in
  the plan; preserve aired history and whole-airing policy protections correctly.
- Keep continuing rollout activation separate. Do not enable all 513 channels,
  bypass fixed pins, or remove the kill switch as part of GUI migration.

Upgrade TV parsing, models, directory refresh, guide/explore/landing group displays,
focus/navigation, pad behavior, and compatibility. Preserve existing channel keys
for favorites/history and avoid playback resets on cosmetic edits. Handle retired
identities and authoritative-empty catalogs explicitly.

## After frontend feedback

Integrate the chosen experience into Stash's `/plugins/stash-justwatch` route,
using the PluginApi-provided React runtime and real operations. Do not ship the
mock adapter as the production data source. All mutation paths must honor Apply,
including creation, renumber/swaps, archive, group changes, bulk actions, restores,
and programming preferences.

An Apply submits an immutable snapshot. Edits made during that request remain a
newer dirty draft and require another Apply. Preserve drafts through validation,
network failure, and revision conflicts. Do not let applying one channel publish
another channel's draft. Wait for a correlated durable receipt before announcing
success. Distinguish successful persistence from background preparation failure.

Implement the required management features and accessibility behavior from the
plan. Keep useful prototype assets, but integrate one coherent production UI.
Update the applicable old guidance: read-only network/CSV authority and autosave
rules are superseded by my explicit decisions here. Preserve unaffected safety,
identity, strict-loading, source semantics, and scheduling invariants.

## Validation, dev deployment, and delivery

Run the plan's acceptance matrix. Include meaningful tests for migration identity,
Apply retries/concurrency/draft isolation, rule semantics, scheduler guards, dynamic
TV groups, compatibility, and authoritative emptiness. Run the Python suite, focused
TV JVM tests, debug APK build, and browser flows. Add a reproducible browser harness
if needed. Do not weaken tests to conceal regressions or call unexecuted checks
passed. Capture actual measurements for the 513-channel and large-entity-list cases.

Dev Stash uses **port 9998**, with the API key file `/opt/stash-dev/API_KEY`.
Never print secrets. Read the actual dev rollout/data and back them up before
migration. Installing code alone must not activate continuing scheduling.

Read the current TV repo's root AGENTS.md before device operations. At planning
time it allowed the `.105` dev stick / USB serial `G072JN0734330EBH`, with emulator
fallback. Older plugin notes naming `.196` are stale. **Do not launch, inspect,
screenshot, or interactively test production `.169` or other family TVs.**
Production deployment is not required by this assignment.

Rehearse migration and rollback on disposable data, then validate the completed
experience on dev: browser edit → Apply → persisted definition → directory refresh
→ correct TV group/content behavior. Keep unrelated local changes intact. Document
any unavailable environment or baseline evidence gaps precisely while completing
all other authorized work.

Deliver:

1. The five prototypes and recorded design selection/feedback.
2. Working selected browser UI, backend, migration/recovery tools, and TV changes.
3. Actual test/dev verification evidence and debug APK location.
4. Updated user, API/schema, AGENTS.md, migration, release, and rollback docs.
5. `docs/CHANNEL-CURATION-IMPLEMENTATION-REPORT.md` with changed files/commits,
   completed acceptance cases, known limitations, and release-ready instructions.

Keep concise progress updates, especially when you reach the prototype review gate
or discover a concrete dependency. No additional design permission requests are
needed after my frontend feedback. Finish the intended implementation rather than
stopping with recommendations for somebody else to do it.
