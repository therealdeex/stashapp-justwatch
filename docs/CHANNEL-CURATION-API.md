# Channel curation — API and schema additions (contract v1, additive)

Status: implemented on the plugin side; TV integration in progress.
Nothing here changes an existing v1 payload shape — every addition is a new
operation or a new optional field, negotiated through `Capabilities`.

## Capabilities additions

```jsonc
"features": {
  "channelLibrary": {           // present when the deployment is migrated
    "version": 1,
    "directoryOperation": "GetChannelDirectory",
    "libraryOperation": "GetChannelLibrary",
    "definitionOperation": "GetChannelDefinition",
    "historyOperation": "GetChannelHistory"
  },
  "channelGroups": { "version": 1, "singleMembership": true,
                     "legacySectionFallback": true },
  "explicitApply": { "version": 1, "applyOperation": "ApplyChannelChanges",
                     "validateOperation": "ValidateChannelChanges",
                     "resultOperation": "GetChannelApplyResult" },
  "poolPreview": { "version": 1, "operation": "PreviewChannelPool" }
},
"limits": { "…": "…", "libraryChannels": 899 }   // legacy maxChannels stays 99
```

## Operations

| Operation | Mode | Sync/Task | Purpose |
| --- | --- | --- | --- |
| GetChannelLibrary | `GetChannelLibrary` | sync | revision, groups, paged/searched lightweight channel rows (no raw sources) |
| GetChannelDirectory | `GetChannelDirectory` | sync | ordered groups + playback rows + signatures + `emptyLibrary` |
| GetChannelDefinition | `GetChannelDefinition` | sync | one full editable record + plain-language summary |
| ValidateChannelChanges | `ValidateChannelChanges` | sync | typed errors + per-op effect summary, no writes |
| PreviewChannelPool | `PreviewChannelPool` | sync | draft-correlated pool count + the ACTUAL bounded playable rotation |
| ApplyChannelChanges | `ApplyChannelChanges` | task | the ONLY write path: touched-record transaction |
| GetChannelApplyResult | `GetChannelApplyResult` | sync | durable receipt lookup by `requestId` |
| GetChannelHistory | `GetChannelHistory` | sync | bounded revision archive (restore = a NEW Apply) |

## The library document (storage schema 1)

`<stash Dir>/stash-justwatch-data/channel-library.json`. Storage version is
independent of the API contract. Strict loading: unknown fields, non-boolean
flags, duplicate ids/numbers, dangling group references, and future schema
versions are hard errors (`LibraryError`) — never normalized away.

```jsonc
{
  "schemaVersion": 1,
  "libraryId": "lib_…",
  "revision": 12,
  "migration": { "seedCatalog": "just-watch-v4-final", "seedDigest": "…",
                 "customCount": 4, "networkCount": 513, "retiredRolloutIds": [] },
  "groups": [ { "id": "grp_…", "name": "General", "position": 1,
                "legacySection": "general" } ],        // legacySection optional
  "channels": [ {
    "id": "ch_…|net_…", "kind": "ch|net",             // band 1-99 / 100-899
    "number": 100, "name": "…", "glyph": "…|null", "color": "#RRGGBB",
    "groupId": "grp_…",                                // exactly one
    "sort": "shuffle", "seed": 123456789,
    "enabled": true, "archived": false, "paused": false,
    "source": { /* see below */ }, "sourceLabel": "…",
    "programming": { "mode": "fixed" },
    "provenance": { "origin": "v4-final-proposal|custom|created",
                    "stableKey": "100|Name", "legacySection": "general",
                    "seedCount": 573 }
  } ],
  "settings": { },          // legacy TV-editor settings pass through
  "recentRequests": [ /* receipts, newest first, retention-capped */ ]
}
```

### Sources

* legacy custom shapes (`savedFilter`/`tag`/`performer`/`studio`) — stored and
  projected byte-for-byte as always;
* the compiled tier's `filter` shape — unchanged semantics;
* the new `criteria` shape — the visual rule builder's output:

```jsonc
{ "type": "criteria",
  "tags": ["9"], "tagsAny": ["1"], "excludeTags": ["9320"],
  "performers": ["157"], "performersAny": ["1","2"], "excludePerformers": ["9"],
  "studios": ["819"], "studiosAny": ["3"], "excludeStudios": ["4"],
  "date": {"from": "2020-01-01", "to": ""}, "duration": {"min": 600, "max": 7200},
  "createdAt": {"withinDays": 180}, "q": "text",
  "studioSceneCount": {"max": 2}, "performerSceneCount": {"min": 100, "max": 500} }
```

**Dynamic entity selection (owner decision 2026-09-23):** `studioSceneCount`
and `performerSceneCount` select performers/studios by ACTIVITY instead of by
id list — `{max: 2}` means "studios (or performers) with fewer than 2
scenes", `{min, max}` a range. They project to Stash's nested relational
filters (`studios_filter`/`performers_filter` with a `scene_count` criterion),
so membership resolves server-side on every query and never goes stale — no
materialized id lists. They compose with every other row (AND) and with
id-based includes/excludes, and add a day-granular epoch to rotation
versions so client caches stay honest. Requires a Stash version with the
nested `*_filter` fields (present on dev since 2026-09; check before
production release).

