# Channel Studio: channel organization and UX implementation plan

Prepared 2026-10-05 against commit `1e96af4`. This is a plan, not an implemented feature. Inspect the current checkout before implementation; preserve later changes and unrelated untracked files.

The owner likes the existing master–detail editor. Keep it, make organization actions discoverable, and let the owner review a complete arrangement before explicitly applying it. The essential outcome is that inserting a channel, moving a block, assigning groups, and putting Performers into 300–699 take a guided operation rather than dozens of individual edits.

## 1. What exists and what needs changing

The supplied screenshots show a dense left dial and a long editor with number, group, rules, programming, and preview sections. Organization actions are hard to discover beside the detailed content controls. Improve those controls without replacing the page with a spreadsheet or adding tabs.

Verified repository findings:

| Area | Current implementation | Consequence |
| --- | --- | --- |
| Number changes | `EditorPane` in `ui/index.js`; conflict text says “Use Swap” | No insertion, displacement choices, block moves, or range arrangement |
| Swap | `SwapDialog` / `doSwap`; immediately submits a separate transaction and requires a clean channel draft | Cannot combine an existing channel edit with its number resolution |
| Groups | `GroupsManagerDialog`, behind the topbar overflow menu | Create/rename/reorder/delete already exist; improve access and persistence rather than building a second manager |
| Bulk grouping | `moveDialog`, behind Select → Move to group | Existing-group assignment and create-group-plus-assignment already commit atomically |
| Selection | Checkboxes and Select all for the filtered collection | Add explicit group/range selection, selection scope, and shift-click selection |
| Backend operations | `channel.put`, `channel.create`, `channel.swap`, `group.put`, `group.delete`, `channels.move`, `channels.patch` | Reuse these for group/state changes; add a narrow number-map operation |
| Number validation | `library._check_channel_draft` checks occupancy in the original library; explicit swap pairs get an exemption | A collection of ordinary `channel.put` operations cannot reliably express valid permutations or insertions |
| Transaction storage | Deep candidate, final validation, process-safe lock, expected revision, durable receipts | Extend this path; organization must not introduce another writer |
| Library reads | Lightweight channel rows; UI already fetches all pages | Plan from complete occupancy, never the rendered virtual list or only a search result |
| Styling | `ui/styles.css`, `.jw-studio`, `.jw-*` components, theme variables; 44px virtual dial rows | Improve readability without breaking virtualization or other Stash pages |

The comment near `EditorPane.buildOps` claiming the swap-pair variable is out of scope is stale: the inspected backend passes it explicitly. Remove or correct it while implementing the new flow; do not reproduce that diagnosis as a current bug.

Relevant sources: `AGENTS.md`, `docs/CHANNEL-CURATION-API.md`, `docs/CHANNEL-CURATION-HANDOFF.md`, `docs/CHANNEL-CURATION-REMEDIATION-REPORT.md`, `ui/index.js`, `ui/styles.css`, `justwatch/library.py`, `justwatch/channel_ops.py`, `justwatch/channel_service.py`, `justwatch/refresh.py`, and `justwatch/contract.py`.

## 2. Product decisions

Ship these in the first implementation:

1. A visible **Organize channels…** button and **Groups…** button in the topbar.
2. A shared number-resolution sheet used by create, duplicate, direct renumber, and Organize.
3. Bulk group assignment, create-and-assign, and arrange a group or selection within a number range.
4. Move an ordered block, insert with minimal shifting, and explicitly shift an interval by an offset.
5. Before/after review, persistent organization drafts, draft undo/redo, and receipt-based Apply.
6. Clearer hierarchy, readable labels, narrow-screen support, and paste-ready optional Custom CSS/JavaScript.

Keep numbers as the ordering mechanism. Do not add drag ordering. Reordering group headers can continue to use explicit up/down controls.

Groups organize channels; they do not change rules, source membership, or the continuing rollout. A group may contain both namespaces, but each channel keeps its existing kind and legal number band: `ch` 1–99, `net` 100–899.

