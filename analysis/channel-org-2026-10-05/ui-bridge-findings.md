# UI bridge findings — Phase 4a (backend/behavioral verification, 2026-10-05)

The partial Phase-3 UI (`ui/index.js`, Kimi K3, unfinished) was bridged to the
landed Phase-2 backend by encoding the UI's REAL wire packets as fixtures and
driving them through the real stack: `channel_ops.op_preview_channel_arrangement`
(via the `Ctx`/`FakeClient` idioms) → the response's packet, frozen the way
`freezeArrangementPacket` freezes it → `channel_ops.op_apply_channel_changes`
(string args, JSON-string ops — exactly `runTask`'s flattening). Encoded in
`tests/test_arrangement_ui_bridge.py` (12 scenarios + 2 xfail tripwires).
File/component refs below are `ui/index.js` vs `justwatch/…`.

## (a) MISMATCHES — integration bugs for Phase 4b

### M1 — CRITICAL: `packet` is a bare op list; the UI expects `{expectedRevision, ops}`
- Backend: `organization.py:816-852` `_packet()` returns a LIST of ops;
  `plan()` publishes it at `organization.py:919` (and `None` for noops,
  `organization.py:866`).
- UI reads an OBJECT everywhere:
  - `ui:1208` `clone((resp.packet && resp.packet.ops) || [])` — with a list,
    `.ops` is `undefined`, so **Stage freezes an EMPTY op list**; Apply would
    then commit a no-op packet (valid, `_check_ops` passes) and burn a
    revision reporting success while changing nothing.
  - `ui:1235-1236` `resp.packet.expectedRevision` (fallback to `resp.revision`
    exists, but only because `.ops` silently empties first).
  - `ui:1277-1281` `combineArrangementResponses` iterates `packet.ops` of both
    responses → combined packet would be empty.
  - `ui:1297` `buildReversal` iterates `staged.packet.ops`.
  - `ui:2378-2390` relocate-augment does `work.packet.ops.push/find` →
    **TypeError** against a list (crashes the number-resolution sheet's
    relocate choice).
  - `ui:1054-1056` the org-store schema documents
    `packet: { expectedRevision, ops, requestId }`.
- Frozen contract decision 2 says "the complete, deterministically ordered op
  list" without fixing the envelope — the two sides picked different ones.
- Cheapest fix: wrap in `plan()` (`"packet": {"expectedRevision":
  doc["revision"], "ops": _packet(out)} | None`) + update the Phase-2 tests
  that assert the list shape (`tests/test_channel_organization.py:89,201,336`,
  `tests/test_channel_ops.py` none). Frontend-only fix would touch ≥6 sites.

### M2 — move_block silently drops `tempRefs`
- UI: `ui:2675-2680` `buildIntent()` for move_block always sends BOTH
  `channelIds` (existing, block order) and `tempRefs` (draft rows in block
  order) + `start`; group/interval scopes legitimately include temp rows
  (`ui:2594-2601`).
- Backend: `organization.py:446-463` `_intent_move_block` reads ONLY
  `intent.get("channelIds")` — `tempRefs` are neither planned nor reported.
  Every other selection intent routes through `_resolve_selection`
  (`organization.py:128-163`), which handles `tempRefs`.
- Effect today: a block scoped over a group containing drafts plans the
  existing rows only, `valid: true` — a silent omission, exactly what the
  sheet promises never happens (`ui:2932` "nothing is omitted silently").
  Pinned as xfail tripwire
  `test_move_block_with_draft_rows_never_drops_them_silently`.
- Fix (4b decision): either route move_block through `_resolve_selection`
  (temps get create skeletons at their block slot, like arrange_range does)
  or have the UI block temp rows from block scope with a typed message until
  supported.

### M3 — replayed committed receipts are re-annotated (nuance, no UI impact)
- `channel_ops.py:468-485`: on ANY committed status — including the
  idempotent replay returned from `library.apply_transaction`
  (`library.py:493-503`) — the op re-runs `refresh.process_pending` (no-op,
  journal already drained) and re-adds `refresh`/`note` to the receipt.
- The UI consumes only `status`/`revision`/`idMap` here, which agree.
  Documented by `test_lost_response_replays_receipt_without_second_commit`.
  No action required; noted so a future assertion on "replay returns the
  stored receipt byte-for-byte" doesn't misfire.

### M4 — unfinished Phase-3 glue (expected, listed so 4b doesn't misread it)
These are defined but never wired; not wire mismatches:
- `submitArrangement` (frozen contract decision 6) does not exist; arrangement
  Apply has no submission path yet (`ArrangementBar` `ui:3153` is never
  mounted; its `onApply` would be the seam).
- `makeOrgStore` (`ui:1062`), `enqueueSubmit` (`ui:421`), `OrganizeSheet`
  (`ui:2535`), `NumberResolutionSheet` (`ui:2183`) are never instantiated.
- `fetchCapabilities` (`ui:406`) exists but nothing consumes
  `features.arrangement`; `arrange.available` (`ui:3214,3671,4076-4093`) is
  prop-fed, and the `arrangement` prop is never constructed. The backend side
  is correct (`contract.capabilities()["features"]["arrangement"]`, tested in
  `test_capabilities_advertise_arrangement`).

## (b) Fields referenced vs emitted

- Everything the UI consumes from the preview response is emitted with
  matching names/casing: `revision`, `valid`, `noop`,
  `numberChanges[{channelId,tempRef,from,to,selected,reason}]`, `displaced`,
  `groupChanges[{channelId,tempRef,from,to}]`,
  `created[{tempRef,number,groupId}]`,
  `capacity{neededSlots,shortfall,range{start,end,totalSlots,outsiderSlots},outside{start,end,freeSlots}}`,
  `choices{free{nextHigher,nearest,firstFree,list},swap{available,with,withName,withNumber,reason},shiftUp/shiftDown{available,firstFree,movedCount,reason},relocate{available,reason}}`,
  `warnings[{code,message}]`, `errors[{path,code,message}]`,
  `correlationToken` — with the single exception of `packet`'s envelope (M1).
  `suggestions` is emitted but unconsumed by the UI (harmless).
- Receipt fields the UI consumes — `status`, `revision`, `error`,
  `currentRevision`, `errors`, `message`, `idMap` — all emitted
  (`library.py:535-541`, `channel_ops.py:496-506`).
- Preview request: JSON-string `intent`/`channels`, `explain` flag, numeric
  `expectedRevision`, `correlationToken` (1–128 enforced at
  `channel_ops.py:419-421`, echoed verbatim at `:435`) — all accepted as the
  UI sends them (`_parse_json_arg` `channel_ops.py:44-50`).
- Apply request: task args flattened to strings by `runTask` (`ui:307-315`):
  string `expectedRevision` and JSON-string `ops` both parse
  (`_as_int` `channel_ops.py:35-41`, `_parse_json_arg`).
- Edge note: an overlay `groupId` must exist in the COMMITTED doc
  (`organization.py:110-112`); today the editor only offers committed groups,
  so this stays clean — but if 4b lets drafts reference packet-created
  groups, previews will type `unknown_group`.

## (c) Confirmed clean (exercised end-to-end by the bridge tests)

1. Preview envelope + stale-revision guard + token echo (`channel_ops.py:403-437`).
2. All intent shapes the UI builds parse and plan: insert/swap/
   relocate_occupant/assign_group(+createGroup {name} only → derived grp_*
   id)/arrange_range(useAvailable + exclusive w/ outside)/move_block/
   shift_interval/free_number.
3. Number-resolution `choices` matrix fields match the sheet's rendering
   (`ui:2285-2331`) field-for-field, incl. tempRef's swap-unavailable reason.
4. Packet op order group.put → channel.create skeletons → channels.move →
   channels.renumber matches the UI's combine order (`ui:1279-1281`) and
   `_apply_ops` (`library.py:894-965`); skeletons merge with the owner's
   draft (source/color/glyph ride; id/seed/provenance never sent).
5. Opt-in `channel.put` agreement rule: put number forced to the plan's final
   number (`ui:1229-1232`), accepted alongside `channels.renumber`
   (`library.py:763-768`); a dirty name commits in the SAME packet with no
   clean-draft prerequisite; identity (seed/kind/provenance) survives.
6. One revision per Apply — insert+create (S1/S2), two concatenated
   same-revision packets (S7), replan (S9).
7. Displaced-bystander review data (`displaced[]` with selected:false,
   `block-displaced`) matches ArrangementBar's counts (`ui:3158-3164`).
8. Exclusive range relocates every outsider once and stores nothing
   (group/channel records byte-stable apart from numbers).
9. revision_conflict carries `currentRevision`; the frozen packet + drafts
   survive; replan = new revision + NEW requestId; the dead requestId replays
   its rejection digest-disciplined (`library.py:490-503`).
10. Lost-response replay: same requestId resubmits byte-identically → same
    receipt, revision advanced exactly once (S10).
11. Cosmetic invariants through a UI-shaped packet: empty refresh journal,
    rotation_version inputs unchanged, membership signatures unchanged,
    publication file untouched, presentation signature moves only on touched
    rows (S11).
12. buildReversal's inverse map (`ui:1290-1324`) applies as a fresh validated
    Apply; later unrelated source edits are preserved (S12).
13. `ValidateChannelChanges` accepts arrangement packets (incl. renumber-only)
    and queries Stash zero times for them.

GATE: `python3 -m pytest tests/test_arrangement_ui_bridge.py -q` → 12 passed,
2 xfailed (the M1/M2 tripwires). Full suite: **439 passed, 2 xfailed**
(427 pre-existing + 12 new). Nothing committed.

---

# Phase 4c — behavioral review of the INTEGRATED frontend (2026-10-05, read-only)

The completed Phase-3 UI (ui/index.js 6245 lines, extras/) was reviewed against
justwatch/library.py, organization.py, channel_ops.py. M1 is fixed
(`plan()` now wraps the packet: organization.py:914-915) and M2 is fixed
(move_block routes through `_resolve_selection`, organization.py:447-478).
Full suite: **443 passed** (tripwires converted). `node --check` clean on both
JS files. Findings below are ranked; every one was verified against the actual
code, and the critical/major ones carry repros.

## F1 — CRITICAL: client-generated group ids never validate (`bad_group_id`)
- `ui/index.js:442-449` `newGroupId()` returns `"grp" + 10 hex` — NO
  underscore. Server: `library.py:60` `GRP_ID_RE = ^grp_[0-9a-z]{1,24}$`
  (`library.py:677-678` rejects in `_check_ops`).
- Every client-id group path is dead on arrival (preflight
  `ValidateChannelChanges` rejects, Apply aborts typed):
  editor inline "Create group…" (`ui:4204` + `buildOps` `ui:3792-3798`),
  NewChannelDialog "Create group…" (`ui:2082`), bulk "Move to group… create a
  new group" (`ui:5804-5807`). Also poisons NumberResolutionSheet overlays for
  such drafts (`unknown_group`, organization.py:110-112) — the create-at-
  occupied flow can't preview a draft that references an uncommitted group.
- Repro (real `apply_transaction`): ops `[group.put {id:"grpa1b2c3d4e5",…},
  channels.move…]` → `status: rejected, error: validation_failed,
  ops[0].group.id bad_group_id "group ids look like grp_…"`.
  Server-side `new_group_id()` is `grp_<12hex>` (library.py:106-107); the
  arrangement path is unaffected (it sends `{name}` only and the server
  derives, per the bridge tests). Fix: `"grp_"` prefix in `newGroupId()`.

## F2 — MAJOR: opt-in `channel.put` clobbers the reviewed group change
- `ui/index.js:1230-1234` applies the agreement rule to `number` only; the
  put's `groupId` is the DRAFT's (stale) value. Combined packet order puts
  `channels.move` before the appended puts, and `_apply_ops` runs ops in list
  order (`library.py:913-957`) → the put reverts the group the user just
  reviewed. No validation error (numbers are the only packet-aware final plan).
- Repro (real transaction, rev 7): `[channels.move ch→grp_b, channel.put
  {groupId: grp_a, …}]` → `status: committed`, final `groupId: grp_a` while
  the reviewed plan said grp_b. `rebaseDraftsAfterArrangement` (`ui:1342`)
  then moves the draft+base to the phantom grp_b → durable UI/server
  divergence until reload. Reachable via arrange_range+alsoAssign and
  assign_group on any row with a content draft (the "include draft…" opt-in,
  `ui:3168-3180`). Fix: force `groupId` to the plan's final group like the
  number (or omit groupId from opt-in puts on group-changed rows).

## F3 — MAJOR: transport failure during arrangement Apply permanently locks the bar
- Transport throw: `submitOps` returns `{status:"transport"}` WITHOUT clearing
  the pending slot (`ui:5236-5240`); `submitArrangement`'s branch
  `else if (receipt.status !== "transport")` (`ui:5385`) leaves org.pending
  set. `ArrangementBar` disables Apply whenever `pending` is truthy and labels
  it "Applying…" (`ui:3249-3251`) → stuck forever; the only exits are Discard
  (throws away the staging) or waiting for staleBase→replan. Violates "drafts
  survive transport failures". The unknown-receipt path recovers correctly
  (pending cleared → Apply re-enables → same frozen requestId replays); only
  the transport path sticks. (Bulk and editor paths recover: their retry
  buttons re-enable and reuse the requestId.)

## F4 — MAJOR: staging (or undo/redo) while an arrangement Apply is in flight corrupts completion
- Nothing disables "Organize channels…" (`ui:6011-6015`), the editor's
  "Move / insert…", or Ctrl+Z during `arrBusy`. `makeOrgStore.stage/undo/redo`
  all set `pending = null` and replace `staged` (`ui:1084-1110`). On commit,
  `finishArrangementCommit(receipt, org.get().staged || staged)` (`ui:5378`)
  receives the NEW staging: it drops temp drafts named by the NEW packet's
  `channel.create` ops (never committed — draft loss), rebases drafts via the
  NEW `beforeAfter` (numbers/groups that never happened), and
  `clearStagedOnly()` silently discards the new staging. Server stays correct;
  local state is corrupted. Fix: ignore/queue stage-while-pending, and have
  `submitArrangement` pass its OWN `staged` closure (not `org.get().staged`).

## F5 — MAJOR: editor Apply bypasses the submit coordinator
- `doApply` calls `applyChannelChanges` directly (`ui:3864`); only
  bulk/groups/arrangement go through `enqueueSubmit` (`ui:421`, `ui:5215`).
  An editor Apply in flight + any bulk/arrangement action submits immediately
  with the same un-bumped `expectedRevision` → the loser takes a spurious
  `revision_conflict` (server-safe: lock + revision check; no double-commit),
  but this breaks frozen contract decision 6 ("one coordinator for every
  mutation path") and produces self-inflicted conflicts in a single tab.

## F6 — MAJOR: `order: "alpha"` is rejected for arrange_range
- The Order chip stores `"number" | "alpha"` (`ui:2617`, `ui:3056-3063`);
  `buildIntent` forwards it verbatim for arrange_range (`ui:2702`). Server
  accepts only `"number" | "name"` (organization.py:370-374).
- Repro (real `organization.plan`): `order:"alpha"` →
  `valid: False, intent.order bad_intent "order must be 'number' or 'name'"`.
  move_block handles alpha client-side (safe), so the Alphabetical chip is
  broken exactly for Arrange-within-range. Fix: send `"name"`.

## F7 — MINOR: move_block loses interleaved temp rows' block order at the wire
- The UI builds ONE ordered list then splits ids/temps (`ui:2721-2730`);
  `_resolve_selection` always plans ids-then-temps (organization.py:139-157),
  so a temp row between existing rows loses its block position. Honest (the
  review shows the server's actual order), but the promised order isn't the
  planned order.

## F8 — MINOR: reload recovery ignores rejected receipts other than conflict
- `recoverPending` (`ui:5147-5168`) toasts only committed / revision_conflict;
  a queued apply rejected `validation_failed` (or otherwise) clears the slot
  with no user signal.

## F9 — MINOR: pre-base-pin drafts rebase against a stale base after an arrangement
- `rebaseDraftsAfterArrangement` only rewrites `base` when present
  (`ui:1344-1352`); drafts authored before base-pinning get number/group
  updated but keep the old base → the next editor mount's three-way rebase can
  surface phantom conflicts. Legacy-session edge only.

## Verified clean in this round
Packet envelope end-to-end (freeze/combine/relocate-augment vs
organization.plan); tempRef lifecycle (no server ids pre-commit; overlay
merge incl. source/color/glyph); replay discipline (frozen requestId reuse
guards in both scopes; digest mismatch impossible via the UI guards);
unknown-receipt handling (never success; pending kept for same-id retry);
reload recovery for editor (draft `pending`) and bulk/arrangement
(ops-pending + org pending); capability gating (features.arrangement only;
basic editing + groups + bulk work without it; NewChannel blocks occupied
numbers when gated); `extras/channel-studio-custom.js` (idempotent
re-registration with prior-cleanup, route-scoped, feature-detected,
view-only — no mutation surface, no private React, no credentials).

## Acceptance rows still unverified at browser level (for the harness step)
Bridge tests already prove the server semantics; the browser step should
verify UI rendering/interaction for: create-at-occupied-300 with the
shift-up/shift-down chain (one revision); downward insert to a free 299;
existing-row-into-occupied displacement review; 299↔300 swap with a dirty
name in one packet; new-channel sheet showing no Swap; block move
171/173/180→300 with all displaced rows reviewed; shift +10 typed blocker
errors; Performers group into 300–699 (outsiders preserved, capacity exact);
exclusive-range outsider relocation; shortfall message with nothing staged;
mixed-band split buttons; archived/paused occupancy warnings; **create group +
assign + renumber in the UI (currently blocked by F1 — re-test after the
fix)**; cyclic map / create-into-vacated through the editor; typed-error
surfacing under fields; filter + virtualized selection exactness incl.
hidden-selection notices; concurrent-writer conflict bar with "Overwrite rN";
lost-response and reload-during-Apply recovery in the browser; edit-while-
applying ("newer edits still draft"); content-draft rebase notice after an
organizational Apply; undo + staged reversal after commit; old-backend gating
pill and fallback editing; dark/light, 200% zoom, narrow view, keyboard-only
review/Apply; extras snippet install/remove (route-limited, core flow intact).
