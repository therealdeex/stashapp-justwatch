# Channel Studio organization — frontend design + contract proposal

Author: frontend owner (Kimi). Phase 1 deliverable for
docs/CHANNEL-ORGANIZATION-UX-PLAN.md against commit 1e96af4. No
implementation here; this file freezes the interaction design and proposes
the JS-side contract for negotiation with the backend proposal. UI facts
below are verified against the current ui/index.js (4100 lines) and
ui/styles.css (983 lines).

Verified anchors I will build on / replace:

- Topbar: App render (~line 3920): title, search, group filter, a "Select…"
  toggle, one unlabeled overflow menu (Groups…, Export CSV, Keyboard
  shortcuts…, Download diagnostics, Reload library), revision chip,
  dirty/applying pills.
- Bulk mode: bulkMode + bulkSelected (Set of ids) + the bulk bar (Select
  all, Move to group…, Pause/Resume/Archive/Restore, Clear); moveDialog
  (~line 3720) reuses ConfirmDialog with a detail node and already composes
  group.put + channels.move in ONE Apply.
- Groups: GroupsManagerDialog (~line 1420) — staged draft, single Apply,
  create/rename/reorder/delete.
- Swap: SwapDialog (~line 1760) + doSwap (~line 2546) — an immediate
  channel.swap Apply that refuses to run with a dirty draft; the number
  field's hint links "Swap numbers" / "Swap with a nearby channel…" and the
  validation message says "Use Swap.".
- Stale comment at ~line 2288 above buildOps claims the server's swap-pair
  exemption crashes (swap_pairs out of scope). Per plan section 1 this is
  stale — the inspected backend passes it explicitly. Deleted with the swap
  path.
- Dial: WindowedList + DIAL_ITEM_HEIGHT = 44 (JS) and .jw-dial-row /
  .jw-group-header height 44px (CSS) — the geometry must stay in agreement.
  DialRow renders the number TWICE today (.jw-dial-number cell and again
  inside .jw-dial-name as "300 · Name").
- Apply coordinator: module-level applyChannelChanges(requestId,
  expectedRevision, ops) + pollApplyReceipt (visibility-wake, 120 s
  wall-clock budget), per-channel pending inside the draft store, the
  single-slot jw-studio-ops-pending-v1:<libraryId> record for non-editor
  submits, submitOps shared by bulk/groups/patches, the in-flight registry
  plus settled-requestId toast dedupe. Editor drafts live in
  jw-studio-drafts-v1:<libraryId> (sessionStorage with in-memory fallback).
- The UI never calls Capabilities today — feature detection needs a new
  boot-time fetch.
- rebaseDraft(base, fresh, draft) (field-level three-way) already exists and
  is exactly the rebase primitive an organizational Apply needs.

## 1. Action hierarchy and discoverability

### 1.1 Topbar (wide, 761 px and up)

Left to right: Channel Studio title + tagline; search input; group filter;
spacer; + New channel (also kept in the dial tools row — it is the dial's
primary empty-state action); Select channels (renamed from "Select…";
toggles bulk mode); Organize channels…; Groups…; the overflow menu; revision
chip; dirty/applying pills. The overflow keeps exactly Export CSV, Keyboard
shortcuts…, Download diagnostics, Reload library — organization actions
never hide there again. All four organization buttons are plain labeled
buttons, never icon-only.

"Organize channels…" opens the Organize sheet with its scope prefilled from
the current selection (section 4). "Groups…" opens the existing
GroupsManagerDialog.

### 1.2 Narrow (below 761 px): one labeled Actions menu

Choice: a labeled "Actions" menu, NOT wrapping. Justification: the topbar
is chrome over a 44 px-row virtualized dial; wrapping grows the chrome
height unpredictably (worse at 200 percent zoom) and the existing
max-width: 760px block already assumes compact chrome. An unlabeled
ellipsis was the discoverability bug; a labeled menu is not. At narrow
width: "+ New channel" stays visible (creation is the top mobile task);
Select channels, Organize channels…, and Groups… move into "Actions"
(aria-haspopup menu, labeled items, focus return on close — the existing
jw-menu pattern).

### 1.3 Group-header actions

