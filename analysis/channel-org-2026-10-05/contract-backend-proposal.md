# Channel organization — backend contract proposal

Prepared 2026-10-05 at HEAD `1e96af4` by the backend planner (Phase 0 +
backend half of Phase 1). Pure proposal — **no code or tests were modified**.
The orchestrator reconciles this with the frontend design proposal before the
contract freezes. Everything here is contract-v1 additive: one new sync
operation, one new opcode inside the existing Apply/Validate packet, one new
`features` object, and a new pure-Python planner module. No storage-schema
change of any kind (group records keep rejecting unknown fields — no
`numberRange`; the library document shape is untouched).

---

## 1. Verified baseline (at `1e96af4`, working tree = master + 5 untracked docs)

### 1.1 Test suite

```
python3 -m pytest tests/ -q
→ 382 passed in 45.00s   (exit 0)
```

Matches the orchestrator's recorded 382-passed baseline (wall time differs by
machine load only).

### 1.2 The exact current op whitelist

`justwatch/contract.py`:

- `OPERATIONS` (contract.py:82-108) — the client-visible operation names; the
  editing surface adds `GetChannelLibrary`, `GetChannelDirectory`,
  `GetChannelDefinition`, `ValidateChannelChanges`, `PreviewChannelPool`,
  `ApplyChannelChanges`, `GetChannelApplyResult`, `GetChannelHistory`,
  `GetChannelRefreshStatus`, `RequeueChannelRefresh`.
- `SYNC_OPERATIONS` (contract.py:113-119) — everything above except the three
  task-only writes (`saveCatalog`, `refreshData`, `applyChannelChanges`).
- Opcode whitelist lives in `justwatch/library.py:392-393`:
  ```python
  _OK_OPS = ("channel.put", "channel.create", "channel.swap", "group.put",
             "group.delete", "channels.move", "channels.patch")
  ```

`justwatch/main.py`: `SYNC_MODES` (main.py:46-53) and `TASK_MODES`
(main.py:54-56, contains `apply_channel_changes`); handler table `_HANDLERS`
(main.py:1019-1045, e.g. `"validate_channel_changes":
channel_ops.op_validate_channel_changes` at 1038, `"apply_channel_changes":
channel_ops.op_apply_channel_changes` at 1040); `_dispatch` (main.py:1048)
normalizes camelCase→snake_case and rejects modes outside `ALL_MODES`.

### 1.3 Current Capabilities payload (features flags)

`contract.capabilities()` (contract.py:122-198). `features` keys today:

| key | shape | line |
| --- | --- | --- |
| `publishedSchedule` | `{version:1, pageSize:50, horizonHours:72}` | 130 |
| `programming` | `{version:2, networks:true, horizonHours:168, protectedHours:24, freshnessSharePercent:15, statusOperation}` | 137-144 |
| `customChannels` | `true` | 145 |
| `networks` | `{version:1, minNumber:100}` | 148 |
| `channelLibrary` | `{version:1, directoryOperation, libraryOperation, definitionOperation, historyOperation}` | 152-158 |
| `channelGroups` | `{version:1, singleMembership:true, legacySectionFallback:true}` | 159-163 |
| `explicitApply` | `{version:1, applyOperation, validateOperation, resultOperation}` | 164-169 |
| `refreshStatus` | `{version:1, operation, requeueOperation}` | 173-177 |
| `poolPreview` | `{version:1, operation}` | 178 |
| `globalSettings` / `draftPreview` / `healthSnapshots` | `true` | 181-183 |
| `rotation` | `{size:50, scanLimit:1000}` | 184 |
| `channelNumbers` | `[1, 99]` | 185 |
| `sorts` / `glyphs` | lists | 186-187 |

`limits`: `{maxChannels:99, lineupPerPage:50, lineupPerPageMax:100,
libraryChannels:899}` (contract.py:189-197). A new feature object follows the
`poolPreview`/`refreshStatus` style exactly (§3d).

### 1.4 Where Apply/Validate validate ops

- **ValidateChannelChanges** → `channel_ops.op_validate_channel_changes`
  (channel_ops.py:184-197): `library._check_ops(doc, ops)` (shape/reference/
  band/uniqueness-vs-original) + `_reference_errors` (Stash-side entity
  checks, CHANGED sources only, cap 60 — channel_ops.py:203, 206-289) +
  `_effect_summary` (per-op membership-vs-metadata classification,
  channel_ops.py:292-332). Sync, never writes.
- **ApplyChannelChanges** → `channel_ops.op_apply_channel_changes`
  (channel_ops.py:403-443) → `library.apply_transaction`
  (library.py:396-485): digest over `{"expected", "ops"}` (433) → lock
  (`library_lock`, flock/msvcrt, library.py:354-385) → replay by
  `requestId`+digest (436-446) → `_check_ops` (447) → `revision_conflict`
  check (455-463) → `copy.deepcopy` candidate (464) → `_apply_ops` staging
  (466) → `_enforce_identity` (467) → `pre_commit` = `refresh.commit_hook`
  intent journaling (468-469, hook at refresh.py:96-106) → revision +1 →
  receipt persisted WITH the document by `_finish` (494-504). Rejections
  return receipts carrying only `{"error": code}` (from `_OpError`,
  library.py:488-492) or `{"error": "validation_failed", "errors": [...]}`.

