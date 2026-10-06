# Channel-organization remediation — verification summary (2026-10-06)

Real-browser behavioral proof for the R1–R11 remediation: the actual `ui/index.js`
bundle in headless Chromium against the real Python planner/transaction layer
(`justwatch.organization` + `justwatch.library` + `justwatch.channel_ops`) in
per-context temp storage, via `analysis/channel-org-2026-10-05/browser-verify-org.cjs`.
All storage is synthetic and temporary; no dev/prod Stash host, no live data, no
production surfaces touched.

## Gates — every gate exits 0

| Gate | Command | Result |
| --- | --- | --- |
| Python suite | `python3 -m pytest -q` | **491 passed** (exit 0), `pytest-final.log` |
| JS syntax (core) | `node --check ui/index.js` | OK |
| JS syntax (extras) | `node --check extras/channel-studio-custom.js` | OK |
| Full browser harness (21 flows) | `node analysis/channel-org-2026-10-05/browser-verify-org.cjs analysis/channel-org-remediation-2026-10-06` | **exit 0 — GATE PASS: 0 behavioral failures, 0 crashes** |
| R12 negative proof | throwaway copy + one deliberately failing check | **exit 1** — a behavioral failure now fails the command, not only crashes |

## R12 — the gate is real

`browser-verify-org.cjs` previously returned exit 0 with failing assertions. It now
exits nonzero when ANY behavioral assertion fails (`failNew` **or** `failKnown`) or any
flow crashes; known-vs-new failure classification is kept in the JSON for reporting but
can never satisfy the gate. Proven in both directions: full run → 0; a deliberately
broken assertion in a throwaway copy → exit 1 with
`GATE: FAIL — 1 failing assertion(s)/crash(es)`.

## Flow-by-flow results (21/21 pass)

Old flows 1–11 (updated to the reworked UI: dynamic "Stage N channel moves" labels,
the persistent empty bar, chip-based conflict strategy, task-focused group sheet):

| Flow | Scenario | Result |
| --- | --- | --- |
| 1 | create-at-occupied + shift chain + downward mirror | pass |
| 2 | existing 299 → 300 with dirty name (opt-in put + renumber) | pass |
| 3 | block 171/173/180 → 300 with bystanders | pass |
| 4 | Performers 300–699 useAvailable + capacity + shortfall + R7 alpha | pass |
| 5 | exclusive range with outside interval | pass |
| 6 | create group + assign in one Apply (R5) | pass |
| 7 | revision conflict + R6 unknown-outcome retry | pass |
| 8 | edit-in-flight, undo/redo, rebase, reversal, R2 stage-in-flight | pass |
| 9 | capability gating fallback | pass |
| 10 | visual sweep + geometry + keyboard + **R11 column-alignment check** (≤2 px header/row drift) | pass |
| 11 | extras snippet lifecycle | pass |

New R1–R10 regression flows (12–21):

| Flow | Regression | Key assertions (all pass) |
| --- | --- | --- |
| 12 | **R1** stale preview | Stage dies the instant an input changes (before the 350 ms debounce); a held 400-preview landing late is discarded (review + staged packet stay 500-based); the number-resolution sheet obeys the same rule (303 plan never stages after typing 305) |
| 13 | **R2** submitted snapshot | Stage during flight is refused (see note 1); **Undo during flight** reveals plan A while B's packet runs; B commits; A survives as stale with "Reload and replan"; the bystander draft keeps its name and adopts only the committed map (401, never an uncommitted number) |
| 14 | **R3** newer creation edits | server record keeps the submitted name; drafts not emptied — the newer name remaps onto the final `net_` id; the temp draft drops |
| 15 | **R4** include-draft group | committed group is the REVIEWED Trio for assignment-only AND combined range+group variants; the wire `channel.put` carries the reconciled `groupId` |
| 16 | **R5** group creation | editor inline create in ONE Apply (grp_ prefix); occupied-create previews via the `groups` argument (no unknown_group) and commits `group.put → channel.create → renumber` in one packet; bulk create-and-assign |
| 17 | **R6** unknown outcome | abort before submission → Check result / Retry same Apply, never stuck on "Applying…"; honest unknown after Check; byte-identical retry commits once under the kept requestId; lost-response-AFTER-commit → Check result reconciles the durable receipt with no resubmission |
| 18 | **R7** order enum | frontend sends `order:"name"`; review plans by name; staged packet packs Alpha Net → 303, Zulu Net → 304 |
| 19 | **R8** replan retention | external revision + reload + Reload-and-replan keeps scope group `grp_perfo01` (not the first group), Alphabetical order, useAvailable strategy, 400–410 range, and the combined Trio assignment; both preview halves re-run at the new revision |
| 20 | **R9** one coordinator | editor Apply + bulk Pause of the same channel serialize: expected 12 → 13, both committed, no self-inflicted revision conflict |
| 21 | **R10** malformed payloads | `assignments:true`, `assignments:42`, nested-array entry, `kind:"invalid"` → typed `bad_assignment`/`bad_kind` through real Validate AND Apply; durable replay-exact REJECTED receipts; revision + 18 definitions untouched; no crash |

## Notes for the orchestrator (no open product defects found)

1. **Stage-during-flight is refused by design** (`ui/index.js` `stageArrangement`,
   pending guard + "still unresolved" toast). This is stronger than the review's R2
   asked for and structurally blocks the original repro; the surviving R2 hazard is
   Undo during flight, which flow 13 now exercises. The remediation report should
   state this decision explicitly.
2. **The arrangement bar persists as a "Nothing staged" empty state** while the undo
   stack is non-empty (UX7), and Stage labels are dynamic ("Stage N channel moves").
   Old bar-unmount expectations were updated in flows 4/8 accordingly.
3. **Transient test failure, resolved mid-session:** against an earlier revision of
   the uncommitted backend diff, `test_unreferenced_pending_group_is_omitted_from_packet`
   failed (its base arrange_range was a genuine planner noop, contradicting the test's
   own premise). The backend owner amended the test (3 channels, range 295–306) while
   verification ran; the final suite is 491 passed, exit 0. No product change involved.

## Evidence

- `dry-run-report.json` — machine-readable harness report (`gate.pass: true`, 21/21)
- `dry-run.log` — full per-check log ("FAIL " occurs 0 times)
- `pytest-final.log` — `491 passed in 38.81s`
- `dark-0*.png`, `light-0*.png` — synthetic dark/light captures;
  `dark-03-organize-review.png` is the R11 visual sanity capture (CHANNEL / NUMBER /
  GROUP / WHY columns aligned, reason text inside the WHY column, 36 px rows)
- Harness changes live in `analysis/channel-org-2026-10-05/browser-verify-org.cjs`
  (R12 gate, preview delay/reorder + lost-after-commit route arms, `groups` recording,
  flows 12–21); no product files modified by verification.
