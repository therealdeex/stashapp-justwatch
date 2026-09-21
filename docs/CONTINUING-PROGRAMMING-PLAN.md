# Continuing programming for Just Watch networks

Implementation plan and acceptance contract — 2026-09-21.

## Outcome

Turn curated networks into continuing broadcasts that explore their full eligible
libraries, remember previous airings, vary the order between passes, and make room
for new additions. Preserve channel identity, editorial membership, and stable
shared airings across TVs. Keep fixed playback available as an explicit mode and
as the legacy-client compatibility path.

This document is a proposal for implementation, not authorization to deploy to
production. No production configuration or channel catalog was changed during the
investigation. The separate 513-network proposal remains out of scope.

## Evidence and current behavior

Read-only production inspection on 2026-09-21 found:

- 795 networks, all `programmingMode: fixed`, all with `sort: shuffle`.
- Zero custom channels in the production catalog and no publication JSON files.
- An active hourly user timer. Its successful process exit establishes that a
  task was queued, not that publications were successfully produced.
- 518 networks with compiled counts above 50. These are catalog counts, not a new
  live recount; they establish the mismatch between source size and rotation size.
- Production rotation bounds of 50 playable scenes / 1,000 scanned rows. Shuffle
  uses `random_<immutable seed>`, so it does not evolve merely because time passes.
- Production `Schedule` resolves custom catalog channels only. The production
  `programming.py` matched the local file; `main.py` and `lineup.py` did not.
  Recheck deployment differences before implementing a migration.
- Local programming and lineup tests: 28 passed. No production TV was inspected.

Relevant code:

| Location | Current responsibility / limitation |
| --- | --- |
| `justwatch/programming.py` | Custom-only paths; 72-hour publication; 48-hour retained programs; persistent deck and counts; six-hour cached source index; only custom non-fixed channels prepared. |
| `justwatch/main.py` | Directory fields, custom-only Schedule lookup, task dispatch and programming desk. |
| `justwatch/lineup.py` | Shared source projection and fixed bounded rotations. Preserve these semantics. |
| `justwatch/networks.py` | Strict compiled network loading. |
| `tools/import_channels.py` | Sole producer of `networks.json`; identities and seeds derive from CSV identity. |
| `tools/prepare_programming.py` | Queues a task but does not verify its completion. |
| TV `features/justwatch/JustWatchPluginClient.kt` | Continuing-channel set and published-program fetch currently support custom channels only. |
| TV `features/justwatch/BroadcastSchedule.kt` | Shared fixed-loop math plus cached publication playback. |
| TV `ui/pages/justwatch/JustWatchViewModel.kt` | Published-program dependency dispatch currently checks `ChannelKey.Custom` only. |

TV paths above are under
`~/dev/StashAppAndroidTV/app/src/main/java/com/github/damontecres/stashapp/`.

## Design decisions

1. Extend the existing server-side engine; do not create an independent TV-side
   shuffle or reset seeds every day.
2. Use the full eligible source, subject to explicit indexing bounds. The old
   50-item Lineup stays intact for fixed mode and older clients. A Schedule page
   of 50 airings is pagination, not a 50-video membership cap.
3. Target a rolling seven-day publication, replenished hourly. Preserve the
   current airing and at least the next 24 hours at whole-airing boundaries.
4. Maintain durable consumption history independently from the publication
   horizon. Extending the horizon must not reset selection state.
5. Reconsider the flexible future when membership changes, policy changes, or a
   deliberate replan occurs. Routine hourly extension does not reshuffle it.
6. Prioritize new library additions in approximately 15% of flexible slots by
   default. Make the share configurable within a bounded range. Target first
   airing within 24–72 hours where capacity permits; expose unmet targets.
7. Keep the broadcast shared. Per-viewer watched-history-aware skipping is a
   separate future feature, not part of this implementation.
8. Keep runtime dependencies in the Python standard library. Use deterministic
   tie-breaking and injectable clocks for repeatable tests and simulations.