### 1.5 How receipts are keyed

Receipts = the library document's `recentRequests` list (newest-first,
`RECEIPTS_KEPT = 200`, library.py:55). Key = client `requestId`; payload
identity = `digest` (sha256-16 of `{"expected": revision, "ops": ops}`).
Same requestId + same digest → replay stored receipt verbatim, idempotent
even after a lost response (library.py:436-446; test at
tests/test_channel_library.py:104-113). Same requestId + different digest →
`request_replayed_with_different_content`. REJECTED receipts carry the same
digest, so identical retries replay the rejection. `idMap` maps client
`tempId` → server-assigned id for creations (library.py:774).
`GetChannelApplyResult` = `library.get_receipt` (library.py:898-904);
`status: "unknown"` when absent (channel_ops.py:454-464).

### 1.6 Cosmetic classification today (number/group changes vs refresh/rotation)

**Refresh intent journaling** — `refresh.commit_hook` (refresh.py:96-106)
runs inside the transaction lock as the `pre_commit` hook and calls
`refresh.affected_channels` (refresh.py:109-141). `pool_changed`
(refresh.py:128-137) compares ONLY:

- source signature (`criteria.source_signature`),
- `sort`, `seed` (int-compared),
- normalized programming policy (`library.normalize_programming` —
  library.py:843-866, engine-aware so a continuing `newShare` survives),
- `archived` / `paused` / `enabled` booleans (creation counts as
  before_channel is None).

`number`, `name`, `color`, `glyph`, `groupId` are **absent from every
comparison** → a renumber/regroup can never enqueue refresh work. The
module docstring states it outright (refresh.py:32-34). The
`_effect_summary` used by Validate mirrors the same predicate
(channel_ops.py:306-315) and classifies `channel.swap`/`channels.move`/
`channels.patch`/`group.put`/`group.delete` as `metadata, reindex: False`
(channel_ops.py:324-331) — but `channels.renumber` does not exist yet and
needs its own branch (§3e).

**rotationVersion inputs** — `lineup.rotation_version`
(lineup.py:222-242) hashes `[*lineup.source_key(source), sort, seed, size]`
plus the dynamic epoch (`effective_epoch`, lineup.py:161-180, only for
createdAt/scene-count sources). `source_key` (lineup.py:84-103) keys the full
canonical rule set. **Number, name, group, and the library revision are not
inputs.** On a library deployment the Lineup op serves the channel's OWN
hash unprefixed (main.py:339-348; the audit-C12 comment at 343-346 says a
rename/group/color Apply must not churn lineups); only the LEGACY catalog and
compiled-network paths prefix revisions (`r{rev}-` main.py:365,
`n{rev}-` main.py:380).

**Schedule/publication/ledger safety** — publications are per-channel-id
(`programming/<id>.json`); the stale guard `_publication_stale`
(refresh.py:386-405) compares source/sort/seed/policy/playable state only;
the continuing `configuration` digest (refresh.py:356-359) is
`[source, sort, seed, policy]`. Renumbering touches none of them.
Deliberate contrast: `presentation_signature` (channel_service.py:286-295)
DOES include number/name/group — it is the cosmetic-identity signal enhanced
clients use to refresh the directory WITHOUT resetting playback, so it is
correct (and required) that an arrangement changes it.

### 1.7 Where swap_pairs flows today

`_check_ops` builds `swap_pairs` as a frozenset of `frozenset((a, b))` from
every `channel.swap` op in the packet (library.py:524-527) and passes it into
both `_check_channel_draft` calls — `channel.put` (library.py:546-547) and
`channel.create` (library.py:556-557). Inside `_check_channel_draft`
(library.py:654-715) the occupancy check (669-678) looks up the CURRENT
holder of the draft number in the ORIGINAL library (`by_id`) and emits
`duplicate_number` ("…; use Swap") unless the pair is an exempted swap pair
(675-676). Consequences (the planning finding, verified):

- Only exact 2-swaps are exempt. 3-cycles, insertions, block moves, and
  create-into-vacated are inexpressible: e.g. `channel.create` at an occupied
  number always fails because the create's `channel.get("id")` is `None`, so
  the exemption `frozenset((None, holder))` can never match a real swap pair.
- `_apply_ops` independently re-checks band + uniqueness on the FINAL
  candidate after staging (library.py:802-811), raising `_OpError`s
  `bad_number` / `duplicate_number`, then sorts the store by number (812).
  This final scan is already candidate-based — the per-op draft check is the
  only original-library-coupled piece.
- The stale UI comment near `EditorPane.buildOps` (ui/index.js ~line 2290)
  claiming `swap_pairs` is out of scope is confirmed stale: the backend
  builds and passes it explicitly at library.py:524-547.

### 1.8 Test harness idioms in use

- tests/test_channel_library.py — `base_library()` (19-42) document builder;
  `data_dir` fixture saving it (45-48); direct `library.apply_transaction`
  calls asserting on receipts (92-229); two-process lock race via
  subprocess pool (207-229); `duplicate_number` typed-error regression
  (241-249); create → `idMap` (146-156).
- tests/test_channel_ops.py — `op_library()` builder (136-155), `Ctx` fake
  context (158-164), `FakeClient` scripted Stash (119-133); cosmetic-apply
  asserts `receipt["refresh"] == {}` and `refresh.read_pending() == []`
  (193-210); lost-response receipt recovery (224-236).