Dial group headers keep click = collapse. They gain a small menu button
(aria-label "Actions for group <name>", 28 px tall inside the 44 px row —
geometry unchanged) with: Select group (enters bulk mode with all member
ids selected), Arrange numbers… (opens the Organize sheet scoped to that
group), Edit group… (opens GroupsManagerDialog focused on that row).

### 1.4 Number field: Move / insert… and the occupied-number affordance

Identity card number field (f-number) changes:

- Beside the input, a persistent link-button "Move / insert…" opens the
  number-resolution sheet for THIS channel at its current draft number.
- When the typed number is occupied, the hint reads: 300 is "Channel Name"
  plus a "Resolve conflict…" button (same sheet, targeting 300). The
  intended number STAYS in the draft while the sheet is open and after
  cancel — never silently reset, never an automatic swap. The field error
  becomes: Channel 300 is taken by "<name>". Use Resolve conflict… — and it
  stops being an Apply blocker only when a staged arrangement resolves it
  (section 5.3 overlay).
- "Create group…" becomes the last option of the Group select (sentinel
  value) in both the editor group card and NewChannelDialog: choosing it
  reveals an inline name field; the group is created in the same Apply
  packet (client-generated grp* id — the pattern moveDialog already proves
  commits atomically).

### 1.5 Editor channel menu

The editor overflow gains "Move / insert…" and keeps Duplicate…; Duplicate
routes through the resolution sheet when its chosen number is occupied. The
immediate-swap entries are deleted (section 9).
## 2. Selection model

- Data structure: selection = { ids: Set<channelId>, anchorId }, keyed by
  channel ID — never DOM nodes, never dial row indexes. The ordered basis
  for range math is filteredIds: the already-memoized allRows (filtered +
  number-sorted, includes temp rows) mapped to ids. That is the FULL
  filtered collection; virtualization is irrelevant to selection math.
- Shift-click: with Shift held and an anchorId present, select the
  inclusive id range between anchor and clicked id in filteredIds (either
  direction), unioned with the existing selection. Works for offscreen rows
  because it is pure list math.
- Scope indicator: the bulk bar's count line becomes explicit — "87
  selected · of 512 shown (filter: Performers)". When the selection
  contains ids outside the current filtered set: "…· 12 selected are hidden
  by the current filter" plus Show (clears the filter) and Clear hidden
  buttons. Selection is never silently dropped by filtering.