“Arrange Performers in 300–699” initially means a reviewed, one-time renumbering of the chosen group. It does not silently reserve that range forever. Persistent preferred ranges and exclusive reservations are a later feature: they need a deliberate storage compatibility and rollback design because schema-1 group records currently reject unknown fields. Do not sneak a `numberRange` field into current storage or infer a reservation from a group's present minimum/maximum number.

## 3. Main interaction design

### 3.1 Keep the familiar page

Keep the left dial and right editor, with a sticky selected-channel header and sticky Apply area. Topbar actions should read **New channel**, **Select channels**, **Organize channels**, and **Groups**. Leave export, diagnostics, and reload in overflow. At narrower widths, wrap controls or use one labeled Actions menu; do not hide all organization behind an unlabeled ellipsis again.

Next to the number input, expose **Move / insert…**. A typed occupied number shows the occupant's number and name and **Resolve conflict…**. Keep the intended number in draft while resolving; do not silently reset it or apply a swap.

Group headers offer **Select group**, **Arrange numbers…**, and **Edit group…** through a labeled contextual action. The selected channel's Group field includes **Create group…**. Use the existing single-membership model throughout.

### 3.2 Number conflict choices

The sheet describes the requested destination and every displaced channel. Offer only choices that are legal for the operation:

| Choice | Existing-channel move | New channel / duplicate | Behavior |
| --- | --- | --- | --- |
| Use a free number | Yes | Yes | Suggest next higher free, nearest free, or explicitly chosen free number in the legal band |
| Swap numbers | Yes | No | Destination holder takes the moving channel's original number |
| Insert and shift upward | Yes | Yes | Move the contiguous occupied run toward the first higher free slot |
| Insert and shift downward | Yes | Yes | Move the contiguous occupied run toward the first lower free slot |
| Relocate the occupant | Yes | Yes | Send the destination holder to an explicitly chosen free number |
| Cancel | Yes | Yes | Keep all drafts; change nothing on the server |

A new channel has no original slot to swap into. Do not label a relocation as a swap. For insertion, explain “3 existing channels move; the shift stops at the first free number.” Never offer automatic archive, overwrite, deletion, reseeding, or kind conversion as a collision solution.

If a direction is unavailable, show why: “No free number above 899.” A full band cannot accept another channel. Archived, paused, and disabled records still own their numbers; filtering them out does not free capacity.

### 3.3 Organize channels sheet

Use a wide sheet/dialog with sequential sections, not tabs:

1. **Channels:** selected IDs, an entire group, an inclusive current-number interval, or the current filter. Show scope and count. “Select all 87 matching channels” includes virtualized/offscreen matches. Hidden selections remain visible in the count and can be cleared.
2. **Action:** Assign group; Arrange within range; Move block to starting number; Shift interval by offset. Show the relevant controls only.
3. **Destination and order:** group selection or create-group name, start/end numbers where needed, and ordering. Default to existing number order. Alphabetical ordering is opt-in, deterministic, and uses an ID tie-breaker. New temporary rows also need a stable tie-breaker.
4. **Conflicts:** preserve other channels in place by default. Offer relocation of blockers only as an explicit strategy with a visible destination interval. Show total capacity, needed slots, occupied slots, and blockers.
5. **Review:** a searchable/virtualized table with Channel, Old number → New number, Old group → New group, and Reason. Distinguish selected rows from displaced bystanders with text. Show all affected rows, not only the selected group. Highlight errors and expose the complete list when a compact summary is shown.

The sheet's main action is **Stage arrangement**. This updates local draft truth and the dial preview. It does not write. A scoped sticky bar then says, for example, “87 channels renumbered · 12 other channels relocated · Apply arrangement.” Review can be reopened. Close preserves the staged arrangement; Discard is explicit.

Show **Apply channel** versus **Apply arrangement** when both scopes exist. Do not make Apply unexpectedly include every unrelated dirty channel. All operations use one submission coordinator, but their scope remains explicit.

### 3.4 Owner example: Performers in 300–699

Flow: Performers header → Arrange numbers → start 300, end 699 → keep current order → Review → Stage → Apply arrangement.