---

## 2. Design summary (what changes, one paragraph)

A new pure planner module `justwatch/organization.py` (Phase 2) computes
deterministic number/group plans from a complete occupancy model + one
intent, with zero I/O and zero Stash queries. A new sync op
`PreviewChannelArrangement` (handler `preview_channel_arrangement`) wraps it:
load, revision check, plan, echo. A new narrow opcode `channels.renumber`
inside the existing Apply/Validate packet commits the plan. Occupancy
validation moves from "per-op vs original library (+swap-pair exemptions)" to
"packet-aware final-number plan shared by Validate and Apply, plus the
existing final-candidate scan under the lock". Cosmetic classification needs
no behavioral change — §1.6 shows number/group already sit outside every
membership predicate — only an explicit `_effect_summary` branch and
regression tests.

---

## 3. Exact proposed contract additions

### 3a. `PreviewChannelArrangement` (new sync operation)

Registration (Phase 2 touchpoints, all additive):

- contract.py `OPERATIONS` += `"previewChannelArrangement":
  "PreviewChannelArrangement"`; `SYNC_OPERATIONS` += the same token.
- main.py `SYNC_MODES` += `"preview_channel_arrangement"`; `_HANDLERS` +=
  `"preview_channel_arrangement": channel_ops.op_preview_channel_arrangement`.
- handler in channel_ops.py: `library.exists` guard (same `_library` error as
  the other library ops), load, `expectedRevision` check, call planner,
  wrap. **No writes, no receipts, no journal, no history, no id/seed
  allocation.** Bounded by construction (≤899 rows, pure arithmetic).

#### Request

```jsonc
{
  "expectedRevision": 12,          // int; library revision the client reviewed
  "correlationToken": "org-8f3a…", // client-opaque string 1-128 chars, echoed verbatim
  "intent": { … },                 // exactly one intent object; "type" discriminates
  "channels": [                    // OPTIONAL draft overlays — temp creations ONLY
    {
      "tempRef": "temp-1",         // required; 1-64 chars; must NOT match ^(ch|net)_[0-9a-f]{8}$; unique in request
      "kind": "net",               // required; band is fixed by kind even for uncommitted rows
      "name": "Hentai Gold",       // optional; ordering/display only (1-60 chars if present)
      "groupId": "grp_…",          // optional; must be an EXISTING group id
      "sort": "shuffle"            // optional; informational only
    }
  ]
}
```

Overlay policy (deliberately narrow):

- Overlays exist so a pending create/duplicate can be placed without
  committing it. They never receive server ids/seeds in the preview.
- Existing channels are planned from the COMMITTED document, never from
  client-side draft state — the server cannot trust a stale browser copy.
  An existing channel whose draft carries an uncommitted group change: the
  preview reports `groupChanges` against committed groups; the UI reconciles
  with its own draft. Unrelated pending drafts neither occupy nor release
  numbers (plan §4) — preview simply does not see them.
- If a tempRef collides with a real id pattern or duplicates another tempRef:
  `bad_temp_ref` / `duplicate_temp_ref`.

#### Response

```jsonc
{
  "pluginId": "stash-justwatch",
  "contractVersion": 1,
  "revision": 12,                        // library revision the plan is based on
  "libraryId": "lib_…",
  "correlationToken": "org-8f3a…",       // echoed verbatim
  "noop": false,                         // true when the plan changes nothing
  "numberChanges": [
    {
      "channelId": "net_abc12345",       // null for temp rows
      "tempRef": null,                   // tempRef for overlay rows
      "from": 300, "to": 301,
      "selected": true,                  // in the intent's selection (vs displaced bystander)
      "reason": "insert-shift-up"        // see reason vocabulary below
    }
  ],
  "groupChanges": [
    { "channelId": "ch_def67890", "tempRef": null,
      "from": "grp_general", "to": "grp_performers" }
  ],
  "created": [                           // planner-assigned placements for overlays
    { "tempRef": "temp-1", "number": 300, "groupId": "grp_performers" }
  ],
  "displaced": [],                       // the subset of numberChanges with selected=false
  "capacity": { … },                     // see below
  "suggestions": { … },                  // populated for free_number intent; else null
  "warnings": [ { "code": "…", "message": "…" } ],
  "errors":  [ { "path": "intent.range", "code": "…", "message": "…" } ],
  "valid": false,
  "applyPacketHint": null                // submit-ready ops when valid && !noop (§3a.4)
}
```

`capacity`:

```jsonc
{
  "kind": "net", "bandStart": 100, "bandEnd": 899,
  "totalSlots": 800, "occupiedSlots": 513, "freeSlots": 287,
  "neededSlots": 180, "shortfall": 0,
  "range": { "start": 300, "end": 699, "totalSlots": 400,
             "occupiedSlots": 222, "freeSlots": 178, "outsiderSlots": 42 },
  "outside": null          // mirror block for the exclusive strategy's outside interval
}
```

`reason` vocabulary on numberChanges: `"direct"` (uncontested move),
`"insert-shift-up"`, `"insert-shift-down"`, `"block-displaced"` (bystander
pushed by a block), `"range-pack"`, `"exclusive-outside-relocation"`,
`"shift-interval"`, `"relocate"`, `"swap"`.

