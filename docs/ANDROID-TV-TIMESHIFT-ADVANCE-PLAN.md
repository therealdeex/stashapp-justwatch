# Android TV: preserve skipped playback at video completion

Investigation date: 2026-10-03. Status: implemented in the Android repo
(reducer completion policy + `JustWatchAdvancementTest`); see that repo for the
change set. This document is retained as the investigation record.

## Finding

The current Android TV code directly explains the reported behavior. In Just Watch,
skipping ahead selects a future airing, but video completion unconditionally tunes
the channel at the current wall clock. That can return to a video the viewer already
skipped. This is an explicitly implemented and tested behavior, rather than evidence
of a corrupted plugin schedule.

Example: the channel is currently airing A. The viewer skips B and C and watches D.
If D finishes while the live channel is still on A or B, the app resolves A or B
instead of advancing to E.

This conclusion is from source inspection and execution of existing JVM tests. The
reported session was not captured, and no device reproduction or production access
was performed. The exact channel, remote action, installed APK revision, and player
backend from that session are unknown. The observed code path matches the symptom
without requiring a player failure or a server defect.

## Evidence and ownership

Android repository: `/home/shahram/dev/StashAppAndroidTV`, HEAD `f4c536a4`.
Plugin repository: `/home/shahram/dev/stash-justwatch`, HEAD `1e96af4`.
Inspect current HEADs and local changes again before implementing. The Android
checkout contains extensive unrelated Reels/Room work; preserve it.

Paths below are relative to the Android repository, with the common package root
`app/src/main/java/com/github/damontecres/stashapp/`:

| Location at investigation | Behavior |
| --- | --- |
| `ui/pages/justwatch/JustWatchPlayerController.kt:89` | Media3 `STATE_ENDED` calls `onEnded(position, generation)`. Player repeat is off; advancement belongs to the app. |
| `ui/pages/justwatch/JustWatchPage.kt:195` | Forwards completion to `viewModel.onPlayerEnded(generation)`. |
| `ui/pages/justwatch/JustWatchViewModel.kt:1416` | Checks the tune generation, then dispatches `PlayerEnded`. |
| `features/justwatch/JustWatchReducer.kt:76` | Always calls `tuneTo(state, state.channel)` on completion. |
| `features/justwatch/JustWatchReducer.kt:353` | `tuneTo` clears `timeShifted`, discards the current program, and emits `TuneChannel`. |
| `ui/pages/justwatch/JustWatchViewModel.kt:1889` | `TuneChannel` resolves `nowPlaying(channel)`, using the current clock. |
| `features/justwatch/JustWatchReducer.kt:147` | Media next already anchors at `program.endEpochMs`, starting the successor from its beginning. |
| `features/justwatch/JustWatchReducer.kt:161` | D-pad video skips anchor at the adjacent airing and use a deterministic mid-video join offset. |
| `ui/pages/justwatch/JustWatchViewModel.kt:1368` | Video-skip entry points mark `timeShifted = true`. |
| `ui/pages/justwatch/JustWatchViewModel.kt:2026` | Time-shifted loads retain their resolved position; live loads recompute the position after scene fetching. |
| `ui/pages/justwatch/JustWatchViewModel.kt:1447` | Failure recovery already anchors at the failed airing's end specifically to avoid going backward after a skip. |

`features/justwatch/BroadcastSchedule.kt` already supports `programAt(channel,
epochMs)` for both legacy loops and published schedules. The plugin client already
sends the selected timestamp in `Schedule.at`. The plugin's
`justwatch/programming.py:281` serves schedules relative to that timestamp and can
serve an encore loop after a publication expires. No new API, queue, scheduler,
rollout change, or server write is needed for this fix.

Two existing reducer tests encode the old behavior:

- `JustWatchReducerTest.kt:176`: the test for recording the last channel explicitly
  sets `timeShifted = true`, sends `PlayerEnded`, and expects it to become false.
- `JustWatchReducerTest.kt:323`: `scene end re-syncs the current channel to live`
  expects a `TuneChannel` effect.

The latter remains valid for live playback; the former needs separate assertions
for direct retunes versus time-shifted completion.

## Proposed behavior

Use the existing `timeShifted` state as the session's playback policy. These are
implementation recommendations based on the reported expectation, not additional
owner decisions.

| Situation | Completion behavior |
| --- | --- |
| Time-shifted video, including multiple skips ahead | Resolve at the finished airing's `endEpochMs`; play its successor from position zero. Keep `timeShifted = true`. |
| Backward skip, restart, seek, pause/resume, or background pause | Apply the same time-shifted rule, because these actions already set the same state flag. |
| Ordinary live playback | Preserve the existing current-clock retune behavior. |
| Explicit Back to live, direct channel tune, or channel change | Clear time shift and resolve the channel at the current clock. |
| Completion while tuning, without a resolved program, or after terminal error | Ignore it. |
| Callback from an older tune generation | Ignore it. |
| No successor exists at the requested timestamp | Use the existing same-channel return-to-live fallback, only after an actual empty resolution. |
| Schedule/transport exception | Preserve existing unavailable/failure handling; do not turn an exception into a return-to-live success. |

After a D-pad skip, the manually selected video still starts at its existing
mid-video join offset. Its *automatic successor* starts from the beginning. Do not
reuse that join offset on every completion, which would skip portions of all
subsequent videos. Continue from the currently resolved airing, not the original
live program, an `upNext` display field, or the original skip count.

The schedule remains global and read-only. Only the local viewer's position moves.
Guide listings remain wall-clock broadcasts. A single-scene loop is allowed to
repeat the same scene in a new airing; advancement identity is the airing interval,
not scene-id inequality.

## Implementation steps