- "Select all N matching channels" replaces "Select all"; sets ids to the
  full filtered set with the exact N in the label. Temp (uncreated) rows
  are excluded with a hint ("new drafts have no number on the server yet —
  Apply them first"); a temp row can be the range anchor but never a range
  target for number operations.
- Keyboard: in bulk mode each row checkbox is focusable; Space toggles;
  Shift+Space range-selects from the anchor; Ctrl/Cmd+A while the dial list
  has focus selects all matching; Escape exits bulk mode (drafts
  unaffected). Row aria-selected reflects the checkbox state in bulk mode
  and the editor selection otherwise.
- Group-header "Select group" is the group-scope shortcut into this same
  model.

## 3. Shared number-resolution sheet

One component (NumberResolutionSheet, section 9) serves create, duplicate,
direct renumber, and the Organize sheet's single-row conflicts. Props:
subject { channelRef, name, kind, currentNumber|null, isNew }, target,
onStage(choice, plan), onCancel. channelRef is an existing id or a temp-*
ref — the sheet never distinguishes them in copy except where the choice
matrix requires it.

Choice matrix (plan section 3.2 semantics, verbatim):

| Choice | Existing channel | New / duplicate |
| --- | --- | --- |
| Use a free number (suggestions: next higher free, nearest free, or a typed free number in band) | yes | yes |
| Swap numbers (occupant takes the subject's ORIGINAL number) | yes | absent — never labeled swap |
| Insert and shift upward | yes | yes |
| Insert and shift downward | yes | yes |
| Relocate the occupant to a chosen free number | yes | yes |
| Cancel (keep all drafts, change nothing) | yes | yes |

Copy rules: every offered choice carries its server-verified displacement
line, e.g. "3 existing channels move (301 to 302 … 303 to 304); the shift
stops at the first free number, 305." Each displaced row is named (glyph
tile + number + name) in an expandable list — the browser renders the
planner's authoritative output and never computes displacements itself.
Unavailable choices render disabled with the reason: "No free number above
899.", "Band 1–99 is full.", "Only offered for existing channels."
Full-band honesty: the occupancy summary line reads "299 of 800 numbers in
100–899 are free (archived and paused channels keep their numbers)" —
archived/paused/disabled occupy; filtering never frees capacity.

Data flow: choice AVAILABILITY derives from the complete lightweight
occupancy the UI already holds (lib.channels, all pages — never the
rendered window); the CONSEQUENCE text and final map come from
PreviewChannelArrangement. One preview call per sheet open asks for all
legal choices at once (explain: true, section 5.2) so flipping between
choices is instant and consistent; a typed relocate-to / free-number target
re-previews that one choice (300 ms debounce). Stage writes the chosen plan
into the draft/organization overlay and closes; Cancel closes with zero
mutations. A staged result surfaces as the scoped sticky bar (4.5) or, for
a single existing-channel move, inside that channel's draft.

## 4. Organize channels sheet

Wide dialog (new jw-dialog-xwide, about 1080 px cap, full-height column, NO
tabs), title "Organize channels", sequential numbered sections. Every input
change re-previews sections 4–5 debounced. Stage arrangement and Discard
are the only consequential buttons.

### 4.1 Channels (scope)

Radio scope: Selected channels ("87 selected — change selection" returns to
bulk mode); A whole group (group picker); A number interval (inclusive
from/to; hint: "the interval selects the channels currently in those
slots"); Current filter ("all 96 matching performers"). Below: resolved
count plus the resolved id list behind a disclosure ("Show the 87
channels"), including offscreen/virtualized matches, with the
hidden-selection count and Clear. Mixed-namespace scopes show the split
immediately: "4 of these are My Channels (1–99) and cannot enter 300–899"
with an explicit "Use the 83 network channels only" choice — never a silent
omission.

### 4.2 Action

Radio: Assign group; Arrange within range; Move block to start; Shift
interval by offset. Only the relevant section-3 controls render. Assign
group can combine with a range arrangement in one reviewed packet (group
assignment is independent, plan section 3.4).

### 4.3 Destination and order

Group: existing-group select or Create group… inline name. Range: start /
end numbers with band legality per kind. Move block: starting number.
Shift: signed offset. Ordering: default "Keep current number order";
opt-in "Alphabetical (A–Z, ties by channel id)" — the id tie-break (temp-*
refs tie-break for pending rows) is stated in the control hint.

### 4.4 Conflicts

Default strategy: Use available numbers (preserve outsiders in place) —
"320 and 450 stay with their current channels; the selection packs around
them." Optional "Make this range exclusive (one time)": reveals a required
outside-interval input ("Relocate every other channel in 300–699 to free
numbers in …"), the copy "This is a one-time arrangement, not a permanent
reservation.", and a capacity review line: "Range 300–699 holds 400 slots ·
selection needs 180 · 2 outsiders must move · 612 free numbers in 100–899
outside the range". Every displaced outsider is listed in Review. A
capacity shortfall disables Stage with the exact math ("401 channels into
400 slots — 1 short"); no partial arrangement can stage. Shift-by-offset
never moves unselected blockers: collisions list as typed errors with a
"Resolve each…" link into the number-resolution sheet.

### 4.5 Review

Searchable + virtualized table (reuses WindowedList; fixed 36 px rows):
columns Channel (tile + name), Old to New number, Old to New group, Reason
("selected — packed into range", "displaced — shift stops at first free
305", "relocated — outside exclusive range"). Row text distinguishes
selected rows from displaced bystanders; a filter box narrows by name or
number; errors pin to the top. Stage arrangement writes local truth only
(5.3) and closes to the sticky bar: "87 channels renumbered · 12 other
channels relocated · Apply arrangement · Review · Discard". The dial
previews staged numbers immediately (overlay order; selection and reveal
track ID, never number, so a renumber never loses the selected row; the
WindowedList resetKey does NOT reset on stage, avoiding scroll jumps).
Closing the sheet preserves the staged arrangement; Discard is an explicit
confirm.

Scope separation: when a channel-level draft is also dirty, the editor
Apply bar and the arrangement bar coexist with explicit labels — Apply
channel vs Apply arrangement. Apply arrangement NEVER sweeps unrelated
dirty channels into its packet; the only channel content ops allowed inside
an arrangement packet are ones the owner explicitly opted in per row in
Review (e.g. the dirty name on a swapped channel).
## 5. Frontend contract proposal (negotiating position)

### 5.1 Capabilities

New boot-time runOp("Capabilities") (the UI never called it). Gate:

```json
"channelOrganization": {
  "version": 1,
  "previewOperation": "PreviewChannelArrangement",
  "renumberOpcode": "channels.renumber",
  "intents": ["assign_group", "arrange_range", "move_block", "insert",
              "shift_interval", "relocate_occupant", "swap", "suggest_free"],
  "correlation": "client-generated"
}
```

Missing feature: the sheet and Organize button hide behind a topbar hint
("Arrangement tools need a plugin update — basic editing still works") and
the old immediate-swap behavior is NOT resurrected. channelLibrary alone
never implies the opcode.

### 5.2 PreviewChannelArrangement — ONE op with intent variants

Position: one operation with an intent discriminated union, not seven ops.
Rationale: one capability flag, one correlation lifecycle, one server
planner entry point, one place to add intents later; per-intent ops
multiply the validation/receipt surface for zero client benefit.

Request (Map boundary values stay strings per existing convention; JSON
shown for shape):

```json
{
  "expectedRevision": "42",
  "correlationId": "org-<uuid>",
  "explain": true,
  "intent": {
    "kind": "insert | arrange_range | move_block | shift_interval | relocate_occupant | swap | suggest_free | assign_group",
    "subject": { "channelRef": "net_1a2b3c4d | temp-9f3ab2c1", "includeDraft": false },
    "scope": { "ids": ["net_…"], "groupId": null, "interval": [300, 699], "filterText": null },
    "target": 300,
    "range": [300, 699],
    "direction": "up | down",
    "offset": 10,
    "start": 300,
    "order": "number | alpha",
    "strategy": "useAvailable | exclusive",
    "outsideInterval": [700, 899],
    "groupId": "grp… | null",
    "newGroupName": null
  },
  "overlay": {
    "tempCreates": [{ "tempRef": "temp-9f3ab2c1", "kind": "net", "number": 300, "groupId": "grp…" }],
    "numberOverrides": { "ch_00aa11bb": 12 }
  }
}
```

overlay is narrowly scoped placement for pending rows explicitly included
in this arrangement (plan section 4) — temp refs, never server ids; seeds
and ids stay server-owned. explain: true returns all legal choices with
per-choice plans (the resolution sheet); omitted plans the single intent.

Response:

```json
{
  "revision": 42,
  "correlationId": "org-<uuid>",
  "occupancy": { "band": [100, 899], "free": 612,
                 "suggestions": { "nextHigher": 303, "nearest": 299 } },
  "choices": [{ "kind": "insert", "direction": "up", "available": true,
                "reason": null, "moved": 3,
                "summary": "3 existing channels move; the shift stops at 305" }],
  "plan": {
    "assignments": [{ "channelRef": "net_…", "from": 301, "to": 302, "reason": "shifted_up" }],
    "groupChanges": [{ "channelRef": "net_…", "from": "grpA", "to": "grpB" }],
    "groupCreates": [{ "id": "grp9f…", "name": "Performers", "position": 4 }],
    "displaced": [{ "channelRef": "net_…", "from": 320, "to": 701, "reason": "outsider_exclusive" }],
    "counts": { "selected": 87, "bystanders": 12 }
  },
  "capacity": { "needed": 180, "available": 399, "shortfall": 0, "band": [100, 899] },
  "warnings": [{ "code": "mixed_namespace", "message": "4 My Channels rows cannot enter 300–899",
                 "channelRefs": ["ch_…"] }],
  "errors": [{ "code": "capacity_shortfall | band_full | cross_band | unknown_ref | bad_intent",
               "message": "…", "path": "intent.range" }],
  "packet": {
    "ops": [
      { "op": "group.put", "group": { "id": "grp9f…", "name": "Performers", "position": 4 } },
      { "op": "channels.move", "channelIds": ["net_…"], "groupId": "grp9f…" },
      { "op": "channels.renumber",
        "assignments": [{ "channelId": "net_11111111", "number": 300 }] }
    ]
  }
}
```

packet is SERVER-EMITTED and stored verbatim: the UI freezes it, persists
it, and submits it byte-identical on retry. This keeps the op compiler
(group.put before channels.move before channels.renumber; stripping or
reconciling number fields inside any included channel.put) in ONE place.
The backend may want the UI to compile ops from plan; I hold for
server-emitted packet — a UI-side compiler reintroduces exactly the second
collision algorithm the plan forbids ("the browser renders authoritative
plans"). The UI only AUGMENTS a packet by appending explicitly-included
channel.put / channel.create ops (the dirty name on a swapped channel; a
temp row whose final number comes from the plan). If the backend prefers to
pre-build those too (subject.includeDraft: true), I will take them
pre-built.

channels.renumber opcode: exactly plan section 5 — {op,
assignments:[{channelId, number}]}, nonempty, one assignment per id, real
integers, legal bands, cycles legal, final duplicates and untouched
occupant collisions reject. The UI emits it verbatim from packet.ops.

Correlation: client-generated correlationId ("org-<uuid>"), persisted with
the staged draft, echoed by preview, embedded in the Apply ops payload so
the receipt can carry it back. If the backend prefers server-issued tokens
I can adapt, but client-generated survives reload without a recovery
round-trip — that is my preference.

### 5.3 Draft overlay shape (local, sessionStorage)

Key: jw-studio-org-v1:<location.host>:<libraryId> — server + library
scoped; a different deployment never sees the draft. sessionStorage matches
the existing privacy characteristics (browser-session storage) and survives
reload within the tab.

```json
{
  "v": 1,
  "staged": {
    "baseRevision": 42,
    "correlationId": "org-<uuid>",
    "intent": { "…": "as previewed" },
    "plan": { "assignments": [], "groupChanges": [], "groupCreates": [], "displaced": [] },
    "beforeAfter": { "net_…": { "number": [301, 302], "groupId": ["grpA", "grpB"] } },
    "tempRefs": { "temp-9f3ab2c1": { "number": 300 } },
    "packet": { "ops": ["frozen"] },
    "label": "Arrange Performers into 300–699",
    "stagedAt": 1759700000000
  },
  "pending": {
    "requestId": "req-<uuid>",
    "expected": 42,
    "packetDigest": "<serialized ops>",
    "correlationId": "org-<uuid>",
    "beforeAfter": {},
    "submittedAt": 1759700001000
  },
  "undo": ["previous staged snapshots"],
  "redo": []
}
```

Undo/redo stacks are bounded (50 entries), local-only, and snapshot the
organization metadata draft (plan / beforeAfter / label) — never per-channel
content drafts. pending mirrors the ops-pending discipline: after a reload
the store re-polls GetChannelApplyResult with the same requestId BEFORE
allowing a new arrangement Apply (receipt recovery, no duplicate commit).

### 5.4 What the UI sends on Apply arrangement

ApplyChannelChanges task with expectedRevision = staged.baseRevision, ops =
staged.packet.ops plus any explicitly-included channel.put / channel.create
ops, with the SAME requestId discipline as today: identical packet plus
revision reuses the requestId; ANY change means a new requestId AND a fresh
preview.
## 6. Apply coordinator integration

- One coordinator: arrangement Apply goes through the SAME
  applyChannelChanges + pollApplyReceipt + receipt-truth path. I extend
  submitOps rather than fork it: submitArrangement(staged) shares the
  transport/persist/retry core and adds the org pending payload plus
  receipt handling (receipt.idMap reconciles groupCreates and temp creates;
  receipt.revision becomes the new base).
- Serialization: a module-level in-tab submit mutex (promise chain) —
  editor Applies and arrangement Applies never overlap submissions from
  this tab; a second Apply waits for the first RECEIPT, not just the
  submit, so expectedRevision is never guessed. Editing during flight stays
  free; newer edits remain dirty (existing per-channel rule, unchanged).
- Exact retry: packet digest + expectedRevision + requestId persisted
  (5.3 pending); a transport failure keeps all of it; retry resubmits
  byte-identical ops with the same requestId — server receipt replay makes
  it exactly-once.
- Revision conflict: keep staged intact, offer "Reload and replan"
  (refetch library, re-run preview with the same intent, show the NEW
  displacement review before any re-Apply). Never blindly replay the old
  map onto a new revision; the old map stays visible as "your previous
  plan" for comparison.
- Field-level rebase of bystander drafts: after a committed arrangement,
  for every channel in beforeAfter that ALSO has a content draft in
  jw-studio-drafts-v1: refetch its definition, run the EXISTING
  rebaseDraft(base, fresh, draft) — number/groupId land as server-changed
  fields: untouched locally means adopt the acknowledged number/group;
  touched on both sides means keep the local value and surface
  number/groupId as named conflicts in that editor (the existing conflict
  notice line carries it). NEVER channel.put a bystander wholesale from an
  arrangement.
- Undo/redo: pre-Apply only, via the 5.3 stacks (Ctrl+Z / Ctrl+Shift+Z
  while the Organize sheet or staged bar has focus).
- Stage reversal (post-commit): a button on the success toast builds the
  inverse field map from beforeAfter, runs a FRESH preview + review (later
  edits may collide; created channels/groups are explained as
  non-reversible, never auto-deleted), and submits as a NEW Apply with a
  new correlationId. The ordinary History surface stays untouched.

## 7. Accessibility and styling plan

- Dialogs reuse the existing Dialog focus trap (initial focus, Tab cycle,
  focus return to the opener). New sheets get aria-label titles; sections
  are headed landmark groups. Escape closes a sheet but drafts persist —
  the staged arrangement and channel drafts survive and the sheet reopens
  to the same state.
- A visually-hidden aria-live="polite" region announces staged changes:
  "Arrangement staged: 87 channels renumbered, 12 relocated." /
  "Arrangement applied at r43." Toasts already carry role="status".
- Reduced motion: the global prefers-reduced-motion rule already kills
  transitions and animations; new components add no JS-driven motion.
- 200 percent zoom / narrow screens: all new layout in rem/flex with
  flex-wrap; the wide sheet caps at 96vw/96vh; the Review table virtualizes
  vertically and scrolls horizontally as a block (no clipped controls);
  the topbar follows 1.2.
- Themes: only --jw-* and Stash theme tokens (existing convention), no
  literal surface colors; status colors keep text companions. CSS scoped
  under .jw-studio plus explicit dialog roots (.jw-sheet-numres,
  .jw-sheet-organize) because overlays render above the page root.
- 44 px dial geometry is load-bearing (DIAL_ITEM_HEIGHT equals the CSS
  row/header heights): all dial additions (checkbox column, group menu
  button) live INSIDE the 44 px row; no padding changes.
- Readability: raise .jw-dial-number from --jw-faint to --jw-muted and
  12 to 13 px (tabular-nums kept); REMOVE the duplicated number from
  .jw-dial-name ("300 · Name" becomes "Name") — the number cell already
  carries it; stored names and accessible names stay untouched (the row
  aria-label keeps "300 · Name").
- Disclosure: long rules/programming sections gain a disclosure with a
  one-line summary ("3 rules · excludes 2 tags") and an error badge on the
  disclosure header when a hidden field carries an error — authored
  exclusions and error-bearing controls are never silently collapsed.

## 8. Optional custom snippets (extras/)

Deliver extras/channel-studio-custom.css (presentational overrides against
the documented .jw-* selectors) and extras/channel-studio-custom.js
(demonstrating the hook), each with copy/paste/removal instructions
targeting Stash Settings → Interface → Custom CSS / Custom JavaScript
(labels verified on DEV), preserving any existing owner snippets.

Hook proposal (versioned, feature-detected, route-scoped):

```js
window.JWChannelStudio = {
  version: 1,
  // register once per name; re-registration runs the previous cleanup
  // first (idempotent re-execution after Stash re-injects snippets).
  registerExtension(name, { version, setup }) {}, // setup(ctx) -> cleanup
};
// ctx = { route: "/plugins/stash-justwatch", on(event, cb),
//         getViewState(), announce(text) }
// events: "staged" | "applied" | "selection" — view notifications ONLY;
// there is NO mutation API.
```

Rules: snippets never trigger Apply (no mutation surface exists in ctx),
never touch React internals or PluginApi, cleanup runs on unregister and on
route leave, and baseline functionality works with snippets absent or
removed. Feature detection is window.JWChannelStudio && version >= 1.

## 9. Component inventory and diff shape

New in ui/index.js (single-owner edit, phase 3):

| Component / unit | Responsibility | Approx. location |
| --- | --- | --- |
| useCapabilities boot fetch | Capabilities handshake + feature gates | transport section (~line 300) |
| makeOrgStore | 5.3 staged/pending/undo persistence | beside makeDraftStore (~line 900) |
| NumberResolutionSheet | section 3 choice matrix + per-choice plans | replaces SwapDialog slot (~1760) |
| OrganizeSheet | section 4 five-section organizer | after GroupsManagerDialog (~1700) |
| OrganizeReviewTable | virtualized Review table (WindowedList) | inside the OrganizeSheet block |
| ArrangementBar | scoped sticky staged bar + Apply arrangement | App render, beside the bulk bar |
| GroupHeaderMenu | Select group / Arrange numbers… / Edit group… | dial header render (~3870) |
| submitArrangement + submit mutex | section 6 coordinator integration | beside submitOps (~3550) |
| announce live region | screen-reader announcements | App root |

Modified: topbar (1.1/1.2), bulk bar → scope-aware selection bar (section
2), NewChannelDialog (occupied number → sheet; Create group…), EditorPane
identity card (Move / insert…, Resolve conflict…, group select), DialRow
(checkbox column, deduped number, aria), moveDialog (gains "Arrange numbers
after the move…" hand-off), SHORTCUT_ROWS (Shift-click, Ctrl+Z rows).

Deleted: SwapDialog, the doSwap immediate-transaction path, the two swap
hint links, and the stale swap_pairs comment at ~2288. The channel.swap
OPCODE stays untouched server-side (contract compat); the UI simply stops
using it.

Diff estimate: ui/index.js about +1500 / −260 (net +1240 → roughly 5340
lines); ui/styles.css about +460 / −25 (sheets, review table, selection
bar, number contrast, disclosure styles). New files:
extras/channel-studio-custom.css, extras/channel-studio-custom.js. No other
files touched by frontend.

## Open negotiating points (for the contract freeze)

1. ONE preview op with intent variants (mine) vs per-intent ops — I hold
   one op.
2. Server-emitted frozen packet in the preview response (mine) vs the UI
   compiles ops from plan — I hold server-emitted; the UI only appends
   explicitly-included channel.put / channel.create.
3. Client-generated correlationId (mine — survives reload) vs server-issued
   tokens.
4. Group creates inside packet.ops pre-ordered by the backend compiler
   (group.put → channels.move → channels.renumber) vs UI ordering — I want
   backend-ordered.
5. explain: true all-choices preview (one round-trip per sheet open) vs
   per-choice previews — I want the combined form, per-choice re-preview
   only for typed targets.
6. Storage scope key location.host + libraryId on sessionStorage — flag if
   the backend's libraryId is not stable across migrations.

## Top design risks

1. Dual selection semantics in one dial row (editor focus vs bulk
   checkboxes + shift ranges) — mitigated by a bulk-mode-only checkbox
   column, the explicit scope line, and ID-keyed math; still the most
   confusable surface.
2. Client/server plan drift: choice availability is computed from local
   occupancy while consequences are server-planned; a concurrent commit can
   make an enabled choice preview as illegal — handled by re-render on
   preview errors plus Reload-and-replan, never by client re-planning.
3. Virtualization plus the staged overlay: overlay renumbers re-sort the
   dial; selection/reveal must track ID and the WindowedList resetKey must
   NOT reset on stage (scroll jumps); no geometry changes inside 44 px
   rows.
4. Packet scope creep: opting a dirty channel into an arrangement packet
   risks a full-record channel.put stomping concurrent edits — restricted
   to channels the arrangement already touches, with a per-row opt-in and
   the field-level rebase for everyone else.
5. Pending-record collision: the existing single-slot
   jw-studio-ops-pending-v1 record vs the new org pending — two writers
   must not clobber each other's recovery record; org pending moves into
   its own key (jw-studio-org-v1) and submitOps learns to recover rather
   than overwrite a foreign pending entry.