`warnings` codes: `archived_rows_moved`, `paused_rows_moved`,
`disabled_rows_moved` (they occupy and can be moved, but flag it),
`presentation_will_change` (arrangements change `presentation_signature` by
design — clients refresh the directory without playback resets),
`health_numbers_stale` (published health rows keep last-computed numbers
until the next health pass; content health is unaffected).

**Stale protection:** if `expectedRevision != library.revision` the response
is `valid: false` with a single error `{path: "expectedRevision", code:
"stale_revision", message: …}` and `currentRevision` on the error object
(mirroring `revision_conflict`'s shape). Combined with the echoed
`correlationToken`, a slow response can never replace a newer review: the UI
discards any response whose (revision, token) pair doesn't match its current
draft state. A different revision requires a new preview and a new requestId
at Apply time (plan §4).

#### Intent variants

Every intent lists its selection as `channelIds` (existing ids) and/or
`tempRefs` (overlay rows). An empty total selection → `empty_selection`.
Selection determinism (§3a.2).

**1. `assign_group`** — regroup without renumbering.

```jsonc
{ "type": "assign_group",
  "channelIds": ["ch_…", …], "tempRefs": ["temp-1", …],
  "groupId": "grp_performers",                       // XOR createGroup
  "createGroup": { "name": "Performers", "position": 7 } }  // position optional → appended
```

Mixed namespaces are FINE here (groups are cross-namespace). Errors:
`unknown_channel`, `unknown_temp_ref`, `unknown_group`, `duplicate_group`
(casefolded name clash), `bad_name`. Number changes: none.

**2. `arrange_range`** — pack a selection into an inclusive range
(the "Performers into 300–699" flow).

```jsonc
{ "type": "arrange_range",
  "channelIds": […], "tempRefs": […],
  "range": { "start": 300, "end": 699 },   // inclusive; must sit inside ONE band
  "order": "number",                        // "number" (default) | "name"
  "strategy": "useAvailable",               // default | "exclusive"
  "outside": { "start": 100, "end": 299 }   // REQUIRED iff exclusive; same band; disjoint from range
}
```

- `useAvailable`: free positions = range slots − outsiders (every channel
  numbered in the range that is NOT selected — archived/paused/disabled
  included). Selection rows already inside the range are removed from
  occupancy first, then packed into free positions in deterministic order.
  Outsiders never move.
- `exclusive`: additionally relocate EVERY outsider in the whole range
  (including beyond the packed group's last slot) into free positions of
  `outside`, ascending by their current number; then pack. One-time
  arrangement — no reservation is recorded anywhere (storage untouched).
  Outside destinations can never land inside the range (disjointness
  validated: overlap → `range_overlap`).
- Errors: `bad_range` (start>end, not ints/booleans, outside a band),
  `cross_band` (range spans the 99/100 boundary), `band_mixed` (selection
  mixes kinds, or selection kind ≠ range's band — the error message lists
  the offending ids so the UI can offer an explicit per-band split; NEVER a
  silent omission), `no_capacity` (free-in-range < selection count, or
  exclusive outside-interval capacity < outsider count; `capacity.shortfall`
  carries the exact number), `range_overlap` (disjointness).

**3. `move_block`** — an explicitly ordered block to a start number.

```jsonc
{ "type": "move_block",
  "channelIds": ["net_171…", "net_173…", "net_180…"],  // this order IS the block order
  "start": 300 }
```

- Selected rows placed consecutively at `start … start+n-1` in the given
  order; single band required (`band_mixed` otherwise); block must fit the
  band (`no_capacity`/`cross_band` at the end).
- Displaced bystanders (any unselected channel inside the block span, then
  cascade) keep their ORIGINAL relative order (ascending current number) and
  push upward through available positions until all fit; default scope is the
  band's remaining interval above the block. Cascade exceeding band end →
  `no_capacity`.
- Selected rows vacate their old slots first, so the cascade may flow into
  them. This is distinct from skipping blockers — every displaced row is
  returned for review.

**4. `insert`** — one record into an occupied or free number.

```jsonc
{ "type": "insert",
  "channelId": "ch_…",       // XOR tempRef
  "number": 300,
  "direction": "up" }         // "up" (default) | "down"
```

- Target free (after removing the mover's own slot from occupancy): direct
  move, zero displacement, `reason: "direct"`.
- Target occupied, `up`: F = first free number ≥ target within the band;
  records in `target … F-1` shift +1; mover lands on target. No free number
  above → `no_capacity` ("No free number above 899.").
- `down`: mirror toward the first free number ≤ target.
- Exactly one mover. A tempRef insert is the create/duplicate conflict-sheet
  path; the planner returns the mover's placement in `created`, shifts in
  `numberChanges`. Swap is structurally unavailable for temp rows (no
  original slot) — a swap intent naming a tempRef is `bad_temp_ref`.

**5. `shift_interval`** — move every record currently in a range by an offset.

```jsonc
{ "type": "shift_interval",
  "range": { "start": 200, "end": 250 },   // selects records CURRENTLY in those slots
  "offset": 10 }                            // nonzero int
```

- Moving set = all channels (any state) numbered in the range; each maps
  `current → current + offset`; relative gaps preserved. The response's
  `numberChanges` IS the resolved id list for the pre-stage display
  (plan §4: intervals select records, not numeric offsets).
- Any destination held by a NON-moving record → `range_overlap` listing the
  conflicting (number, holder) pairs — no automatic relocation; the owner
  picks another action or an explicit reviewed plan (plan §4).
- Destination out of band → `cross_band`. Empty moving set → `noop` response.

**6. `relocate_occupant`** — the conflict-sheet's explicit relocation.

```jsonc
{ "type": "relocate_occupant", "channelId": "net_…", "to": 250 }
```

Occupied destination (by a row the intent doesn't move) →
`destination_occupied` with the holder's id/name in the message. `to` ==
current → `noop`.

**7. `free_number`** — suggestions for the "Use a free number" choice.

```jsonc
{ "type": "free_number", "kind": "ch", "near": 42, "count": 5 }
```

No changes planned. Response `suggestions`:

```jsonc
{ "nextHigher": 43,     // first free ≥ near in the band (null when none)
  "nearest": 41,        // min |n − near| over free numbers
  "firstFree": 1,       // lowest free in the band
  "list": [43, 44, 45, 41, 40] }   // up to count, deterministic scan outward from near
```

Occupancy includes committed channels AND numbers claimed by overlays.

**8. `swap`** — preview of the existing exchange semantics.

```jsonc
{ "type": "swap", "a": "ch_…", "b": "ch_…" }
```

Existing ids only, same band (`unknown_channel`, `bad_swap` self/cross-band,
reusing the opcode's codes). Two `numberChanges` with `reason: "swap"`.

#### Deterministic ordering and tie-breakers

- Default selection order = ascending CURRENT number.
- `order: "name"` = casefold(name) ascending, tie-break token ascending;
  existing rows' token = channel id, overlay rows' token = `"t:" + tempRef`.
  The `"t:"` prefix cannot collide with ids (ids must match
  `^(ch|net)_[0-9a-f]{8}$`; `bad_temp_ref` enforces the negation for
  overlays). Alphabetical is always opt-in.
- Temp rows are totally ordered among themselves by tempRef (stable within a
  session's draft), and after same-named existing rows via the token rule.
- Outsider relocation order (exclusive) = ascending current number.
- Block cascade order = ascending original number of the displaced rows.
- Same input document + same intent ⇒ byte-identical plan, forever (no
  clocks, no sets iterated unordered, no dict-order dependence — planner
  sorts everything explicitly).

#### Typed error codes (planner + opcode)

Same `{path, code, message}` style as `library._check_ops`. Full catalog:

| code | meaning |
| --- | --- |
| `stale_revision` | expectedRevision ≠ current revision (carries `currentRevision`) |
| `bad_intent` | unknown intent `type` / malformed intent object |
| `empty_selection` | no channelIds and no tempRefs where some are required |
| `unknown_channel` | id not in the committed library |
| `unknown_temp_ref` / `bad_temp_ref` / `duplicate_temp_ref` | overlay ref problems |
| `bad_range` | malformed/inverted/out-of-universe range or outside interval |
| `range_overlap` | destination interval overlaps untouched occupants (shift_interval; exclusive outside∩range) |
| `cross_band` | planned move exits the record's kind band; range spans 99/100 |
| `band_mixed` | selection mixes namespaces where a single band is implied; lists offending ids |
| `no_capacity` | shortfall (carries counts via `capacity`); insert with no free run end |
| `destination_occupied` | single assignment lands on an untouched record (carries holder) |
| `duplicate_destination` | two assignments land on the same final number |
| `ambiguous_assignment` | same id assigned twice / put vs renumber disagree / swap∩renumber overlap |
| `bad_number` | not a real int (booleans rejected), or out-of-band on put-shaped drafts |
| `empty_assignment` | renumber opcode with an empty assignments list |
| `bad_assignment` | renumber entry not an object / unknown keys / missing keys |
| `unknown_group` / `duplicate_group` / `bad_name` | group intent problems |

`no-op` plans are NOT errors: `valid: true, noop: true`, empty change lists
— the UI must not submit a useless revision increment (plan §4).

#### `applyPacketHint` (valid && !noop)

A compilation aid the UI may submit nearly verbatim:

```jsonc
{
  "expectedRevision": 12,
  "ops": [
    { "op": "group.put", "group": { "name": "Performers", "position": 7 } },
    { "op": "channel.create", "tempId": "temp-1",
      "channel": { "kind": "net", "number": 300, "name": "Hentai Gold",
                   "groupId": "grp_performers" } },   // SKELETON — see below
    { "op": "channels.renumber",
      "assignments": [ { "channelId": "net_abc12345", "number": 301 }, … ] },
    { "op": "channels.move", "channelIds": […], "groupId": "grp_performers" }
  ]
}
```

`channel.create` entries are SKELETONS: the preview never sees the pending
draft's source/color/glyph, so the UI must merge its full pending-create
draft into the channel object before submit (the planner never fabricates
sources). Every other op in the hint is submit-ready. The UI remains free to
compile its own packet (e.g. folding agreeing put-number edits); the hint
exists so simple flows cannot drift from the reviewed plan.

### 3b. `channels.renumber` opcode (inside ApplyChannelChanges / ValidateChannelChanges)

```jsonc
{ "op": "channels.renumber",
  "assignments": [
    { "channelId": "net_11111111", "number": 300 },
    { "channelId": "net_22222222", "number": 301 }
  ] }
```

Validation rules, in check order (each typed, path `ops[i].assignments[j].…`):

1. `assignments` present, a list, nonempty → else `empty_assignment`.
2. Each entry a JSON object with keys EXACTLY `{"channelId", "number"}` →
   `bad_assignment` (missing or unknown keys; unknown keys rejected like
   storage-shape validation, never ignored).
3. `channelId` a string naming an EXISTING library channel → `unknown_channel`.
   **Creations never appear here by design** — a creation's final number
   lives in its `channel.create` (§3c); the renumber map is total over
   existing ids only.
4. `number` a real int — booleans rejected explicitly (`isinstance(v, bool)`
   guard, mirroring library.py:254) → `bad_number`.
5. Band check against the channel's OWN kind (`BANDS`, library.py:68:
   ch 1-99, net 100-899) → `cross_band`. Kind is never inferred from the
   number.
6. No `channelId` twice → `ambiguous_assignment`.
7. No two assignments share a final number → `duplicate_destination`.
8. A channel in the renumber map must not also appear in any `channel.swap`
   in the same packet → `ambiguous_assignment` (the plan's "new UI packets
   … avoid mixing sequential swaps with explicit number maps" becomes an
   enforced invariant; standalone `channel.swap` stays fully compatible).
9. Packet-aware put composition (§3c).
10. **Final-candidate occupancy**: computed by the shared final-number plan
    (§3b.1) — a final number held by a record the packet never touches →
    `destination_occupied` (holder named in the message).

Cycles and swaps through renumber are LEGAL: the map is a simultaneous
assignment on the staged candidate (`1→2, 2→3, 3→1` passes; a 2-cycle is
just a swap without the clean-draft prerequisite — acceptance row "299 swaps
300 while its name is dirty" works by pairing `channel.put` (name, number
agreeing) + `channels.renumber`). Create-at-vacated-number is legal when the
vacating op is in the same packet (§3c). Real final duplicates still reject:
`duplicate_destination` from the check pass, and the surviving
`_apply_ops` under-lock scan (library.py:802-811) raising `duplicate_number`
as the unreachable-except-by-bug belt-and-braces.

#### 3b.1 The validation restructure (occupancy moves to the final candidate)

Functions touched (all in `justwatch/library.py` unless noted); Validate and
Apply stay in exact agreement because both interpret ops through ONE shared
helper:

- **New `_final_numbers(library, ops) -> (final_map, errors)`** — the single
  interpreter of "what number does every channel end at if this packet
  commits". Pure arithmetic over the original document: start from current
  numbers; apply `channel.put` numbers (drafts must carry numbers today —
  `_check_channel_draft` rejects a missing number, library.py:669-672);
  apply `channel.create` numbers keyed by tempId; apply `channel.swap` as
  pairwise exchanges; apply `channels.renumber` last (it agrees with or
  supersedes nothing — rule 8/9 forbid ambiguity). Also returns
  band-check errors per moved record. NO mutation, NO candidate copy.
- **`_check_ops` (library.py:511-641)** keeps every existing per-op
  shape/reference/mode check, plus the new renumber rules 1-9. Its
  occupancy story changes:
  - `swap_pairs` (524-527) is retired as an occupancy exemption. In its
    place `_check_ops` calls `_final_numbers` and enforces final uniqueness
    over the touched set (`duplicate_destination`) and against untouched
    holders (`destination_occupied`).
  - `_check_channel_draft` (654-715) loses the original-library occupancy
    branch (669-678) as a hard error and gains a narrow packet-aware one:
    emit `duplicate_number` (same code, same "…; use Swap"-style message
    shape, so existing frontend error handling survives) only when the
    holder is NOT reassigned anywhere in this packet. Signature changes from
    `swap_pairs=frozenset()` to `renumber_map`/`final_map` context. Creates
    (whose id is None) benefit automatically: a create at a number the
    packet itself vacates no longer errors.
  - Order preserved: `_check_ops` still runs BEFORE the revision check
    (library.py:447 vs 455), so drafts get precise field errors first;
    ValidateChannelChanges inherits the whole final-occupancy pass for free
    because it calls `_check_ops` (channel_ops.py:189).
- **`_apply_ops` (library.py:718-813)** — unchanged composition order
  (group.put first, 727-744; then in-list order), plus one new staging arm:
  `elif kind == "channels.renumber": for a in op["assignments"]:
  by_id[a["channelId"]]["number"] = a["number"]`. The existing final scan
  (802-811) REMAINS the authoritative under-lock occupancy check on the
  staged candidate — it now agrees by construction with `_check_ops`
  because both consume `_final_numbers` semantics; it stays as the race/
  bug backstop raising `duplicate_number`/`bad_number`.
- **`_enforce_identity` (816-833)** — untouched. Renumber carries no
  identity fields and the final candidate check still guarantees
  id/seed/kind/provenance stability through every path.
- **`_OK_OPS` (392-393)** += `"channels.renumber"`.

What does NOT change: `.library.lock`, revision recheck, one revision
increment, atomic receipt/document persistence, digest replay discipline,
rejected-receipt behavior, strict loading, library-absent legacy fallback,
bounded reference checks (a renumber-only packet touches no sources, so
`_reference_errors` performs zero Stash lookups — organization-only changes
never make scene/entity queries, per plan §5).

### 3c. Composition with the other opcodes (one unambiguous final number per channel)

| opcode | number contribution |
| --- | --- |
| `channels.renumber` | final number for each listed EXISTING id |
| `channel.create` | its own `channel.number` (required, band-checked by `_check_channel_draft`) |
| `channel.put` | its `channel.number` — must AGREE with the renumber map when both present; disagreement → `ambiguous_assignment` (agreement = redundant-but-consistent, accepted; the submitted ops are never silently rewritten — receipts digest raw ops) |
| `channel.swap` | its exchange — mutually exclusive with renumber on the same ids (`ambiguous_assignment`) |
| `group.put` / `group.delete` / `channels.move` / `channels.patch` | none |

The invariant is: at most one final-number authority per channel per packet,
so the final number map is a total function and Validate/Apply can never
diverge on interpretation. Compile order inside `_apply_ops` is unchanged
(group-create before group-move — create-group + assign + renumber is one
atomic Apply; plan §5 "Compile group-create before group-move"). A rejected
packet leaves definitions and revision byte-identical (existing behavior,
tests/test_channel_library.py transaction semantics).

### 3d. Capabilities addition (contract v1 additive)

```jsonc
"features": {
  // … everything in §1.3 unchanged …
  "arrangement": {
    "version": 1,
    "previewOperation": "PreviewChannelArrangement",
    "renumberOpcode": "channels.renumber"
  }
}
```

Style matches `poolPreview`/`refreshStatus`. Additive: old clients never
read it; new UI on an old plugin reads `features.arrangement` — absent →
advanced actions explain themselves and basic editing (put/swap/move/patch)
remains fully supported (plan §5: never assume `channelLibrary` alone
implies the opcode). `operations`/`SYNC_OPERATIONS` gain the new op;
`contractVersion` stays 1. Limits unchanged (`libraryChannels: 899` already
describes the full number space; the strict count cap at library.py:179-180
equals band capacity 99+800, so planner capacity math subsumes it).

### 3e. Cosmetic-classification guarantee (where number/group-only changes are decided)

A `channels.renumber`-only Apply must produce: no refresh intent journal
entry, no rotationVersion churn, no schedule/publication/ledger change. This
falls out of existing code with NO behavior change needed — the renumber
staging arm mutates only `number`, which is absent from every predicate
cited in §1.6:

- **Journal:** `refresh.affected_channels` (refresh.py:109-141) — `number`
  not compared → `pool_changed` False → `commit_hook` enqueues nothing →
  the Apply's inline `process_pending` (channel_ops.py:429-437) drains an
  empty journal → `receipt["refresh"] == {}`, `read_pending() == []`
  (the exact assertion pattern of tests/test_channel_ops.py:193-210).
- **Effect summary (Validate):** `_effect_summary` (channel_ops.py:292-332)
  gains one branch: `channels.renumber` → `{"op": "channels.renumber",
  "kind": "metadata", "reindex": False, "count": len(assignments)}` — the
  one deliberate code change in classification.
- **rotationVersion:** `lineup.rotation_version` inputs
  (lineup.py:222-242, 84-103) exclude number/group/revision; the library
  Lineup path serves it unprefixed (main.py:339-348) — cached lineups
  survive an arrangement byte-identically.
- **Schedule/continuing:** publications keyed by channel id; stale guards
  (refresh.py:386-405, 356-359) compare source/sort/seed/policy/playable
  only; no consumption reset (`presentation_signature`,
  channel_service.py:286-295, changes by design — that is the client-facing
  "directory changed, playback didn't" signal).
- Phase 2 locks all of this with a dedicated regression: a renumber-only
  Apply over a multi-channel fixture asserts empty journal, unchanged
  `rotation_version` inputs, unchanged published programming file, unchanged
  membership signature, changed presentation signature.

---

## 4. Test plan (Phase 2)

New file `tests/test_channel_organization.py` (planner units — no I/O, no
fixtures needed beyond dict builders) plus additions to
`tests/test_channel_library.py` (transaction/opcode) and
`tests/test_channel_ops.py` (op surface, cosmetic invariants). Reused
idioms: `base_library()` / `op_library()` builders, `data_dir` fixture,
direct `library.apply_transaction` receipt assertions, `Ctx` + `FakeClient`,
the cosmetic-assert pattern (test_channel_ops.py:193-210), the replay/
conflict patterns (test_channel_library.py:104-131), and the multi-channel
fixture shape from test_channel_ops.py:177-190.

| # | Acceptance-matrix row | Test (file :: case) |
| --- | --- | --- |
| 1 | Create at occupied 300; run 300,301,302; 303 free — one Apply, one revision | test_channel_library :: `test_create_into_number_vacated_by_same_packet_renumber` |
| 2 | Same creation, downward; 299 free | test_channel_organization :: `insert_down_mirror` |
| 3 | Existing 305 → occupied 300 (mover removed from occupancy first) | test_channel_organization :: `insert_mover_removed_from_occupancy_first` |
| 4 | 299 swaps 300 while name dirty — one packet, no clean-draft prerequisite | test_channel_library :: `test_put_name_plus_renumber_swap_in_one_packet` |
| 5 | New-channel sheet: swap absent, relocation/free-number present | test_channel_organization :: `insert_temp_no_swap_but_relocate_and_free_number` |
| 6 | Block 171,173,180 → 300: order kept, consecutive, all displaced reviewed | test_channel_organization :: `move_block_preserves_order_and_reviews_displaced` |
| 7 | Shift +10 with untouched blockers → typed error, gaps preserved on success | test_channel_organization :: `shift_interval_blockers_range_overlap` / `shift_interval_preserves_gaps` |
| 8 | Range 300-699 useAvailable: all land, outsiders stay, capacity exact | test_channel_organization :: `arrange_range_skips_outsiders_exact_capacity` |
| 9 | Exclusive range: every outsider relocated outside; no reservation recorded | test_channel_organization :: `arrange_range_exclusive_relocates_all_outsiders` + storage-shape assert (no new fields) |
| 10 | 401 into 400: shortfall 1, nothing staged/committed | test_channel_organization :: `arrange_range_shortfall_reports_no_partial` |
| 11 | Mixed namespace/band overflow: explicit, never silent | test_channel_organization :: `band_mixed_lists_offenders` |
| 12 | Full band; archived/paused/disabled occupy honestly | test_channel_organization :: `occupancy_includes_archived_paused_disabled` |
| 13 | group.put + channels.move + renumber atomic; rejection byte-identical | test_channel_library :: `test_group_create_move_renumber_atomic_and_rejection_clean` |
| 14 | Cyclic map + create-into-vacated accepted; final duplicates reject | test_channel_library :: `test_renumber_cycles_and_swaps_legal` / `test_final_duplicate_still_rejects` |
| 15 | Bad payloads: empty list, bad ids, boolean numbers, unknown keys, dup ids, swap∩renumber | test_channel_library :: `test_renumber_bad_payloads_typed` (each code asserted) |
| 16 | Revision conflict / replay / lost response — unchanged semantics | test_channel_library :: `test_renumber_replay_and_conflict` (reuses 104-131 patterns) |
| 17 | Cosmetic invariants (matrix row "Cosmetic arrangement") | test_channel_ops :: `test_renumber_only_apply_is_fully_cosmetic` (empty journal, no reindex, unchanged `rotation_version` inputs, unchanged publication file, membership sig stable, presentation sig moved) |
| 18 | Preview hygiene: stale_revision, token echo, zero writes, no id allocation | test_channel_ops :: `test_preview_arrangement_read_only_and_correlated` |
| 19 | Old backend gating | frontend-side capability check; backend test only asserts `features.arrangement` present when library ops are |

Plus a planner determinism property test: same fixture + intent run twice →
identical dict output; and an ordering test for `order: "name"` with
id/tempRef tie-breaks.

---

## 5. Risks / open questions for the orchestrator

1. **`applyPacketHint` create skeletons** — the hint's `channel.create`
   entries carry kind/number/name/groupId only; the UI must merge the full
   pending-create draft (source, color, glyph) before submit. Acceptable, or
   should the hint omit creations entirely?
2. **Health numbers go stale after big arrangements** — published health rows
   keep the number from their last compute until the next health pass.
   Recommendation: accept + document (`health_numbers_stale` warning);
   a forced requeue reindexes content just to restamp a number — wasteful.
3. **Exclusive `outside` interval required?** Plan §3.4 says "explicitly
   chosen"; proposal enforces required (`bad_range` when absent). Confirm no
   auto-chosen outside interval is wanted later.
4. **Feature key naming** — `features.arrangement` vs
   `features.channelArrangement`. Proposal: `arrangement` (short, matches
   `poolPreview` brevity). Freeze with the frontend.
5. **swap∩renumber exclusion** — proposal rejects the combination
   (`ambiguous_assignment`) rather than defining staging-order semantics.
   Simpler mental model; standalone swap unaffected. Confirm.
6. **Multi-band arrangements** — one packet with per-band sub-plans
   (renumber is per-channel-band-checked, so one op carries both bands)
   vs two packets/two revisions. Proposal: one packet; the UI composes
   sub-plans into a single renumber op. Confirm.
7. **`duplicate_number` code preserved** on the narrow packet-aware put
   check so existing frontend error handling survives; new final-occupancy
   failures use `destination_occupied`/`duplicate_destination`. Confirm the
   frontend is happy distinguishing three occupancy codes.
8. **Validate response additions?** ValidateChannelChanges inherits the
   final-occupancy pass via `_check_ops` with no shape change. If the
   frontend wants `effects` to include per-arrangement rows, §3e's new
   `_effect_summary` branch already provides them.

## 6. Constraint compliance (explicit)

- No scene/Stash queries in the planner or in renumber-only packets
  (`_reference_errors` short-circuits on unchanged sources).
- Occupancy input is ALL channels incl. archived/paused/disabled
  (§3a intents, test 12).
- Moving records removed from occupancy before planning (§3a intents 2-4).
- Bands immutable: ch 1-99, net 100-899 (`BANDS`, per-kind checks, `cross_band`).
- Mixed-band group ops need explicit split (`band_mixed` lists offenders;
  never silent omission).
- No storage schema change; group records reject unknown fields — no
  `numberRange`, no reservation state anywhere.
- Planner is pure Python (`justwatch/organization.py`, stdlib only, no I/O,
  deterministic); preview read-only, no identity allocation, correlated by
  revision + correlationToken.
