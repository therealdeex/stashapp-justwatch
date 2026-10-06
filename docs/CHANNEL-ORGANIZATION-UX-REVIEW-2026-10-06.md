# Channel organization: implementation review and UX recommendations

Reviewed 2026-10-06 at `213f0dc` (`feat(org): channel organization & arrangement (0.9.0)`). This review adds documentation and synthetic-fixture evidence only; it does not change implementation or live library data.

The architecture is a useful foundation: one deterministic Python planner, explicit number-map transactions, capability gating, and a familiar dial/editor interface. Ordinary insertion, block moves, range packing, and exclusive-range relocation work in the exercised happy paths. The feature is not ready to call complete: several user-facing failures from the implementation's own review are still present, and this review reproduced additional draft-loss and wrong-plan problems.

## Verification and limits

- `python3 -m pytest -q`: **443 passed in 42.18s**.
- `node --check ui/index.js`, `node --check extras/channel-studio-custom.js`, and `git diff --check`: passed.
- Re-ran `analysis/channel-org-2026-10-05/browser-verify-org.cjs` against the CURRENT bundle and real Python planner/transaction layer in temporary storage: **11 flows executed; 7 clean flows and 4 flows containing 6 known failed checks**, with no harness crashes. The failures are F1 twice, F3, F4, F5, and F6.
- Added isolated browser probes for group assignment with an opted-in content draft, newer creation edits during an Apply, group-scope replanning, and stale-preview staging. All four reproduced the problems below.
- Probed malformed number-map payloads against actual `ValidateChannelChanges` and `library.apply_transaction`: exceptions rather than typed rejections, with no receipt.
- Visually inspected fresh dark/light synthetic-fixture captures. Their base typography and icon stubs do not fully reproduce Stash chrome, so do not infer that the real application uses the fixture's serif font or text icon placeholders. Review-column geometry and checkbox sizing are plugin-owned defects reproducible from the CSS/markup.
- T3 collaborative preview status/open explicitly reported no automation host. Used the repository's headless browser harness after that unavailable response. No production UI was accessed or captured. No DEV/production owner definitions, rollout files, or migration data were changed.

Evidence is in `analysis/channel-org-review-2026-10-06/`: `browser-report.json`, `additional-browser-results.json`, `stale-preview-result.json`, `malformed-payload-results.json`, and three synthetic screenshots. The original full rerun's temporary output is `/tmp/channel-org-review-2026-10-06/`.

## Findings, in priority order

### R1 — P1: Stage can freeze an old plan under the new destination label

References: `ui/index.js:2757`, `ui/index.js:2881`, `ui/index.js:2890`.

The preview effect increments its sequence immediately but leaves the previous valid response and `busy=false` until the 350ms debounce expires. Stage checks the old response's validity, not whether it matches the current intent. Its packet is frozen from the old response while its label and saved intent use the new controls.

Browser repro: preview Performers in **400–410**; change the inputs to **500–510**; immediately click Stage. The staged label says “Arrange 2 channels into 500–510” and its stored intent also says 500–510, but the packet assigns **400 and 401**. Apply commits those old numbers successfully at r13.

Fix: invalidate stage eligibility synchronously when any planning input changes. Associate every response with a request/intent fingerprint, overlays, and revision; Stage must require an exact current match and build its label/review/packet from that same immutable result. Apply should also verify this binding. Make the number-resolution sheet obey the same rule during its debounce. Add browser checks that click Stage immediately after changing ranges, scope, destination, order, and conflict choice; delay/reorder preview responses deliberately.

### R2 — P1: completing an Apply uses a different, newer staged arrangement

References: `ui/index.js:1084`, `ui/index.js:1093`, `ui/index.js:5382`.

Stage/undo/redo replace `org.staged` and clear `org.pending` even during an Apply. Completion passes `org.get().staged || staged` into `finishArrangementCommit`, so it can reconcile the receipt against the NEW draft rather than the submitted snapshot. It then clears that new staging.

Browser repro: hold an Apply that moves Chart One to 305; stage a separate 298–299 → 400–401 arrangement while it runs. The server commits only Chart One → 305, but Chart Three's existing content draft is rewritten to **401** while its actual server number remains **299**. The newer arrangement disappears without a commit.