For this hypothetical fixture, 180 Performers channels need 180 of 400 slots. If outsiders occupy 320 and 450:

- Default **Use available numbers** skips those positions. The 180 group members all land within 300–699; outsiders stay put.
- Optional **Make this range exclusive** relocates every outsider in 300–699 to free numbers in an explicitly chosen interval in the same namespace, then packs the Performers group from 300. Explain that this is a one-time exclusive arrangement, not a persistent reservation. Require enough outside capacity.

Selected channels already within the destination range are removed from occupancy before assignment. For exclusivity, all outsiders in the range count, even ones beyond the packed group's last number. If the group has more members than capacity, show the shortfall and disable staging. If it contains `ch` records, explain that those cannot move into 300–699 and offer an explicit net-only subset or separately configured per-band arrangements. Do not silently omit them.

Group assignment is independent: a selected set can move into an existing group without renumbering, or create a group and move there in the same Apply. A combined group assignment + arrangement is also one reviewed transaction.

## 4. Deterministic number planning

Implement a pure Python planner, preferably `justwatch/organization.py`. It consumes a complete lightweight occupancy model plus explicit intent and returns a final number map, group changes, blockers, displacement reasons, and typed errors. Use no Stash scene queries. The browser renders authoritative plans rather than maintaining a second collision algorithm.

Required rules:

- Remove moving records from occupancy first. Their old slots become available to the plan. Sort selected records deterministically.
- For an upward insertion into occupied N, find the first free F ≥ N, shift records in N…F−1 upward by one, and place the selected record at N. Downward insertion is the mirror. If N is free, move directly. Reject no-capacity and cross-band plans.
- For a block insertion, place selected records consecutively beginning at N; keep displaced unselected records in their original relative order and push them upward through available positions until all fit. Default scope is the band's remaining interval. This is distinct from skipping blockers.
- Range packing uses free positions inside the chosen inclusive range, respecting other occupants. Keep current order unless the owner chooses another order.
- Exclusive arrangement relocates outsiders deterministically to available positions in the explicitly chosen outside interval, then packs the selection. Outside destinations cannot overlap the exclusive range.
- Shift-by-offset maps each selected record from oldNumber to oldNumber + offset, preserving gaps. It does not automatically move unselected blockers. Show collisions and let the owner choose another action or an explicit reviewed relocation plan.
- Number intervals select the records currently in those slots; they are not implicit numeric offsets. Display resolved IDs and counts before staging.
- Archived/paused/disabled records participate in occupancy. All channels remain present. Unrelated rules, state, names, colors, glyphs, seeds, provenance, and programming remain untouched.
- Return an empty/no-op result when nothing changes; the UI should not submit a useless revision increment.

Add a read-only, capability-gated `PreviewChannelArrangement` operation. Proposed input: expected revision, intent, draft correlation token, and narrowly scoped placement overlays for drafts explicitly included in this arrangement. Existing refs are IDs; pending creations use temp refs. Proposed response: library revision, correlation token, number changes, group changes, displaced rows, capacity, warnings/errors, and data needed to compile the final Apply packet. Finalize exact JSON names in phase 1 and document them before parallel implementation.

Pending create/duplicate placement must be supported without committing the channel just to reserve a slot. The planner models temporary rows without publishing server IDs or seeds. A creation's final number goes into its ordinary `channel.create`; the renumber map only needs existing IDs. Group creation/assignment continues through existing group operations. If unrelated pending drafts cannot be composed safely, surface that conflict rather than assuming they occupy or release a committed number.

Preview has no write, receipt, history, refresh, or identity-allocation side effects. Apply uses a frozen explicit map, not “shift whatever is there when the task finally runs.” A different library revision requires a new preview and a new request ID.

## 5. Atomic backend changes

Add this narrow opcode to `ApplyChannelChanges` and `ValidateChannelChanges`:

```json
{
  "op": "channels.renumber",
  "assignments": [
    {"channelId": "net_11111111", "number": 300},
    {"channelId": "net_22222222", "number": 301}
  ]
}
```

