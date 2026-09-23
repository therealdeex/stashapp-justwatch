# Channel curation remediation — handoff prompt

Copy the prompt below into the implementation harness with access to both repos.

---

Repair the completed channel-curation implementation against the independent audit.
Read these files first:

- `/home/shahram/dev/stash-justwatch/docs/CHANNEL-CURATION-AUDIT.md`
- `/home/shahram/dev/stash-justwatch/docs/CHANNEL-CURATION-REMEDIATION-PLAN.md`
- `/home/shahram/dev/stash-justwatch/docs/CHANNEL-CURATION-DESIGN-DECISION.md`

Repositories:

- Plugin/browser: `/home/shahram/dev/stash-justwatch`
- TV: `/home/shahram/dev/StashAppAndroidTV`

This is an implementation assignment, not another planning exercise. Complete the
fixes, meaningful regressions, dev integration, documentation, and release-ready
artifacts. The prior frontend review is finished: **Option A is selected**. Do not
build five more prototypes or introduce a new design-approval gate. Make routine
technical decisions and carry the remediation through validation.

## Preserve the owner's decisions and data

Keep the existing owner library, seeded originally from the 513-network proposal
plus customs, including all edits since migration. Preserve IDs, seeds, aired
history, groups, sources, and policy intent. Exactly one group per channel, with
groups visible in both browser and TV. Every mutation requires explicit Apply.
Keep dynamic performer/studio selection, with accurate documented semantics.

Do not solve defects by reseeding the library, reverting to the 795-channel tier,
disabling editing, resetting scheduling history, changing excluded content, or
broadly activating continuing mode. The operator rollout/fixed pins remain intact.
Production deployment is outside this assignment; finish dev verification and
release/rollback documentation without waiting for a production approval.

## Baseline and evidence

The audit examined plugin `2536613` and TV `46c23ad3`. Check actual HEADs and read
current AGENTS.md files; do not reset local work to these commits. Preserve the
existing untracked migration and continuing-review artifacts.

The audit ran 329 Python tests and 44 focused TV JVM tests successfully. Those
passes did NOT cover the independent reproductions. Read/run:

- `analysis/channel-curation-audit/reproduce.py` → `results.json`
- `analysis/channel-curation-audit/browser-reproduce.cjs` → `browser-results.json`

The Python probes use disposable directories and synthetic clients. The browser
loads the real production UI with mocked GraphQL, first unmodified to demonstrate
the boot crash, then with only an in-memory count() shim to expose hidden bugs.
No production code was changed by the audit. Preserve these pre-fix observations;
add proper post-fix assertions rather than relabeling the old outputs as passing.

## Required repairs (C1–C12)

1. **C1 — frontend startup.** Supply the missing draft-store count() contract and
   actual memory fallback. The production page currently throws
   `draftsRef.current.count is not a function` after loading a valid library.
   Add a test that mounts the production route, not a prototype.
2. **C2 — source-to-playback parity.** Preview uses the new criteria projector;
   Lineup and both indexers still use the old one. New criteria channels throw
   during playback, and edited filter channels silently drop supported rules.
   Unify query/identity/epoch semantics across preview, Lineup, health, and both
   schedulers; include text q and distinguish different criteria source keys.
3. **C3 — transaction atomicity.** A pause followed by conflicting creates returns
   rejected but persists the pause without a revision increment. Stage operations
   independently; a rejected receipt must accompany untouched definitions. Include
   digests in rejected receipts so exact retries actually replay.
4. **C4 — identity enforcement.** Fix the unreachable/stale-variable bulk-patch
   validation. An arbitrary seed patch currently commits. Enforce identity and
   allowed fields on the complete candidate through every opcode.
5. **C5 — durable work and health merging.** Commit refresh intent atomically with
   definitions, fix queue read/modify/write races, acknowledge only the processed
   generation, and merge health into the latest snapshot. The audit demonstrates
   missing crash-recovery work, deletion of concurrently enqueued work, and loss
   of the first channel's health in a two-channel batch.