Rows combine with AND; the ANY/ALL toggle picks the storage field per facet.
Stash cannot express (ANY row) AND (ALL row) inside one facet criterion, so a
source setting both is a validation error (`conflicting_rows`) — the GUI's
toggle moves ids between the fields instead. Exclusions ride their facet
criterion's `excludes` (a direct Stash criterion, never a NOT wrapper); an
exclude-only pool (`{"excludeTags": ["9320"]}`) is the compiled tier's
historical "everything except" shape and remains valid. Hierarchy: tags and
studios project with `depth: -1`. Recency resolves to a UTC cutoff at query
time (the authored `withinDays` is what is stored).

## Apply semantics

```
ApplyChannelChanges {
  requestId,          // client-generated, unique per SUBMIT (not per retry)
  expectedRevision,   // optimistic concurrency
  ops: [ channel.put{channel} | channel.create{tempId, channel}
       | channel.swap{a, b} | group.put{group} | group.delete{id, moveTo?}
       | channels.move{channelIds, groupId}
       | channels.patch{channelIds, patch{enabled|paused|archived}} ]
}
```

* Receipts are part of the document: `{requestId, status: committed|rejected,
  revision|error, errors?, idMap?}`. A retry of the same `requestId` with the
  same payload replays the stored receipt (idempotent even after a lost
  response); the same id with a DIFFERENT payload is rejected
  (`request_replayed_with_different_content`). REJECTED receipts carry the
  same payload digest, so an identical retry of a REJECTED request replays
  its rejection exactly.
* Transactions are staged atomically: every op is applied to an independent
  candidate document and the final candidate is validated (identity,
  numbering, groups, sources, mode-per-namespace) before anything is
  committed. A REJECTED transaction leaves definitions and revision
  byte-identical — only its receipt is recorded.
* Identity is server-owned and enforced on the FINAL candidate through every
  opcode: an existing channel's `id`/`seed`/`kind`/`provenance` never change
  (attempts rejected as `identity_change`, including through
  `channels.patch`, whose keys are allow-listed to
  `enabled|paused|archived` as real booleans); created channels get
  server-assigned identity, mapped back to the caller's `tempId` in
  `receipt.idMap`.
* Effects: membership changes (source/sort/seed/policy/playing-state) write
  durable per-channel refresh intent BEFORE the definitions commit (a
  `pre_commit` hook inside the writer lock — there is no crash window between
  "committed" and "enqueued"); cosmetic changes (name/number/color/glyph/
  group) never reindex. The Apply task performs the incremental refresh
  inline; the scheduler drains the same journal. Workers acknowledge only the
  journal generation they processed (concurrent enqueues survive) and merge
  health into the CURRENT snapshot per channel. A stale worker (identity
  superseded mid-build: source, sort, seed, policy, or playable state)
  re-queues and publishes nothing.
* `programmingMode` in GetChannelDirectory (and both legacy directories and
  Schedule) is the RESOLVED effective mode from one shared resolver: customs
  serve fixed/explore/discovery; networks serve fixed/continuing under the
  operator rollout with the authored fixed pin always winning. Validation
  rejects CHANGES into a mode the namespace cannot serve
  (`bad_mode_for_kind`); a stored legacy out-of-namespace mode is
  grandfathered until deliberately edited and reads honestly as fixed.
* PreviewChannelPool reports the channel's ACTUAL bounded playable rotation
  via the shared `fetch_rotation` (up to 50 playable rows, at most 1000
  scanned): `rotationSize`/`rotationComplete` are measured, never estimated
  from `count`. Authored `q` (and saved-filter `q`) reach every query path —
  preview, Lineup, health, and both schedulers' indexers. Dynamic rules probe
  the live Stash for nested-filter support; an older server gets the typed
  "dynamic performer/studio rules are not supported by this Stash version"
  error at validation/preview, never a playback surprise. Validation also
  checks newly authored entity references against Stash (bounded at 60
  lookups per request; metadata-only edits of a broken stored source stay
  valid so they remain recoverable).
* Lineup `rotationVersion` on a library deployment is the channel's OWN
  ordering identity (membership + order + epoch) — NOT prefixed with the
  global library revision, so a cosmetic Apply elsewhere does not churn
  cached lineups. `Lineup.revision` still reports the library revision.
* A queued task id is NOT success: clients poll `GetChannelApplyResult` for
  the correlated receipt. `status: "unknown"` means expired-or-never-reached;
  resubmitting the SAME requestId is safe.

## Compatibility

* Legacy `Directory` on a migrated deployment: customs in `channels`, nets in
  `networks` with legal legacy `section` names (the group's `legacySection`
  fallback; owner-defined groups land in "general"). Owner group names never
  enter `section`. Counts are historical seed counts until a fresh health
  computation supersedes them (`poolFreshness` on FullDirectory rows).
* An intentionally empty owner tier emits a present-but-EMPTY `networks`
  block — an ABSENT block remains the pre-networks "keep your fallback"
  signal, which a migrated deployment never sends.
* Legacy `SaveCatalog` maps onto one customs-only Apply. A channel whose
  stored rules are composite gets `unsupported_edit` (nothing written) rather
  than silent truncation.
* Old TV + new plugin: unchanged payloads; new TV + old plugin: unchanged
  fallback (capability-gated).
