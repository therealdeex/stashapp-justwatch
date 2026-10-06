/* Channel Studio — OPTIONAL owner JavaScript (demonstrates the documented
 * plugin hook). Presentation/notification ONLY: the hook deliberately has
 * NO mutation surface, so a snippet can never bypass the draft + explicit
 * Apply flow.
 *
 * Install:  Stash → Settings → Interface → Custom JavaScript.
 * APPEND this file's contents below any JavaScript you already have there
 * (do not replace your existing snippets), then Save. See extras/README.md.
 *
 * Remove:   delete these lines again from the same box and Save. The hook
 * runs each extension's cleanup on unregister and when you leave the
 * Channel Studio route, so nothing leaks.
 *
 * Contract (versioned, feature-detected, route-scoped):
 *   window.JWChannelStudio = {
 *     version: 1,
 *     registerExtension(name, { version, setup })  // setup(ctx) -> cleanup
 *   }
 *   ctx = {
 *     route,            // "/plugins/stash-justwatch"
 *     on(event, cb),    // "staged" | "applied" | "selection"  (views only)
 *     getViewState(),   // read-only snapshot of the page state
 *     announce(text),   // polite screen-reader announcement
 *   }
 * Rules for snippets: no second React, no external dependencies, no
 * React/PluginApi internals, no direct storage writes, no synthetic
 * clicking to trigger Apply, and always return a cleanup function.
 */

(function () {
  "use strict";

  // Feature-detect + version gate: stay silent (and harmless) when the
  // plugin or the hook is absent or older.
  var hook = window.JWChannelStudio;
  if (!hook || typeof hook.registerExtension !== "function" || hook.version < 1) return;

  // registerExtension is idempotent per name: if Stash re-injects custom
  // JavaScript (settings save, navigation), the previous registration's
  // cleanup runs BEFORE setup runs again.
  hook.registerExtension("channel-studio-owner-demo", {
    version: 1,
    setup: function (ctx) {
      // Only act on the Channel Studio route.
      if (ctx.route !== "/plugins/stash-justwatch") return function () {};

      // Example 1 — log arrangement milestones (view notifications only).
      var offStaged = ctx.on("staged", function (info) {
        console.info("[channel-studio] arrangement staged:", info && info.label);
      });
      var offApplied = ctx.on("applied", function (info) {
        console.info("[channel-studio] applied at r" + (info && info.revision));
      });
      var offSelection = ctx.on("selection", function (info) {
        // info.count — the current selection size (read-only).
        void info;
      });

      // Example 2 — a polite announcement when the page finishes booting.
      var state = ctx.getViewState();
      if (state && state.channelCount != null) {
        ctx.announce("Channel Studio ready — " + state.channelCount + " channels at r" + state.revision + ".");
      }

      // Example 3 — mark the page when an arrangement is staged, so your
      // Custom CSS can restyle it if you like. Purely presentational.
      function syncBadge() {
        var s = ctx.getViewState();
        document.body.classList.toggle("jw-owner-has-staged", !!(s && s.stagedArrangement));
      }
      var offStaged2 = ctx.on("staged", syncBadge);
      var offApplied2 = ctx.on("applied", syncBadge);
      syncBadge();

      // Cleanup (runs on unregister, re-injection, and route leave).
      return function () {
        offStaged(); offApplied(); offSelection();
        offStaged2(); offApplied2();
        document.body.classList.remove("jw-owner-has-staged");
      };
    },
  });
})();