1. **Write failing completion regressions first.** Add a reducer test with a
   resolved, time-shifted program. Assert one `TuneProgramAt` effect at its exact
   end, `joinInProgress = false`, and retained time shift. Add a ViewModel scenario
   where the live schedule is A, two or more skips reach D, and D's completion
   loads E. Assert E's load position is zero and that the clock-based A is not
   loaded. The current code must fail these expectations.

2. **Introduce a dedicated completion handler in `JustWatchReducer`.** Require
   `phase == Playing` and a non-null program. For shifted playback, capture
   `program.endEpochMs` before clearing the program and emit the existing
   `TuneProgramAt` effect. For live playback, retain `tuneTo`. Reuse the tuning
   state transition without coupling horizon fallback to the mid-video join flag:
   allow `tuneProgramAnchor` to accept a separate `resyncOnOffAir` argument.
   Existing manual callers should retain their current flag values; automatic
   shifted completion uses `joinInProgress = false, resyncOnOffAir = true`.

3. **Make accepted whole-video skips set time shift atomically.** Today
   `skipToNextProgram()` and `skipVideo()` set the flag before the reducer decides
   whether it can accept the action. Move that assignment into the accepted
   `NextProgramRequested`/`SkipVideoRequested` reducer transitions and make those
   wrappers dispatch only. An ignored skip during tuning must not silently change
   the completion policy. This is a small correction made relevant by relying on
   the flag; a broad migration of every state mutation is outside scope.

4. **Keep tune cancellation and generation ownership intact.** Reuse
   `tuneToProgramAt`/`tuneInternal` rather than launching a second advancement path.
   Existing `onPlayerEnded` generation checks must remain. Once completion starts
   a new tune, repeated callbacks for the old generation must do nothing. A
   suspended successor lookup must not load a video after a newer direct tune or
   Back to live action. Phase guards should use `phase`, not `playing`, since
   Media3 may report `onIsPlayingChanged(false)` before reporting `STATE_ENDED`.

5. **Update behavior documentation and outdated comments.** Adjust completion
   comments in the reducer, `JustWatchEvent.PlayerEnded`, seek handling, state
   documentation, and applicable `features/justwatch/AGENTS.md` statements that
   imply completion always returns to live. The existing Back to live option
   should remain available throughout shifted playback. Preserve channel history,
   mute state, OSD clearing, and failure budgets.

## Regression coverage and acceptance

Use existing reducer and recovery test fixtures; add a dedicated
`ui/pages/justwatch/JustWatchAdvancementTest.kt` if it keeps the new lifecycle tests
clear. Use an injectable/fixed clock or a stable synthetic schedule covering the
actual test clock; do not use sleeps or real Stash data.

- Multiple forward skips followed by two consecutive natural completions produce
  D → E → F while live is still A. Same channel throughout; successors load at zero.
- Exercise D-pad mid-video skips and media-next start-of-video skips.
- Backward skip and restart/seek/pause state continue from the selected airing.
- Unshifted completion still emits a live tune; Back to live clears the shift and
  returns to the current program even while a successor lookup is suspended.
- Skip requests rejected during tuning leave `timeShifted` unchanged.
- No resolved program, tuning/error phase, stale generation, and duplicate end
  callbacks never issue extra loads. Include the playing-false/phase-Playing case.
- A single-scene legacy loop advances its interval even though scene ids repeat.
- Published custom and network channels advance across a cached-page boundary by
  querying the successor's timestamp, without falling back to a legacy lineup.
- An empty published successor returns to live on the same channel; an exception
  preserves unavailable/failure semantics. Model empty publication as `emptyList`,
  not `null`: `null` means a legacy schedule in `BroadcastSchedule.Dependencies`.
- Keep the existing failure-recovery interleaving tests green. Do not change the
  server-side continuing ledger to reflect local skips or local completions.

Run focused tests, formatting checks appropriate to changed Kotlin files, and
`:app:assembleDebug`. The initial focused command is:

```sh
cd /home/shahram/dev/StashAppAndroidTV
./gradlew :app:testDebugUnitTest \
  --tests 'com.github.damontecres.stashapp.features.justwatch.JustWatchReducerTest' \
  --tests 'com.github.damontecres.stashapp.features.justwatch.BroadcastScheduleTest' \
  --tests 'com.github.damontecres.stashapp.features.justwatch.PublishedScheduleTest' \
  --tests 'com.github.damontecres.stashapp.ui.pages.justwatch.JustWatchRecoveryTest' \
  --console=plain
```

Include the new advancement class in that command once created. Inspection-time
baseline: **87 tests passed** (45 reducer, 27 broadcast schedule, 9 published
schedule, 6 recovery); build successful. These passes establish the existing
baseline and explicitly do not validate the proposed fix.

For hardware verification, follow the Android repository's current AGENTS.md:
use the dedicated `.105` Fire TV stick, USB serial `G072JN0734330EBH` or WiFi
`192.168.8.105:5555`. Use the emulator only as an allowed fallback. Use dev Stash
on port 9998 and a neutral/synthetic short-video fixture to reproduce multiple
skips and natural completion quickly. Check both the normal player backend and
MPV if available; verify actual end callbacks, zero successor position, and the
Back to live action. Record only what was actually tested; report unavailable
hardware/backends as limitations. No production UI, captures, app launches, or
interactive debugging. Production deployment is outside this implementation task.

## Delivery and rollback

Deliver a focused Android change, regression evidence, updated behavior comments,
and a debug APK if built. Report device/backend coverage and any pre-existing build
failure accurately. No plugin migration, contract bump, scheduler run, or production
deployment is required. Reverting the Android change or reinstalling the previous
APK restores the previous behavior; there is no persistent data migration.
