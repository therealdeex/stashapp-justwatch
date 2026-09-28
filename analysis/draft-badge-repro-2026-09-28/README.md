# Draft badge stuck after Apply (2026-09-28)

Owner report: edits + Apply on `Feature Length (60+ Min)` (net_4a1078b4)
left the "draft" chip showing; the change itself HAD committed.

## What actually happened

The server side was always correct: every Apply committed and its receipt is
durable in the library document (`recentRequests`). The owner's session had
r17 (sort → newest, 15:52:19Z) and r18 (back to shuffle, 15:52:21Z) — both
committed, actor channel-studio.

The defect was client-side: the receipt poll (`pollApplyReceipt`) waited on a
CHAIN of `setTimeout` calls (each poll's sleep scheduled from inside the
previous timer callback). Chrome intensively throttles **chained** timers in
hidden/occluded/backgrounded tabs down to ~1 wake per minute, so the poll
stalled "Applying…" indefinitely (budget was 60 iterations ≈ 2 min of
unthrottled time, but minutes-to-hours of throttled time). Meanwhile the
header chip and the rail pill read the drafts store, which keeps the entry
until the receipt lands — so the UI said "draft" even though the commit was
durable. The iteration cap also stretched wrong under throttling (iterations,
not wall clock).

Reproduction notes: the stall reproduced in this environment only when poll
iteration 1 (t+700ms) missed the receipt (task commit took longer than
700ms), making iteration 2 a second-generation chained timer — which is why
early A/B runs looked harness-dependent. A control-timer experiment proved
the mechanism: fresh (first-generation) timers fired on time while the
chained heartbeat/poll chain stayed silent for ~18s and later resumed en bloc
(throttling, not cancellation). No long tasks, no React render loop, no route
interception involvement (`server.cjs` reproduces without `page.route`).

## Fix (ui/index.js)

`pollApplyReceipt` now:

- bounds the wait by a **wall-clock budget** (`APPLY_POLL_BUDGET_MS`, 120s of
  `Date.now()`), not an iteration count;
- wakes an **immediate poll** on `visibilitychange` / `focus` / `pageshow` —
  returning to the tab resolves a receipt that may have been durable for
  minutes; listeners are removed in `finally`;
- keeps the unchanged semantics: unknown past the budget → honest
  "No receipt yet — draft kept" state, and re-Apply reuses the requestId
  (idempotent replay).

The now-unused `sleep` helper was removed; `APPLY_POLL_TRIES` replaced by
`APPLY_POLL_BUDGET_MS`.

## Verification

- `verify-fix.cjs` — deterministic: installs a +120s penalty on every
  `setTimeout` scheduled after the Apply click (simulating intensive
  throttling), asserts the bar stalls while the receipt is durable, then
  dispatches `visibilitychange` and asserts the bar resolves to
  "Applied at rN" and the draft chip clears. All three checks pass.
- `regression-normal-flow.cjs` — unthrottled applies resolve on the first
  poll (3/3).
- `repro-freeze.cjs` — the original reproduction (stalls whenever poll #1
  misses the commit window).
- `server.cjs` — the same shim UI served over a plain HTTP proxy (no
  Playwright route interception) to rule out harness artifacts.
- `restore.cjs` — returns the dev library record to its pre-test value.

The editor's remount recovery (draft `pending` → resume poll on init) and the
idempotent re-Apply path are unchanged and benefit from the same wake logic.