Seven days / 24 hours / 15% are initial product defaults to validate through
simulation. Do not silently change them to make tests pass. Record any justified
adjustments and their measured effect.

## Invariants and deliberate policy change

The existing AGENTS rule that networks “stay fixed-mode” is the one intentional
behavioral change in this plan. Update that documentation alongside implementation
to allow continuing network schedules. All other network invariants remain:

- CSV-authored membership, exclusions, numbering, identity and seeds are retained.
- Networks remain read-only and outside `catalog.json` / SaveCatalog.
- No network health snapshots; compiled counts remain the directory health data.
  Scheduling diagnostics are a separate operational surface, not replacement health.
- Never hand-edit `networks.json`; change authoring input/importer and regenerate.
- Do not switch to `data/proposed_channels_final.csv` or deploy the 513-network
  proposal as part of this work.
- Preserve JAV exclusions and all sanctioned exceptions. Global editor settings
  must never alter custom or network source membership.
- Keep writes in tasks; sync Schedule/Directory/Lineup/desk reads never build.
- Preserve catalog locking, optimistic revisions, strict corruption errors,
  immutable IDs/seeds, and the raw-interface single JSON stdout envelope.
- Existing custom fixed/explore/discovery behavior remains supported. Explicit
  non-shuffle custom play orders must not silently become shuffled.

## Phase 1 — policy, identity and capability contract

Add explicit network programming policy to the authoring/import pipeline. Prefer
optional CSV columns compiled into a validated `programming` object using the
existing mode vocabulary. Missing policy remains fixed for backward compatibility;
`programmingMode` continues to describe the resolved mode. Centralize resolution
so Directory, preparation and Schedule cannot disagree.

Use an operational rollout allowlist to activate selected network IDs before broad
activation. Keep activation separate from editorial identity and source membership.
Document precedence between authored policy and rollout control. Default rollout
must not activate all networks merely because new code was deployed.

Accept both `ch_[0-9a-f]{8}` and `net_[0-9a-f]{8}` in publication storage with full
match validation. Introduce a shared channel resolver for catalog and compiled
networks. Do not copy networks into the custom catalog to reuse existing functions.

Advertise additive capabilities for network schedules and their semantics, e.g.
`features.programming.networks`, horizon and protected-window durations. Preserve
contract v1 existing shapes, fields and operations; add optional fields only.
Keep `Lineup` fully usable by old clients regardless of continuing policy.

Directory should expose resolved policy plus lightweight publication status/version
for custom and network channels. Avoid reading every large schedule JSON during
each directory request: publish an atomic lightweight status manifest and use it.
Define a separate publication generation/version; catalog revision and network
revision alone cannot invalidate evolving schedules.

## Phase 2 — durable state and publication lifecycle

Separate these concepts, even if initially stored in one atomic per-channel file:

- Source index and fingerprint, indexing timestamp, dynamic-source epoch.
- Consumption state at the committed schedule boundary: pass number, remaining
  deck, committed per-scene counts, last scheduled times, recent neighbors.
- Historical aired intervals (bounded; initially 30 days for quality metrics).
- Protected future airings and a reproducible checkpoint after them.
- Flexible future airings and provisional selection state.
- Publication version, generation time, prepared-through time and diagnostics.

Critical correctness requirement: do not treat counts and deck mutations from
discarded future slots as if those videos actually aired. The existing builder
updates counts while planning all future slots. Replanning must restore a checkpoint
at the protected boundary and replay retained airings, or implement an equivalent
ledger with provisional reservations. Never merely truncate `programs` and reuse
the end-of-horizon deck/counts. Also distinguish “scheduled” from “aired” in metrics.

On each preparation:

1. Resolve policy and source fingerprint and load/validate prior state.
2. Refresh membership when due, changed, or its dynamic epoch expires.
3. Advance committed history according to wall-clock time, idempotently.
4. Preserve whole airings through `now + 24h`; when a cutoff falls inside an
   airing, preserve that airing too. With no prior publication, start at now.