Fix: retain an immutable submitted packet independently of the editable organization draft. Correlate completion with its request ID and submitted before/after map. Reconcile only that snapshot; retain a newer staging as dirty and rebase/review it against the acknowledged revision. Store mutations must never erase an unresolved submitted packet. Test stage/undo/redo/discard and navigation/reload during flight.

### R3 — P1: newer creation edits are discarded after arrangement Apply

Reference: `ui/index.js:5276`.

`finishArrangementCommit` unconditionally drops every created temp draft after receiving `idMap`; it does not compare the current draft to the submitted `channel.create`. This problem exists even without staging a second arrangement.

Browser repro: author a valid new channel named “Submitted channel name,” stage its placement at occupied 300, start a delayed Apply, then change the name to “Newer unsent channel name.” The committed record correctly has the earlier name, but afterward **all drafts are empty**. The later name was lost rather than remapped to the final ID.

Fix: compare each current temp draft to its submitted wire snapshot. If newer edits exist, remap them to `receipt.idMap[tempId]` with the fresh acknowledged base, preserving source/name/programming changes and pending-error state as appropriate. Drop only a fully acknowledged unchanged draft. Test edits after Stage as well as during Apply and receipt recovery.

### R4 — P1: including a content draft silently reverses the reviewed group move

Reference: `ui/index.js:1230`.

The freeze helper forces opted-in `channel.put.number` to agree with the arrangement, but leaves its stale `groupId`. That put is appended AFTER `channels.move`, so it restores the old group without a validation error.

Browser repro: edit Chart One's name, select it, choose Assign group → Trio, and “include draft.” Review says **Alpha → Trio**. The committed name is updated, but the final group is still **Alpha**. The exact packet is a move to `grp_trio001` followed by a full put carrying `grp_alpha001`.

Fix: reconcile ALL planned organizational fields into opted-in puts, including final group membership. Validate final review truth against the compiled candidate before enabling Apply. Extend real-browser tests beyond the existing copied-wire fixtures: assert the final group matches the actual reviewed move both for assignment-only and combined range+group operations.

### R5 — P2: inline group creation is broken; fixing the prefix alone is insufficient

References: `ui/index.js:448`, `justwatch/organization.py:110`, `ui/index.js:3793`.

`newGroupId()` returns `grp` + hex; storage requires **`grp_`**. Inline creation in the editor/new-channel/bulk move paths therefore rejects with `bad_group_id`. This is reproduced by the existing browser flow. The server-planned organizer create-group path is distinct and should retain its current deterministic ID behavior.

A new channel placed at an occupied number also sends its uncommitted group as an overlay. The planner accepts only committed groups, so that flow gets `unknown_group` even once ID formatting is repaired. The number-resolution freeze path also needs to include the pending group's `group.put`, not just the channel skeleton.

Fix the ID generator AND composition of planned groups, placements, and creation. Preview must model a group being created in the same frozen packet without requiring a preliminary group commit. Test editor inline creation, new channel at free/taken slots, bulk create-and-assign, and combined create-group + create-channel + renumber as separate paths.

### R6 — P2: a transport error leaves Apply arrangement permanently disabled

References: `ui/index.js:3249`, `ui/index.js:5389`.

The transport branch retains pending request identity, which is necessary for safe retry, but the bar treats any pending identity as an actively running request. The button remains disabled and says “Applying…” even after `arrBusy` clears.

Reproduced by aborting the task request: three seconds later the button is still disabled. A reload is not a reliable escape; recovery retains an unknown receipt, and the organization pending state remains present.

Fix: distinguish actively submitting/polling, outcome unknown, rejected, and committed. Preserve the exact submitted packet/request ID while showing an actionable **Check result / Retry same Apply** state. Never require discarding the draft to recover. Exercise both an abort before submission and a lost response AFTER a real commit.

### R7 — P2: Alphabetical ordering uses a rejected enum

References: `ui/index.js:3059`, `justwatch/organization.py:370`.

