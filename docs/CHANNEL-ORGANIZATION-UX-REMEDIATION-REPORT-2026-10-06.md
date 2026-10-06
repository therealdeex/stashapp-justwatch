# Channel organization remediation report — 2026-10-06

Implements every finding (R1–R12) and all eight UX improvements from
`docs/CHANNEL-ORGANIZATION-UX-REVIEW-2026-10-06.md` on top of the reviewed
baseline `213f0dc`. Changes are uncommitted in the working tree (8 tracked
files, +2608/−372) plus new verification evidence in
`analysis/channel-org-remediation-2026-10-06/`. Untracked review evidence and
handoff docs were preserved untouched. No production surface was accessed; no
live owner definitions were changed; nothing was deployed.

## How it was built (actual assignments)

| Role | Route (verified live) | Task | Scope |
|---|---|---|---|
| Orchestrator | — (this thread) | decomposition, frozen interfaces, integration, review-finding resolution, final verification | — |
| Backend owner | `claudeAgent_z_ai` / `glm-5.3-flash` (effort high) | `org-backend-impl-1` + fix round 2 | `justwatch/organization.py`, `channel_ops.py`, `library.py`, Python tests |
| Frontend owner (sole) | `claudeAgent` / `kimi-for-coding` — **Kimi K2.8 Preview** (effort max) | `org-frontend-impl-1` + fix round 2 | `ui/index.js`, `ui/styles.css`, `extras/` |
| Verification owner | `claudeAgent_z_ai` / `glm-5.3-flash` (effort high) | `org-testing-impl-1` | harness + evidence |
| Independent reviewer | `claudeAgent_z_ai` / `glm-5.3-flash` (effort max, read-only) | `org-review-1` | adversarial diff review |

Frozen interfaces before concurrent edits: order enum `number|name`
(deprecated `alpha` alias), pending groups as an additive `groups` preview
argument compiled into leading `group.put` ops, submission lifecycle
`idle|submitting|polling|outcome_unknown|rejected|committed` with immutable
submitted snapshots, preview binding fingerprint `{intent, assign, overlays,
groups, revision}`. One frontend owner at all times; file scopes were
disjoint.

**Model substitution disclosure:** the Kimi (claudeAgent) route hit its
5-hour usage quota (HTTP 403) mid-way through the frontend fix round, after
F1–F4 had landed. Rather than silently substituting another model, the
orchestrator completed the remaining fixes (F5–F8, see R11/R12 section below
and the review round below) directly and recorded it here. The main
implementation round was 100% Kimi K2.8 Preview.

## Review finding → fix → verification

Verification notation: **P** = `python3 -m pytest -q` (final: 491 passed);
**H** = real-bundle harness flow (real `ui/index.js` + real Python planner/
transaction layer, temp storage, final run 21/21 pass, exit 0, evidence in
`analysis/channel-org-remediation-2026-10-06/final-shots/`); **N** =
`node --check ui/index.js` + `node --check extras/channel-studio-custom.js`
+ `git diff --check` (all pass).

- **R1 — Stage freezing an old plan under the new label → FIXED.** Every
  planning input feeds `previewFingerprint({intent, assign, overlays,
  groups, revision})`; `stageable` requires the held response's key to equal
  the key recomputed synchronously on each render, so Stage is dead the
  instant anything changes — before the 350 ms debounce. Label, review,
  packet and ops all derive from that one bound response; late/reordered
  responses are discarded by key (plus seq/correlationToken guards). The
  number-resolution sheet shares the rule via one `activeChoice` memo.
  Verified: H flow 12 (400–410 → 500–510 instant Stage; delayed + reordered
  responses; numres debounce), P (plan purity/alias tests).
- **R2 — completing an Apply used a newer staged arrangement → FIXED.**
  `org.pending` is an immutable submitted snapshot `{requestId, expected,
  packet, beforeAfter, tempRefs, correlationToken, label, state}` (session-
  storage durable); stage/undo/redo/discard never touch it; completion
  reconciles only that snapshot. A Stage during an unresolved flight is
  REFUSED outright ("still unresolved" — stronger than the review required);
  an Undo-revealed older staging survives the commit as explicitly stale
  ("Based on rX — the library is now rY" → Reload and replan) and is never
  applied to content drafts. Verified: H flows 8 and 13.
- **R3 — newer creation edits discarded → FIXED.** On receipt, each temp
  draft is diffed against its submitted wire snapshot
  (`draftDiffersFromSubmitted`; plan-authoritative number/groupId and
  server-owned identity excluded). Unchanged drafts drop; newer edits remap
  to `receipt.idMap[tempId]` with the fresh acknowledged base AND the
  submitted plan's final number/groupId adopted (no phantom renumber); a
  definition-fetch failure keeps the draft marked conflicted instead of a
  semantics-breaking null base. Verified: H flow 14; review round 1 findings
  1–2 (major) fixed and re-verified in the final harness run.
