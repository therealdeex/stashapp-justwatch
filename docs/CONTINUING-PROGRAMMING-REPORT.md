# Continuing programming — implementation handoff report

2026-09-21. Implementation of `docs/CONTINUING-PROGRAMMING-PLAN.md` across
both repositories. **Implementation and dev verification are complete;
production deployment/pilot activation was NOT performed and needs a separate
explicit instruction.**

## Changed behavior

- Activated networks (rollout file, default absent → all fixed) now air
  **continuing** broadcasts: whole eligible library, deterministic per-pass
  deck with per-pass shuffle variation, 7-day rolling publication, 24h
  whole-airing protection, ~15% arrival slots (24–72h first-air target),
  soft cooldown/time-of-day/spacing preferences, durable consumption that
  survives restarts, deterministic encore during outages (flagged degraded).
- `Schedule` serves `net_` ids; Directory/FullDirectory overlay resolved
  modes + lightweight schedule status; new `ProgrammingStatus` op +
  `programming/status.json` manifest; `ProgrammingDesk` carries bounded
  network diagnostics and last-run outcomes; capabilities advertise
  `features.programming` (v2, networks, 168h/24h/15%) — all additive on
  contract v1.
- TV: published schedules dispatch for Network keys (capability-gated);
  guide/tune/skip/landing resolve one publication; a failed airing hops to
  the next valid airing on the same channel (bounded); old plugins/rollout
  off keep the fixed loop everywhere.
- Customs are untouched (schema-1 engine, 72h horizon, all 172 pre-existing
  tests green); importer accepts an optional `programming_mode` CSV column
  (blank compiles byte-identically — verified against the production CSV).

## Commits

- stash-justwatch `738e8cd` engine + integration (32 tests),
  `2de5bdc` simulation + 3 engine-defect fixes (3 regression tests, 217 total),
  `4cb1f92` docs/pilot/AGENTS rule revision. v0.7.0.
- StashAppAndroidTV `992119f7` network schedules + failure recovery + tests.

## Tests and builds

- Plugin: **217 passed** (172 pre-existing unmodified + 45 new), stdlib only.
- TV: full `testDebugUnitTest` green (162+ tests incl. 6 new); `assembleDebug`
  built (`0.9.1-123-ga56bb37b-106`, armeabi-v7a + arm64).
- Importer byte-compatibility: regenerating networks.json from the
  production CSV is byte-identical (795 networks, revision `31004561099d`).

## Simulation evidence (`analysis/continuing-simulation/`, 30 days, offline)

Gates: **all pass** — 0 unexpected protected-airing changes (445 expected,
membership-removal-driven), 0 ineligible future airings after reconciliation,
0 in-pass duplicate consumption, 700/700 unique on the large fixture (vs 50
baseline), feasible arrivals first-air ≤ 72h (median 28.3h, feasible max
32.2h), horizon never below 24h while healthy. Variety: evening-lineup
repetition 0.734→0.0 (large), 0.985→0.255 (short clips); median same-scene
gap ~14 days. Resources: avg prepare 26ms, p95 79ms, max 0.44s (5000-row
rebuild), 22MB peak, 1.4MB largest publication, 2119 GraphQL queries/30d/8ch.

Honest limitations (in report.md): thin libraries repeat by necessity (23h
pass ≈ daily cycle); a shrinking rolling-recency source can regress
same-window diversity below its fixed baseline; bulk 200-import drained
138/200 in 30 days (oldest pending 475h — warned, not promised); pass-boundary
adjacency on the thinnest channels (min gap 0.4h); Huge fixture 42.4%/30d is
correct arithmetic, not a cap.

## Live dev verification (Stash :9998, read-only + task)

Capabilities advertise v0.7.0 with the programming feature; Schedule for a
network without rollout answers `fixed`; with the dev rollout
(`net_8091d3ea` 8 playable scenes, `net_461ab3e3` 1 scene, `net_04685144`
0 matches) `prepare_programming.py --verify` reports ready; coverage
168.3h/168h/0 (the zero-coverage network honestly `expiring: true`); Schedule
serves real wall-clock airings with per-pass uniqueness; Directory shows
continuing + status for the three, fixed for the other 792; Lineup unchanged;
second hourly run is idempotent (identical publication version). The dev
rollout file is left in place (see AGENTS.md) — delete it to return dev to
all-fixed.

## Blocked verification (precise)

On-device TV interaction: the .105 stick is USB-docked at dev-lab2 with no
display attached — WiFi adb unreachable (`no route to host`), `screencap`
returns empty without an HDMI surface, and the app has no deep link into
Just Watch, so blind navigation cannot be confirmed. What WAS verified on
device: APK 106 (armeabi-v7a) installs, launches (MainActivity resumed),
72 startup log lines, **zero crash-buffer entries**, clean force-stop. The
full client-side path is covered by unit tests (two-clients-see-the-same-
airing, paging, error semantics) and awaits a headed session on .105.

## Production actions NOT performed (require explicit instruction)

Deploy plugin to 192.168.8.40; install APK on any production TV; create the
production rollout file / activate the pilot; any catalog or networks.json
change; the 513-network proposal remains untouched. Pilot manifest:
`docs/PILOT-MANIFEST.md` (7 ids by structural spread + rollout file content +
activation/rollback commands). Rollback = `enabled: false` in the rollout
file; TVs fall back to the fixed loop at their next Directory load (one
possible mid-airing discontinuity at cutover back).

## Known gaps / follow-ups

- Per-viewer watched-history skipping and global cross-network coordination:
  out of scope by plan.
- The TV player-failure hop is device-untested (blocked as above).
- The three engine defects the simulation caught (addition-driven
  count-drain, deleted-scene deck residue crash, pass-boundary adjacency)
  are fixed and regression-tested here; the pilot's first week should watch
  `ProgrammingStatus` coverage/degraded flags daily.
