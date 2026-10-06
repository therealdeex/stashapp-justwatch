# Implementation handoff prompt

Copy the prompt below into the implementation harness. Attach the two owner screenshots if the harness can receive them; the plan also describes their relevant layout.

---

Implement `docs/CHANNEL-ORGANIZATION-UX-PLAN.md` in `/home/shahram/dev/stash-justwatch`. Read `AGENTS.md` and the plan before editing. The planning baseline was `1e96af4` on 2026-10-05; inspect current git state and preserve newer/user changes and unrelated untracked files. This is an implementation task: finish code, meaningful tests, docs, and verification, not another plan.

Act as the orchestrator. Use **Claude–Z.ai GLM-5.3-Flash for most work**: repository inspection, backend planner/API/transactions, tests, compatibility, and documentation. Use **Codex–Kimi K3 with high reasoning for frontend design and frontend implementation**, including the interaction design, React UI, CSS, accessibility, and final frontend review. Keep that allocation during follow-up fixes. Delegate bounded tasks with concrete contracts and exclusive file ownership, integrate their results, and resolve review findings.

If this harness is T3 Code, first discover the live provider/model catalog through `orchestrator_capabilities`. At planning time the requested routes were:

| Responsibility | providerInstanceId | model | Reasoning option |
| --- | --- | --- | --- |
| Most implementation work | `claudeAgent_z_ai` | `glm-5.3-flash` | Provider's supported effort; high was available |
| Frontend design + implementation | `codex_kimi` | `k3` | `reasoningEffort: high` |

These are observed IDs, not a guarantee for another harness. Re-resolve display labels and model IDs there; do not silently substitute `codex_zai`, ordinary Claude, or Claude–Kimi for the requested routes. If a requested route is missing, report the exact mismatch and request an explicit substitute while doing independent work that does not depend on it.

Use T3 `delegate_task` for these cross-provider child tasks, retaining each task ID and a stable client request ID for retries of that task. Do not launch separate top-level conversations merely to implement subagent work. Each delegated review round is a new task with the original brief, prior findings, responses, and unresolved objections. Inspect the live tool schema for how model effort is passed; an instruction saying “high” is not a substitute for setting the supported runtime option. In another harness, use its equivalent child-task routing and reasoning controls. Never claim a requested model did work unless the run metadata confirms it.

The observed T3 target shapes were:

```json
{"providerInstanceId":"claudeAgent_z_ai","model":"glm-5.3-flash","options":{"effort":"high"}}
```

```json
{"providerInstanceId":"codex_kimi","model":"k3","options":{"reasoningEffort":"high"}}
```

Pass these as `target` after checking the live catalog/schema. Supply each task's complete brief explicitly: delegated tasks may not inherit the parent conversation or screenshots.

The owner's goal is easier channel organization while keeping the existing master–detail Channel Studio. Deliver all of these:

1. Visible Organize channels and Groups actions; group-header Select group / Arrange numbers / Edit group actions; clearer selection scope and shift-click selection across the full filtered collection.
2. One number-resolution sheet for create, duplicate, and move. Choices include free-number suggestions, existing-channel swap, upward/downward insertion to the first available slot, and explicit relocation of the destination occupant. A new channel cannot swap because it has no original slot. Never overwrite or silently archive/delete an occupant.
3. Group assignment and create-and-assign, reusing the existing manager/operations; arrange a group or selected set into an inclusive number range; move a consecutive ordered block; shift an interval by an offset while preserving gaps.
4. The specific Performers → 300–699 flow. Preserve outsiders by using available positions by default. Optional one-time exclusivity relocates all outsiders in the whole range into an explicitly selected outside interval. Review capacity and all displaced rows. Do not interpret this as a persistent reservation.
5. A complete before/after review, Stage arrangement, persistent scoped organization drafts, local undo/redo, and explicit receipt-based Apply. Offer Stage reversal after commit through fresh validation and a new Apply; never restore an entire library or delete created records automatically.
6. Frontend polish and optional paste-ready Custom CSS and Custom JavaScript assets, with installation/removal instructions for Stash Settings → Interface. Keep all functionality working when those snippets are absent.

Important implementation findings: bulk group assignment and create-group-plus-assignment already exist. The group manager already creates/renames/reorders/deletes. Improve discoverability and draft persistence rather than adding competing managers. Current `doSwap` is a separate immediate transaction that requires a clean draft. Replace this UI path with staged number-map composition. The nearby comment claiming `swap_pairs` is out of scope is stale; the backend currently passes it explicitly.