- **R4 — include-draft reversing the reviewed group move → FIXED.**
  `freezeArrangementPacket` forces `wire.groupId` from `groupChanges` onto
  every opted-in put alongside the number agreement rule; review truth =
  compiled candidate. Verified: H flow 15 (assignment-only AND combined
  range+group variants; wire put carries the reconciled groupId).
- **R5 — inline group creation broken; prefix alone insufficient → FIXED.**
  `newGroupId()` emits `grp_`+8 hex; the planner accepts an additive
  `groups` preview argument (typed validation: `bad_group_id`,
  `group_id_conflicts_committed`, `duplicate_group_id`, `bad_group_name`,
  `duplicate_group`, `bad_intent`), treats validated pending groups as
  addressable, and compiles their `group.put` first — one Apply creates the
  group and assigns/places channels. Only REFERENCED pending groups are
  compiled (review round 1 finding 4; orphan puts eliminated). Verified: H
  flows 6 and 16 (editor, occupied-create via `groups`, bulk
  create-and-assign, combined ordering group.put → channel.create → moves);
  P (packet-omission + composition tests).
- **R6 — transport error left Apply permanently disabled → FIXED.**
  Lifecycle states `submitting → polling → outcome_unknown` (transport
  failure AND exhausted poll budget `status:"unknown"` — review round 1
  finding 1); the bar never sticks on "Applying…", offers **Check result**
  (one honest poll; reconciles a genuinely committed durable receipt without
  resubmitting) and **Retry same Apply** (byte-identical packet + requestId;
  server replays by digest), including in the bar's empty state; recovery
  after reload sets the same state; discard is never required; a new Stage
  cannot silently overwrite an unresolved submission. Verified: H flows 7
  and 17 (abort before submit; lost response AFTER a real commit).
- **R7 — `alpha` rejected → FIXED.** Chips store/emit `"name"`;
  `normalizeOrder` upgrades legacy persisted `"alpha"` on load; the planner
  accepts exact `"alpha"` as a documented deprecated alias for `"name"` and
  orders the UNIFIED selection (casefold, token tiebreak) so temp rows
  interleave instead of appending; arrange_range emits `channel.create`
  skeletons for temps. Verified: H flows 4 and 18 (frontend wire packet uses
  `"name"`); P (alias + interleave tests).
- **R8 — reload/replan lost the original scope/settings → FIXED.** The
  staged intent now embeds the FULL editable form (scope type + explicit
  group id + interval, action, destination group, new-group name, range,
  block start, offset, order, conflict strategy, outside interval, combined
  assignment, pendingGid); rehydration validates ids against current groups
  and warns on membership changes ("was N, now M"); stale reversals are
  rebuilt and re-validated fresh via `stageReversal` (Back-to-edit on a
  reversal opens its read-only annotated review with a Replan action —
  review round 1 finding 8). Verified: H flow 19 (Performers scope retained
  after reload — never the first group; all settings retained; combined
  composition re-commits).
