# Channel organization — FROZEN contract (Phase 1 output)

Decided by the orchestrator 2026-10-05 from:
- `contract-backend-proposal.md` (GLM-5.3-Flash, claudeAgent_z_ai)
- `design-frontend-proposal.md` (Kimi K3, codex_kimi)

Everything both proposals state in agreement is adopted verbatim from the
backend proposal (op registration, request/response shapes, intent variants
1–8, deterministic ordering/tie-breakers, error-code table, the
`channels.renumber` validation rules 1–10 and the `_final_numbers`
validation restructure, composition table §3c, capabilities §3d, cosmetic
guarantees §3e, test plan §4). This file records ONLY the deltas and the
resolved negotiation points. Where this file and a proposal disagree, THIS
FILE WINS.

## Resolved decisions

0. **(Phase 4a ruling, 2026-10-05)** `packet` is an OBJECT
   `{expectedRevision, ops}`, not a bare op list (matches the UI reader and
   the original proposal's applyPacketHint shape). And `move_block`
   resolves `tempRefs` exactly like every other intent (temp rows take
   consecutive block slots; placements land in `created` + packet
   skeletons) — never silently dropped.

1. **One preview operation.** `PreviewChannelArrangement` (mode token
   `preview_channel_arrangement`, sync) with a single `intent` object
   discriminated by `type`. No per-intent operations. (Both agents proposed
   this independently.)

2. **`applyPacketHint` is renamed `packet` and is server-authoritative.**
   When `valid && !noop`, the response carries `packet`:
   the complete, deterministically ordered op list for everything the
   preview planned — `group.put` ops first, then `channel.create`
   skeletons (kind/number/name/groupId only), then `channels.move`, then
   `channels.renumber`. The browser does NOT recompile or re-plan; it
   (a) merges its full pending-create drafts into the `channel.create`
   skeletons (the planner never fabricates sources/colors/glyphs),
   (b) MAY append explicitly opted-in `channel.put` ops for dirty
   channels whose number agrees with the renumber map
   (`ambiguous_assignment` otherwise), and
   (c) freezes the result once at Stage. Apply submits exactly the frozen
   ops; a retry resubmits byte-identical ops/revision/requestId. There is
   exactly ONE planner (Python); the browser renders authoritative plans.

3. **`correlationToken` is client-generated**, 1–128 opaque chars, echoed
   verbatim in the response and recorded in the frozen packet's metadata
   (never inside ops). The UI discards any preview response whose
   `(revision, correlationToken)` pair does not match its current draft
   state, so a slow response can never replace a newer review.

4. **`explain: true` request flag** (added to the backend proposal): on
   `insert` and `free_number` intents the response additionally carries a
   `choices` block so the shared number-resolution sheet can render every
   legal alternative in ONE call:
   ```jsonc
   "choices": {
     "free":   { "nextHigher": 43, "nearest": 41, "firstFree": 1, "list": [43,44,45,41,40] },
     "swap":   { "available": true, "with": "net_…", "withName": "…", "withNumber": 300 },
     "shiftUp":   { "available": true, "firstFree": 303, "movedCount": 3 },
     "shiftDown": { "available": true, "firstFree": 299, "movedCount": 1 },
     "relocate":  { "available": true }
   }
   ```
   `swap.available` is false when the mover is a tempRef (no original slot).
   Each unavailable choice carries `"reason"` (e.g. "No free number above
   899."). `choices` is null for other intents. Per-choice re-preview is
   only needed when the owner types an explicit target.

5. **Org draft persistence** (Kimi's `makeOrgStore`): sessionStorage key
   `jw-studio-org-v1:<libraryId>` — same origin-scoping and privacy
   characteristics as the existing `jw-studio-drafts-v1:` /
   `jw-studio-ops-pending-v1:` keys (sessionStorage, per-tab, no host
   suffix needed). Contents: staged arrangement (number/group overlay +
   undo/redo history, bounded), the frozen packet once Stage→Apply begins
   (`{baseRevision, ops, requestId, correlationToken, before/after}`), and
   pending-receipt state.

6. **ONE pending-submission slot.** The existing
   `jw-studio-ops-pending-v1:<libraryId>` entry remains the single
   in-flight record for BOTH scopes; its entry gains `"scope":
   "channel" | "arrangement"`. Submissions stay serialized per tab through
   the existing coordinator (`submitOps` + `inFlightRef`); arrangement
   Apply joins it via `submitArrangement` with an explicit scope, never a
   second coordinator.

7. **Three occupancy error codes confirmed**: legacy `duplicate_number`
   survives only for a put whose holder is untouched by the packet;
   `destination_occupied` / `duplicate_destination` are the new
   final-occupancy codes. The new UI understands all three.

8. **Multi-band arrangements compile into ONE packet** (one
   `channels.renumber` op carries per-band assignments; per-kind band
   checks apply per channel). Mixed-band selections still produce
   `band_mixed` listing offenders unless the owner explicitly splits.

9. **swap ∩ renumber in one packet = `ambiguous_assignment`.** The new UI
   never emits `channel.swap`; `SwapDialog`/`doSwap` and the stale
   `swap_pairs` comment are deleted. The opcode stays supported
   server-side for older clients.

10. **`features.arrangement`** name confirmed:
    ```jsonc
    "arrangement": { "version": 1,
      "previewOperation": "PreviewChannelArrangement",
      "renumberOpcode": "channels.renumber" }
    ```

11. **Health numbers may go stale after an arrangement** — accepted,
    surfaced as the `health_numbers_stale` warning. No forced reindex.

12. **Exclusive outsider strategy requires an explicit `outside`
    interval** (`bad_range` when absent); never auto-chosen.

13. Backend open questions 1–8 from the proposal: resolved as
    1=skeletons kept, 2=accept+warn, 3=required, 4=`arrangement`,
    5=reject combination, 6=one packet, 7=confirmed (three codes),
    8=`_effect_summary` branch only.

## File ownership for Phase 2/3

- **GLM (backend)**: `justwatch/organization.py` (new), `justwatch/library.py`,
  `justwatch/channel_ops.py`, `justwatch/contract.py`, `justwatch/main.py`,
  `tests/test_channel_organization.py` (new), `tests/test_channel_library.py`,
  `tests/test_channel_ops.py`, `docs/CHANNEL-CURATION-API.md`, `README.md`.
  Never touch `ui/` or `extras/`.
- **Kimi (frontend)**: `ui/index.js`, `ui/styles.css`,
  `extras/channel-studio-custom.css`, `extras/channel-studio-custom.js`.
  Never touch `justwatch/`, `tests/`, or docs.
- **Orchestrator**: this file, the implementation report, integration
  verification, and any cross-boundary fix decisions.