Before parallel implementation, have GLM and Kimi agree the exact preview/intents, number-map opcode, draft packet, errors, and component interface. Give `ui/index.js` and `ui/styles.css` one frontend owner. The orchestrator owns the integration boundary and verification.

Implement a deterministic pure Python organization planner and a read-only capability-gated `PreviewChannelArrangement`, using complete lightweight occupancy and no scene queries. It must model explicitly included temporary creations and draft placements without writing or publishing server identities. Correlate preview results with revision and draft identity so stale responses cannot replace a newer review. Use stable order and tie-breakers. Remove moving channels from occupancy first. Include archived/paused/disabled channels as occupied. Respect immutable bands `ch` 1–99 / `net` 100–899; mixed-band group operations require explicit splitting or explanation, never silent omission.

Add the narrow `channels.renumber` opcode, with explicit `{channelId, number}` assignments, to the existing transaction writer/validator. Validate shape, references, identity, and final candidate occupancy. The current original-library occupancy check rejects legal multi-record moves; change that validation boundary carefully. Legal cycles, swaps, and create-at-a-number-vacated-in-the-same-packet must work, while final duplicates and collisions with untouched records remain errors. Reject ambiguous repeated/conflicting assignments. Organization-only changes must not require fetching hundreds of full definitions or serialize their source rules as wholesale puts.

Reuse existing `channel.create`, `group.put`, `channels.move`, and `channels.patch` where appropriate. Capabilities must advertise the new preview and opcode support; contract v1 and existing operations stay compatible. Initial work needs no storage-schema change. Persistent preferred/reserved group ranges are deferred: schema-1 group records reject unknown fields, so do not add a casual `numberRange` property.

Keep ONE coordinator for every write path. Per-channel Apply and arrangement Apply have explicit separate scopes; never include unrelated dirty content implicitly. Persist frozen packets and pending receipts across reload. Serialize local submissions without guessing the next revision; allow newer edits to remain dirty during an in-flight request. Retry the exact same ops/revision/request ID. Poll `GetChannelApplyResult`; task IDs and unknown receipts are not success. On external revision conflict, retain drafts, reload/replan, and require review of the new result. Rebase number/group changes field by field into unrelated content drafts without overwriting their source or newer organizational edits.

Preserve all project invariants: server-owned IDs/kinds/seeds/provenance; strict corrupt-store errors; exact owner source/exclusion semantics; process-safe library lock; one revision increment; atomic receipt/document persistence; existing task/sync split; bounded rotation; continuing rollout gating and aired ledger. Number/group-only transactions are cosmetic: no reindex, refresh enqueue, rotation-version churn, or schedule reset caused solely by organization.

Kimi should keep the dial/editor/no-tabs composition, improve readable labels and number contrast, expose the organization controls, add a wide sequential review sheet, keep header/Apply scope available during scroll, and verify dark/light, narrow screens, keyboard focus, 200% zoom, and reduced motion. The dial's virtual row height is 44px: CSS and list geometry must agree. Scope customization to Channel Studio and its own dialog roots. Optional JS must be idempotent, clean up on navigation/re-execution, use a documented feature-detected hook for behavior, and never auto-Apply or manipulate private React internals. No second React or external runtime dependencies.

Follow all phases and the acceptance matrix in the plan. Test pure planning and real transactions in temporary storage: insertion directions, block displacement, range capacity, outsider relocation, band limits, archived occupancy, cycles, creation into vacated slots, bad payloads, create+group+renumber atomicity, concurrent revision conflicts, replay/lost responses/reload, newer drafts, field-level rebase, reversal conflicts, and cosmetic playback/refresh invariants. Bridge actual production-bundle UI packets to the real Python validator/transaction layer; mocked success alone is insufficient. Run focused checks as each phase lands, then the full Python suite and real-bundle frontend behavioral checks. Record the current baseline rather than quoting old handoff counts.

For interactive T3 browser verification use its collaborative preview tools first. Verify visuals only on DEV at `localhost:9998` or a local synthetic fixture. Do not capture, screenshot, record, snapshot, or visually inspect production Stash or production Fire TV. Do not deploy production, migrate deployments, toggle rollout files, or change live owner definitions. Use isolated fixtures for write testing; live DEV owner definitions also need preservation.

Deliver implementation, tests, updated API/README, `extras/channel-studio-custom.css`, `extras/channel-studio-custom.js`, copy/paste/removal instructions, DEV-only visual evidence, and `docs/CHANNEL-ORGANIZATION-UX-IMPLEMENTATION-REPORT.md`. The report must state the real model assignments, completed acceptance scenarios, checks and limitations, and walk through inserting at a conflict, assigning/creating groups, moving Performers into 300–699, and undo/reversal. Keep any later production deployment separate.

---