The IDs above are illustrative. Require a nonempty list, existing IDs, one assignment per ID, real integer numbers (reject booleans), legal bands, and exact allowed keys. Permit cycles and swaps through this operation. Reject duplicate final destinations and collisions with untouched records.

Separate shape/reference/identity validation from final occupancy validation. Stage the whole transaction on an independent candidate, then validate its final numbering. In particular, create-at-300 plus existing-record moves that vacate 300 must pass even when 300 is occupied in the original library. Do not merely suppress `duplicate_number`, or add exemptions that stop enforcing final uniqueness.

Establish unambiguous composition: at most one final number assignment per existing channel in a packet. The compiler should strip redundant number changes from full-record puts or ensure they agree; reject ambiguous conflicting explicit assignments. Existing standalone `channel.swap` stays compatible. New UI packets use `channels.renumber` for arrangements and avoid mixing sequential swaps with explicit number maps.

Reconcile this with the current candidate construction in `library.validate_changes`, `apply_transaction`, `_check_ops`, `_stage_ops`, and final identity validation. Validate and Apply must agree. Compile group-create before group-move. Final group membership and channel identity remain validated on the complete candidate.

Preserve:

- `ApplyChannelChanges` as a task; preview/validation as bounded sync operations.
- `.library.lock`, revision recheck, one revision increment, atomic receipt/document persistence, same-payload retry discipline, and rejected receipt behavior.
- Source/reference validation for actual content edits; organization-only changes should not make scene/entity lookups or require hundreds of full definitions.
- Stable IDs, kinds, seeds, provenance, exclusion rules, effective programming, and continuing ledgers.
- Cosmetic effect classification for number/group changes: no rotation rebuild, refresh intent, schedule rewrite, or playback consumption reset solely because of an arrangement.
- Existing payloads, strict corruption behavior, library-absent legacy fallback, and contract v1.

Advertise arrangement support additively in `Capabilities`, including the preview operation and opcode support. Old plugin/new UI should explain the unavailable advanced actions and retain supported basic editing. Do not assume `channelLibrary` alone implies the new opcode exists. Initial work needs no storage-schema change.

## 6. Drafts, Apply, concurrency, and undo

Extend the existing shared coordinator instead of creating a second fetch/write/retry mechanism. Persist the organization draft and its immutable submitted packet separately from per-channel drafts, scoped to server + library identity. Preserve drafts across dialog close, channel navigation, reload, typed validation errors, and transport failures. Keep the existing privacy characteristics of browser-session storage.

A packet contains base revision, exact ops, resolved target IDs, temp-create refs, correlation identity, and a before/after metadata map. Apply freezes it once. Retry reuses the exact request ID, revision, and serialized content. A task ID or unknown receipt is not success. Poll `GetChannelApplyResult`; use `receipt.idMap` to reconcile creations. Do not clear unacknowledged/newer draft state.

Serialize submissions from this tab while allowing the owner to continue editing a newer draft. Do not silently enqueue a later packet against a guessed revision. Other tabs/processes can still conflict; preserve intent and offer **Reload and replan**, showing the new displacement review before another Apply. Never blindly replay an old arrangement onto a new revision.

When a touched channel also has an unsaved content draft, show the scope explicitly. If only organizational fields are committed, rebase the content draft field by field: update the acknowledged number/group base, retain unrelated edits, and surface a genuine divergent number/group edit as a conflict. Do not use wholesale `channel.put` for bystanders, which could overwrite their rules or newer metadata.

Undo/redo before Apply is local, bounded, and applies to the organization's metadata draft. After a committed arrangement, provide **Stage reversal** using the inverse touched-field map, then fresh validation/review and a NEW Apply. This is not a full historical library restore. If later changes collide or the original operation created channels/groups, explain the limits; do not delete them automatically or discard later source edits. Preserve the ordinary history/restore surface.

## 7. Styling and optional Stash settings snippets