The chip stores/sends `order: "alpha"`; the planner accepts `"number" | "name"`. Alphabetical Arrange within range therefore shows `bad_intent` and cannot stage.

Fix the shared enum and use actual frontend-generated packets in the regression. Keep mixed existing/temp block ordering explicit too: splitting a sorted unified list into IDs then temp refs currently moves all temp rows to the end of a block.

### R8 — P2: Reload and replan loses the original scope/settings

References: `ui/index.js:2600`, `ui/index.js:2611`, `ui/index.js:5494`.

The staged intent stores a scope type and resolved IDs, but not enough UI state to reconstruct the original group/interval and combined assignment. Reopening passes `initialIntent`; scope group initializes from `initialScope` only and otherwise chooses the FIRST group. Order, destination group, create-group name, and combined-assignment choice also reset.

Browser repro: stage Performers into 400–410; make an unrelated external revision; reload/replan. The sheet changes its scope from **Performers** (`grp_perfo01`) to **My Channels** (`grp_my00001`), then reports a mixed-band error. If the first group is in the target band, the sheet could instead generate a plan for the wrong channels.

Fix: persist/reconstruct complete editable intent and explicit scope selection. Show any membership changes caused by the new revision. Replan must retain the selected group/IDs, ordering, combined assignment, conflict strategy, and outside interval. A stale reversal also needs fresh reversal construction/validation; the current branch says it was already validated and leaves the stale packet in place.

### R9 — P2: channel Apply still bypasses the coordinator

Reference: `ui/index.js:3869`.

The editor calls `applyChannelChanges` directly; bulk/groups/arrangement serialize through `enqueueSubmit`. Reproduced by applying an editor draft and pausing the same channel from the bulk toolbar: the operations compete against the same revision and the second receives a self-inflicted conflict.

Fix: use one submission lifecycle for all scopes, including receipt reconciliation and revision adoption before releasing the queue. Arrange packets already reviewed at an older revision must become explicitly stale, rather than silently changing their expected revision. Preserve exact retry identity in all scopes.

### R10 — P2: malformed inputs crash before typed validation and leave no receipt

References: `justwatch/library.py:417`, `justwatch/library.py:443`, `justwatch/library.py:586`.

`_check_ops` invokes `_final_numbers` before validating shapes. `assignments: true` or `assignments: 42` raises `TypeError` while iterating; a create with `kind: "invalid"` raises `KeyError` in `BANDS[kind]`. Both Validate and Apply reproduce the exceptions. The Apply request has no stored rejection receipt, so a client cannot recover its outcome through the normal receipt interface. Definitions/revision remain unchanged in these probes.

Fix: perform shape/type/band validation before number interpretation, or make the interpreter consume only validated records while returning precise typed errors. Test malformed assignment containers, nested entries, unhashable IDs, invalid kinds, and malformed put/create records through both actual ops. Deterministically reject invalid transactions with a durable receipt; do not broadly catch programming faults and mislabel them as valid rejection.

### R11 — P2: the review table misaligns the very changes the owner must approve

References: `ui/index.js:3170`, `ui/styles.css:1044`, `ui/styles.css:499`.

The organizer has a FOUR-column header/grid but FIVE direct row children: glyph, name, number, group, reason. Auto-placement puts the reason on a second grid row under the glyph, while names/numbers/groups sit under the wrong headings. Those extra contents overflow the fixed 36px virtual-row stride. Fresh dark and light screenshots reproduce it. The number-resolution sheet has its own different column definition; fix both explicitly rather than globally guessing column counts.

The “Also assign” checkbox is stretched by `.jw-field input { width: 100% }`, separating it from its label. Restrict full-width styling to appropriate text/number/select fields or explicitly reset checkbox/radio dimensions.

Fix: wrap glyph+name as ONE Channel cell or explicitly assign grid positions; align header and row widths, enforce the virtual stride, and give each overflowed value an accessible full-text view. Test geometry at desktop, narrow widths, and 200% zoom, plus semantic readability.

### R12 — P2: the browser verification command returns success despite failed checks

Reference: `analysis/channel-org-2026-10-05/browser-verify-org.cjs:1013`.