5. If replanning is necessary, restore state at this boundary, release discarded
   reservations, and build the flexible future from that state.
6. Otherwise append from the existing publication end without moving earlier slots.
7. Publish enough whole airings to cover seven days, subject to explicit resource
   budgets; report shortfalls rather than claiming complete coverage.
8. Revalidate the channel/source/policy generation before atomic replacement.
   Publish status metadata only for successfully committed generations.

Use a versioned internal storage schema and a tested reader/migration for existing
custom publications. Preserve current airings during migration. Keep backups or an
explicit reversible migration path; corruption must not silently reseed/reset state.
Atomic replacement and process locking must cover every state transition. If files
are split, define a generation manifest/commit point to prevent torn state.

Deleted/unplayable/newly-excluded material is an exception to future protection:
remove invalid upcoming airings and rebuild from the earliest affected boundary,
retaining client notice where possible. Never continue scheduling an ineligible
video just to honor a freeze window. A deleted currently-playing file may fail;
the client must recover to the next valid airing. Specify and test this behavior.

## Phase 3 — selection and freshness

Maintain a per-channel pass over eligible scene IDs, shuffled by immutable channel
seed plus pass number. Consume each once before starting another pass. Track IDs
across index refreshes; never rebuild a pass simply because preparation reran.

Use bounded candidate selection, not a full-library sort for every airing. Rank
candidates using an explicit priority order:

1. Eligibility and active-pass membership (hard constraints).
2. New-arrival allocation when due and a qualifying arrival is pending.
3. Repeat cooldown, relaxing only when capacity makes it impossible.
4. Same-time-of-day repeat avoidance over recent days.
5. Performer/studio spacing where the channel permits it.
6. Least-recently-aired / exposure fairness and deterministic tie-breaking.

New arrivals selected ahead of their base queue position must be removed from that
queue, so prioritization cannot cause a second appearance in the same pass. Historical
videos newly eligible after a source edit are not automatically “new library arrivals.”
Bootstrap existing libraries without labeling the entire library new. Record
`created_at` where supported and first-seen/index-baseline state; define conservative
fallback semantics for absent timestamps. Use the installed Stash GraphQL schema.

Allocate arrival slots using a persistent accumulator or equivalent deterministic
quota, so hourly runs do not reset the share. When no arrivals are waiting, give
the slot to the normal deck. Large imports drain gradually; report oldest pending
arrival age and estimated capacity instead of promising impossible deadlines.

Default cooldown can start at 48 hours for continuing networks, but is a soft
preference; a channel with six hours of content must repeat. Compute eligible
duration, report attainable variety, and avoid filler or cross-channel borrowing.
Respect explicit custom policies. Do not force studio diversity on single-studio
channels or allow spacing to starve difficult candidates indefinitely.

Use an explicit IANA programming timezone (deployment default America/Toronto) for
time-of-day comparison and spotlight/day rules. Keep storage and API timestamps
in UTC. Test daylight-saving transitions. Consider a three-hour local viewing
window and a seven-day comparison window as simulation defaults. Penalize repeat
appearances in that window rather than enforcing a rule that could block playback.

Existing discovery counts describe scheduling exposure, not user watch history.
Keep the labels and behavior honest. Cross-network overlap metrics are useful,
but global coordination that forbids simultaneous shared videos is a later phase.

## Phase 4 — indexing, scheduler and operations

Reuse `lineup.build_scene_filter` and saved-filter resolution including text `q`.
Index playable duration and selection metadata from the same eligible source used
by the channel. Page through all eligible rows up to the documented 100,000-row
limit; report over-limit sources rather than silently pretending they are complete.

Six-hour reindexing is an initial upper bound, not a reason to query all 795 sources
at once. Deduplicate identical source fingerprints, stagger due times, prioritize
channels with the least remaining coverage, and bound work per task. Persist a
fair work cursor so a limited batch cannot always service the first channels only.
Handle recency-source expiration at its actual epoch boundary, even within an
otherwise reusable cache. Recheck duration/file availability on refresh.