6. **C6 — schedule and build guards.** Do not serve source-incompatible publications
   or encore after Apply. Respect running-airing boundaries/current-Lineup fallback.
   Guard source/order/policy/state/effective-mode/generation under the actual writer
   lock. An older shuffle worker currently publishes after the source's sort was
   changed to newest.
7. **C7 — programming parity.** Preserve network programming policies through
   adapters and cosmetic edits. Honor fixed pins. Resolve effective mode once for
   editor, both directories, Schedule, and schedulers. Do not offer a mode that
   cannot be prepared periodically and served for that channel namespace.
8. **C8 — drafts and rebasing.** Save newer edits even while an immutable snapshot
   is applying; preserve them through navigation/remount/creation ID remap. Track
   each draft's base definition/revision and pending request identity. Reload must
   not let an old full-record draft overwrite someone else's newer fields by
   borrowing the new global revision.
9. **C9 — Apply everywhere.** Group renames currently write on blur. Stage group
   edits with Apply/Discard and commit new-group+move atomically. Use one shared
   coordinator for all mutation paths, including unknown-outcome receipt recovery;
   transport failure is not proof that “Nothing changed.”
10. **C10 — migration/restore safety.** Drift must mean nonzero/no commit, including
    dry-run. Recheck strict input snapshots against real writers. Back up the real
    compiled path and full managed scheduler state. Restore exact state/absences,
    not overlay newer publication/rollout files. Preserve already-migrated edits.
11. **C11 — preview/validation truth.** Pass q, report actual playable rotation,
    reject malformed references/dates/IDs, negotiate nested-filter capability, and
    make dynamic max exclusive consistently. Raw Stash entity scene_count is NOT
    the proposal's post-JAV eligible-count semantics; explain the difference and
    preserve seed definitions until deliberately edited.
12. **C12 — TV invalidation.** Parse and compare per-channel membership/order/epoch/
    effective-mode identities. Cosmetic/group changes should update presentation
    without invalidating all timelines. Test actual ViewModel refresh decisions,
    not just a helper fingerprint.

Follow the detailed remediation plan for architecture, execution order, acceptance
cases, storage upgrade/recovery design, and scope. Fix the common underlying paths;
do not patch only the individual fixtures. Add fault-injection/concurrency tests
for the real operation boundaries and browser tests for the selected production UI.

## Verification and execution constraints

Use temporary data for migration, restore, crash, and race tests. Back up the actual
dev deployment before schema upgrade or end-to-end edits. Dev Stash is port **9998**;
its API key file is `/opt/stash-dev/API_KEY`. Do not print credentials.

Read the current TV device policy. At audit time the permitted dev target was
`.105` / USB `G072JN0734330EBH`, with emulator fallback. Do not use `.169` or other
family TVs for launch, UI inspection, screenshots, or interactive testing. Older
plugin device notes are not a substitute for the actual TV AGENTS.md.

Run the full Python suite, production-browser tests, relevant TV JVM tests, and
assembleDebug. Then verify the actual selected Stash UI → Apply → stored definition
→ fresh directory/content → TV behavior on dev. Retest no-write-before-Apply,
newer in-flight draft survival, two-tab nonoverlapping edits/conflicts, request
recovery, current-vs-old content pools, and exact backup/restore. Verify 513-channel
list performance and bounded large-entity queries. Do not invent results when the
dev library is small or a device is unavailable.

Correct stale API/migration/implementation docs and any inaccurate design-decision
claims while retaining the original owner choice. Deliver
`docs/CHANNEL-CURATION-REMEDIATION-REPORT.md` containing a C1–C12 fix/test matrix,
actual tested commits and commands, independent post-fix evidence, dev checks,
APK location, accurate remaining baseline limitations, and release/rollback steps.

Do not claim completion while these reproduced failures remain, and do not stop
at fixing the startup error. No additional frontend selection or routine approval
is needed. Finish the corrective implementation and report precisely what was
verified, what changed, and any concrete evidence gaps.
