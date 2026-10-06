# Android TV completion after skips — implementation handoff

Copy the prompt below into an implementation session with access to both local
repositories.

---

Implement the Android TV Just Watch playback fix described in:

`/home/shahram/dev/stash-justwatch/docs/ANDROID-TV-TIMESHIFT-ADVANCE-PLAN.md`

The reported bug: after skipping ahead through several videos on a channel,
natural completion returns to the video currently airing on the live schedule
instead of continuing after the video just watched.

Work primarily in `/home/shahram/dev/StashAppAndroidTV`. Read applicable AGENTS.md
files and record current HEAD/local changes first. Investigation baselines were
Android `f4c536a4` and plugin `1e96af4`; the Android checkout has substantial
unrelated Reels/Room work. Preserve all existing changes.

The source-level cause is established:

- `JustWatchPlayerController` forwards `STATE_ENDED` through `JustWatchPage` to
  `JustWatchViewModel.onPlayerEnded(generation)`.
- The ViewModel checks generation and sends `PlayerEnded`.
- `JustWatchReducer` unconditionally calls `tuneTo` on completion, clearing
  `timeShifted` and emitting `TuneChannel`.
- That effect resolves `BroadcastSchedule.nowPlaying`, discarding the skipped
  airing's position. Existing tests deliberately expect this behavior.

Implement the following scoped behavior:

1. When a resolved video completes while `timeShifted == true`, stay on the same
   channel, resolve its successor at the finished program's `endEpochMs`, and
   start that successor from position zero. Retain time shift across subsequent
   completions. Do not apply the D-pad's mid-video join offset automatically.
2. Preserve current-clock retuning for ordinary live playback. Direct channel
   tunes and explicit Back to live still clear time shift.
3. Ignore completion without a resolved program or outside the Playing phase.
   Use phase rather than the `playing` boolean, because end callbacks can follow
   `onIsPlayingChanged(false)`. Keep generation/cancellation guards and prove old
   or duplicate callbacks cannot replace a newer tune.
4. Reuse `TuneProgramAt`, `tuneToProgramAt`, and `tuneInternal`; do not introduce
   a private queue or a parallel advancement coroutine. Separate
   `resyncOnOffAir` from `joinInProgress` in the reducer helper as needed.
   Shifted automatic completion uses `joinInProgress = false` and same-channel
   live fallback only on a genuinely empty target. Preserve exception/failure
   handling rather than disguising transport errors as success.
5. Set time shift inside accepted whole-video-skip reducer transitions. Remove
   the premature flag mutations from `skipVideo()`/`skipToNextProgram()` so ignored
   input does not change playback policy. Avoid a broad unrelated state refactor.
6. Existing pause/restart/seek/background actions already mark time shift; they
   receive the same sequential completion behavior. Preserve manual D-pad join
   semantics and media-next semantics. Update obsolete completion comments and
   docs that say completion always returns to live.

Write failing regressions before changing the behavior. Required evidence includes
a live A with skips reaching D, then completions producing E and F at position zero
while live remains A. Cover legacy loops, published custom/network channels, page
boundaries, single-scene repeated airings, backward skips, pause/restart/seek state,
Back to live, rejected input during tuning, stale/duplicate callbacks, empty-target
fallback, and transport errors. A repeated scene id is valid in a later airing;
identify forward progress using schedule intervals. Fake published dependencies
must return `emptyList` for no airing and `null` only for legacy mode.

Update the existing timeshift-reset test to distinguish direct live retunes from
shifted completion. Keep the existing unshifted scene-end-to-live test. Follow
the plan's focused test command, adding the new advancement tests. The investigation
ran 87 existing tests successfully; those tests establish the old behavior, not the
fix. Run relevant Kotlin formatting checks and build the debug APK.

Verify on the dedicated dev `.105` Fire TV stick per the Android repository's
current AGENTS.md, using dev Stash port 9998 and neutral/synthetic short videos.
Use an emulator only as an allowed fallback. Cover actual natural end callbacks
and MPV if available. No production launch, UI inspection, captures, or interactive
debugging. Do not deploy to production as part of this task. Report hardware or
backend verification gaps accurately.

The plugin already supports timestamped schedule lookup, so no plugin code change,
schema migration, contract bump, continuing rollout change, or server ledger write
is expected. Local skipping must not change the globally published schedule.

Complete the focused implementation, regression tests, documentation updates, and
dev verification that is available. Finish with the changed files, test results,
APK location, remaining verification limits, and rollback instructions. Do not stop
at another plan. If you create or work on a PR, register it with the current T3
thread using `link_pull_request` when available.