Implement baseline UX and interaction in the plugin's `ui/index.js` / `ui/styles.css`. Deliver optional paste-ready `extras/channel-studio-custom.css` and `extras/channel-studio-custom.js` plus installation/removal instructions. The snippets should personalize presentation and shortcuts through a documented, scoped plugin hook; channel mutations must still use the plugin's draft and Apply flow.

Stash supports both custom CSS and injected JavaScript; see its official [Interface options documentation](https://docs.stashapp.cc/in-app-manual/interface/). Target Settings → Interface → Custom CSS / Custom JavaScript, verify the labels on DEV, and preserve any existing user snippets when explaining how to append these.

Design requirements:

- Keep Stash theme variables, dark/light support, familiar restrained chrome, brand glyph tiles, and number-based rows. Use text and symbols alongside status colors.
- Make small labels readable; strengthen number contrast and hierarchy. Avoid shrinking text to fit more controls.
- Remove duplicate display of the number in the rail name if the separate number cell already carries it; keep the full owner-authored name unchanged in storage and accessible text.
- Increase organization action visibility; show destination/occupant summaries near number editing. Keep technical rollout explanations out of ordinary organization flows.
- Group long rules/programming sections with succinct summaries and optional disclosure. Do not hide authored exclusions or error-bearing controls behind closed disclosures without an indicator.
- Maintain selected-channel header and current Apply scope while scrolling. Reveal the selected row after staging a renumber without unexpected focus loss; do not change selection by number rather than ID.
- Support keyboard focus, labeled controls, proper dialog focus containment/return, Escape that keeps drafts, accessible selection, screen-reader announcements, reduced motion, 200% zoom, and narrow screens.
- Preserve 44px virtual dial geometry unless `DIAL_ITEM_HEIGHT`, CSS, and measurement logic change together. A pasted CSS override alone must not increase row heights or wrap virtualized names.
- Scope CSS to `.jw-studio` and explicitly identified Channel Studio dialog roots. Do not restyle global Stash buttons, navigation, performer pages, or generic Bootstrap classes.
- Optional JS is idempotent, route-scoped, cleaned up on navigation/re-execution, and uses the existing `PluginApi` React instance if needed. No external dependencies, second React, embedded credentials, direct storage writes to the library, private React-fiber manipulation, or synthetic clicking to trigger Apply.
- If a plugin hook is introduced for customization, feature-detect/version it and provide a cleanup function. Baseline functionality must work with custom snippets absent or removed.

Deliver DEV-only screenshots of the final page, conflict sheet, group/range review, and narrow-screen state. Use synthetic data or DEV. The supplied owner screenshots may guide layout; do not take new production screenshots or inspect production UI.

## 8. Implementation phases and ownership

| Phase | Primary owner | Work and completion gate |
| --- | --- | --- |
| 0. Baseline | GLM-5.3-Flash | Read repo instructions, inspect current state, record focused baseline, identify tests/harnesses and API wiring |
| 1. Contract and design | Orchestrator + Kimi K3 high + GLM | Agree exact intent/preview/op/draft schemas; Kimi produces the sheet and action hierarchy design; freeze contracts before concurrent edits |
| 2. Planner and transaction | GLM-5.3-Flash | Pure planner, preview, atomic number-map op, capability additions, typed errors, Python regressions; no scene queries for organization |
| 3. Frontend | Codex Kimi K3 high | Selection, shared conflict sheet, organization review/draft overlay, groups discoverability, scoped coordinator integration, accessibility, CSS, optional settings snippets |
| 4. Integration and review | GLM for behavioral tests; Kimi for frontend review | Browser-to-Python cases, race/retry/reload checks, dev visuals, compatibility checks; resolve all substantive findings |
| 5. Handoff | Orchestrator | Update API/README, produce implementation report and usage examples, report verified scope and limits |

Do not let multiple agents edit `ui/index.js` simultaneously. Assign one frontend owner. Backend work can run alongside frontend design once the contract is agreed. The orchestrator owns integration and final decisions, rather than treating each agent's completion claim as verification.

## 9. Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Create at occupied 300; run 300,301,302; 303 free | New row at 300; existing rows become 301,302,303; one Apply and one revision |
| Same creation, downward choice; 299 free | Original 300 goes to 299; creation at 300 |
| Existing row 305 moves into occupied 300 | Remove 305 first; legal reviewed displacement uses the available old slot if needed |
| Existing row 299 swaps with 300 while its name is dirty | Name edit and number map can be explicitly included in one packet; no clean-draft prerequisite |
| New channel conflict sheet | Swap absent; relocation and free-number choices available |
| Move selected 171,173,180 as block starting 300 | Selected order preserved, consecutive targets, all displaced rows reviewed |
| Shift selected interval by +10 | Relative gaps preserved; untouched blockers produce typed errors |
| Performers group into 300–699, preserve outsiders | Every selected legal record lands in range; outsiders stay; capacity exact |
| One-time exclusive range | Every outsider in the whole range relocated into the chosen outside interval; no permanent reservation implied |
| 401 rows into 300–699 | Clear one-slot shortfall; no staged or committed partial arrangement |
| Mixed namespace or band overflow | Explicit explanation, optional user-chosen split, no silent omissions or kind changes |
| Full band, archived rows, paused/disabled rows | Honest occupancy; no automatic reclaim, overwrite, archive, or delete |
| Create group + assign + renumber | One valid transaction; rejected operation leaves definitions/revision unchanged |
| Cyclic map and create into vacated number | Validate and Apply accept legal final candidates; final duplicates still reject |
| Bad IDs, duplicate assignments, booleans, unknown keys | Typed errors; identity remains immutable |
| Filters and virtualized selection | Exact scope/count across offscreen rows; hidden selections explained |
| Concurrent writer between preview and Apply | Revision conflict; drafts retained; replan and review required |
| Lost submit response; reload during Apply | Same packet/request retries or receipt recovery; no duplicate commit |
| Edit while Apply is in flight | Earlier packet may commit; newer edits remain dirty |
| Organizational Apply on a row with content draft | Source draft preserved and metadata rebased without wholesale overwrite |
| Cosmetic arrangement | Rotation version, eligible scene membership, published airings, continuing ledger, and refresh queue unchanged |
| Draft undo and staged reversal after commit | Previewable field-level change; new Apply for reversal; later source edits preserved |
| Old backend or unmigrated deployment | Advanced feature gated; supported basic editing/fallback preserved |
| Dark/light, 200% zoom, narrow view, keyboard | Usable review/Apply and no overlaps, lost focus, or broken virtual-row geometry |
| Custom snippets installed/removed | Route-limited presentation; core functionality and explicit Apply remain correct |

Use temporary libraries and deterministic fixtures for substantive tests. Reuse relevant cases in `tests/test_channel_library.py`, `tests/test_channel_ops.py`, `tests/test_curation_remediation.py`, `tests/test_channel_edit_followup.py`, and the real-bundle harness patterns under `analysis/channel-edit-audit-2026-09-24/` and `analysis/ui-polish-2026-09-28/`. Historical autosave/prototype harnesses are not authoritative.

Run focused tests as changes land, then the full Python suite and real-bundle behavioral checks. Compare with the actual baseline; do not inherit old passing counts from previous handoffs. For interactive browser verification in T3, use its collaborative preview tools first. Mock success alone does not establish backend compatibility: bridge real UI packets into the real Python validator/transaction layer in isolated storage.

## 10. Deployment boundaries and deliverables

Implementation may modify code/tests/docs and temporary fixtures. Do not change live owner definitions, migrate a deployment, toggle continuing rollout, or deploy production under this handoff. DEV visual verification uses `localhost:9998` only. Never capture production browser/Fire TV UI; production verification is machine-readable only if later explicitly authorized.

Required deliverables: implemented baseline flows; additive API documentation; meaningful planner/transaction/browser regressions; optional CSS/JS assets and copy/paste/removal instructions; DEV visual evidence; and `docs/CHANNEL-ORGANIZATION-UX-IMPLEMENTATION-REPORT.md` recording phases, tests, model assignments, limitations, and exact user workflows. Persistent reserved ranges are explicitly deferred, not a partially implemented first-release promise.