Measure index memory, file sizes, query count and build cost on representative
large sources and the full network tier. Seven days of short clips may exceed the
existing 10,000-program cap. Decide from measurements whether paged publications
are necessary; never simply raise caps without a bound or truncate silently.

Separate the short commit locks from lengthy network/index work where feasible.
Prevent overlapping workers from advancing the same channel twice. Isolate
per-channel failures so remaining channels make progress. Changes during indexing
must invalidate that candidate publication, not overwrite newer policy/state.

Extend operational reporting to distinguish task queued, running, succeeded,
partially failed and stalled. Verify the available Stash task API before implementing
polling; use durable task-result/status data if task completion cannot be queried.
Never report “healthy” solely because `runPluginTask` returned an ID.

Maintain a deterministic emergency encore when coverage expires, but expose it as
degraded operation with last-success time and an actionable diagnostic. On recovery,
finish the currently airing encore before fresh programming begins. Count actual
encore exposure consistently; do not manufacture all missed normal airings as watched.
Automatic status checks should alert before expiry (initial threshold: <24h ready).
Do not introduce external messaging integrations as part of this task.

## Phase 5 — TV, guide and Channel Studio

In `JustWatchPluginClient.kt`, support published schedules for both Custom and
Network keys. Include active networks in continuing-policy tracking after parsing
Directory. Generalize internal ID handling while preserving typed channel keys.
Gate the new path by advertised capability and resolved channel mode.

In `JustWatchViewModel.kt`, dispatch published-program requests for networks as
well as custom channels. Check all tune, next/previous skip, guide, landing-page
now/next, resume and channel-switch paths. They must resolve the same publication;
none may secretly use the fixed 50-item loop for an active continuing network.

In `BroadcastSchedule.kt`, preserve the existing distinctions:

- `null`: fixed/legacy path is allowed.
- Empty published result: no airing is available for this published channel/time.
- Fetch error: use a still-covering cached published page, otherwise surface
  unavailability; do not silently switch to an unrelated fixed timeline.

Support paging across the 50-airing response boundary and beyond the cached window.
Keep page TTL, publication version and time-range coverage coherent. One TV must
not start a different schedule from another. Verify time synchronization/clock-skew
behavior and ensure normal shared-broadcast playback joins the intended airing
position; audit existing join-offset behavior rather than assuming it is correct.

Expose understated “Encore”/preparing/unavailable states when appropriate. Studio
should describe continuing programming as advancing, with source size and schedule
coverage; preserve loop language for fixed channels. Networks remain read-only.
Extend ProgrammingDesk for network diagnostics using pagination/bounded summaries.
Do not compute all network-pair overlaps in a synchronous request: make detailed
overlap analysis offline or on demand for a bounded selection.

## Phase 6 — tests and simulation

Retain the existing suites and add tests for the behavior, not implementation shape.

Server acceptance:

- Networks resolve through Schedule without entering catalog.json; fixed and old
  clients still receive the unchanged Lineup contract.
- A 500-scene network eventually schedules all 500, not just the first 50; no
  repeats within a pass; subsequent pass order differs deterministically.
- Hourly reruns, process restarts and identical input/time do not reset the deck
  or change retained airings. Input publication objects are not mutated.
- A source addition is inserted once, respects the protected prefix and configured
  share, and reaches the freshness target when capacity allows.
- Flexible replanning releases reservations and restores counts/deck correctly.
- Removal, exclusion, duration edits, source edits, dynamic recency expiration and
  branding-only edits have their explicitly intended effects.
- Sparse/single-scene/single-studio sources relax preferences without hanging,
  starving candidates, or inventing content outside the source.
- Deleted files, malformed publications, lock contention, concurrent tasks, partial
  writes, source-query errors and stale commits cannot silently reset programming.
- Migration preserves active custom airings; downgrade/rollback behavior is tested.
- Long outage encore and recovery are deterministic, observable and continuous.
- An activated network's eligible IDs obey the exact source and nowhere-tag rules.

TV acceptance:

- Custom and network publications both tune correctly; fixed channels still loop.
- Guide, now/next, tune and skip agree at airing/page boundaries.
- Two clients at the same server time see the same airing and boundaries.
- Cached page expiry, transient errors, beyond-horizon requests, empty schedules,
  mode changes and feature negotiation behave intentionally.
- Old-plugin/new-TV and new-plugin/old-TV combinations remain usable. An old TV
  will still loop; document that limitation rather than claiming identical playback.

Build an offline, clock-injected simulator for at least 30 days, with small, large,
highly overlapping, short-clip and long-form sources. Inject hourly preparation,
new arrivals, deletions, source changes, scheduler outages and restarts. Compare
the fixed-50 baseline against the proposed engine using the same fixtures.

Report unique eligible coverage; repeat interval distribution; same three-hour
viewing-window recurrence; studio/performer adjacency; pending arrival ages;
protected-airing changes; horizon coverage; build time; peak memory; storage and
query estimates. Clearly distinguish synthetic estimates from live measurements.

Hard gates: zero unexpected protected-airing changes, zero ineligible future items
after reconciliation, zero duplicate consumption caused by replan/restart, no
unbounded loops, and >50 unique scheduled videos for a sufficiently large fixture.
For feasible arrival fixtures, each new item's first airing must meet 72h. Report
bulk-import overload separately. Require measured improvement in daily-viewing
repetition against baseline and explain thin-library limitations. Commit the
simulation configuration and machine-readable results so the comparison is repeatable.

## Phase 7 — migration and staged rollout

1. Re-read both repositories' AGENTS.md and inspect dirty work before editing.
   Compare current production/development artifacts without overwriting differences.
2. Implement plugin support with activation off, storage migration and tests.
3. Implement TV support and compatibility tests. Build the debug APK.
4. Run offline simulations and measure realistic preparation/resource cost.
5. Validate on dev Stash port 9998 and the dedicated dev stick .105 (USB serial
   G072JN0734330EBH is allowed); consult current TV AGENTS.md if devices change.
6. Prepare a concrete pilot manifest of 5–10 existing network IDs spanning source
   sizes/types, including channels selected by the owner when available. Do not
   invent favorites. Prepare migration backups and rollback commands.
7. Deliver reviewable changes and validation evidence. Production deployment is a
   separate explicitly authorized action. Do not deploy the pending network catalog.
8. Once authorized, deploy plugin capability first, then compatible TV build;
   prepare pilot schedules before activating them. Avoid advertising an active
   schedule that has not successfully been published. Select a whole-airing cutover
   boundary and verify its handling rather than switching every client mid-airing.
9. Observe the pilot for at least seven days: advancing publication coverage,
   uniqueness, fresh-arrival delays, encore incidents and viewer feedback. Expand
   in batches only after the measured behavior is satisfactory.

Current production TV policy prohibits launching, screenshots, interactive testing
and follow-up inspection on .169. Only a separately authorized discrete silent
install is allowed. Use dev .105 for device verification; server metadata can be
checked read-only. Never expose API keys in argv, logs, documents or results.

Rollback: deactivate selected continuing policies, retain their state/backups,
restore compatible code/storage as needed, and let Directory/cache invalidation
return upgraded clients to fixed mode. Specify cutover timing and possible visible
playback discontinuity. Do not delete catalogs or regenerate channel identities.

## Deliverables and completion

- Plugin and TV changes in reviewable commits, with migration and capability docs.
- Authored policy/rollout examples and regenerated artifacts only where intended.
- Repeatable simulation tool and report; relevant Python/Kotlin tests and APK build.
- Measured resource limits and operational status/diagnostic tooling.
- Updated README and AGENTS descriptions of the intentionally revised network rule.
- Pilot manifest, deployment checklist and executable rollback instructions.
- A handoff report listing tests actually run, failures/limitations, untested device
  paths, production actions not performed and remaining rollout work.

Implementation completion is distinct from production pilot completion. Do not
claim the latter from unit tests or a successfully queued preparation task.