- **R9 — editor Apply bypassed the coordinator → FIXED.** The editor's
  Apply rides the same `enqueueSubmit` chain (`submitChannelApply`) with
  revision adoption before the queue releases — adopted SYNCHRONOUSLY into a
  ref-level slot on fetch, not at render (review round 1 finding 6). A
  packet reviewed at an older revision is explicitly refused at Apply
  ("Reload and replan"), never silently re-based. Verified: H flows 8 and 20
  (editor + bulk back-to-back: serialized, second runs at the first
  receipt's revision, zero self-inflicted conflicts).
- **R10 — malformed inputs crashed before typed validation → FIXED.**
  `_check_ops` pre-validates shapes for all opcodes ahead of a
  never-raising number interpreter; malformed containers, nested entries,
  unhashable ids, invalid kinds and malformed put/create/group records
  return typed `{path, code, message}` errors (`bad_assignment`,
  `bad_kind`, `unknown_channel`, `bad_group_id`, …); Apply stores a durable
  REJECTED receipt (digest included) recoverable via GetChannelApplyResult;
  identical retries replay exactly; definitions/revision never move; no
  broad exception laundering. Verified: H flow 21 (review's exact payloads ×
  Validate + Apply + receipt replay); P (17 malformed-payload tests).
- **R11 — review table misaligned → FIXED.** Glyph+name are ONE Channel
  cell; organizer review has an explicit 4-column grid, the number-resolution
  sheet its own 3-column grid; 36 px stride enforced; the "Also assign"
  checkbox is excluded from the full-width input rule. Full text of
  overflowed cells is exposed as `title` AND as an accessible name on a
  focusable cell (keyboard/screen-reader disclosure — review round 1
  finding 10). Verified: H flow 10 (column alignment ≤2 px, dark+light
  sweeps, 200% zoom, focus-trap); screenshots in the evidence dir.
- **R12 — harness exited 0 despite failures → FIXED.** The gate now exits
  NONZERO on ANY behavioral assertion failure (known- or new-classified) or
  crash; STRICT is retired — known-failure markers can never satisfy the
  gate. Twelve new real-bundle flows (12–21) pin R1–R10. Negative proof:
  a deliberately broken assertion exits 1. Verified: final run 21/21 pass,
  exit 0 (H); negative proof recorded by the verification owner.

## Completed UX improvements (review §"UI/UX improvements")

1. **Task-focused entry:** group-header action opens "Place "<group>" in a
   range" with the group fixed and count shown; Scope+Destination combined,
   then Conflicts, then Review; "Change selection…" exposes advanced scope.
2. **Consequence summary next to the button** ("N of your channels move · M
   other channels keep their numbers / relocate out of the range · What airs
   stays the same"); buttons read "Stage N channel moves" / "Apply N channel
   moves" (dynamic counts).
3. **Visual range occupancy strip** (selected/occupied/free cells, per-number
   inspection, blocker list, honest cap with fallback); plain "Keep other
   channels in place" vs "Move other channels out of this range", outside
   interval revealed only for the latter.
4. **Contextual insert:** dial-row "New channel before/after this" stages the
   draft at the target and opens the conflict resolver; the organize sheet
   now gates Stage on complete source rules for temp rows in scope with an
   actionable notice + "Open "<name>"…" (review round 1 finding 9).
5. **Scannable Review:** "Your selected channels" vs "Other channels that
   move", unchanged group cells hidden, Selected/Displaced text alongside
   color, sticky affected-count summary, "← Back to edit".
6. **Everyday copy:** `WARNING_COPY` plain-language text for all routine
   warnings; technical diagnostics in an expandable "Technical details".
7. **Draft state as a change list:** staged / submitting / outcome-unknown /
   newer-stale all visible; Retry and Undo/Redo discoverable even on the
   empty bar; closing the sheet preserves partially entered settings
   (`organizeFormRef`); a new Stage replacing an earlier unsubmitted plan is
   announced (and an unresolved Apply refuses replacement outright).
8. **Reduced chrome:** single New-channel action per context (dial on
   desktop, topbar when compact); group headers show member count + actual
   number span/gap summary ("100–145 · 3 gaps") with no reservation
   implication.

No persistent reserved group ranges were added.

## Final verification (orchestrator's own runs on the final tree)

```
python3 -m pytest -q                                  -> 491 passed in 40.59s
node --check ui/index.js                              -> OK
node --check extras/channel-studio-custom.js          -> OK
git diff --check                                      -> clean
node analysis/channel-org-2026-10-05/browser-verify-org.cjs \
     analysis/channel-org-remediation-2026-10-06/final-shots
  -> 21/21 flows pass; GATE: PASS — 0 behavioral failures, 0 crashes
  -> HARNESS_EXIT=0
```

Evidence: `analysis/channel-org-remediation-2026-10-06/` — `final-shots/`
(full-run report/log/screenshots incl. dark/light/200%-zoom and the R11
alignment capture), `dry-run-report.json` (`gate.pass: true`),
`verification-results.json`, `verification-summary.md`, `pytest-final.log`.

## Independent review round

A read-only adversarial review (glm-5.3-flash, effort max) audited the diff,
the invariants (identity/revision/bands/contract v1-additive/strict loading/
no DOM injection — no violations found) and issued 10 findings. Disposition:
finding 3 (R12) = the verification owner's task, done; findings 1–2 (major),
4–9 fixed by the owners and re-verified above; finding 10 fixed by the
orchestrator; nothing open.

## Remaining limitations

- The visual sweep uses the synthetic fixture, so Stash's real chrome
  (fonts/icons) is only approximated; DEV visual sign-off by the owner is
  still worthwhile (all verification stayed on synthetic/temp surfaces).
- Poll budget for an Apply receipt is 120 s; a longer server backlog
  surfaces as the actionable outcome-unknown state rather than blocking.
- `move_block` wire intents list `channelIds` then `tempRefs`; for
  `order:"number"` temp rows therefore plan after existing rows (they have
  no number to sort by). `order:"name"` sorts server-side on the unified
  list, which is the path the UI uses.
- Cross-tab concurrent Applies each get honest revision conflicts; there is
  deliberately no cross-tab submission lock (session-scoped pending state is
  per-tab by design).
- The reviewer's finding 6 (render-timing revision adoption) was fixed via a
  synchronous ref-level slot; a full multi-writer server-side queue remains
  out of scope (Stash's job queue serializes applies per library lock).
- Nothing is deployed: dev/prod rollout, plugin reload, and any DEV visual
  pass remain explicit operator steps.