The harness records failing assertions in `failKnown` / `failNew` but returns nonzero under STRICT only for harness crashes. The current full run exits 0 despite six failed assertions; setting STRICT would not make these failed acceptance checks fail the command.

Fix: the release gate must fail for behavioral assertion failures as well as crashes. Temporary expected failures can be reported during development, but cannot satisfy the final acceptance gate. Add R1–R10 as real-bundle regressions rather than leaving them only in a narrative report. The required final implementation report is also absent from this commit.

## UI/UX improvements after correctness fixes

Keep the master–detail editor and restrained theme. The biggest remaining opportunity is reducing the decisions needed for ordinary organization, rather than expanding the toolbar further.

1. **Open on the user's task.** A Performers header action should open “Place Performers in a range,” with that group already fixed and its count visible. Combine Scope + Destination, then Conflict handling, then Review. A small “Change selection” control exposes advanced scope selection. Do not make a straightforward group move begin with five generic sections.
2. **Summarize the consequences next to the button.** For example: “180 channels move · 12 other channels keep their numbers · What airs stays the same.” Use **Stage 180 channel moves** and **Apply 180 channel moves** rather than generic arrangement wording. Label channel-only Apply clearly so its scope remains distinct.
3. **Show range occupancy visually.** A compact strip of occupied/free positions and counts makes gaps, blockers, and capacity understandable. Let the owner inspect a blocker by number. Say **Keep other channels in place** versus **Move other channels out of this range**, and reveal the outside interval only for the latter.
4. **Add contextual insert actions.** Row actions for “New channel before this” / “New channel after this” prefill the target and open the existing conflict resolver. Complete channel rules before allowing a whole create+arrangement commit, or display an actionable validation notice in the placement sheet. The current occupied-create flow opens a Stage-able sheet before any source rules exist, which predictably rejects at Apply.
5. **Make Review reliably scannable.** Repair the current columns first. Then separate “Your selected channels” and “Other channels that move,” hide unchanged group fields by default, keep full names accessible, and use Selected/Displaced text alongside color. Show a sticky affected-count summary and an obvious back-to-edit action.
6. **Use everyday copy for normal changes.** The routine yellow warning currently describes health snapshots and presentation signatures. Put technical details in diagnostics or an expandable details area. An organization flow should explain channel numbers, groups, and playback consequences in ordinary language.
7. **Treat draft state as a visible change list.** Show precisely what is staged, what is currently submitting, and what is newer/unsent. Keep Retry and Undo/Redo discoverable even when the arrangement bar disappears. Closing a sheet should preserve partially entered organization settings, not only already-staged packets. Tell the owner when a new Stage replaces an earlier unsubmitted plan or compose the intents explicitly.
8. **Reduce chrome duplication.** Desktop currently has New channel both in the topbar and above the dial. Prefer the dial action on desktop and a topbar action when the rail becomes compact. Give group headers current member count and actual number span/gap summary without suggesting that a permanent reservation exists.

No persistent reserved group ranges should be added as cosmetic UI state. If requested later, implement them as a separate deliberate storage/validation/rollback feature, as specified in the original plan.

An interactive illustrative mockup is saved in `analysis/channel-org-review-2026-10-06/ux-concept.html`. It demonstrates the shorter task-focused flow and consequence summary using example counts. It is a design artifact, not the implemented editor or a replacement planner; it makes no network requests or library writes.

## Suggested next implementation pass

Fix R1–R4 first because they can lose drafts or commit something different from the reviewed intent. Then fix group creation/composition, retry/recovery, enum alignment, intent-preserving replan, shared submission, and malformed-input rejection. Repair review geometry before polishing the appearance. Finish by making the acceptance gate truly fail on defects and producing the missing implementation report.

Keep the original model allocation for that pass: Claude–Z.ai GLM-5.3-Flash for backend/tests/docs and orchestration support; Codex–Kimi K3 with high reasoning for frontend design/implementation. Frontend source should have one owner. Verify fixes using the real bundle against isolated Python storage; mock success and the 443-test green suite alone do not close these frontend findings.
