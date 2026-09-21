# Handoff prompt: continuing Just Watch network programming

Copy the prompt below into the implementing coding harness. Give it filesystem
access to both repositories and the companion plan.

---

Implement continuing programming for the curated networks in Just Watch, following
`/home/shahram/dev/stash-justwatch/docs/CONTINUING-PROGRAMMING-PLAN.md` as the design
and acceptance contract. Read the entire plan before editing. The companion TV
repository is `/home/shahram/dev/StashAppAndroidTV`.

The user sees the same videos in the same order every day. Read-only production
inspection on 2026-09-21 found 795 networks, all fixed-mode with stable shuffle;
each uses at most 50 playable videos. Production had zero custom channels and no
published schedules. Its hourly scheduler was running, but the existing publication
engine only prepares non-fixed custom channels. This is an integration/design gap,
not something solved by scheduling the same playlist a year ahead.

Your task is implementation and dev verification across both repositories. Do not
stop after another plan. Work through server scheduling, durable state, network
policy, TV integration, migration, tests, simulation and operational documentation.
Use existing architecture where practical. Read both AGENTS.md files and inspect
git status first; preserve unrelated work. Revalidate assumptions against current
code because production and local files differed at investigation time.

Required behavior:

- Continuing networks draw from the whole eligible library and retain consumption
  history across hourly runs and restarts. No stable first-50 loop for these channels.
- Maintain a seven-day rolling publication and protect whole airings through at
  least the next 24 hours. Routine extension preserves published airings; additions
  and policy changes can replan the flexible future.
- Consume a deterministic per-channel deck without repeats within a pass and vary
  shuffle ordering between passes. Retain immutable channel IDs and seeds.
- Prioritize genuine new library additions in approximately 15% of flexible slots,
  targeting first airing within 24–72 hours when capacity permits. Bootstrap old
  content correctly; bulk imports must report capacity limits honestly.
- Apply feasible repeat cooldown, performer/studio spacing and local-time viewing
  window diversity. Preserve custom non-shuffle play orders and explicit policies.
- Guide, tune, skip, landing-page now/next and published schedule pagination must
  agree for both custom and network keys. Preserve shared broadcast semantics.
- Make expired scheduling/encores observable and distinguish a task being queued
  from successful schedule generation.

Critical implementation trap: current build() counts and consumes every future
slot immediately. You cannot truncate flexible future programs while keeping its
end-of-horizon deck/counts. Introduce boundary checkpoints or a reservation ledger
so replanning releases canceled future allocations without losing or double-counting
scenes. Keep scheduled exposure distinct from actual wall-clock airings.

The plan intentionally revises the rule that networks must remain fixed-mode;
update that rule with the implementation. All other invariants remain. Networks
stay read-only, outside catalog.json, CSV-authored, with unchanged identities and
source/exclusion semantics. No network health snapshots. Only tasks publish;
sync operations remain read-only. Keep the fixed Lineup contract for compatibility.
Use additive contract v1 capabilities/fields and a separate versioned storage
migration where needed. Never hand-edit networks.json.

Do not activate every network on deployment. Implement an explicit pilot activation
mechanism defaulting off and prepare a concrete 5–10-channel pilot manifest. Do
not deploy or substitute the pending 513-network catalog. Personal watch-history
skip selection and global cross-network schedule coordination are out of scope.

Important files to inspect:

- Plugin: justwatch/programming.py, main.py, lineup.py, networks.py, contract.py,
  catalog.py; tools/import_channels.py, prepare_programming.py; ui/index.js;
  tests/test_programming.py, test_ops.py, test_networks.py, test_lineup.py.
- TV under app/src/main/java/com/github/damontecres/stashapp/: features/justwatch/
  JustWatchPluginClient.kt, BroadcastSchedule.kt, channel-key definitions, and
  ui/pages/justwatch/JustWatchViewModel.kt plus guide/landing-page consumers.
- TV tests: PublishedScheduleTest.kt, BroadcastScheduleTest.kt and related client
  parsing/view-model tests. Discover actual current locations rather than guessing.

Scale matters: 795 sources, some overlapping, seven days of airings, and potentially
short clips. Stagger/deduplicate indexing, bound work, prioritize expiring schedules
fairly, avoid quadratic per-scene selection and all-pairs synchronous overlap
analysis, and publish lightweight status metadata for Directory. Measure resource
use. Preserve per-channel error isolation and process-safe atomic publication.

Implement the plan's tests and a repeatable 30-day offline simulation with arrivals,
deletions, outages and restarts. Compare against fixed-50 behavior. Prove membership
safety, protected-airing stability, correct consumption after replans, >50-video
coverage for large sources, arrival latency when feasible, and improved daily
viewing variety. Record realistic capacity/resource limitations and actual results.

You may edit both repos, run tests/builds and perform authorized dev verification.
Dev Stash is port 9998. Follow the CURRENT TV AGENTS.md: dedicated dev stick .105
or USB G072JN0734330EBH; no production-TV launch, screenshot, interactive testing or
follow-up device inspection. Keep credentials out of output and command arguments.
Do not deploy to production, install on production TVs, enable production pilots,
or change the production catalog without a separate explicit instruction. Complete
all implementation and reviewable rollout/rollback preparation before seeking any
such approval. If dev hardware is unavailable, finish independent implementation,
tests and documentation and identify only the blocked verification precisely.

Provide progress updates and finish with: changed behavior, changed files/commits,
tests and build results, simulation evidence, migration/rollback instructions,
pilot readiness, limitations and production actions not performed. Do not claim
production success based on dev tests or a queued task ID.

---
