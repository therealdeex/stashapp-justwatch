// Just Watch — Channel Studio (the owner's channel-library editor).
//
// Registers /plugins/stash-justwatch through Stash's PluginApi route surface
// and edits the plugin's channel library (My Channels 1-99 + the network tier
// 100-899) through the channel-curation operations:
//
//   GetChannelLibrary / GetChannelDefinition / GetChannelHistory     reads
//   PreviewChannelPool                                        draft preview
//   PreviewChannelArrangement                     arrangement plans (sync)
//   ValidateChannelChanges                                     pre-flight
//   ApplyChannelChanges (TASK)  ->  GetChannelApplyResult    THE write path
//
// Design invariants (supersede the v0.7 autosave editor):
//   * NO autosave. Every edit lands in a per-channel draft (sessionStorage);
//     nothing reaches the server without an explicit Apply.
//   * Writes dispatch through Stash's job queue (runPluginTask "Apply Channel
//     Changes"). A queued job id is NOT success: the client polls
//     GetChannelApplyResult with a client-generated requestId until the
//     durable receipt resolves. RECEIPTS ARE THE TRUTH.
//   * The requestId is generated once per SUBMIT and reused verbatim on retry
//     after a transport failure — the server replays the stored receipt
//     idempotently. A changed draft (or a changed expectedRevision) means a
//     NEW requestId: the server rejects a replayed id with different content.
//   * revision_conflict keeps the draft and offers "Apply onto r{N}"; it never
//     silently reloads or overwrites.
//   * XSS: no raw-HTML injection anywhere; server strings are React children.
//   * No second React: React comes from PluginApi; no extra libraries.

(() => {
  const api = window.PluginApi;
  if (!api || !api.React || !api.register || typeof api.register.route !== "function") {
    console.warn("[stash-justwatch] PluginApi with React + register.route unavailable; UI not registered.");
    return;
  }

  const React = api.React;
  const h = React.createElement;
  const { useState, useEffect, useRef, useCallback, useMemo } = React;

  // ------------------------------------------------------------------
  // Constants
  // ------------------------------------------------------------------

  const PLUGIN_ID = "stash-justwatch";
  const ROUTE_PATH = "/plugins/stash-justwatch";
  // Legacy path kept registered: early bookmarks/links used it. The server
  // only serves the app shell for /plugins/* (its /plugin mount owns assets
  // and /javascript), so the plural path is the only deep-linkable one.
  const LEGACY_ROUTE_PATH = "/plugin/stash-justwatch";
  const ASSET_BASE = "/plugin/stash-justwatch/assets/";

  const ROTATION_SIZE = 50; // the on-air loop bound (mirrors the contract)
  const ROTATION_SCAN_LIMIT = 1000;
  const PREVIEW_DEBOUNCE_MS = 350;
  const SEARCH_DEBOUNCE_MS = 250;
  const APPLY_POLL_INTERVAL_MS = 2000;
  // Wall-clock budget for waiting on the durable receipt. Iteration caps are
  // wrong for this: Chrome intensively throttles CHAINED timers (each sleep
  // scheduled from inside the previous one) in hidden/occluded/backgrounded
  // tabs down to ~1 wake per minute, so a capped loop can sit "Applying…" for
  // many minutes while the receipt has been durable for seconds. The budget
  // is measured against Date.now(), and returning to the tab wakes an
  // immediate poll (see pollApplyReceipt).
  const APPLY_POLL_BUDGET_MS = 120000;
  const REFRESH_POLL_MS = 4000;
  const REFRESH_POLL_MAX = 30; // ~2 min of honest pending state, then rest
  const MAX_NAME_LEN = 60;
  const GRAPHQL_INT_MAX = 2147483647; // Stash's Int is signed 32-bit
  const BANDS = { ch: [1, 99], net: [100, 899] };
  const DRAFT_COUNT_CAP = 100; // chips shown per facet line
  const CHIP_FILTER_MIN = 12;  // facet lists longer than this get a filter box
  const CLEAR_CONFIRM_MIN = 10; // bulk removals larger than this ask first

  const SORTS = [
    { key: "shuffle", label: "Shuffle" },
    { key: "newest", label: "Newest" },
    { key: "oldest", label: "Oldest" },
    { key: "top_rated", label: "Top rated" },
    { key: "longest", label: "Longest" },
    { key: "shortest", label: "Shortest" },
  ];

  // Same brand palette as before; the TV renders these as number-cell tints.
  const PALETTE = [
    "#E91E63", "#D32F2F", "#F57C00", "#F9A825", "#AFB42B", "#388E3C",
    "#00897B", "#00ACC1", "#3949AB", "#5E35B1", "#7B1FA2", "#455A64",
  ];

  // The shared glyph set from the plugin contract — the ONLY glyphs the server
  // accepts (`channel.glyph` must be in this set or null).
  const GLYPH_POOL = [
    "\ue131", "\uf004", "\uf005", "\uf007", "\uf008", "\uf015", "\uf030",
    "\uf03d", "\uf043", "\uf06b", "\uf06c", "\uf06d", "\uf06e", "\uf072",
    "\uf07a", "\uf084", "\uf091", "\uf0a1", "\uf0c4", "\uf0c5", "\uf0d6",
    "\uf0eb", "\uf0f1", "\uf111", "\uf11b", "\uf130", "\uf135", "\uf14e",
    "\uf182", "\uf186", "\uf19d", "\uf1b0", "\uf1b9", "\uf1da", "\uf1e0",
    "\uf1fc", "\uf236", "\uf256", "\uf2cc", "\uf2e7", "\uf3a5", "\uf44b",
    "\uf44e", "\uf4d8", "\uf4e3", "\uf508", "\uf52d", "\uf52e", "\uf54c",
    "\uf56d", "\uf5bb", "\uf5e4", "\uf6de", "\uf6fa", "\uf753", "\uf773",
    "\uf8d7", "\uf8d9",
  ];

  const FACET_KEYS = ["tags", "tagsAny", "excludeTags", "performers", "performersAny",
    "excludePerformers", "studios", "studiosAny", "excludeStudios"];
  // A facet cannot be both ANY and ALL (Stash has one criterion per facet);
  // ids + a scene-count rule DO compose (they AND).
  const EXCLUSIVE_FACETS = [["tags", "tagsAny"], ["performers", "performersAny"], ["studios", "studiosAny"]];

  const DIAL_ITEM_HEIGHT = 44;
  const PICKER_ITEM_HEIGHT = 36;

  // The one editor power shortcut, advertised next to the Apply button.
  const APPLY_SHORTCUT = (() => {
    try { return /mac/i.test(navigator.platform) ? "⌘⏎" : "Ctrl+Enter"; } catch (e) { return "Ctrl+Enter"; }
  })();

  const TERMINAL_STATUSES = new Set([
    "FINISHED", "COMPLETE", "COMPLETED", "FAILED", "CANCELLED", "CANCELED", "REMOVED", "ABORTED",
  ]);

  const TOAST_ICONS = { ok: "✓", err: "!" };

  // Loading skeleton: a card-shaped block of shimmer lines (the global
  // prefers-reduced-motion query stills the shimmer).
  function skelCard(n) {
    const widths = ["w60", "w80", "", "w40", "", "w80", ""];
    return h("div", { className: "jw-skel-card", "aria-hidden": "true" },
      Array.from({ length: n }, (_, i) => h("div", {
        key: i, className: "jw-skel jw-skel-line " + widths[i % widths.length],
      })));
  }

  // Keyboard legend rows for the "Keyboard shortcuts…" dialog.
  const SHORTCUT_ROWS = [
    [["/"], "Search channels"],
    [[APPLY_SHORTCUT], "Apply the current draft"],
    [["Esc"], "Close dialogs; exit chip manage mode"],
    [["Enter", "Space"], "Select the focused channel in the dial"],
    [["Shift", "click"], "Select a range of channels (in Select channels mode)"],
    [["Ctrl", "A"], "Select all matching channels (dial focused, Select mode)"],
    [["Ctrl", "Z"], "Undo the last staged arrangement (before Apply)"],
    [["Ctrl", "Shift", "Z"], "Redo a staged arrangement"],
  ];

  // ------------------------------------------------------------------
  // Browser diagnostics: a bounded, structured event buffer.
  //
  // Every entry is whitelisted scalar fields (op/stage/httpStatus/ms/code/
  // requestId/channelId) — NO free text, names, search text, paths, keys or
  // bodies — so "Copy error details" / "Download diagnostics" exports are
  // safe by construction and work fully offline (the buffer is local).
  // Server-side correlation happens through the requestId + timestamps.
  // ------------------------------------------------------------------

  const DIAG_LIMIT = 250;
  const diagBuffer = [];

  function diagEvent(type, fields) {
    const entry = Object.assign({ ts: new Date().toISOString(), type: String(type) }, fields || {});
    diagBuffer.push(entry);
    if (diagBuffer.length > DIAG_LIMIT) diagBuffer.splice(0, diagBuffer.length - DIAG_LIMIT);
  }

  function diagScrub(text) {
    // One-line, capped, path/URL/credential-stripped — for unexpected window
    // errors only; the audit requires sanitizing values even in messages.
    return String(text == null ? "" : text)
      .replace(/[?&](apikey|token|key)=[^\s&]+/gi, "$1=[redacted]")
      .replace(/(https?:)?\/\/\S+/g, "[url]")
      .replace(/\/[\w.\-]+\/[\w.\-\/]+/g, "[path]")
      .slice(0, 160);
  }

  function diagBundle(context) {
    return JSON.stringify(Object.assign({
      generatedAt: new Date().toISOString(),
      plugin: PLUGIN_ID,
      route: (window.location && window.location.pathname) || "",
      context: context || {},
      events: diagBuffer.slice(),
    }, null), null, 2);
  }

  function downloadDiagnostics() {
    const blob = new Blob([diagBundle({ trigger: "manual" })], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "channel-studio-diagnostics.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(() => true, () => legacyCopy(text));
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) { return false; }
  }

  window.addEventListener("error", (e) => {
    diagEvent("window_error", { stage: "window", code: diagScrub(e.message || "error") });
  });
  window.addEventListener("unhandledrejection", (e) => {
    diagEvent("unhandled_rejection", {
      stage: "window",
      code: diagScrub((e.reason && e.reason.message) || e.reason || "rejection"),
    });
  });

  // ------------------------------------------------------------------
  // Transport
  // ------------------------------------------------------------------

  // Session cookie is the normal auth; when the page was opened with
  // ?apikey=… (a bookmark/automation flow), reuse that key for same-origin
  // GraphQL too and mirror it where Stash's own SPA keeps it.
  const API_KEY = (() => {
    try {
      const fromUrl = new URLSearchParams(window.location.search).get("apikey");
      if (fromUrl) {
        try { if (!window.localStorage.getItem("apikey")) window.localStorage.setItem("apikey", fromUrl); } catch (e) { /* private mode */ }
        return fromUrl;
      }
      return window.localStorage.getItem("apikey") || "";
    } catch (e) { return ""; }
  })();

  function gqlHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (API_KEY) headers.Apikey = API_KEY;
    return headers;
  }

  async function gql(query, variables) {
    const mode = variables && variables.args && variables.args.mode
      ? String(variables.args.mode)
      : (variables && variables.name ? "task:" + String(variables.name) : "graphql");
    const started = Date.now();
    let httpStatus = 0;
    let outcome = "ok";
    const fail = (message, stage, status) => {
      const err = new Error(message);
      err.stage = stage;
      if (status != null) err.status = status;
      diagEvent("transport", { op: mode, httpStatus, ms: Date.now() - started, outcome });
      throw err;
    };
    let resp;
    try {
      resp = await fetch("/graphql", {
        method: "POST",
        headers: gqlHeaders(),
        credentials: "same-origin",
        body: JSON.stringify({ query, variables: variables || {} }),
      });
    } catch (e) {
      outcome = "network";
      if (e && e.stage) throw e;
      fail("Could not reach Stash (network failure)", "network");
    }
    httpStatus = resp.status;
    if (!resp.ok) {
      outcome = "http_" + resp.status;
      fail("Stash answered HTTP " + resp.status, "http", resp.status);
    }
    let body;
    try {
      body = await resp.json();
    } catch (e) {
      outcome = "malformed";
      fail("Stash returned a non-JSON response (HTTP " + resp.status + ")", "envelope", resp.status);
    }
    if (body.errors && body.errors.length) {
      outcome = "graphql_error";
      fail(body.errors[0].message || "GraphQL error", "graphql");
    }
    diagEvent("transport", { op: mode, httpStatus, ms: Date.now() - started, outcome });
    return body.data;
  }

  // One Map variable carries the whole args envelope; the plugin reads
  // args["mode"] and treats absent keys as "not provided".
  async function runOp(mode, args) {
    const data = await gql(
      "mutation JwOp($pid: ID!, $args: Map) { runPluginOperation(plugin_id: $pid, args: $args) }",
      { pid: PLUGIN_ID, args: Object.assign({ mode }, args || {}) },
    );
    return data.runPluginOperation;
  }

  async function runTask(taskName, args) {
    const flat = {};
    Object.keys(args || {}).forEach((k) => { flat[k] = String(args[k]); });
    const data = await gql(
      'mutation JwTask($name: String!, $a: Map!) { runPluginTask(plugin_id: "stash-justwatch", task_name: $name, args_map: $a) }',
      { name: taskName, a: flat },
    );
    return data.runPluginTask; // job id — never treated as success by itself
  }

  // A queued task is not an outcome. Poll the plugin's durable receipt store
  // until this requestId resolves; "unknown" past the budget is reported, not
  // invented — the caller keeps its draft and may resubmit the SAME id.
  //
  // The wait must survive timer throttling: browsers starve chained timers in
  // hidden/occluded/backgrounded pages (down to ~1 wake per minute), so the
  // loop is bounded by a wall-clock budget rather than an iteration count,
  // and a visibilitychange/focus/pageshow flip polls IMMEDIATELY — coming
  // back to the tab resolves a receipt that has been durable for minutes.
  async function pollApplyReceipt(requestId) {
    const started = Date.now();
    let wake = null;
    const wakeNow = () => { if (wake) { const w = wake; wake = null; w(); } };
    const onWake = () => wakeNow();
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", onWake);
      window.addEventListener("focus", onWake);
      window.addEventListener("pageshow", onWake);
    }
    try {
      for (let i = 0; ; i++) {
        const remain = APPLY_POLL_BUDGET_MS - (Date.now() - started);
        if (remain <= 0) break;
        await new Promise((resolve) => {
          const timer = setTimeout(() => done(), Math.min(i === 0 ? 700 : APPLY_POLL_INTERVAL_MS, remain));
          function done() { clearTimeout(timer); wake = null; resolve(); }
          wake = done;
        });
        let receipt = null;
        try {
          receipt = await runOp("GetChannelApplyResult", { requestId });
        } catch (e) {
          continue; // transient query failure: the task may still be running
        }
        if (receipt && receipt.status && receipt.status !== "unknown") return receipt;
      }
    } finally {
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("visibilitychange", onWake);
        window.removeEventListener("focus", onWake);
        window.removeEventListener("pageshow", onWake);
      }
    }
    return {
      requestId, status: "unknown",
      message: "the server never reported a result for this request — applying again with the same draft reuses the same requestId",
    };
  }

  // THE write path: task submit + receipt correlation. `onSubmitted` fires
  // once the task was ACCEPTED by the queue (before polling) so callers can
  // distinguish "submitting" from "polling" in the lifecycle UI.
  async function applyChannelChanges(requestId, expectedRevision, ops, onSubmitted) {
    await runTask("Apply Channel Changes", {
      mode: "ApplyChannelChanges",
      requestId,
      expectedRevision: String(expectedRevision),
      ops: JSON.stringify(ops),
    });
    if (onSubmitted) { try { onSubmitted(); } catch (e) { /* lifecycle hint only */ } }
    return pollApplyReceipt(requestId);
  }

  // ------------------------------------------------------------------
  // Arrangement seam (frozen contract 2026-10-05, decisions 1-4):
  // ONE sync preview op with an intent discriminated union; the server is
  // the ONLY planner — the browser renders authoritative plans and freezes
  // the server-emitted `packet`. PHASE-4 BRIDGE: this helper is the single
  // place that knows the wire shape; if the landed op takes nested maps
  // directly instead of JSON-string fields, change ONLY here.
  // ------------------------------------------------------------------
  async function previewChannelArrangement(req) {
    const args = {
      expectedRevision: req.expectedRevision,
      correlationToken: String(req.correlationToken),
      intent: JSON.stringify(req.intent),
    };
    if (req.explain) args.explain = true;
    if (req.overlays && req.overlays.length) args.channels = JSON.stringify(req.overlays);
    // Pending (uncommitted) groups: the planner validates them and emits
    // their group.put ops FIRST in the packet, so ONE Apply creates the
    // group and assigns/places channels (frozen interface 2026-10-06).
    if (req.groups && req.groups.length) args.groups = JSON.stringify(req.groups);
    return runOp("PreviewChannelArrangement", args);
  }

  function newCorrelationToken() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return "org-" + window.crypto.randomUUID();
    }
    return "org-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e9).toString(36);
  }

  // Capability handshake (features.arrangement gates the organize surface).
  // Absent/failed -> arrangement tools explain themselves; basic editing
  // (put/create/move/patch, groups) keeps working unchanged.
  async function fetchCapabilities() {
    try {
      const caps = await runOp("Capabilities");
      return caps && typeof caps === "object" ? caps : null;
    } catch (e) {
      diagEvent("capabilities", { outcome: "unavailable" });
      return null;
    }
  }

  // Tab-local submit mutex: bulk Applies and arrangement Applies never
  // overlap SUBMISSIONS from this tab — a second Apply waits for the first
  // RECEIPT, so expectedRevision is never guessed (frozen contract
  // decision 6: one coordinator, explicit scope).
  let submitChain = Promise.resolve();
  function enqueueSubmit(fn) {
    const run = submitChain.then(() => fn());
    submitChain = run.catch(() => {});
    return run;
  }

  let idSeq = 0;
  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return "req-" + window.crypto.randomUUID();
    }
    return "req-" + Date.now().toString(36) + "-" + (idSeq++).toString(36) + "-" + Math.floor(Math.random() * 1e6).toString(36);
  }

  function newTempId() {
    const bytes = new Uint8Array(4);
    (window.crypto || { getRandomValues: (b) => b.forEach((_, i) => { b[i] = (Math.random() * 256) | 0; }) })
      .getRandomValues(bytes);
    return "temp-" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function newGroupId() {
    // Server-side group ids look like grp_<8 lowercase hex>; a
    // client-generated id lets group.put / channels.move reference a group
    // created in the same transaction (anything else is rejected
    // bad_group_id).
    const bytes = new Uint8Array(4);
    (window.crypto || { getRandomValues: (b) => b.forEach((_, i) => { b[i] = (Math.random() * 256) | 0; }) })
      .getRandomValues(bytes);
    return "grp_" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // ------------------------------------------------------------------
  // Entity names: resolved from picker/lookup queries, cached per session.
  // Unknown ids render as "#id" (null = looked up and gone from Stash).
  // ------------------------------------------------------------------

  const ENTITY_NAMES = { tag: new Map(), performer: new Map(), studio: new Map() };

  function entityName(kind, id) {
    const m = ENTITY_NAMES[kind];
    if (!m || !m.has(String(id))) return "#" + id;
    return m.get(String(id)) || "#" + id;
  }

  async function searchEntities(kind, q) {
    const filter = { per_page: 100, sort: "scenes_count", direction: "DESC" };
    if (q) filter.q = q;
    const queries = {
      tag: ["query($f: FindFilterType!) { findTags(filter: $f) { tags { id name scene_count } } }", (d) => ((d.findTags || {}).tags || [])],
      performer: ["query($f: FindFilterType!) { findPerformers(filter: $f) { performers { id name scene_count } } }", (d) => ((d.findPerformers || {}).performers || [])],
      studio: ["query($f: FindFilterType!) { findStudios(filter: $f) { studios { id name scene_count } } }", (d) => ((d.findStudios || {}).studios || [])],
    };
    const [queryString, pick] = queries[kind];
    const rows = pick(await gql(queryString, { f: filter }));
    rows.forEach((r) => ENTITY_NAMES[kind].set(String(r.id), r.name));
    return rows.map((r) => ({ id: String(r.id), name: r.name, count: r.scene_count }));
  }

  // Resolve display names for already-selected ids (chips), bounded.
  const resolveSeq = { tag: 0, performer: 0, studio: 0 };
  async function resolveEntityNames(kind, ids) {
    const m = ENTITY_NAMES[kind];
    const missing = [...new Set(ids.map(String))].filter((id) => !m.has(id)).slice(0, 60);
    if (!missing.length) return;
    const seq = ++resolveSeq[kind];
    const singles = {
      tag: "query($id: ID!) { findTag(id: $id) { name } }",
      performer: "query($id: ID!) { findPerformer(id: $id) { name } }",
      studio: "query($id: ID!) { findStudio(id: $id) { name } }",
    };
    await Promise.all(missing.map(async (id) => {
      try {
        const d = await gql(singles[kind], { id });
        const node = d.findTag || d.findPerformer || d.findStudio;
        m.set(id, (node && node.name) || null);
      } catch (e) {
        m.set(id, null);
      }
    }));
    if (seq === resolveSeq[kind]) { /* callers re-render on their own tick */ }
  }

  // ------------------------------------------------------------------
  // Glyph plumbing: shared FA codepoints rendered via Stash's bundled
  // FontAwesome (the Icon component is react-fontawesome — SVG, not a font).
  // ------------------------------------------------------------------

  const fasKeyByCodepoint = new Map(); // codepoint -> FontAwesomeSolid key
  function resolveGlyphs() {
    const FAS = (api.libraries && api.libraries.FontAwesomeSolid) || {};
    fasKeyByCodepoint.clear();
    Object.keys(FAS).forEach((key) => {
      if (/^fa\d+$/.test(key)) return; // indexed duplicates; prefer named keys
      const def = FAS[key];
      const icon = def && def.icon;
      const unicode = Array.isArray(icon) ? icon[3] : (Array.isArray(def) ? def[3] : null);
      if (typeof unicode === "string" && /^[0-9a-f]+$/i.test(unicode)) {
        const cp = parseInt(unicode, 16);
        if (!fasKeyByCodepoint.has(cp)) fasKeyByCodepoint.set(cp, key);
      }
    });
  }

  function glyphName(codepoint) {
    const key = fasKeyByCodepoint.get(codepoint.codePointAt(0));
    if (!key) return "Glyph";
    const words = key.replace(/^fa/, "").replace(/([a-z])([A-Z])/g, "$1 $2");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function isComponent(x) {
    return !!x && (typeof x === "function" || typeof x === "object" && !!x.$$typeof);
  }

  // UI-chrome icons (NOT channel glyphs): render Stash's bundled FA when both
  // the key and the Icon component exist, else `fallback` (default nothing) —
  // every use site must stay complete without an icon.
  function FaIcon({ name, size, className, fallback }) {
    const IconCmp = api.components && api.components.Icon;
    const FAS = (api.libraries && api.libraries.FontAwesomeSolid) || {};
    const def = FAS[name];
    if (!def || !isComponent(IconCmp)) {
      return fallback ? h("span", { className: className || "" }, fallback) : null;
    }
    return h(IconCmp, {
      icon: def,
      className: "jw-fa " + (className || ""),
      style: { fontSize: (size || 13) + "px" },
    });
  }

  function Glyph({ codepoint, size, color, className }) {
    const IconCmp = api.components && api.components.Icon;
    const key = codepoint ? fasKeyByCodepoint.get(codepoint.codePointAt(0)) : null;
    const FAS = (api.libraries && api.libraries.FontAwesomeSolid) || {};
    if (key && isComponent(IconCmp)) {
      return h(IconCmp, {
        icon: FAS[key],
        className: "jw-glyph " + (className || ""),
        style: { fontSize: (size || 14) + "px", color: color || "#fff" },
      });
    }
    return h("span", {
      className: "jw-glyph " + (className || ""),
      style: { fontSize: (size || 14) + "px", color: color || "#fff", lineHeight: 1 },
    }, codepoint || "");
  }

  // Brand tile: the FA glyph when the channel carries one; otherwise the
  // channel number stands in (glyph is optional in the library).
  function GlyphTile({ codepoint, color, size, fallback }) {
    const s = size || 28;
    const inner = codepoint
      ? h(Glyph, { codepoint, size: Math.round(s * 0.5) })
      : h("span", { style: { fontSize: Math.round(s * 0.34) + "px", fontWeight: 700 } }, fallback != null ? String(fallback) : "");
    return h("div", {
      className: "jw-glyph-tile",
      style: {
        width: s + "px", height: s + "px", borderRadius: Math.max(6, s / 5) + "px",
        background: color || "#455A64",
      },
    }, inner);
  }

  // ------------------------------------------------------------------
  // Pure helpers
  // ------------------------------------------------------------------

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function deepEqual(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function idsOf(v) {
    return Array.isArray(v) ? v.map(String).filter((x) => /^\d+$/.test(x)) : [];
  }

  function sortedIds(list) {
    return [...new Set(list.map(String))].sort((a, b) => Number(a) - Number(b));
  }

  // Canonical form of a criteria/filter source: every list key present (empty
  // when unused) so "the editor touched nothing" stays equal to the stored
  // record. Mirrors the server's canonical stored source.
  function canonicalSource(source) {
    const s = source || {};
    if (s.type !== "filter" && s.type !== "criteria") return s;
    const out = { type: s.type };
    for (const k of FACET_KEYS) out[k] = idsOf(s[k]);
    if (s.date && (s.date.from || s.date.to)) out.date = { from: s.date.from || "", to: s.date.to || "" };
    if (s.duration && (s.duration.min || s.duration.max)) {
      out.duration = Object.assign(
        {},
        s.duration.min ? { min: s.duration.min } : {},
        s.duration.max ? { max: s.duration.max } : {},
      );
    }
    if (s.createdAt && s.createdAt.withinDays) out.createdAt = { withinDays: s.createdAt.withinDays };
    for (const k of ["studioSceneCount", "performerSceneCount"]) {
      const spec = s[k];
      if (spec && (spec.min != null || spec.max != null)) {
        out[k] = Object.assign(
          {},
          spec.min != null ? { min: spec.min } : {},
          spec.max != null ? { max: spec.max } : {},
        );
      }
    }
    if (s.q) out.q = s.q;
    return out;
  }

  function sourcesEquivalent(a, b) {
    return JSON.stringify(canonicalSource(a)) === JSON.stringify(canonicalSource(b));
  }

  // The ONE draft→wire serializer: preview, Validate, Apply and the pending
  // snapshot's retry identity ALL describe the authored rules through this
  // function, so the client never submits a shape the server must reject for
  // structural reasons (audit E1). Rules:
  //   * genuinely empty facet arrays are OMITTED — the server rejects a
  //     present-but-empty list (the ANY/ALL toggle and last-chip removal
  //     both leave [] behind in the editing model);
  //   * bounds are PRESENCE-based: an explicit 0 is a real bound, blank is
  //     absence (audit E8);
  //   * q is trimmed and dropped when blank.
  // canonicalSource stays COMPARISON-ONLY (it deliberately fills empty keys
  // so an untouched editor equals the stored record — never a wire form).
  function toWireSource(source) {
    const s = source || {};
    if (s.type !== "filter" && s.type !== "criteria") return s;
    const out = { type: s.type };
    for (const k of FACET_KEYS) {
      const v = idsOf(s[k]);
      if (v.length) out[k] = v;
    }
    if (s.date && (s.date.from || s.date.to)) {
      out.date = { from: s.date.from || "", to: s.date.to || "" };
    }
    if (s.duration && typeof s.duration === "object") {
      const spec = {};
      if (s.duration.min != null) spec.min = s.duration.min;
      if (s.duration.max != null) spec.max = s.duration.max;
      if (spec.min != null || spec.max != null) out.duration = spec;
    }
    if (s.createdAt && s.createdAt.withinDays != null) {
      out.createdAt = { withinDays: s.createdAt.withinDays };
    }
    for (const k of ["studioSceneCount", "performerSceneCount"]) {
      const v = s[k];
      if (v && typeof v === "object") {
        const spec = {};
        if (v.min != null) spec.min = v.min;
        if (v.max != null) spec.max = v.max;
        if (spec.min != null || spec.max != null) out[k] = spec;
      }
    }
    if (typeof s.q === "string" && s.q.trim()) out.q = s.q.trim();
    return out;
  }

  function toWireChannel(channel) {
    const c = clone(channel);
    c.source = toWireSource(c.source);
    delete c.pendingGroupName; // client-side sentinel plumbing, never a wire field
    return c;
  }

  // Three-way rebase of a local draft against fresh server state (audit C8):
  //   base  = the acknowledged definition the draft was created from
  //   fresh = the server's current record
  //   draft = the local working draft
  // Field-level: untouched locally -> take the server's value (an external
  // edit to another field merges in silently); touched locally AND unchanged
  // on the server -> keep the local edit; changed on BOTH sides -> keep the
  // local value and name the field as a conflict the owner must review.
  // source and programming compare canonically (absent == empty).
  function rebaseDraft(base, fresh, draft) {
    if (!base || !fresh || !draft) return { draft, conflicts: [] };
    if (deepEqual(base, fresh)) return { draft, conflicts: [] };
    const conflicts = [];
    const keys = new Set([...Object.keys(base), ...Object.keys(fresh), ...Object.keys(draft)]);
    const out = clone(draft);
    const fieldEqual = (a, b, k) => {
      if (k === "source") return sourcesEquivalent(a && a[k], b && b[k]);
      if (k === "programming") {
        return JSON.stringify(canonicalProgramming(a && a[k])) === JSON.stringify(canonicalProgramming(b && b[k]));
      }
      return deepEqual(a ? a[k] : undefined, b ? b[k] : undefined);
    };
    for (const k of keys) {
      if (["id", "kind", "seed", "provenance"].includes(k)) {
        // server-owned identity: always the fresh value
        out[k] = fresh[k];
        continue;
      }
      const localTouched = !fieldEqual(draft, base, k);
      const serverChanged = !fieldEqual(fresh, base, k);
      if (localTouched && serverChanged) {
        conflicts.push(k);
        out[k] = draft[k]; // keep the local choice; surface it for review
      } else if (!localTouched) {
        out[k] = clone(fresh[k]);
      }
    }
    return { draft: out, conflicts };
  }

  // Mirrors the server's programming policy clamp so a field the user touched
  // and reverted compares clean against a shorter stored policy. newShare
  // (continuing networks) passes through — the server preserves it losslessly.
  function canonicalProgramming(raw) {
    const p = raw && typeof raw === "object" ? raw : {};
    const int = (key, def, lo, hi) => {
      const v = p[key] != null ? p[key] : def;
      return typeof v === "number" ? Math.max(lo, Math.min(hi, v)) : def;
    };
    const out = {
      mode: ["fixed", "explore", "discovery", "continuing"].includes(p.mode) ? p.mode : "fixed",
      spacing: int("spacing", 0, 0, 10),
      repeatHours: int("repeatHours", 0, 0, 168),
      spotlight: ["studio", "performer"].includes(p.spotlight) ? p.spotlight : "none",
      spotlightDay: int("spotlightDay", 5, 0, 6),
      spotlightHour: int("spotlightHour", 20, 0, 23),
    };
    if (typeof p.newShare === "number") out.newShare = p.newShare;
    return out;
  }

  function fullProgramming(p) {
    return Object.assign(canonicalProgramming(p), { mode: (p && p.mode) || "fixed" });
  }

  function nextFreeNumber(channels, kind) {
    const [lo, hi] = BANDS[kind] || BANDS.net;
    const taken = new Set(channels.map((c) => c.number));
    for (let n = lo; n <= hi; n++) if (!taken.has(n)) return n;
    return null;
  }

  function pickDefaultColor(number) {
    return PALETTE[(Math.max(1, number) - 1) % PALETTE.length];
  }

  function fmtCount(n) {
    return typeof n === "number" ? n.toLocaleString("en-US") : "—";
  }

  function fmtDuration(seconds) {
    if (!seconds || seconds <= 0) return "0m";
    const s = Math.round(seconds);
    const hh = Math.floor(s / 3600);
    const mm = Math.round((s % 3600) / 60);
    return hh ? hh + "h " + mm + "m" : mm + "m";
  }

  // Minute rendering with authored precision: whole minutes read as ints,
  // sub-minute bounds keep up to two decimals instead of silently flooring
  // (mirrors criteria._fmt_minutes — E8).
  function fmtMinutes(seconds) {
    const m = Number(seconds) / 60;
    if (!Number.isFinite(m)) return "0 min";
    if (Number.isInteger(m)) return m + " min";
    return (Math.round(m * 100) / 100) + " min";
  }

  // The inverse for the minute INPUT fields: seconds -> typed text (no forced
  // rounding), or "" when the bound is absent.
  function minutesText(seconds) {
    if (seconds == null) return "";
    const m = Number(seconds) / 60;
    if (!Number.isFinite(m)) return "";
    return Number.isInteger(m) ? String(m) : String(Math.round(m * 100) / 100);
  }

  const SOURCE_LABELS = {
    savedFilter: "saved search",
    tag: "tag set",
    performer: "performer",
    studio: "studio",
    criteria: "rules",
    filter: "rules",
  };

  function describeRow(ch, groupsById) {
    const g = groupsById.get(ch.groupId);
    const bits = [g ? g.name : ""];
    bits.push(SOURCE_LABELS[ch.sourceType] || ch.sourceType || "");
    return bits.filter(Boolean).join(" · ");
  }

  // ---------- plain-language summary (ported from the approved prototype,
  // with the dynamic activity rows added) ----------

  function nameList(kind, list, cap) {
    const capN = cap == null ? 3 : cap;
    if (!list.length) return null;
    const shown = list.slice(0, capN).map((id) => entityName(kind, id));
    const rest = list.length - shown.length;
    return shown.join(", ") + (rest > 0 ? " +" + rest + " more" : "");
  }

  function summarizeLines(source) {
    const s = source || {};
    if (s.type === "savedFilter") {
      return ["Airs from the saved search linked to this channel — used verbatim, including its text query. Editing that search in Stash changes what airs."];
    }
    if (s.type === "tag") {
      const listing = idsOf(s.ids).length ? idsOf(s.ids) : idsOf([s.id]);
      return ["Scenes tagged with any of: " + (nameList("tag", listing, 8) || "—") + " (sub-tags included)."];
    }
    if (s.type === "performer") return ["Scenes featuring " + entityName("performer", s.id) + ". No other rules."];
    if (s.type === "studio") return ["Scenes from studio " + entityName("studio", s.id) + " (sub-studios included). No other rules."];
    if (s.type !== "filter" && s.type !== "criteria") return ["Airs from a " + (s.type || "unknown") + " source."];

    const active = [];
    if (idsOf(s.tags).length || idsOf(s.tagsAny).length || idsOf(s.excludeTags).length) {
      const parts = [];
      if (idsOf(s.tags).length) parts.push("tagged with ALL of: " + nameList("tag", idsOf(s.tags), 8));
      if (idsOf(s.tagsAny).length) parts.push("tagged with any of: " + nameList("tag", idsOf(s.tagsAny), 8));
      if (idsOf(s.excludeTags).length) parts.push("without any of: " + nameList("tag", idsOf(s.excludeTags), 8));
      active.push("Scenes " + parts.join(", ") + " (sub-tags included)");
    }
    if (idsOf(s.performers).length) active.push("Featuring ALL of: " + nameList("performer", idsOf(s.performers), 8));
    if (idsOf(s.performersAny).length) active.push("Featuring any of: " + nameList("performer", idsOf(s.performersAny), 8));
    if (idsOf(s.excludePerformers).length) active.push("NOT featuring: " + nameList("performer", idsOf(s.excludePerformers), 8));
    if (idsOf(s.studios).length) active.push("From studios (sub-studios included): " + nameList("studio", idsOf(s.studios), 8));
    if (idsOf(s.studiosAny).length) active.push("From any of: " + nameList("studio", idsOf(s.studiosAny), 8));
    if (idsOf(s.excludeStudios).length) active.push("NOT from: " + nameList("studio", idsOf(s.excludeStudios), 8));
    if (s.date && (s.date.from || s.date.to)) {
      active.push("Dated " + (s.date.from || "the beginning") + " to " + (s.date.to || "today"));
    }
    if (s.duration && s.duration.min != null) active.push("Running at least " + fmtMinutes(s.duration.min));
    if (s.duration && s.duration.max != null) active.push("Running at most " + fmtMinutes(s.duration.max));
    if (s.createdAt && s.createdAt.withinDays) {
      active.push("Added to the library within the last " + s.createdAt.withinDays + " days (moves with the calendar)");
    }
    for (const [facet, noun] of [["studioSceneCount", "studios"], ["performerSceneCount", "performers"]]) {
      const spec = s[facet];
      if (!spec || !(spec.min != null || spec.max != null)) continue;
      const lo = spec.min;
      const hi = spec.max;
      let cond;
      if (lo != null && hi != null) cond = "with " + lo + "–" + hi + " scenes";
      else if (hi != null) cond = "with fewer than " + hi + " scenes";
      else cond = "with " + lo + " or more scenes";
      active.push("Dynamically: any " + noun + " " + cond + " — membership updates itself as the library grows");
    }
    if (typeof s.q === "string" && s.q.trim()) active.push("Matching the text search “" + s.q.trim() + "”");
    if (!active.length) return ["No rules yet — this pool would be empty until at least one rule is on."];
    const head = active.length + " rule" + (active.length === 1 ? "" : "s") + ", combined with AND" +
      (active.length > 1 ? " — then ALL of the following must hold:" : ":");
    return [head, ...active.map((a, i) => (i + 1) + ". " + a)];
  }

  // Legacy sources (and the compiled tier's filter shape) keep their stored
  // type; the visual rows read and write the same field names.
  function isRuleSource(source) {
    return !!source && (source.type === "criteria" || source.type === "filter");
  }

  // One-way conversion of a legacy source into editable criteria. Tag sets
  // are ANY-of unions (that is their historical semantics), so they convert to
  // the any-lists; single performer/studio ids are mode-independent. Unused
  // keys stay ABSENT — the server rejects present-but-empty id lists, and the
  // canonical comparison treats absent and empty alike.
  function convertLegacySource(source) {
    const s = source || {};
    const out = { type: "criteria" };
    if (s.type === "tag") {
      const tags = sortedIds(idsOf(s.ids && s.ids.length ? s.ids : [s.id]));
      if (tags.length) out.tagsAny = tags;
    } else if (s.type === "performer") {
      const ids = sortedIds(idsOf([s.id]));
      if (ids.length) out.performersAny = ids;
    } else if (s.type === "studio") {
      const ids = sortedIds(idsOf([s.id]));
      if (ids.length) out.studiosAny = ids;
    }
    // savedFilter: the linked search's own criteria live in Stash and are not
    // copied — the converted pool starts empty and the owner rebuilds it.
    return out;
  }

  // Client-side mirror of the server's structural checks — run against the
  // WIRE source (toWireSource) so "the client says valid" and "the server
  // accepts" describe the same shape. Meaningful invalid values stay in the
  // draft (and the wire) so the user gets a typed field error instead of a
  // silent normalization.
  function ruleSourceClientErrors(source) {
    const errors = [];
    const s = source || {};
    for (const [a, b] of EXCLUSIVE_FACETS) {
      if (idsOf(s[a]).length && idsOf(s[b]).length) {
        errors.push({ field: "source." + a, code: "conflicting_rows", message: "This facet cannot be both ALL and ANY — use the toggle to move the ids into one row." });
      }
    }
    for (const k of ["studioSceneCount", "performerSceneCount"]) {
      const spec = s[k];
      if (spec && spec.min != null && spec.max != null && Number(spec.max) <= Number(spec.min)) {
        errors.push({ field: "source." + k, code: "bad_scene_count", message: "The upper bound must exceed the lower one." });
      }
    }
    if (s.duration) {
      for (const half of ["min", "max"]) {
        const v = s.duration[half];
        if (v == null) continue;
        if (typeof v !== "number" || Number.isNaN(v) || v < 0) {
          errors.push({ field: "source.duration", code: "bad_duration", message: "Durations must be 0 minutes or more." });
        } else if (v > GRAPHQL_INT_MAX) {
          errors.push({ field: "source.duration", code: "bad_duration", message: "That duration is too large — Stash accepts at most " + GRAPHQL_INT_MAX.toLocaleString() + " seconds." });
        }
      }
      if (s.duration.min != null && s.duration.max != null
          && Number(s.duration.max) < Number(s.duration.min)) {
        errors.push({ field: "source.duration", code: "bad_duration", message: "Maximum duration is below the minimum." });
      }
    }
    if (s.date && s.date.from && s.date.to && s.date.from > s.date.to) {
      errors.push({ field: "source.date", code: "bad_date", message: "The start date is after the end date." });
    }
    return errors;
  }

  function debounce(fn, ms) {
    let handle = null;
    const wrapped = (...args) => {
      if (handle) clearTimeout(handle);
      handle = setTimeout(() => { handle = null; fn(...args); }, ms);
    };
    wrapped.cancel = () => { if (handle) clearTimeout(handle); handle = null; };
    return wrapped;
  }

  // ------------------------------------------------------------------
  // Drafts: a genuine in-memory map (the source of truth) mirrored to
  // sessionStorage when it is available. Versioned, never applied
  // automatically. Keyed jw-studio-drafts-v1:<libraryId>.
  //
  // Entry shape (v1): { v: 1, draft, base, pending, savedAt }
  //   draft   — the current editable draft (typed even while a previous
  //             snapshot is applying; audit C8)
  //   base    — the LAST ACKNOWLEDGED server definition this draft was
  //             created from (null for temp channels). The three-way rebase
  //             (base vs fresh server vs draft) decides what a reload or an
  //             unrelated commit may merge — never a borrowed global revision.
  //   pending — an immutable submitted-but-unresolved Apply
  //             { requestId, expected, snapshot } that survives
  //             navigation/remount/reload (audit C8/C9).
  // ------------------------------------------------------------------

  function makeDraftStore(libraryId) {
    const key = "jw-studio-drafts-v1:" + libraryId;
    let listeners = [];
    let memory = null; // Map id -> entry; storage failures never lose drafts
    const emit = () => listeners.slice().forEach((fn) => { try { fn(); } catch (e) { /* noop */ } });
    function readAll() {
      if (memory) return Object.fromEntries(memory);
      let seeded = {};
      try {
        const parsed = JSON.parse(sessionStorage.getItem(key) || "{}");
        if (parsed && typeof parsed === "object") seeded = parsed;
      } catch (e) { seeded = {}; }
      memory = new Map(Object.entries(seeded));
      return Object.fromEntries(memory);
    }
    function writeAll(all) {
      memory = new Map(Object.entries(all));
      try { sessionStorage.setItem(key, JSON.stringify(all)); } catch (e) { /* full/private storage: the memory map IS the fallback */ }
    }
    return {
      get(id) {
        const entry = readAll()[id];
        return entry && entry.v === 1 && entry.draft ? entry : null;
      },
      put(id, draft, extra) {
        const all = readAll();
        const prev = all[id] || {};
        all[id] = Object.assign({ v: 1, savedAt: Date.now() }, prev,
                                { draft }, extra || {});
        writeAll(all);
        emit();
      },
      setExtras(id, extra) {
        const all = readAll();
        if (all[id] != null) {
          all[id] = Object.assign({}, all[id], extra);
          writeAll(all);
          emit();
        }
      },
      drop(id) {
        const all = readAll();
        if (all[id] != null) { delete all[id]; writeAll(all); emit(); }
      },
      ids() { return Object.keys(readAll()); },
      count() { return Object.keys(readAll()).length; },
      clear() {
        memory = new Map();
        try { sessionStorage.removeItem(key); } catch (e) { /* noop */ }
        emit();
      },
      subscribe(fn) { listeners.push(fn); return () => { listeners = listeners.filter((f) => f !== fn); }; },
    };
  }

  // Pending NON-editor submits (bulk/groups/patches): the request identity
  // must survive a reload so an unknown outcome resolves to exactly one
  // commit — never a duplicated resubmission under a fresh id (audit C9).
  function opsPendingKey(libraryId) { return "jw-studio-ops-pending-v1:" + libraryId; }
  function readOpsPending(libraryId) {
    try {
      const parsed = JSON.parse(sessionStorage.getItem(opsPendingKey(libraryId)) || "null");
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch (e) { return null; }
  }
  function writeOpsPending(libraryId, entry) {
    try { sessionStorage.setItem(opsPendingKey(libraryId), JSON.stringify(entry)); } catch (e) { /* memory-only session */ }
  }
  function clearOpsPending(libraryId) {
    try { sessionStorage.removeItem(opsPendingKey(libraryId)); } catch (e) { /* noop */ }
  }

  // ------------------------------------------------------------------
  // Organization draft store (frozen contract decision 5):
  // sessionStorage key jw-studio-org-v1:<libraryId> — same privacy
  // characteristics as the draft/ops-pending keys (per-tab, browser-session
  // storage). Contents: the staged arrangement (server-planned overlay +
  // bounded undo/redo), the FROZEN packet once Stage->Apply begins, and the
  // pending-receipt state. Local truth only: nothing here reaches the
  // server until Apply arrangement submits the frozen packet byte-identical.
  //
  // staged: { baseRevision, correlationToken, label, intent, response,
  //           packet: { expectedRevision, ops, requestId }, beforeAfter,
  //           tempRefs, optIns, stagedAt }
  // pending: the IMMUTABLE submitted snapshot { requestId, expected,
  //           opsDigest, correlationToken, packet, beforeAfter, tempRefs,
  //           submittedAt, state: "submitting"|"polling"|"outcome_unknown" }.
  //           It survives reload and is reconciled EXCLUSIVELY by its own
  //           requestId/packet — never by the editable staged draft (R2).
  // undo/redo: bounded stacks of prior `staged` values (null = no staging).
  // Stage/undo/redo/discard intentionally NEVER touch pending.
  // ------------------------------------------------------------------

  function makeOrgStore(libraryId) {
    const key = "jw-studio-org-v1:" + libraryId;
    const UNDO_CAP = 50;
    let listeners = [];
    let memory = null;
    const emit = () => listeners.slice().forEach((fn) => { try { fn(); } catch (e) { /* noop */ } });
    function blank() { return { v: 1, staged: null, pending: null, undo: [], redo: [] }; }
    function read() {
      if (memory) return memory;
      let parsed = null;
      try { parsed = JSON.parse(sessionStorage.getItem(key) || "null"); } catch (e) { parsed = null; }
      memory = parsed && parsed.v === 1 && typeof parsed === "object"
        ? Object.assign(blank(), parsed) : blank();
      return memory;
    }
    function write() {
      try { sessionStorage.setItem(key, JSON.stringify(memory)); } catch (e) { /* memory-only session */ }
      emit();
    }
    return {
      get() { return read(); },
      stage(staged) {
        const s = read();
        s.undo.push(s.staged);
        if (s.undo.length > UNDO_CAP) s.undo.splice(0, s.undo.length - UNDO_CAP);
        s.redo = [];
        s.staged = staged;
        // R2: a staged/undone/redone/discarded plan NEVER erases an
        // unresolved submitted packet — receipt reconciliation correlates
        // with the pending snapshot, not the editable draft.
        write();
      },
      undo() {
        const s = read();
        if (!s.undo.length) return false;
        s.redo.push(s.staged);
        s.staged = s.undo.pop();
        write();
        return true;
      },
      redo() {
        const s = read();
        if (!s.redo.length) return false;
        s.undo.push(s.staged);
        s.staged = s.redo.pop();
        write();
        return true;
      },
      discardStaged() { // explicit Discard — undoable like any other stage
        const s = read();
        if (!s.staged) return;
        s.undo.push(s.staged);
        if (s.undo.length > UNDO_CAP) s.undo.splice(0, s.undo.length - UNDO_CAP);
        s.redo = [];
        s.staged = null;
        write();
      },
      setPending(p) { const s = read(); s.pending = p; write(); },
      setPendingState(state) {
        const s = read();
        if (s.pending) { s.pending = Object.assign({}, s.pending, { state }); write(); }
      },
      clearPending() { const s = read(); s.pending = null; write(); },
      clearStagedOnly() { const s = read(); s.staged = null; write(); },
      subscribe(fn) { listeners.push(fn); return () => { listeners = listeners.filter((f) => f !== fn); }; },
    };
  }

  // ------------------------------------------------------------------
  // Organization pure helpers
  // ------------------------------------------------------------------

  // R1: ONE binding fingerprint per preview response. Stage eligibility
  // requires the bound response's fingerprint to equal the fingerprint of
  // the CURRENT controls — the instant any planning input changes the old
  // response stops being stageable, before any debounce fires.
  function previewFingerprint(parts) {
    return JSON.stringify(parts);
  }

  // The planner's canonical order enum is "number" | "name"; "alpha" is a
  // deprecated alias that must never be emitted (normalized to "name").
  function normalizeOrder(order) {
    return order === "alpha" ? "name" : (order === "name" ? "name" : "number");
  }

  // Pending (uncommitted) groups referenced by drafts — the preview/apply
  // "groups" argument (frozen interface). position is best-effort client
  // chrome (server assigns real positions in the packet's group.put).
  function pendingGroupsFromDrafts(drafts, committedGroups) {
    if (!drafts) return [];
    const committed = new Set((committedGroups || []).map((g) => g.id));
    const seen = new Set();
    const out = [];
    const maxPos = Math.max(0, ...(committedGroups || []).map((g) => g.position || 0));
    for (const id of drafts.ids()) {
      const entry = drafts.get(id);
      const d = entry && entry.draft;
      if (d && d.pendingGroupName != null && d.groupId && !committed.has(d.groupId) && !seen.has(d.groupId)) {
        const name = String(d.pendingGroupName || "").trim();
        if (!name) continue;
        seen.add(d.groupId);
        out.push({ id: d.groupId, name, position: maxPos + out.length + 1 });
      }
    }
    return out;
  }

  // R3: did the owner edit a temp draft AFTER its wire snapshot was frozen
  // into the submitted packet? number/groupId are plan-authoritative (the
  // arrangement owns them) and never count as "newer edits"; identity is
  // server-owned. Everything else (name/source/color/glyph/sort/flags/
  // programming) compared on the wire form decides.
  function draftDiffersFromSubmitted(draft, submittedChannel) {
    if (!draft || !submittedChannel) return false;
    const wire = toWireChannel(clone(draft));
    const a = Object.assign({}, wire);
    const b = Object.assign({}, submittedChannel);
    for (const k of ["id", "seed", "provenance", "number", "groupId", "pendingGroupName"]) {
      delete a[k];
      delete b[k];
    }
    return JSON.stringify(a) !== JSON.stringify(b);
  }

  // Dial preview overlay from a staged arrangement: number/group changes
  // keyed by channel id OR temp ref (plan rows for pending creations ride
  // as tempRef overlays — they never receive server ids pre-commit).
  function orgOverlayOf(staged) {
    if (!staged) return null;
    const numbers = new Map();
    const groups = new Map();
    const ba = staged.beforeAfter || {};
    for (const ref of Object.keys(ba)) {
      if (ba[ref] && Array.isArray(ba[ref].number)) numbers.set(ref, ba[ref].number[1]);
      if (ba[ref] && Array.isArray(ba[ref].group)) groups.set(ref, ba[ref].group[1]);
    }
    const temps = staged.tempRefs || {};
    for (const ref of Object.keys(temps)) {
      if (temps[ref] && temps[ref].number != null) numbers.set(ref, temps[ref].number);
      if (temps[ref] && temps[ref].groupId) groups.set(ref, temps[ref].groupId);
    }
    if (!numbers.size && !groups.size) return null;
    return { numbers, groups };
  }

  // beforeAfter map from a preview response: ref -> {number:[from,to],
  // group:[from,to]} — the reversal + bystander-rebase source of truth.
  function beforeAfterFromResponse(resp) {
    const out = {};
    for (const nc of (resp && resp.numberChanges) || []) {
      const ref = nc.channelId || nc.tempRef;
      if (!ref) continue;
      const entry = out[ref] || (out[ref] = {});
      entry.number = [nc.from, nc.to];
    }
    for (const gc of (resp && resp.groupChanges) || []) {
      const ref = gc.channelId || gc.tempRef;
      if (!ref) continue;
      const entry = out[ref] || (out[ref] = {});
      entry.group = [gc.from, gc.to];
    }
    return out;
  }

  function tempRefsFromResponse(resp) {
    const out = {};
    for (const cr of (resp && resp.created) || []) {
      if (cr && cr.tempRef) out[cr.tempRef] = { number: cr.number, groupId: cr.groupId || null };
    }
    return out;
  }

  const REASON_TEXT = {
    "direct": "moved directly — the number was free",
    "insert-shift-up": "shifted up to make room",
    "insert-shift-down": "shifted down to make room",
    "block-displaced": "displaced — pushed up by the incoming block",
    "range-pack": "packed into the range",
    "exclusive-outside-relocation": "relocated — outside the exclusive range",
    "shift-interval": "shifted by the interval offset",
    "relocate": "relocated to a chosen number",
    "swap": "swapped numbers",
  };
  function reasonText(reason) {
    return REASON_TEXT[reason] || String(reason || "moved");
  }

  // UX6: routine warnings in everyday language. Anything without an entry
  // here still renders (verbatim) — the raw diagnostics additionally land
  // in an expandable details area at the review.
  const WARNING_COPY = {
    archived_rows_moved: "Some archived channels will move numbers. They stay archived — nothing returns to the guide until you restore them.",
    paused_rows_moved: "Some paused channels will move numbers. They stay paused and off the guide.",
    disabled_rows_moved: "Some off-air channels will move numbers. Their rules are untouched.",
    presentation_will_change: "Numbers and groups will look different on the TV guide. What airs on each channel stays the same.",
    health_numbers_stale: "On-air counts catch up at the next routine refresh — nothing needs re-indexing now.",
  };
  function warningPlainText(w) {
    return WARNING_COPY[w && w.code] || (w && (w.message || w.code)) || String(w);
  }

  // Freeze a preview response into a staged arrangement (frozen contract
  // decision 2): the server-emitted packet is stored verbatim; the UI only
  // (a) merges full pending-create drafts into channel.create skeletons and
  // (b) appends explicitly opted-in channel.put ops whose number AND group
  // AGREE with the plan's final values for that channel (R4 — a stale
  // groupId riding an opted-in put must never silently reverse the
  // reviewed group move).
  function freezeArrangementPacket(resp, opts) {
    const options = opts || {};
    const drafts = options.drafts || null;
    const optInPuts = options.optInPuts || {}; // channelId -> wire channel
    const pendingGroups = options.pendingGroups || [];
    const finalNumbers = {}; // channelId/tempRef -> final number per the plan
    const finalGroups = {};  // channelId/tempRef -> final groupId per the plan
    for (const nc of (resp && resp.numberChanges) || []) {
      const ref = nc.channelId || nc.tempRef;
      if (ref) finalNumbers[ref] = nc.to;
    }
    for (const gc of (resp && resp.groupChanges) || []) {
      const ref = gc.channelId || gc.tempRef;
      if (ref) finalGroups[ref] = gc.to;
    }
    let ops = clone((resp.packet && resp.packet.ops) || []);
    for (const op of ops) {
      if (op.op === "channel.create" && op.tempId && drafts) {
        // The planner never fabricates sources/colors/glyphs — merge the
        // owner's full pending draft into the skeleton (the plan's
        // placement wins where the skeleton carries it; an assign_group-only
        // skeleton omits `number` and the draft's own choice stands).
        const entry = drafts.get(op.tempId);
        if (entry && entry.draft) {
          const full = toWireChannel(clone(entry.draft));
          delete full.id; delete full.seed; delete full.provenance;
          const skel = op.channel || {};
          const merged = Object.assign({}, full);
          if (skel.kind != null) merged.kind = skel.kind;
          if (skel.number != null) merged.number = skel.number;
          if (skel.groupId != null) merged.groupId = skel.groupId;
          if (skel.name != null && skel.name !== "") merged.name = skel.name;
          op.channel = merged;
        }
      }
    }
    for (const id of Object.keys(optInPuts)) {
      const wire = clone(optInPuts[id]);
      if (finalNumbers[id] != null) wire.number = finalNumbers[id]; // agreement rule
      if (finalGroups[id] != null) wire.groupId = finalGroups[id]; // R4
      ops.push({ op: "channel.put", channel: wire });
    }
    // R5 fallback: a pending (uncommitted) group referenced by this packet
    // MUST have its group.put in the same freeze — the planner normally
    // emits it (it was told via the "groups" arg), but packets assembled
    // client-side (number-resolution augmentations) need it explicitly.
    const putIds = new Set(ops.filter((o) => o.op === "group.put" && o.group).map((o) => o.group.id));
    const referenced = new Set();
    for (const op of ops) {
      if (op.op === "channel.create" && op.channel && op.channel.groupId) referenced.add(op.channel.groupId);
      if (op.op === "channels.move" && op.groupId) referenced.add(op.groupId);
      if (op.op === "channel.put" && op.channel && op.channel.groupId) referenced.add(op.channel.groupId);
    }
    const extraPuts = [];
    for (const g of pendingGroups) {
      if (g && g.id && referenced.has(g.id) && !putIds.has(g.id)) {
        extraPuts.push({ op: "group.put", group: { id: g.id, name: g.name, position: g.position != null ? g.position : 1 } });
      }
    }
    if (extraPuts.length) ops = extraPuts.concat(ops);
    return {
      expectedRevision: (resp.packet && resp.packet.expectedRevision != null)
        ? resp.packet.expectedRevision : resp.revision,
      ops,
      requestId: newRequestId(),
    };
  }

  // Frozen contract §5 (backend clarification): assign_group + a number
  // arrangement combine by concatenating two SAME-REVISION preview packets —
  // group ops never touch numbers. Canonical order: group.put →
  // channel.create (skeletons merged per tempId) → channels.move →
  // channels.renumber.
  function combineArrangementResponses(assignResp, rangeResp) {
    const out = clone(rangeResp);
    out.groupChanges = clone(assignResp.groupChanges || []);
    out.warnings = (assignResp.warnings || []).concat(rangeResp.warnings || []);
    out.errors = (assignResp.errors || []).concat(rangeResp.errors || []);
    out.valid = !!(assignResp.valid && rangeResp.valid);
    out.noop = !!(assignResp.noop && rangeResp.noop);
    const groupOps = [];
    const groupSeen = new Set();
    const creates = new Map(); // tempId -> op
    const moves = [];
    const renumbers = [];
    const others = [];
    const take = (ops) => {
      for (const op of ops || []) {
        if (op.op === "group.put") {
          // Two same-revision previews (assign + range) both emit the
          // pending group's put — dedupe by id, keep the first.
          if (!op.group || groupSeen.has(op.group.id)) continue;
          groupSeen.add(op.group.id);
          groupOps.push(op);
        }
        else if (op.op === "channel.create") {
          // Same tempRef in both packets: the assign packet's groupId is the
          // intent's target; the number packet's placement wins the number.
          const prev = creates.get(op.tempId);
          if (prev) {
            op.channel = Object.assign({}, prev.channel, op.channel);
            if (prev.channel && prev.channel.groupId != null) op.channel.groupId = prev.channel.groupId;
          }
          creates.set(op.tempId, op);
        }
        else if (op.op === "channels.move") moves.push(op);
        else if (op.op === "channels.renumber") renumbers.push(op);
        else others.push(op);
      }
    };
    take(assignResp.packet && assignResp.packet.ops);
    take(rangeResp.packet && rangeResp.packet.ops);
    // Merge the created-row overlays the same way (range packet's number
    // wins; the assign packet's groupId wins).
    const createdMap = new Map();
    for (const cr of (assignResp.created) || []) if (cr && cr.tempRef) createdMap.set(cr.tempRef, clone(cr));
    for (const cr of (rangeResp.created) || []) {
      if (!cr || !cr.tempRef) continue;
      const prev = createdMap.get(cr.tempRef) || {};
      createdMap.set(cr.tempRef, Object.assign({}, prev, cr,
        prev.groupId != null && cr.groupId == null ? { groupId: prev.groupId } : {}));
    }
    out.created = [...createdMap.values()];
    out.packet = {
      expectedRevision: rangeResp.revision,
      ops: groupOps.concat([...creates.values()], moves, renumbers, others),
    };
    return out;
  }

  // Post-commit Stage reversal: inverse field map over EXISTING channels
  // only. Created channels/groups are never auto-deleted (they are listed
  // to the owner instead). Returns { ops, beforeAfter, skippedMoved,
  // createdCount } — the caller validates + stages it as a NEW arrangement.
  function buildReversal(staged, libChannels) {
    const byId = new Map((libChannels || []).map((c) => [c.id, c]));
    const assignments = [];
    const groupBack = new Map(); // groupId -> [channelId]
    const inverse = {};
    const skippedMoved = [];
    const createdIds = new Set();
    for (const op of (staged.packet && staged.packet.ops) || []) {
      if (op.op === "channel.create" && op.tempId) createdIds.add(op.tempId);
    }
    for (const ref of Object.keys(staged.beforeAfter || {})) {
      if (String(ref).startsWith("temp-")) continue; // a created row: never auto-deleted
      const ba = staged.beforeAfter[ref];
      const cur = byId.get(ref);
      if (!cur) { skippedMoved.push(ref); continue; } // gone since — leave it
      if (ba.number) {
        if (cur.number === ba.number[1]) {
          assignments.push({ channelId: ref, number: ba.number[0] });
          (inverse[ref] || (inverse[ref] = {})).number = [ba.number[1], ba.number[0]];
        } else {
          skippedMoved.push(ref); // moved again since — never blindly replay
        }
      }
      if (ba.group && cur.groupId === ba.group[1]) {
        const list = groupBack.get(ba.group[0]) || [];
        list.push(ref);
        groupBack.set(ba.group[0], list);
        (inverse[ref] || (inverse[ref] = {})).group = [ba.group[1], ba.group[0]];
      }
    }
    const ops = [];
    if (assignments.length) ops.push({ op: "channels.renumber", assignments });
    for (const [gid, ids] of groupBack) ops.push({ op: "channels.move", channelIds: ids, groupId: gid });
    return { ops, beforeAfter: inverse, skippedMoved, createdCount: createdIds.size };
  }

  // After a committed arrangement: number/groupId land as server-changed
  // fields on every touched channel that ALSO has a content draft —
  // untouched locally -> adopt the acknowledged value; touched on both
  // sides -> keep the local value (the next editor mount's rebaseDraft
  // surfaces it as a named conflict). NEVER a wholesale channel.put.
  function rebaseDraftsAfterArrangement(drafts, beforeAfter) {
    if (!drafts) return;
    for (const ref of Object.keys(beforeAfter || {})) {
      if (String(ref).startsWith("temp-")) continue;
      const entry = drafts.get(ref);
      if (!entry || !entry.draft) continue;
      const ba = beforeAfter[ref];
      const draft = Object.assign({}, entry.draft);
      let touched = false;
      if (ba.number && draft.number === ba.number[0]) { draft.number = ba.number[1]; touched = true; }
      if (ba.group && ba.group[0] && draft.groupId === ba.group[0]) { draft.groupId = ba.group[1]; touched = true; }
      if (!touched) continue; // locally divergent (or untouched): leave for rebaseDraft
      const extras = {};
      if (entry.base) {
        const base = Object.assign({}, entry.base);
        if (ba.number && base.number === ba.number[0]) base.number = ba.number[1];
        if (ba.group && ba.group[0] && base.groupId === ba.group[0]) base.groupId = ba.group[1];
        extras.base = base;
        // The draft is now exactly the acknowledged record: retire it.
        if (deepEqual(draft, base)) { drafts.drop(ref); continue; }
      }
      drafts.put(ref, draft, extras);
    }
  }

  // ------------------------------------------------------------------
  // In-flight applies: a channel's apply survives the editor being navigated
  // away (poll continues; the receipt lands; drafts/library update).
  // channelId -> { requestId, snapshot, expected, promise, name }
  //
  // Free navigation is the design (async Apply): the app subscribes via
  // onInflightChange to show a top-bar pill, and the receipt resolves in a
  // stale editor closure just as well as in a mounted one. A receipt can be
  // observed by TWO finalize calls (the stale closure and a remount that
  // re-attached to the same promise), so settlement toasts are deduped per
  // requestId through settledApplies — UI state updates still run twice.
  // ------------------------------------------------------------------

  const inflightApplies = new Map();
  const inflightListeners = new Set();
  const settledApplies = new Map(); // requestId -> settled-at ms (bounded)

  function inflightNotify() {
    for (const cb of inflightListeners) cb(inflightSnapshot());
  }
  function onInflightChange(cb) {
    inflightListeners.add(cb);
    return () => inflightListeners.delete(cb);
  }
  function inflightSnapshot() {
    return [...inflightApplies.entries()].map(([channelId, v]) => ({ channelId, name: v.name || channelId }));
  }
  function markSettled(requestId) {
    const dup = settledApplies.has(requestId);
    if (!dup) {
      settledApplies.set(requestId, Date.now());
      if (settledApplies.size > 200) settledApplies.delete(settledApplies.keys().next().value);
    }
    return dup;
  }

  // ------------------------------------------------------------------
  // Small shared UI
  // ------------------------------------------------------------------

  function useOutsideClose(open, onClose) {
    const ref = useRef(null);
    useEffect(() => {
      if (!open) return undefined;
      const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
      document.addEventListener("mousedown", onDoc);
      document.addEventListener("keydown", onKey, true);
      return () => {
        document.removeEventListener("mousedown", onDoc);
        document.removeEventListener("keydown", onKey, true);
      };
    }, [open, onClose]);
    return ref;
  }

  function Dialog({ title, onClose, children, footer, wide, dialogClass }) {
    const ref = useRef(null);
    useEffect(() => {
      const previous = document.activeElement;
      const node = ref.current;
      const focusables = () => node
        ? node.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")
        : [];
      const first = focusables()[0];
      if (first) first.focus();
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
        if (e.key === "Tab") {
          const nodes = focusables();
          if (!nodes.length) return;
          const firstNode = nodes[0];
          const last = nodes[nodes.length - 1];
          if (e.shiftKey && document.activeElement === firstNode) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); firstNode.focus(); }
        }
      };
      document.addEventListener("keydown", onKey, true);
      return () => {
        document.removeEventListener("keydown", onKey, true);
        if (previous && previous.isConnected) previous.focus();
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return h("div", {
      className: "jw-overlay", role: "presentation",
      onMouseDown: (e) => { if (e.target === e.currentTarget) onClose(); },
    },
      h("div", {
        className: "jw-dialog" + (wide ? " jw-dialog-wide" : "") + (dialogClass ? " " + dialogClass : ""),
        role: "dialog", "aria-modal": "true", "aria-label": title, ref,
      },
        h("header", { className: "jw-dialog-head" },
          h("h2", { className: "jw-dialog-title" }, title),
          h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Close dialog", onClick: onClose }, "✕"),
        ),
        h("div", { className: "jw-dialog-body" }, children),
        footer ? h("footer", { className: "jw-dialog-foot" }, footer) : null,
      ),
    );
  }

  function ConfirmDialog({ spec }) {
    if (!spec) return null;
    const close = (ok) => { spec.resolve(ok); };
    return h(Dialog, {
      title: spec.title, onClose: () => close(false),
      footer: [
        h("button", { key: "c", className: "jw-btn", onClick: () => close(false) }, "Cancel"),
        h("button", {
          key: "k", className: "jw-btn " + (spec.danger ? "jw-btn-danger" : "jw-btn-primary"),
          onClick: () => close(true),
        }, spec.confirmLabel || "Confirm"),
      ],
    },
      h("p", { className: "jw-confirm-message" }, spec.message),
      spec.detail || null,
    );
  }

  // Fixed-height windowed list: renders only the visible slice (+overscan).
  function WindowedList({ items, itemHeight, render, overscan, resetKey, ariaLabel, className, listStyle }) {
    const ref = useRef(null);
    const [range, setRange] = useState({ start: 0, end: 60 });
    const itemsRef = useRef(items);
    itemsRef.current = items;
    const overscanN = overscan == null ? 10 : overscan;

    const measure = useCallback(() => {
      const node = ref.current;
      if (!node) return;
      const top = node.scrollTop;
      const height = node.clientHeight || 600;
      const start = Math.max(0, Math.floor(top / itemHeight) - overscanN);
      const end = Math.min(itemsRef.current.length, Math.ceil((top + height) / itemHeight) + overscanN);
      setRange((cur) => (cur.start === start && cur.end === end ? cur : { start, end }));
    }, [itemHeight, overscanN]);

    useEffect(() => {
      measure();
      const node = ref.current;
      if (!node) return undefined;
      let raf = 0;
      const onScroll = () => {
        if (raf) cancelAnimationFrame(raf);
        raf = requestAnimationFrame(measure);
      };
      node.addEventListener("scroll", onScroll, { passive: true });
      const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => measure()) : null;
      if (ro) ro.observe(node);
      return () => {
        node.removeEventListener("scroll", onScroll);
        if (ro) ro.disconnect();
        if (raf) cancelAnimationFrame(raf);
      };
    }, [measure]);

    useEffect(() => {
      const node = ref.current;
      if (node) node.scrollTop = 0;
      measure();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [resetKey]);

    const slice = items.slice(range.start, range.end);
    return h("div", {
      className: "jw-winlist" + (className ? " " + className : ""), ref,
      role: "listbox", "aria-label": ariaLabel, style: listStyle || undefined,
    },
      h("div", { style: { height: items.length * itemHeight + "px", position: "relative" } },
        h("div", { style: { position: "absolute", top: range.start * itemHeight + "px", left: 0, right: 0 } },
          slice.map(render)),
      ),
    );
  }

  function Toasts({ items, onDismiss }) {
    return h("div", { className: "jw-toast-zone", role: "status", "aria-live": "polite" },
      items.map((t) => h("div", {
        key: t.id,
        className: "jw-toast" + (t.kind ? " jw-toast-" + t.kind : ""),
        onClick: () => onDismiss(t.id),
      },
        TOAST_ICONS[t.kind]
          ? h("span", { className: "jw-toast-icon", "aria-hidden": "true" }, TOAST_ICONS[t.kind])
          : null,
        h("span", null, t.message),
        t.action ? h("button", {
          className: "jw-btn jw-btn-small jw-toast-action",
          onClick: (e) => { e.stopPropagation(); onDismiss(t.id); t.action.onClick(); },
        }, t.action.label) : null)),
    );
  }

  function StatusChip({ kind, children }) {
    return h("span", { className: "jw-status-chip jw-status-" + kind }, children);
  }

  // Card disclosure (plan §7): long sections collapse behind a one-line
  // summary; an error badge stays on the header so collapsed error-bearing
  // controls are never silent. Starts OPEN — collapse is owner-chosen.
  function Disclosure({ title, summary, errorCount, defaultOpen, children }) {
    const [open, setOpen] = useState(defaultOpen !== false);
    return h("div", { className: "jw-card jw-disclosure" },
      h("button", {
        type: "button",
        className: "jw-card-title jw-disclosure-head",
        "aria-expanded": open ? "true" : "false",
        onClick: () => setOpen((v) => !v),
      },
        h("span", { className: "jw-disclosure-caret", "aria-hidden": "true" }, open ? "▾" : "▸"),
        h("span", { className: "jw-disclosure-title" }, title),
        errorCount
          ? h("span", { className: "jw-disclosure-badge" }, errorCount + " error" + (errorCount === 1 ? "" : "s"))
          : null,
        !open && summary ? h("span", { className: "jw-disclosure-summary" }, summary) : null,
      ),
      open ? children : null,
    );
  }

  // ------------------------------------------------------------------
  // Optional customization hook (extras/): versioned, feature-detected,
  // route-scoped, view notifications ONLY — there is deliberately no
  // mutation surface (snippets can never bypass the draft/Apply flow).
  // ------------------------------------------------------------------
  const studioExt = {
    cleanups: new Map(), // name -> cleanup fn
    listeners: { staged: new Set(), applied: new Set(), selection: new Set() },
  };
  const studioCtxRef = { current: null }; // set while the App route is mounted

  function studioEmit(event, payload) {
    const set = studioExt.listeners[event];
    if (!set) return;
    for (const cb of [...set]) { try { cb(payload); } catch (e) { /* snippet errors never break the page */ } }
  }

  function studioRunCleanups() {
    for (const [name, fn] of [...studioExt.cleanups]) {
      if (typeof fn === "function") { try { fn(); } catch (e) { /* noop */ } }
      studioExt.cleanups.delete(name);
    }
  }

  window.JWChannelStudio = {
    version: 1,
    // register once per name; re-registration runs the previous cleanup
    // first (idempotent after Stash re-injects custom JavaScript).
    registerExtension(name, ext) {
      if (!name || !ext || typeof ext.setup !== "function") {
        throw new Error("JWChannelStudio.registerExtension(name, { version, setup })");
      }
      const prev = studioExt.cleanups.get(name);
      if (typeof prev === "function") { try { prev(); } catch (e) { /* noop */ } }
      studioExt.cleanups.delete(name);
      const ctx = studioCtxRef.current;
      if (!ctx) return () => {}; // route not mounted — nothing to clean up
      let cleanup = null;
      try { cleanup = ext.setup(ctx); } catch (e) { console.error("[stash-justwatch] extension failed", e); }
      studioExt.cleanups.set(name, typeof cleanup === "function" ? cleanup : null);
      return function unregister() {
        const fn = studioExt.cleanups.get(name);
        if (typeof fn === "function") { try { fn(); } catch (e) { /* noop */ } }
        studioExt.cleanups.delete(name);
      };
    },
  };

  // ------------------------------------------------------------------
  // Entity picker (searchable against Stash, windowed, thousands-safe)
  // ------------------------------------------------------------------

  function EntityPickerDialog({ kind, title, selectedIds, onClose, onDone }) {
    const [query, setQueryRaw] = useState("");
    const [rows, setRows] = useState(null);
    const [picked, setPicked] = useState(() => new Set(selectedIds.map(String)));
    const [error, setError] = useState(null);

    const setQuery = useMemo(() => debounce((v) => setQueryRaw(v), SEARCH_DEBOUNCE_MS), []);

    useEffect(() => {
      let alive = true;
      setError(null);
      searchEntities(kind, query)
        .then((r) => { if (alive) setRows(r); })
        .catch((e) => { if (alive) { setRows([]); setError(String((e && e.message) || e)); } });
      return () => { alive = false; };
    }, [query, kind]);

    const toggle = (id) => {
      setPicked((cur) => {
        const next = new Set(cur);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    };

    const pickedList = [...picked];
    const nameOf = (id) => entityName(kind, id);

    const rowRender = (row) => h("div", {
      key: row.id,
      className: "jw-pick-row" + (picked.has(row.id) ? " jw-pick-row-checked" : ""),
      role: "option", "aria-selected": picked.has(row.id) ? "true" : "false", tabIndex: 0,
      onClick: () => toggle(row.id),
      onKeyDown: (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(row.id); } },
    },
      h("span", { className: "jw-pick-check" }, picked.has(row.id) ? "✓" : ""),
      h("span", { className: "jw-pick-name" }, row.name),
      h("span", { className: "jw-pick-count" }, row.count === 0 ? "unused" : fmtCount(row.count)),
      h("span", { className: "jw-pick-eid" }, "#" + row.id),
    );

    return h(Dialog, {
      title, onClose, wide: true,
      footer: [
        h("button", { key: "cancel", className: "jw-btn", onClick: onClose }, "Cancel"),
        h("button", {
          key: "done", className: "jw-btn jw-btn-primary",
          onClick: () => { onDone(sortedIds(pickedList)); onClose(); },
        }, "Done"),
      ],
    },
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-picker-search" }, "Search"),
        h("input", {
          id: "jw-picker-search", className: "jw-input", type: "search",
          placeholder: "Search " + kind + "s…", defaultValue: "",
          onChange: (e) => setQuery(e.target.value.trim().toLowerCase()),
        }),
      ),
      error ? h("p", { className: "jw-error-text", role: "alert" }, error) : null,
      rows == null
        ? h("p", { className: "jw-hint" }, "Searching…")
        : h("div", null,
            h("p", { className: "jw-pick-meta" }, rows.length.toLocaleString() + (rows.length === 1 ? " result" : " results")),
            h(WindowedList, {
              items: rows, itemHeight: PICKER_ITEM_HEIGHT, render: rowRender,
              resetKey: query + ":" + kind, ariaLabel: "Search results",
              className: "jw-pick-list",
            })),
      h("div", { className: "jw-picked-pane" },
        h("div", { className: "jw-picked-head" },
          h("span", null, pickedList.length.toLocaleString() + " selected"),
          pickedList.length
            ? h("button", {
                className: "jw-btn jw-btn-ghost jw-btn-small",
                onClick: () => setPicked(new Set()),
              }, "Clear all")
            : null,
        ),
        pickedList.length
          ? h("div", { className: "jw-entity-summary" },
              pickedList.slice(0, DRAFT_COUNT_CAP).map((id) => h("span", { key: id, className: "jw-entity-chip" },
                h("span", null, nameOf(id)),
                h("button", {
                  "aria-label": "Remove " + nameOf(id),
                  onClick: () => toggle(id),
                }, "✕"),
              )),
              pickedList.length > DRAFT_COUNT_CAP
                ? h("span", { className: "jw-entity-chip" }, "+ " + (pickedList.length - DRAFT_COUNT_CAP) + " more (all kept)")
                : null,
            )
          : h("p", { className: "jw-hint" }, "Nothing selected yet."),
      ),
    );
  }

  // ------------------------------------------------------------------
  // FacetChipList — one facet's entity list (includes or exclusions).
  // Per-chip removal plus batch ergonomics for long lists: filter box,
  // manage mode (toggle chips + Select all shown + Remove N), Clear all.
  // filter/manage/selection are VIEW state only — the draft changes only
  // through the single onRemove/onClearAll callbacks (one edit per action).
  // ------------------------------------------------------------------

  function FacetChipList({ ids, kind, variant, noun, confirm, onRemove, onClearAll }) {
    const [filter, setFilter] = useState("");
    const [manage, setManage] = useState(false);
    const [sel, setSel] = useState(() => new Set());
    const filterRef = useRef(null);

    const nameOf = (id) => entityName(kind, id);
    const idSet = new Set(ids);
    const selLive = new Set([...sel].filter((id) => idSet.has(id)));
    const q = filter.trim().toLowerCase();
    const filtered = q ? ids.filter((id) => nameOf(id).toLowerCase().includes(q)) : ids;
    const shown = filtered.slice(0, DRAFT_COUNT_CAP);
    const overflow = filtered.length - shown.length;
    const showFilter = ids.length > CHIP_FILTER_MIN || manage;

    useEffect(() => {
      if (manage && filterRef.current) filterRef.current.focus();
    }, [manage]);

    const exitManage = () => { setManage(false); setSel(new Set()); };

    const toggleSel = (id) => setSel((cur) => {
      const next = new Set(selLive.size === cur.size ? cur : selLive);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

    const removeSelected = async () => {
      const list = [...selLive];
      if (!list.length) return;
      if (list.length > CLEAR_CONFIRM_MIN) {
        const ok = await confirm({
          title: "Remove " + noun,
          message: "Remove the " + list.length.toLocaleString() + " selected " + noun + " from this rule?",
          confirmLabel: "Remove " + list.length.toLocaleString(),
        });
        if (!ok) return;
      }
      onRemove(list);
      setSel(new Set());
      // the Remove button is now disabled (0 selected) — return focus to the
      // filter so keyboard flow (and Escape-to-exit) keeps working
      if (filterRef.current) filterRef.current.focus();
    };

    if (!ids.length) return null;

    return h("div", {
      className: "jw-facet-chips",
      onKeyDown: (e) => { if (e.key === "Escape" && manage) { e.stopPropagation(); exitManage(); } },
    },
      h("div", { className: "jw-chipbar" },
        showFilter
          ? h("input", {
              ref: filterRef, type: "search",
              className: "jw-input jw-chip-filter",
              placeholder: "Filter these…", "aria-label": "Filter these " + noun,
              value: filter, onChange: (e) => setFilter(e.target.value),
            })
          : null,
        h("button", {
          className: "jw-btn jw-btn-ghost jw-btn-small",
          "aria-pressed": manage ? "true" : "false",
          title: manage ? "Back to plain chips" : "Pick many at once to remove them",
          onClick: () => (manage ? exitManage() : setManage(true)),
        }, manage ? "Done" : "Manage"),
        manage ? h("span", { className: "jw-chipbar-sel" },
          selLive.size.toLocaleString() + " selected") : null,
        manage ? h("button", {
          className: "jw-btn jw-btn-ghost jw-btn-small",
          disabled: !shown.length,
          onClick: () => setSel(new Set(shown)),
        }, "Select all shown") : null,
        manage ? h("button", {
          className: "jw-btn jw-btn-small",
          disabled: !selLive.size,
          onClick: () => void removeSelected(),
        }, "Remove " + (selLive.size ? selLive.size.toLocaleString() + " " : "") + "selected") : null,
        h("button", {
          className: "jw-btn jw-btn-ghost jw-btn-small jw-chipbar-clear",
          title: "Remove every " + noun.replace(/s$/, "") + " from this list",
          onClick: onClearAll,
        }, "Clear all"),
      ),
      filtered.length
        ? h("div", { className: "jw-entity-summary", role: manage ? "group" : undefined },
            shown.map((id) => manage
              ? h("button", {
                  key: id, type: "button",
                  className: "jw-entity-chip jw-entity-chip-check"
                    + (variant === "exclude" ? " jw-entity-chip-excl" : "")
                    + (selLive.has(id) ? " jw-chip-active" : ""),
                  "aria-pressed": selLive.has(id) ? "true" : "false",
                  onClick: () => toggleSel(id),
                },
                  h("span", { className: "jw-entity-check", "aria-hidden": "true" },
                    selLive.has(id) ? "✓" : ""),
                  h("span", null, nameOf(id)))
              : h("span", {
                  key: id,
                  className: "jw-entity-chip" + (variant === "exclude" ? " jw-entity-chip-excl" : ""),
                },
                  h("span", null, nameOf(id)),
                  h("button", {
                    "aria-label": (variant === "exclude" ? "Keep " : "Remove ") + nameOf(id),
                    onClick: () => onRemove([id]),
                  }, "✕"))),
            overflow > 0
              ? (manage
                  ? h("span", { className: "jw-hint" },
                      overflow.toLocaleString() + " more hidden — use the filter")
                  : h("button", {
                      className: "jw-entity-chip jw-entity-overflow",
                      title: "Show and manage all " + filtered.length.toLocaleString(),
                      onClick: () => { setFilter(""); setManage(true); },
                    }, "+ " + overflow.toLocaleString() + " more"))
              : null,
          )
        : h("p", { className: "jw-hint" }, "No " + noun + " match \"" + filter + "\"."),
    );
  }

  // ------------------------------------------------------------------
  // Groups manager — a true staged draft: renames/reorders/creates/deletes
  // stage locally; ONE Apply commits them as a single transaction (audit C9:
  // the old dialog wrote on blur, violating explicit Apply).
  // ------------------------------------------------------------------

  function GroupsManagerDialog({ lib, channels, drafts, onClose, submitOps, toast, refreshLibrary }) {
    const server = useMemo(
      () => (lib.groups || []).slice().sort((a, b) => a.position - b.position),
      [lib]);
    const [rows, setRows] = useState(() => server.map((g) => ({ ...g })));
    const [deletes, setDeletes] = useState({}); // gid -> destination group id
    const [newName, setNewName] = useState("");
    const [busy, setBusy] = useState(false);
    const memberCount = useCallback((gid) =>
      (channels || []).filter((c) => c.groupId === gid).length, [channels]);

    const dirty = useMemo(() => {
      const strip = (list) => list.map((g) => [g.id, g.name.trim(), g.position]);
      return JSON.stringify(strip(rows)) !== JSON.stringify(strip(server))
        || Object.keys(deletes).length > 0;
    }, [rows, server, deletes]);

    const rename = (gid, name) => {
      // raw text while typing (trailing spaces stay typable); dirty-compare
      // and apply() trim, so Apply always commits the trimmed name — even
      // when the field was never blurred (audit: blur-only commit lost renames)
      setRows((cur) => cur.map((g) => (g.id === gid ? { ...g, name: String(name) } : g)));
    };
    const move = (gid, dir) => {
      setRows((cur) => {
        const i = cur.findIndex((x) => x.id === gid);
        const j = i + dir;
        if (i < 0 || j < 0 || j >= cur.length) return cur;
        const next = cur.slice();
        const tmp = next[i];
        next[i] = next[j];
        next[j] = tmp;
        return next.map((g, idx) => ({ ...g, position: idx + 1 }));
      });
    };
    const create = () => {
      const trimmed = newName.trim();
      if (!trimmed) return;
      setRows((cur) => cur.concat({
        id: newGroupId(), name: trimmed, position: cur.length + 1,
        legacySection: null, staged: true,
      }));
      setNewName("");
    };
    const stageDelete = (gid) => {
      const others = rows.filter((x) => x.id !== gid && !deletes[x.id]);
      setDeletes((cur) => ({ ...cur, [gid]: others[0] ? others[0].id : null }));
    };
    const undoDelete = (gid) => {
      setDeletes((cur) => {
        const next = { ...cur };
        delete next[gid];
        return next;
      });
    };

    const apply = async () => {
      if (!dirty || busy) return;
      const clash = new Map();
      for (const g of rows) {
        const key = g.name.trim().toLowerCase();
        if (clash.has(key)) {
          toast("Two groups cannot share the name “" + g.name.trim() + "”.", "err");
          return;
        }
        clash.set(key, g.id);
      }
      const ops = [];
      const byId = new Map(server.map((g) => [g.id, g]));
      rows.forEach((g, idx) => {
        const prior = byId.get(g.id);
        const position = idx + 1;
        if (!prior || prior.name !== g.name.trim() || prior.position !== position) {
          ops.push({ op: "group.put", group: { id: g.id, name: g.name.trim(), position } });
        }
      });
      for (const [gid, moveTo] of Object.entries(deletes)) {
        const op = { op: "group.delete", id: gid };
        if (memberCount(gid) && moveTo) op.moveTo = moveTo;
        ops.push(op);
      }
      if (!ops.length) { onClose(); return; }
      setBusy(true);
      try {
        const receipt = await submitOps(ops, { label: "Groups" });
        if (receipt.status === "committed") {
          // keep stored channel drafts consistent: their group moved/deleted
          if (drafts && Object.keys(deletes).length) {
            for (const [gid, moveTo] of Object.entries(deletes)) {
              for (const c of channels || []) {
                if (c.groupId !== gid) continue;
                const entry = drafts.get(c.id);
                if (entry && moveTo) drafts.put(c.id, Object.assign({}, entry.draft, { groupId: moveTo }));
              }
            }
          }
          await refreshLibrary();
          onClose();
        }
      } finally {
        setBusy(false);
      }
    };

    const discard = () => {
      setRows(server.map((g) => ({ ...g })));
      setDeletes({});
      setNewName("");
    };

    return h(Dialog, {
      title: "Groups", onClose,
      footer: [
        h("button", { key: "d", className: "jw-btn", onClick: discard, disabled: busy || !dirty }, "Discard"),
        h("button", {
          key: "a", className: "jw-btn jw-btn-primary", onClick: () => void apply(),
          disabled: busy || !dirty,
        }, busy ? "Applying…" : "Apply"),
      ],
    },
      h("p", { className: "jw-hint" },
        "Every channel belongs to exactly one group. Changes stage here — one Apply commits the whole set as a single transaction."),
      h("div", { className: "jw-groups-list", role: "list" },
        rows.filter((g) => !deletes[g.id]).map((g, i) => h("div", { key: g.id, className: "jw-groups-row", role: "listitem" },
          h("span", { className: "jw-groups-pos" }, "#" + (i + 1)),
          h("input", {
            className: "jw-input jw-groups-name", value: g.name,
            "aria-label": "Group name " + g.name,
            onChange: (e) => rename(g.id, e.target.value),
            onKeyDown: (e) => { if (e.key === "Enter") e.target.blur(); },
          }),
          h("span", { className: "jw-groups-count" }, memberCount(g.id) + " ch"),
          g.staged ? h(StatusChip, { kind: "accent" }, "new") : null,
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Move " + g.name + " up",
            disabled: busy || i === 0, onClick: () => move(g.id, -1),
          }, "↑"),
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Move " + g.name + " down",
            disabled: busy || i === rows.length - 1 - Object.keys(deletes).length, onClick: () => move(g.id, +1),
          }, "↓"),
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Delete group " + g.name,
            disabled: busy || rows.length - Object.keys(deletes).length <= 1,
            title: rows.length - Object.keys(deletes).length <= 1 ? "Cannot remove the last group." : "Delete group",
            onClick: () => stageDelete(g.id),
          }, h(FaIcon, { name: "faTrashAlt", size: 13, fallback: "Delete" })),
        )),
        Object.entries(deletes).map(([gid, moveTo]) => {
          const g = rows.find((x) => x.id === gid);
          if (!g) return null;
          const members = memberCount(gid);
          const others = rows.filter((x) => x.id !== gid && !deletes[x.id]);
          return h("div", { key: "del-" + gid, className: "jw-groups-row jw-groups-row-deleting", role: "listitem" },
            h("span", { className: "jw-groups-pos" }, "—"),
            h("span", { className: "jw-groups-name" }, g.name),
            members
              ? h("select", {
                  className: "jw-input", "aria-label": "Destination for members of " + g.name,
                  value: moveTo || "", disabled: !others.length,
                  onChange: (e) => setDeletes((cur) => ({ ...cur, [gid]: e.target.value })),
                }, others.map((x) => h("option", { key: x.id, value: x.id }, x.name)))
              : h("span", { className: "jw-groups-count" }, "empty"),
            members ? h("span", { className: "jw-groups-count" }, members + " ch move") : null,
            h("button", {
              className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Keep group " + g.name,
              disabled: busy, onClick: () => undoDelete(gid),
            }, "Undo"),
          );
        }),
      ),
      h("div", { className: "jw-groups-create" },
        h("input", {
          className: "jw-input", placeholder: "New group name", value: newName,
          "aria-label": "New group name",
          onChange: (e) => setNewName(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") create(); },
        }),
        h("button", {
          className: "jw-btn jw-btn-primary", disabled: busy || !newName.trim(), onClick: create,
        }, "Add (staged)"),
      ),
    );
  }

  // ------------------------------------------------------------------
  // New channel dialog → local DRAFT (committed on Apply)
  // ------------------------------------------------------------------

  function NewChannelDialog({ lib, drafts, arrangementAvailable, onClose, onCreate }) {
    const groups = (lib.groups || []).slice().sort((a, b) => a.position - b.position);
    const [kind, setKind] = useState("net");
    const [number, setNumber] = useState(() => nextFreeNumber(lib.channels || [], "net"));
    const [name, setName] = useState("");
    const [groupId, setGroupId] = useState(groups[0] ? groups[0].id : "");
    const [newGroupName, setNewGroupName] = useState("");
    const [error, setError] = useState(null);

    const band = BANDS[kind];
    const inBand = Number.isInteger(number) && number >= band[0] && number <= band[1];
    const occupant = inBand ? (lib.channels || []).find((c) => c.number === number) : null;
    const numberOk = inBand && (!occupant || arrangementAvailable);

    const switchKind = (k) => {
      setKind(k);
      setNumber(nextFreeNumber(lib.channels || [], k));
    };

    const submit = () => {
      const trimmed = name.trim();
      if (!trimmed) { setError("Give the channel a name."); return; }
      if (!numberOk) { setError("Number " + (number || "") + " is not free in " + band[0] + "–" + band[1] + "."); return; }
      let gid = groupId;
      let pendingGroupName;
      if (groupId === "__create__") {
        const gn = newGroupName.trim();
        if (!gn) { setError("Name the new group."); return; }
        gid = newGroupId();
        pendingGroupName = gn;
      } else if (!groupId) { setError("Pick a group."); return; }
      const tempId = newTempId();
      const draft = {
        kind, number, name: trimmed, glyph: null,
        color: pickDefaultColor(number), groupId: gid, sort: "shuffle",
        enabled: true, archived: false, paused: false,
        source: convertLegacySource({ type: "none" }),
        sourceLabel: "", programming: null,
      };
      if (pendingGroupName) draft.pendingGroupName = pendingGroupName;
      drafts.put(tempId, draft);
      // An occupied number on an arrangement-capable backend: the draft keeps
      // the intended number and the resolution sheet opens immediately —
      // never a silent reset, never an automatic swap.
      onCreate(tempId, occupant ? { resolveNumber: number } : null);
    };

    return h(Dialog, {
      title: "New channel", onClose,
      footer: [
        h("button", { key: "c", className: "jw-btn", onClick: onClose }, "Cancel"),
        h("button", { key: "g", className: "jw-btn jw-btn-primary", onClick: submit }, "Create draft"),
      ],
    },
      h("p", { className: "jw-hint" },
        "This stages a local draft. Nothing exists on the server until you Apply in the editor."),
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-new-name" }, "Name"),
        h("input", {
          id: "jw-new-name", className: "jw-input", value: name, maxLength: MAX_NAME_LEN,
          onChange: (e) => setName(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") submit(); },
        }),
      ),
      h("div", { className: "jw-field" },
        h("span", { className: "jw-field-label" }, "Number band"),
        h("div", { className: "jw-chip-row", role: "group", "aria-label": "Number band" },
          h("button", {
            className: "jw-chip" + (kind === "net" ? " jw-chip-active" : ""), "aria-pressed": kind === "net" ? "true" : "false",
            onClick: () => switchKind("net"),
          }, "100–899 · network tier"),
          h("button", {
            className: "jw-chip" + (kind === "ch" ? " jw-chip-active" : ""), "aria-pressed": kind === "ch" ? "true" : "false",
            onClick: () => switchKind("ch"),
          }, "1–99 · My Channels"),
        ),
        h("p", { className: "jw-hint" },
          kind === "ch"
            ? "1–99 are your personal slots ahead of the built-in dial on the TV."
            : "100–899 is the network tier; pick a free number in that range."),
      ),
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-new-number" }, "Number (free: " + band[0] + "–" + band[1] + ")"),
        h("input", {
          id: "jw-new-number", className: "jw-input jw-num-wide", type: "number",
          min: band[0], max: band[1], value: number != null ? number : "",
          onChange: (e) => setNumber(e.target.value === "" ? null : parseInt(e.target.value, 10)),
        }),
        occupant
          ? h("p", { className: "jw-hint" },
              number + " is “" + occupant.name + "”"
              + (arrangementAvailable ? " — you'll pick how to place it next." : " — pick a free number."))
          : null,
        !inBand && number != null
          ? h("p", { className: "jw-error-text" }, "That number is out of the band.")
          : null,
      ),
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-new-group" }, "Group"),
        h("select", { id: "jw-new-group", className: "jw-input", value: groupId, onChange: (e) => setGroupId(e.target.value) },
          groups.map((g) => h("option", { key: g.id, value: g.id }, g.name)),
          h("option", { value: "__create__" }, "Create group…")),
        groupId === "__create__"
          ? h("input", {
              className: "jw-input", style: { marginTop: "6px" },
              placeholder: "New group name", "aria-label": "New group name",
              value: newGroupName, onChange: (e) => setNewGroupName(e.target.value),
            })
          : null,
      ),
      error ? h("p", { className: "jw-error-text", role: "alert" }, error) : null,
    );
  }

  // ------------------------------------------------------------------
  // History modal: restore stages a draft — never a rewind
  // ------------------------------------------------------------------

  function HistoryDialog({ channelId, channelName, fetchPage, onClose, onRestore }) {
    const [entries, setEntries] = useState(null);
    const [offset, setOffset] = useState(0);
    const [error, setError] = useState(null);
    const limit = 20;

    useEffect(() => {
      let alive = true;
      setError(null);
      fetchPage({ offset, limit })
        .then((r) => { if (alive) setEntries(r.entries || []); })
        .catch((e) => { if (alive) setError(String((e && e.message) || e)); });
      return () => { alive = false; };
    }, [offset]);

    const versionIn = (entry) => (entry.channels || []).find((c) => c && c.id === channelId) || null;

    return h(Dialog, {
      title: "History — " + channelName, onClose,
      footer: [
        h("button", { key: "older", className: "jw-btn", disabled: !entries || entries.length < limit, onClick: () => setOffset((o) => o + limit) }, "Older"),
        h("button", { key: "newer", className: "jw-btn", disabled: offset === 0, onClick: () => setOffset((o) => Math.max(0, o - limit)) }, "Newer"),
        h("button", { key: "done", className: "jw-btn jw-btn-primary", onClick: onClose }, "Done"),
      ],
    },
      h("p", { className: "jw-hint" },
        "Restore stages that revision's version of THIS channel as your current draft — Apply commits it as a new change. Nothing rewinds."),
      error ? h("p", { className: "jw-error-text", role: "alert" }, error) : null,
      entries == null
        ? h("p", { className: "jw-hint" }, "Loading…")
        : entries.length === 0
          ? h("p", { className: "jw-hint" }, "No history yet — every Apply is archived here.")
          : h("div", { className: "jw-history-list" },
              entries.map((entry) => {
                const version = versionIn(entry);
                const changed = version != null;
                return h("div", { key: entry.revision, className: "jw-history-row" },
                  h("span", { className: "jw-history-rev" }, "r" + entry.revision),
                  h("span", { className: "jw-history-date" },
                    entry.savedAt ? new Date(entry.savedAt).toLocaleString() : ""),
                  h("span", { className: "jw-hint" }, entry.channelCount + " channels"),
                  h("button", {
                    className: "jw-btn jw-btn-small", disabled: !changed,
                    title: changed ? "Stage this version as the current draft" : "This channel did not change in that revision",
                    onClick: () => { onRestore(clone(version)); onClose(); },
                  }, changed ? "Restore…" : "unchanged"),
                );
              }),
            ),
    );
  }

  // ------------------------------------------------------------------
  // Number-resolution sheet (plan §3.2): ONE component for create,
  // duplicate, direct renumber, and Organize single-row conflicts. The
  // opening explain:true preview powers the whole choice matrix in ONE
  // round-trip; per-choice re-preview only happens for typed targets.
  // New channels never see Swap (no original slot). Staging is local truth
  // (org store); nothing writes until the owner Applies the arrangement.
  // ------------------------------------------------------------------

  function NumberResolutionSheet({ lib, drafts, subject, target, onStage, onClose }) {
    const isTemp = subject.isNew || String(subject.ref).startsWith("temp-");
    const committed = isTemp ? null : (lib.channels || []).find((c) => c.id === subject.ref) || null;
    const tempEntry = isTemp && drafts ? drafts.get(subject.ref) : null;
    const kind = subject.kind
      || (committed && committed.kind)
      || (tempEntry && tempEntry.draft && tempEntry.draft.kind)
      || "net";
    const band = BANDS[kind] || BANDS.net;
    const subjectName = isTemp
      ? String((tempEntry && tempEntry.draft && tempEntry.draft.name) || "(unnamed draft)")
      : String((committed && committed.name) || subject.ref);
    const currentNumber = isTemp
      ? (tempEntry && tempEntry.draft ? tempEntry.draft.number : null)
      : (committed ? committed.number : null);

    const [dest, setDest] = useState(target);
    const [choice, setChoice] = useState("insert-up");
    const [freePick, setFreePick] = useState(null);
    const [relocateTo, setRelocateTo] = useState(null);
    const [explain, setExplain] = useState(null); // opening explain response
    const [resp, setResp] = useState(null);       // the ACTIVE choice's preview
    const [respKey, setRespKey] = useState(null); // its binding fingerprint (R1)
    const [busy, setBusy] = useState(false);
    const [loadError, setLoadError] = useState(null);
    const seqRef = useRef(0);
    const aliveRef = useRef(true);
    const tokenRef = useRef(newCorrelationToken());
    useEffect(() => () => { aliveRef.current = false; }, []);

    const overlays = useMemo(() => (
      isTemp && tempEntry && tempEntry.draft
        ? [{
            tempRef: subject.ref,
            kind,
            name: tempEntry.draft.name || undefined,
            groupId: tempEntry.draft.groupId || undefined,
          }]
        : []
    ), [isTemp, tempEntry, subject.ref, kind]);

    // R5: an uncommitted group rides the SAME preview/Apply — the planner
    // validates it via the "groups" arg and emits its group.put first.
    const pendingGroups = useMemo(() => {
      if (!isTemp || !tempEntry || !tempEntry.draft || tempEntry.draft.pendingGroupName == null) return [];
      const name = String(tempEntry.draft.pendingGroupName || "").trim();
      const gid = tempEntry.draft.groupId;
      return name && gid ? [{ id: gid, name, position: (lib.groups || []).length + 1 }] : [];
    }, [isTemp, tempEntry, lib]);

    // UX4/R5: a whole create+arrangement commit needs real source rules —
    // the placement sheet says so instead of offering a Stage-able plan
    // that predictably rejects at Apply.
    const rulesComplete = useMemo(() => {
      if (!isTemp || !tempEntry || !tempEntry.draft) return true;
      const src = tempEntry.draft.source;
      if (!isRuleSource(src)) return false;
      const wire = toWireSource(src);
      const hasRule = FACET_KEYS.some((k) => wire[k] && wire[k].length)
        || wire.date || wire.duration || wire.createdAt || wire.q
        || wire.studioSceneCount || wire.performerSceneCount;
      return hasRule && ruleSourceClientErrors(wire).length === 0;
    }, [isTemp, tempEntry]);

    const occupantOf = (n) => Number.isInteger(n)
      ? (lib.channels || []).find((c) => c.number === n && c.id !== subject.ref) || null
      : null;
    const occupant = occupantOf(dest);

    const subjectIntentKey = isTemp ? { tempRef: subject.ref } : { channelId: subject.ref };

    // The intent the CURRENT controls describe — one construction shared by
    // the debounced preview effect and the R1 stage-eligibility check, so a
    // response can only ever be staged against the exact controls that
    // produced it.
    const activeChoice = useMemo(() => {
      if (!Number.isInteger(dest)) return null;
      if (choice === "free") {
        if (!Number.isInteger(freePick)) return null;
        return { intent: Object.assign({ type: "insert", number: freePick, direction: "up" }, subjectIntentKey) };
      }
      if (choice === "swap") {
        if (!occupant) return null;
        return { intent: { type: "swap", a: subject.ref, b: occupant.id } };
      }
      if (choice === "relocate") {
        if (!occupant || !Number.isInteger(relocateTo)) return null;
        return { intent: { type: "relocate_occupant", channelId: occupant.id, to: relocateTo } };
      }
      return { intent: Object.assign({ type: "insert", number: dest, direction: choice === "insert-down" ? "down" : "up" }, subjectIntentKey) };
    }, [choice, dest, freePick, relocateTo, occupant, subject.ref, isTemp]);
    const activeKey = activeChoice
      ? previewFingerprint({ intent: activeChoice.intent, overlays, groups: pendingGroups, revision: lib.revision })
      : null;

    async function runPreview(intent, withExplain, key) {
      const seq = ++seqRef.current;
      setBusy(true);
      setLoadError(null);
      try {
        const out = await previewChannelArrangement({
          expectedRevision: lib.revision,
          correlationToken: tokenRef.current,
          intent,
          overlays,
          groups: pendingGroups,
          explain: !!withExplain,
        });
        if (!aliveRef.current || seq !== seqRef.current) return;
        // Stale-guard (frozen contract decision 3): a slow response can
        // never replace a newer review — (revision, token) must match.
        if (out && out.correlationToken && out.correlationToken !== tokenRef.current) return;
        if (withExplain) setExplain(out);
        setResp(out);
        setRespKey(key != null ? key : previewFingerprint({ intent, overlays, groups: pendingGroups, revision: lib.revision }));
      } catch (e) {
        if (!aliveRef.current || seq !== seqRef.current) return;
        setResp(null);
        setRespKey(null);
        setLoadError(String((e && e.message) || e));
      } finally {
        if (aliveRef.current && seq === seqRef.current) setBusy(false);
      }
    }

    // Opening call: insert-up at the destination with the full choice matrix.
    useEffect(() => {
      if (!Number.isInteger(dest)) return undefined;
      const intent = Object.assign({ type: "insert", number: dest, direction: "up" }, subjectIntentKey);
      const key = previewFingerprint({ intent, overlays, groups: pendingGroups, revision: lib.revision });
      void runPreview(intent, true, key);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Re-preview when the destination or the active choice's parameter moves.
    useEffect(() => {
      if (!activeChoice) { setResp(null); setRespKey(null); return undefined; }
      const t = setTimeout(() => { void runPreview(activeChoice.intent, false, activeKey); }, 250);
      return () => clearTimeout(t);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeKey]);

    const choices = (explain && explain.choices) || {};
    const matrix = [
      {
        key: "free",
        label: "Use a free number",
        available: !!choices.free,
        detail: choices.free ? "Suggestions: " + (choices.free.list || []).slice(0, 5).join(", ") : null,
        reason: choices.free ? null : "No free number is left in " + band[0] + "–" + band[1] + ".",
      },
      !isTemp ? {
        key: "swap",
        label: "Swap numbers",
        available: !!(choices.swap && choices.swap.available),
        detail: choices.swap && choices.swap.available
          ? "“" + choices.swap.withName + "” (" + choices.swap.withNumber + ") takes "
            + (currentNumber != null ? "this channel's current number " + currentNumber : "the old slot") + "."
          : null,
        reason: choices.swap && !choices.swap.available ? (choices.swap.reason || "Swap is not possible here.") : null,
      } : null,
      {
        key: "insert-up",
        label: "Insert and shift upward",
        available: !choices.shiftUp || choices.shiftUp.available !== false,
        detail: choices.shiftUp && choices.shiftUp.available
          ? choices.shiftUp.movedCount + " existing channel" + (choices.shiftUp.movedCount === 1 ? "" : "s")
            + " move; the shift stops at the first free number" + (choices.shiftUp.firstFree != null ? " (" + choices.shiftUp.firstFree + ")" : "") + "."
          : null,
        reason: choices.shiftUp && choices.shiftUp.available === false ? choices.shiftUp.reason : null,
      },
      {
        key: "insert-down",
        label: "Insert and shift downward",
        available: !choices.shiftDown || choices.shiftDown.available !== false,
        detail: choices.shiftDown && choices.shiftDown.available
          ? choices.shiftDown.movedCount + " existing channel" + (choices.shiftDown.movedCount === 1 ? "" : "s")
            + " move; the shift stops at the first free number" + (choices.shiftDown.firstFree != null ? " (" + choices.shiftDown.firstFree + ")" : "") + "."
          : null,
        reason: choices.shiftDown && choices.shiftDown.available === false ? choices.shiftDown.reason : null,
      },
      {
        key: "relocate",
        label: "Relocate the occupant",
        available: !!occupant && (!choices.relocate || choices.relocate.available !== false),
        detail: occupant ? "“" + occupant.name + "” moves to a free number you pick." : null,
        reason: !occupant && choice === "relocate" ? "The destination is free — nothing to relocate." : null,
      },
    ].filter(Boolean);

    // The active plan's affected rows (authoritative: rendered, never
    // recomputed client-side).
    const planRows = useMemo(() => {
      const rows = [];
      const src = resp || null;
      for (const nc of (src && src.numberChanges) || []) {
        const ref = nc.channelId || nc.tempRef;
        if (!ref) continue;
        const c = nc.channelId ? (lib.channels || []).find((x) => x.id === nc.channelId) : null;
        const t = nc.tempRef && drafts ? drafts.get(nc.tempRef) : null;
        rows.push({
          key: "n-" + ref,
          name: c ? c.name : (t && t.draft ? t.draft.name : ref),
          glyph: c ? c.glyph : (t && t.draft ? t.draft.glyph : null),
          color: c ? c.color : (t && t.draft ? t.draft.color : null),
          number: [nc.from, nc.to],
          reason: nc.reason,
          displaced: !nc.selected,
        });
      }
      for (const cr of (src && src.created) || []) {
        const t = drafts ? drafts.get(cr.tempRef) : null;
        rows.push({
          key: "c-" + cr.tempRef,
          name: t && t.draft ? t.draft.name : cr.tempRef,
          glyph: t && t.draft ? t.draft.glyph : null,
          color: t && t.draft ? t.draft.color : null,
          number: [null, cr.number],
          reason: "direct",
          displaced: false,
          isNew: true,
        });
      }
      return rows;
    }, [resp, lib, drafts]);

    const stage = () => {
      // R1: only the response bound to the CURRENT controls may freeze —
      // never a stale plan under a new destination label.
      if (!bound || !resp.valid || resp.noop || !rulesComplete) return;
      // Relocate: the preview packet covers the OCCUPANT; the subject's own
      // placement augments the packet (contract-sanctioned): existing rows
      // join the renumber map, pending creations append their skeleton.
      let work = resp;
      let subjectBeforeAfter = null;
      if (choice === "relocate") {
        work = clone(resp);
        work.packet = clone(resp.packet || { expectedRevision: resp.revision, ops: [] });
        const finalForSubject = dest;
        if (isTemp) {
          const d = tempEntry && tempEntry.draft ? tempEntry.draft : {};
          work.packet.ops.push({
            op: "channel.create", tempId: subject.ref,
            channel: { kind, number: finalForSubject, name: d.name || "", groupId: d.groupId || null },
          });
          work.created = (work.created || []).concat([{ tempRef: subject.ref, number: finalForSubject, groupId: d.groupId || null }]);
        } else {
          let renum = work.packet.ops.find((o) => o.op === "channels.renumber");
          if (!renum) { renum = { op: "channels.renumber", assignments: [] }; work.packet.ops.push(renum); }
          renum.assignments.push({ channelId: subject.ref, number: finalForSubject });
          work.numberChanges = (work.numberChanges || []).concat([{
            channelId: subject.ref, tempRef: null, from: currentNumber, to: finalForSubject,
            selected: true, reason: "direct",
          }]);
        }
        subjectBeforeAfter = { number: [currentNumber, finalForSubject] };
      }
      // R5: the freeze includes the pending group's group.put, not just the
      // channel skeleton — one Apply creates the group AND places the row.
      const packet = freezeArrangementPacket(work, { drafts, pendingGroups });
      const beforeAfter = beforeAfterFromResponse(work);
      if (subjectBeforeAfter) beforeAfter[subject.ref] = subjectBeforeAfter;
      const finalNumber = choice === "free" ? freePick : dest;
      onStage({
        baseRevision: work.revision,
        correlationToken: tokenRef.current,
        label: "Place “" + subjectName.slice(0, 40) + "” at " + finalNumber,
        intent: { type: "number-resolution", choice, subject: subject.ref, number: finalNumber },
        response: work,
        packet,
        beforeAfter,
        tempRefs: tempRefsFromResponse(work),
        stagedAt: Date.now(),
      }, finalNumber);
    };

    const noop = resp && resp.valid && resp.noop;
    const respErrors = (resp && resp.errors) || [];
    // R1: stageable requires the bound response to match the live controls.
    const bound = !!(resp && respKey && activeKey && respKey === activeKey);
    const stageable = !!(bound && resp.valid && !resp.noop && !busy && rulesComplete);

    return h(Dialog, {
      title: "Place “" + subjectName + "” at " + (Number.isInteger(dest) ? dest : "…"),
      onClose, dialogClass: "jw-sheet-numres",
      footer: [
        h("button", { key: "c", className: "jw-btn", onClick: onClose }, "Cancel — keep drafts"),
        h("button", {
          key: "s", className: "jw-btn jw-btn-primary", disabled: !stageable,
          title: !rulesComplete
            ? "Finish the channel's rules first — a channel with no source can't go on the air"
            : stageable
              ? "Stage this plan locally — nothing is written until Apply arrangement"
              : "Pick an available choice first",
          onClick: stage,
        }, busy ? "Planning…" : "Stage arrangement"),
      ],
    },
      !rulesComplete && isTemp
        ? h("div", { className: "jw-note-warn", role: "status" },
            "Finish this channel's rules before placing it — a channel with no source can't go on the air. ",
            "Close this sheet, add at least one rule in the editor, then come back — your placement choices are kept.")
        : null,
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-numres-dest" },
          "Destination number (" + band[0] + "–" + band[1] + ")"),
        h("input", {
          id: "jw-numres-dest", className: "jw-input jw-num-wide", type: "number",
          min: band[0], max: band[1], value: dest != null ? dest : "",
          onChange: (e) => setDest(e.target.value === "" ? null : parseInt(e.target.value, 10)),
        }),
        occupant
          ? h("p", { className: "jw-hint" }, dest + " is “" + occupant.name + "”.")
          : Number.isInteger(dest)
            ? h("p", { className: "jw-hint" }, dest + " is free.")
            : null,
        currentNumber != null && Number.isInteger(dest) && dest !== currentNumber && !isTemp
          ? h("p", { className: "jw-hint" }, "The channel keeps " + currentNumber + " until you Apply the staged arrangement.")
          : null,
      ),
      h("div", { className: "jw-choice-list", role: "radiogroup", "aria-label": "Placement choices" },
        matrix.map((row) => h("label", {
          key: row.key,
          className: "jw-choice" + (choice === row.key ? " jw-choice-active" : "")
            + (row.available === false ? " jw-choice-off" : ""),
        },
          h("input", {
            type: "radio", name: "jw-numres-choice", value: row.key,
            checked: choice === row.key, disabled: row.available === false,
            onChange: () => setChoice(row.key),
          }),
          h("span", { className: "jw-choice-body" },
            h("span", { className: "jw-choice-label" }, row.label),
            row.available === false && row.reason
              ? h("span", { className: "jw-choice-reason" }, row.reason)
              : (row.detail ? h("span", { className: "jw-choice-detail" }, row.detail) : null),
            choice === row.key && row.key === "free" ? h("span", { className: "jw-choice-ctrl" },
              ((choices.free && choices.free.list) || []).slice(0, 5).map((n) => h("button", {
                key: n, type: "button",
                className: "jw-chip" + (freePick === n ? " jw-chip-active" : ""),
                "aria-pressed": freePick === n ? "true" : "false",
                onClick: () => setFreePick(n),
              }, String(n))),
              h("input", {
                type: "number", className: "jw-input jw-num-wide", min: band[0], max: band[1],
                placeholder: "free number", "aria-label": "Chosen free number",
                value: freePick != null ? freePick : "",
                onChange: (e) => setFreePick(e.target.value === "" ? null : parseInt(e.target.value, 10)),
              }),
            ) : null,
            choice === row.key && row.key === "relocate" && occupant ? h("span", { className: "jw-choice-ctrl" },
              h("span", { className: "jw-hint" }, "Send “" + occupant.name + "” to"),
              h("input", {
                type: "number", className: "jw-input jw-num-wide", min: band[0], max: band[1],
                placeholder: "free number", "aria-label": "New number for " + occupant.name,
                value: relocateTo != null ? relocateTo : "",
                onChange: (e) => setRelocateTo(e.target.value === "" ? null : parseInt(e.target.value, 10)),
              }),
              Number.isInteger(relocateTo) && occupantOf(relocateTo)
                ? h("span", { className: "jw-error-text" }, relocateTo + " is taken by “" + occupantOf(relocateTo).name + "”.")
                : null,
            ) : null,
          ),
        ))),
      loadError ? h("p", { className: "jw-error-text", role: "alert" }, loadError) : null,
      respErrors.length
        ? h("div", { className: "jw-error-text", role: "alert" },
            respErrors.map((e, i) => h("div", { key: i }, e.message || String(e.code || e))))
        : null,
      noop ? h("p", { className: "jw-hint" }, "Nothing would change — the channel is already there.") : null,
      (resp && (resp.warnings || []).length)
        ? h("div", { className: "jw-note-warn" }, resp.warnings.map((w, i) => h("div", { key: i }, w.message || String(w.code || w))))
        : null,
      planRows.length
        ? h("div", { className: "jw-field" },
            h("span", { className: "jw-field-label" }, "What will happen (" + planRows.length + " row" + (planRows.length === 1 ? "" : "s") + ")"),
            h("div", { className: "jw-review-list jw-review-list-short" },
              planRows.map((r) => h("div", {
                key: r.key,
                className: "jw-review-row" + (r.displaced ? " jw-review-row-displaced" : ""),
              },
                // R11: glyph + name are ONE Channel cell so the row honors
                // the same explicit grid the organizer review uses.
                h("span", { className: "jw-review-channel", title: r.name },
                  h(GlyphTile, { codepoint: r.glyph, color: r.color, size: 24, fallback: r.number[1] }),
                  h("span", { className: "jw-review-name" }, r.name,
                    r.isNew ? h(StatusChip, { kind: "accent" }, "new") : null)),
                h("span", { className: "jw-review-move", tabIndex: 0,
                  "aria-label": (r.number[0] != null ? r.number[0] : "—") + " → " + (r.number[1] != null ? r.number[1] : "—"),
                  title: (r.number[0] != null ? r.number[0] : "—") + " → " + (r.number[1] != null ? r.number[1] : "—") },
                  r.number[0] != null ? String(r.number[0]) : "—", " → ", r.number[1] != null ? String(r.number[1]) : "—"),
                h("span", { className: "jw-review-reason", tabIndex: 0,
                  "aria-label": (r.displaced ? "Displaced — " : "Selected — ") + reasonText(r.reason),
                  title: (r.displaced ? "Displaced — " : "Selected — ") + reasonText(r.reason) },
                  (r.displaced ? "Displaced — " : "Selected — ") + reasonText(r.reason)),
              ))),
          )
        : (busy ? h("p", { className: "jw-hint" }, "Planning…") : null),
    );
  }

  // ------------------------------------------------------------------
  // Organize sheet (plan §3.3): five sequential sections, no tabs.
  //   1 Channels · 2 Action · 3 Destination and order · 4 Conflicts
  //   5 Review (virtualized). Stage writes LOCAL truth only; the sticky
  //   arrangement bar owns the explicit Apply.
  // ------------------------------------------------------------------

  const ORG_ACTIONS = [
    { key: "assign_group", label: "Assign group" },
    { key: "arrange_range", label: "Arrange within range" },
    { key: "move_block", label: "Move block to start" },
    { key: "shift_interval", label: "Shift interval by offset" },
  ];

  function OrganizeSheet({
    lib, drafts, groups, tempRows, filteredIds, selectionIds,
    initialScope, initialIntent, reviewStaged, cachedForm,
    onStage, onSplitSelection, onBackToEdit, onFormChange, onOpenDraft,
    onReplan, onClose,
  }) {
    const committed = lib.channels || [];
    const byId = useMemo(() => new Map(committed.map((c) => [c.id, c])), [lib]);
    const tempById = useMemo(() => new Map((tempRows || []).map((t) => [t.id, t])), [tempRows]);

    // R8: the FULL editable intent survives staging, reload and replan.
    // `seed` is the persisted intent (an explicit staged intent wins over
    // the cached partial form from a previously closed sheet).
    const seed = initialIntent || cachedForm || null;

    // ---- section 1: Channels (scope) ----
    const [scopeType, setScopeType] = useState(() => {
      if (initialScope && initialScope.type) return initialScope.type;
      if (seed && seed.scopeType) return seed.scopeType;
      return selectionIds && selectionIds.size ? "selection" : "group";
    });
    const [scopeGroupId, setScopeGroupId] = useState(() => {
      if (initialScope && initialScope.groupId) return initialScope.groupId;
      if (seed && seed.scopeGroupId && (groups || []).some((g) => g.id === seed.scopeGroupId)) return seed.scopeGroupId;
      return groups[0] ? groups[0].id : "";
    });
    const [scopeStart, setScopeStart] = useState(() => (
      initialScope && initialScope.range ? initialScope.range.start
        : seed && Number.isInteger(seed.scopeStart) ? seed.scopeStart : null));
    const [scopeEnd, setScopeEnd] = useState(() => (
      initialScope && initialScope.range ? initialScope.range.end
        : seed && Number.isInteger(seed.scopeEnd) ? seed.scopeEnd : null));

    // ---- section 2: Action ----
    const [action, setAction] = useState(() => (seed && seed.action) || "arrange_range");
    // Task mode: opened from a group header — the group is fixed and the
    // flow is Destination → Conflicts → Review (UX1). "Change selection"
    // reveals the full generic scope controls.
    const taskGroup = !reviewStaged && initialScope && initialScope.type === "group"
      ? (groups || []).find((g) => g.id === initialScope.groupId) || null : null;
    const [advancedScope, setAdvancedScope] = useState(false);

    // ---- section 3: Destination and order ----
    const [destGroupId, setDestGroupId] = useState(() => (
      seed && seed.destGroupId && (groups || []).some((g) => g.id === seed.destGroupId)
        ? seed.destGroupId : groups[0] ? groups[0].id : ""));
    const [newGroupName, setNewGroupName] = useState(() => (seed && seed.newGroupName) || "");
    // One stable client id per sheet open for the inline-created group (R5).
    const [pendingGid] = useState(newGroupId);
    const [rangeStart, setRangeStart] = useState(() => (
      seed && seed.range && Number.isInteger(seed.range.start) ? seed.range.start : null));
    const [rangeEnd, setRangeEnd] = useState(() => (
      seed && seed.range && Number.isInteger(seed.range.end) ? seed.range.end : null));
    const [blockStart, setBlockStart] = useState(() => (seed && seed.start != null ? seed.start : null));
    const [offset, setOffset] = useState(() => (seed && seed.offset != null ? seed.offset : 10));
    const [order, setOrder] = useState(() => normalizeOrder(seed && seed.order));

    // ---- section 4: Conflicts ----
    const [strategy, setStrategy] = useState(() => (seed && seed.strategy) || "useAvailable");
    const [outStart, setOutStart] = useState(() => (seed && seed.outside ? seed.outside.start : null));
    const [outEnd, setOutEnd] = useState(() => (seed && seed.outside ? seed.outside.end : null));
    // Optional combination (plan §3.4 note): a range arrangement can also
    // assign the selection to a group in the SAME reviewed packet.
    const [alsoAssign, setAlsoAssign] = useState(() => !!(seed && seed.alsoAssign));

    // ---- section 5: Review ----
    const [resp, setResp] = useState(reviewStaged ? reviewStaged.response : null);
    const [respKey, setRespKey] = useState(null); // R1 binding fingerprint
    const [busy, setBusy] = useState(false);
    const [loadError, setLoadError] = useState(null);
    const [reviewFilter, setReviewFilter] = useState("");
    const [optIns, setOptIns] = useState(() => new Set());
    const seqRef = useRef(0);
    const aliveRef = useRef(true);
    const tokenRef = useRef(reviewStaged ? reviewStaged.correlationToken : newCorrelationToken());
    useEffect(() => () => { aliveRef.current = false; }, []);

    // Resolve the scope to concrete refs (existing ids + temp refs).
    const scopeRefs = useMemo(() => {
      let refs = [];
      if (scopeType === "selection") {
        refs = [...(selectionIds || [])];
      } else if (scopeType === "group") {
        refs = committed.filter((c) => c.groupId === scopeGroupId).map((c) => c.id)
          .concat((tempRows || []).filter((t) => t.groupId === scopeGroupId).map((t) => t.id));
      } else if (scopeType === "interval") {
        if (Number.isInteger(scopeStart) && Number.isInteger(scopeEnd)) {
          const lo = Math.min(scopeStart, scopeEnd);
          const hi = Math.max(scopeStart, scopeEnd);
          refs = committed.filter((c) => c.number >= lo && c.number <= hi).map((c) => c.id)
            .concat((tempRows || []).filter((t) => t.number != null && t.number >= lo && t.number <= hi).map((t) => t.id));
        }
      } else if (scopeType === "filter") {
        refs = (filteredIds || []).slice();
      }
      // deterministic: ascending CURRENT number, ties by ref (temps sort last)
      const numOf = (ref) => {
        const c = byId.get(ref);
        if (c) return c.number;
        const t = tempById.get(ref);
        return t && t.number != null ? t.number : Number.MAX_SAFE_INTEGER;
      };
      return refs.slice().sort((a, b) => numOf(a) - numOf(b) || String(a).localeCompare(String(b)));
    }, [scopeType, scopeGroupId, scopeStart, scopeEnd, selectionIds, filteredIds, committed, tempRows, byId, tempById]);

    const scopeChannelIds = scopeRefs.filter((r) => !String(r).startsWith("temp-"));
    const scopeTempRefs = scopeRefs.filter((r) => String(r).startsWith("temp-"));

    // F7: a temp row in scope whose source rules are incomplete would make
    // the plan Stage-able only to fail honestly at Apply (validation_failed).
    // Gate Stage and tell the owner exactly which draft to finish. Same rule
    // as the number-resolution sheet's rulesComplete — one policy, two sheets.
    const incompleteTemps = (() => {
      if (!drafts || reviewStaged) return [];
      const bad = [];
      for (const ref of scopeTempRefs) {
        const entry = drafts.get(ref);
        const d = entry && entry.draft;
        if (!d) continue;
        const src = d.source;
        if (!isRuleSource(src)) { bad.push({ ref, name: (d.name || "").trim() || "Unnamed channel" }); continue; }
        const wire = toWireSource(src);
        const hasRule = FACET_KEYS.some((k) => wire[k] && wire[k].length)
          || wire.date || wire.duration || wire.createdAt || wire.q
          || wire.studioSceneCount || wire.performerSceneCount;
        if (!hasRule || ruleSourceClientErrors(wire).length) {
          bad.push({ ref, name: (d.name || "").trim() || "Unnamed channel" });
        }
      }
      return bad;
    })();
    // A selection scope keeps rows hidden by the current filter BY DESIGN —
    // the scope line says so explicitly, never a silent acting-on-hidden.
    const hiddenInScope = useMemo(() => {
      if (scopeType !== "selection") return 0;
      const shown = new Set(filteredIds || []);
      return [...(selectionIds || [])].filter((id) => !shown.has(id)).length;
    }, [scopeType, selectionIds, filteredIds]);
    const scopeKinds = useMemo(() => {
      const kinds = new Set();
      for (const ref of scopeRefs) {
        const c = byId.get(ref);
        if (c) kinds.add(c.kind || "net");
        else if (tempById.has(ref)) kinds.add(tempById.get(ref).kind || "net");
      }
      return kinds;
    }, [scopeRefs, byId, tempById]);
    const mixedScope = scopeKinds.size > 1;

    const overlays = useMemo(() => scopeTempRefs.map((ref) => {
      const t = tempById.get(ref) || {};
      return { tempRef: ref, kind: t.kind || "net", name: t.name || undefined, groupId: t.groupId || undefined };
    }), [scopeTempRefs, tempById]);

    function buildIntent() {
      if (action === "shift_interval") {
        if (!Number.isInteger(scopeStart) || !Number.isInteger(scopeEnd) || !Number.isInteger(offset) || offset === 0) return null;
        return { type: "shift_interval", range: { start: Math.min(scopeStart, scopeEnd), end: Math.max(scopeStart, scopeEnd) }, offset };
      }
      if (!scopeRefs.length) return null;
      if (action === "assign_group") {
        const base = { type: "assign_group", channelIds: scopeChannelIds, tempRefs: scopeTempRefs };
        if (newGroupName.trim()) base.groupId = pendingGid; // created in the same packet (R5)
        else if (destGroupId) base.groupId = destGroupId;
        else return null;
        return base;
      }
      if (action === "arrange_range") {
        if (!Number.isInteger(rangeStart) || !Number.isInteger(rangeEnd)) return null;
        const intent = {
          type: "arrange_range", channelIds: scopeChannelIds, tempRefs: scopeTempRefs,
          range: { start: Math.min(rangeStart, rangeEnd), end: Math.max(rangeStart, rangeEnd) },
          order, strategy,
        };
        if (strategy === "exclusive") {
          if (!Number.isInteger(outStart) || !Number.isInteger(outEnd)) return null;
          intent.outside = { start: Math.min(outStart, outEnd), end: Math.max(outStart, outEnd) };
        }
        return intent;
      }
      if (action === "move_block") {
        if (!Number.isInteger(blockStart)) return null;
        // The given order IS the block order: number order by default,
        // alphabetical opt-in (id / "t:"+tempRef tie-break, mirroring the
        // server's deterministic rule).
        const nameOf = (ref) => {
          const c = byId.get(ref);
          if (c) return String(c.name || "");
          const t = tempById.get(ref);
          return t ? String(t.name || "") : "";
        };
        const ordered = order === "name"
          ? scopeRefs.slice().sort((a, b) => nameOf(a).toLowerCase().localeCompare(nameOf(b).toLowerCase())
              || (String(a).startsWith("temp-") ? "t:" + a : a).localeCompare(String(b).startsWith("temp-") ? "t:" + b : b))
          : scopeRefs; // already number-sorted
        return {
          type: "move_block",
          channelIds: ordered.filter((r) => !String(r).startsWith("temp-")),
          tempRefs: ordered.filter((r) => String(r).startsWith("temp-")),
          start: blockStart,
        };
      }
      return null;
    }

    const intent = buildIntent();
    const intentKey = JSON.stringify(intent) + "|" + lib.revision;

    // The optional assign_group half of a combined arrange_range packet.
    const assignIntent = (alsoAssign && action === "arrange_range" && scopeRefs.length)
      ? (() => {
          const base = { type: "assign_group", channelIds: scopeChannelIds, tempRefs: scopeTempRefs };
          if (newGroupName.trim()) base.groupId = pendingGid;
          else if (destGroupId) base.groupId = destGroupId;
          else return null;
          return base;
        })()
      : null;

    // R5: pending (uncommitted) groups told to the planner — it validates
    // them and emits their group.put FIRST, so one Apply creates the group
    // and assigns/places channels.
    const pendingGroups = useMemo(() => {
      const name = newGroupName.trim();
      return name ? [{ id: pendingGid, name, position: (groups || []).length + 1 }] : [];
    }, [newGroupName, pendingGid, groups]);

    // R1: the fingerprint of what the controls describe RIGHT NOW. Any
    // change (intent, overlays, pending groups, revision) moves this
    // immediately — before the debounce — so the previously bound response
    // stops being stageable the instant an input changes.
    const currentKey = previewFingerprint({
      intent, assign: assignIntent, overlays, groups: pendingGroups, revision: lib.revision,
    });

    useEffect(() => {
      if (reviewStaged) return undefined; // reopening a review: no re-preview
      if (!intent) { setResp(null); setRespKey(null); return undefined; }
      if (alsoAssign && action === "arrange_range" && !assignIntent) { setResp(null); setRespKey(null); return undefined; }
      const token = tokenRef.current;
      const seq = ++seqRef.current;
      const key = currentKey;
      const handle = setTimeout(async () => {
        setBusy(true);
        setLoadError(null);
        try {
          const rangeOut = await previewChannelArrangement({
            expectedRevision: lib.revision,
            correlationToken: token,
            intent,
            overlays,
            groups: pendingGroups,
          });
          if (!aliveRef.current || seq !== seqRef.current) return;
          if (rangeOut && rangeOut.correlationToken && rangeOut.correlationToken !== token) return;
          let out = rangeOut;
          if (assignIntent) {
            // Frozen contract §5: combine two same-revision packets —
            // group ops never touch numbers.
            const assignOut = await previewChannelArrangement({
              expectedRevision: lib.revision,
              correlationToken: token,
              intent: assignIntent,
              overlays,
              groups: pendingGroups,
            });
            if (!aliveRef.current || seq !== seqRef.current) return;
            if (assignOut && assignOut.correlationToken && assignOut.correlationToken !== token) return;
            out = combineArrangementResponses(assignOut, rangeOut);
          }
          setResp(out);
          setRespKey(key);
        } catch (e) {
          if (!aliveRef.current || seq !== seqRef.current) return;
          setResp(null);
          setRespKey(null);
          setLoadError(String((e && e.message) || e));
        } finally {
          if (aliveRef.current && seq === seqRef.current) setBusy(false);
        }
      }, 350);
      return () => clearTimeout(handle);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [currentKey, reviewStaged]);

    // UX7: the full editable form is reported up on every change so closing
    // the sheet preserves partially entered organization settings.
    const formState = useMemo(() => ({
      scopeType, scopeGroupId,
      scopeStart: Number.isInteger(scopeStart) ? scopeStart : null,
      scopeEnd: Number.isInteger(scopeEnd) ? scopeEnd : null,
      action,
      destGroupId, newGroupName: newGroupName.trim(),
      range: Number.isInteger(rangeStart) && Number.isInteger(rangeEnd)
        ? { start: Math.min(rangeStart, rangeEnd), end: Math.max(rangeStart, rangeEnd) } : null,
      start: Number.isInteger(blockStart) ? blockStart : null,
      offset: Number.isInteger(offset) ? offset : null,
      order: normalizeOrder(order),
      strategy,
      outside: strategy === "exclusive" && Number.isInteger(outStart) && Number.isInteger(outEnd)
        ? { start: Math.min(outStart, outEnd), end: Math.max(outStart, outEnd) } : null,
      alsoAssign,
      scopeCount: scopeRefs.length,
      revision: lib.revision,
    }), [scopeType, scopeGroupId, scopeStart, scopeEnd, action, destGroupId, newGroupName,
      rangeStart, rangeEnd, blockStart, offset, order, strategy, outStart, outEnd, alsoAssign,
      scopeRefs.length, lib.revision]);
    const formStateKey = JSON.stringify(formState);
    useEffect(() => {
      if (reviewStaged || !onFormChange) return;
      onFormChange(formState);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [formStateKey, reviewStaged]);

    // ---- review rows (merged number+group per ref) ----
    const reviewRows = useMemo(() => {
      const src = resp || {};
      const map = new Map();
      const rowFor = (ref) => {
        let row = map.get(ref);
        if (!row) {
          const c = byId.get(ref);
          const t = tempById.get(ref);
          row = {
            key: ref, ref,
            name: c ? c.name : (t ? t.name : ref),
            glyph: c ? c.glyph : (t ? t.glyph : null),
            color: c ? c.color : (t ? t.color : null),
            isTemp: !!t && !c,
            number: null, group: null, reasons: [], displaced: false,
            hasDraft: !!(drafts && !String(ref).startsWith("temp-") && drafts.get(ref)),
          };
          map.set(ref, row);
        }
        return row;
      };
      for (const nc of src.numberChanges || []) {
        const ref = nc.channelId || nc.tempRef;
        if (!ref) continue;
        const row = rowFor(ref);
        row.number = [nc.from, nc.to];
        row.reasons.push(nc.reason);
        if (!nc.selected) row.displaced = true;
      }
      for (const gc of src.groupChanges || []) {
        const ref = gc.channelId || gc.tempRef;
        if (!ref) continue;
        const row = rowFor(ref);
        row.group = [gc.from, gc.to];
      }
      for (const cr of src.created || []) {
        const row = rowFor(cr.tempRef);
        row.number = [null, cr.number];
        row.reasons.push("direct");
        row.isTemp = true;
      }
      let rows = [...map.values()];
      const q = reviewFilter.trim().toLowerCase();
      if (q) {
        rows = rows.filter((r) => r.name.toLowerCase().includes(q)
          || (r.number && (String(r.number[0]).includes(q) || String(r.number[1]).includes(q))));
      }
      return rows;
    }, [resp, byId, tempById, drafts, reviewFilter]);

    const groupName = (gid) => {
      const g = (groups || []).find((x) => x.id === gid);
      return g ? g.name : (gid || "—");
    };

    // UX5: the scannable split — selected rows vs displaced bystanders.
    const selectedRows = reviewRows.filter((r) => !r.displaced);
    const displacedRows = reviewRows.filter((r) => r.displaced);
    // R11: glyph + name are ONE Channel cell; every cell carries its full
    // text as a title (mouse) AND as an accessible name on a focusable cell
    // (F8: keyboard and screen-reader users get the full value too).
    const cellText = (full) => ({ tabIndex: 0, "aria-label": full, title: full });
    const renderReviewRow = (r) => h("div", {
      key: r.key,
      className: "jw-review-row" + (r.displaced ? " jw-review-row-displaced" : ""),
      role: "option", "aria-selected": r.displaced ? "false" : "true",
    },
      h("span", { className: "jw-review-channel", title: r.name },
        h(GlyphTile, { codepoint: r.glyph, color: r.color, size: 24, fallback: r.number ? r.number[1] : "—" }),
        h("span", { className: "jw-review-name" },
          r.name,
          r.isTemp ? h(StatusChip, { kind: "accent" }, "new") : null,
          r.hasDraft && !reviewStaged ? h("button", {
            className: "jw-link jw-review-optin",
            title: "This channel also has unsaved edits — include them in this Apply (its number and group are reconciled to the plan)",
            "aria-pressed": optIns.has(r.ref) ? "true" : "false",
            onClick: (e) => {
              e.stopPropagation();
              setOptIns((cur) => {
                const next = new Set(cur);
                if (next.has(r.ref)) next.delete(r.ref); else next.add(r.ref);
                return next;
              });
            },
          }, optIns.has(r.ref) ? "draft included ✓" : "include draft…") : null)),
      h("span", {
        className: "jw-review-move",
        ...cellText(r.number ? (r.number[0] != null ? r.number[0] : "new") + " → " + (r.number[1] != null ? r.number[1] : "—") : "unchanged"),
      },
        r.number
          ? (r.number[0] != null ? String(r.number[0]) : "—") + " → " + (r.number[1] != null ? String(r.number[1]) : "—")
          : ""),
      h("span", {
        className: "jw-review-group",
        ...cellText(r.group ? groupName(r.group[0]) + " → " + groupName(r.group[1]) : "unchanged"),
      }, r.group ? groupName(r.group[0]) + " → " + groupName(r.group[1]) : ""),
      h("span", {
        className: "jw-review-reason",
        ...cellText((r.displaced ? "Displaced — " : "Selected — ") + r.reasons.map(reasonText).join("; ")),
      },
        (r.displaced ? "Displaced — " : "Selected — ") + r.reasons.map(reasonText).join("; ")),
    );

    const respErrors = (resp && resp.errors) || [];
    const warnings = (resp && resp.warnings) || [];
    const noop = !!(resp && resp.valid && resp.noop);
    const capacity = resp && resp.capacity;
    const capacityLine = (() => {
      if (!capacity || action !== "arrange_range" || !capacity.range) return null;
      const r = capacity.range;
      const parts = [
        "Range " + r.start + "–" + r.end + " holds " + r.totalSlots + " slots",
        "selection needs " + (capacity.neededSlots != null ? capacity.neededSlots : scopeRefs.length),
      ];
      if (strategy === "exclusive") {
        parts.push(r.outsiderSlots + " outsider" + (r.outsiderSlots === 1 ? "" : "s") + " must move");
        if (capacity.outside) {
          parts.push(capacity.outside.freeSlots + " free numbers in "
            + capacity.outside.start + "–" + capacity.outside.end + " outside the range");
        }
      } else {
        parts.push(r.outsiderSlots + " outsider" + (r.outsiderSlots === 1 ? "" : "s") + " keep their numbers");
      }
      if (capacity.shortfall) parts.push("SHORT BY " + capacity.shortfall);
      return parts.join(" · ");
    })();

    // UX3: a compact, truthful occupancy strip for the chosen range —
    // derived from the committed library (archived/paused/disabled rows
    // still occupy), with blockers inspectable by number.
    const occupancy = useMemo(() => {
      if (action !== "arrange_range") return null;
      if (!Number.isInteger(rangeStart) || !Number.isInteger(rangeEnd)) return null;
      const lo = Math.min(rangeStart, rangeEnd);
      const hi = Math.max(rangeStart, rangeEnd);
      const total = hi - lo + 1;
      if (total < 1 || total > 400) return { lo, hi, total, tooWide: total > 400 };
      const inScope = new Set(scopeRefs);
      const byNumber = new Map();
      for (const c of committed) if (c.number != null && !byNumber.has(c.number)) byNumber.set(c.number, c);
      const tempByNumber = new Map();
      for (const t of (tempRows || [])) if (t.number != null && !tempByNumber.has(t.number)) tempByNumber.set(t.number, t);
      const cells = [];
      let inRange = 0, blockers = 0, free = 0;
      const blockerList = [];
      for (let n = lo; n <= hi; n++) {
        const c = byNumber.get(n);
        const t = tempByNumber.get(n);
        if (c && inScope.has(c.id)) {
          cells.push({ n, kind: "selected", title: n + " · " + c.name + " — selected, moves within the range" });
          inRange++;
        } else if (c) {
          cells.push({ n, kind: "occupied", title: n + " · " + c.name + " — stays unless you move other channels out" });
          blockers++;
          if (blockerList.length < 6) blockerList.push(n + " “" + c.name + "”");
        } else if (t) {
          cells.push({ n, kind: "temp", title: n + " · " + (t.name || "new draft") + " — pending draft" });
        } else {
          cells.push({ n, kind: "free", title: n + " — free" });
          free++;
        }
      }
      return { lo, hi, total, cells, inRange, blockers, free, blockerList };
    }, [action, rangeStart, rangeEnd, scopeRefs, committed, tempRows]);

    // R1: stageable ONLY when the bound response matches the live controls.
    const bound = !!(resp && respKey && !reviewStaged && respKey === currentKey);
    // F7: AND every temp row in scope has complete source rules.
    const stageable = !!(bound && resp.valid && !resp.noop && !busy
      && !(capacity && capacity.shortfall) && incompleteTemps.length === 0);

    // UX2: truthy consequence counts rendered next to the Stage/Apply
    // buttons — never generic "arrangement" wording.
    const reviewCounts = (() => {
      const rows = reviewRows;
      const moves = rows.filter((r) => !r.displaced && (r.number || r.group)).length;
      const displaced = rows.filter((r) => r.displaced).length;
      const placed = ((resp && resp.created) || []).length;
      return { moves, displaced, placed, total: rows.length };
    })();
    const stageButtonLabel = (() => {
      const n = reviewCounts.moves;
      if (action === "assign_group") return n ? "Stage " + n + " channel move" + (n === 1 ? "" : "s") : "Stage arrangement";
      if (action === "arrange_range" || action === "move_block") return n ? "Stage " + n + " channel move" + (n === 1 ? "" : "s") : "Stage arrangement";
      return "Stage arrangement";
    })();
    const consequenceLine = (() => {
      if (!resp || !resp.valid || noop) return null;
      const parts = [];
      if (reviewCounts.moves) parts.push(reviewCounts.moves + " of your channels move");
      if (action === "arrange_range" && strategy === "exclusive" && reviewCounts.displaced) {
        parts.push(reviewCounts.displaced + " other channel" + (reviewCounts.displaced === 1 ? "" : "s") + " relocate out of the range");
      } else if (reviewCounts.displaced) {
        parts.push(reviewCounts.displaced + " other channel" + (reviewCounts.displaced === 1 ? " keeps" : "s keep") + " their numbers");
      }
      if (reviewCounts.placed) parts.push(reviewCounts.placed + " new channel" + (reviewCounts.placed === 1 ? "" : "s") + " placed");
      parts.push("What airs stays the same");
      return parts.join(" · ");
    })();

    const stage = () => {
      if (!stageable) return;
      const optInPuts = {};
      for (const id of optIns) {
        const entry = drafts && drafts.get(id);
        if (entry && entry.draft) optInPuts[id] = toWireChannel(clone(entry.draft));
      }
      const packet = freezeArrangementPacket(resp, { drafts, optInPuts, pendingGroups });
      const actionLabel = {
        assign_group: "Assign " + scopeRefs.length + " channels to a group",
        arrange_range: "Arrange " + scopeRefs.length + " channels into "
          + Math.min(rangeStart, rangeEnd) + "–" + Math.max(rangeStart, rangeEnd),
        move_block: "Move a block of " + scopeRefs.length + " channels to " + blockStart,
        shift_interval: "Shift " + Math.min(scopeStart, scopeEnd) + "–" + Math.max(scopeStart, scopeEnd)
          + " by " + (offset > 0 ? "+" : "") + offset,
      }[action] || "Arrangement";
      // R8: persist the COMPLETE editable intent (scope + all settings), so
      // reload/replan reconstructs exactly what the owner authored.
      onStage({
        baseRevision: resp.revision,
        correlationToken: tokenRef.current,
        label: actionLabel,
        intent: Object.assign({}, formState, intent, {
          scopeType, action,
          range: intent.range || null, start: intent.start != null ? intent.start : null,
          offset: intent.offset != null ? intent.offset : null,
          order: normalizeOrder(order),
          pendingGid: newGroupName.trim() ? pendingGid : null,
        }),
        response: resp,
        packet,
        beforeAfter: beforeAfterFromResponse(resp),
        tempRefs: tempRefsFromResponse(resp),
        optIns: [...optIns],
        stagedAt: Date.now(),
      });
    };

    const numInput = (id, label, value, set, min, max, ariaLabel) => h("div", { className: "jw-field" },
      h("label", { className: "jw-field-label", htmlFor: id }, label),
      h("input", {
        id, className: "jw-input jw-num-wide", type: "number",
        min: min != null ? min : undefined, max: max != null ? max : undefined,
        "aria-label": ariaLabel || label,
        value: value != null ? value : "",
        onChange: (e) => set(e.target.value === "" ? null : parseInt(e.target.value, 10)),
      }));

    const sectionHead = (n, label) => h("h3", { className: "jw-orgsec-head" },
      h("span", { className: "jw-orgsec-num" }, n), label);

    const bandMixedError = respErrors.find((e) => e && e.code === "band_mixed");

    return h(Dialog, {
      title: reviewStaged ? "Review staged arrangement"
        : taskGroup ? "Place “" + taskGroup.name + "” in a range"
          : "Organize channels",
      onClose, wide: true, dialogClass: "jw-sheet-organize",
      footer: reviewStaged
        ? [
            // F6: a reversal staging has NO editable form — going "back to
            // edit" would seed the sheet with an intent no control
            // understands. Offer the honest action instead: replan it fresh.
            reviewStaged.intent && reviewStaged.intent.type === "reversal"
              ? h("button", { key: "b", className: "jw-btn", onClick: onReplan || onClose }, "Replan reversal")
              : h("button", { key: "b", className: "jw-btn", onClick: onBackToEdit || onClose }, "← Back to edit"),
            h("button", { key: "c", className: "jw-btn jw-btn-primary", onClick: onClose }, "Close review"),
          ]
        : [
            // UX2: the consequence summary sits next to the Stage button.
            h("span", {
              key: "sum", className: "jw-consequence", role: "status",
            }, consequenceLine || (busy ? "Planning…" : "Complete the sections above to preview the plan.")),
            h("button", { key: "c", className: "jw-btn", onClick: onClose }, "Close — keep staging"),
            h("button", {
              key: "s", className: "jw-btn jw-btn-primary", disabled: !stageable,
              title: stageable
                ? "Stage this plan locally — nothing is written until Apply arrangement"
                : (incompleteTemps.length
                  ? "Finish the new channels' source rules first — see the notice above"
                  : "The plan must be valid, match the current settings, and change something first"),
              onClick: stage,
            }, busy && !bound ? "Planning…" : stageButtonLabel),
          ],
    },
      reviewStaged ? null : [
        incompleteTemps.length ? h("div", {
          key: "s0", className: "jw-orgsec jw-orgsec-notice", role: "alert",
        },
          h("strong", null, "Finish the new channels' source rules before staging."),
          " A channel with no source can't go on the air, so this plan can't be staged yet:",
          incompleteTemps.map((t) => h("span", { key: t.ref, className: "jw-incomplete-temp" },
            h("button", {
              className: "jw-link",
              onClick: () => { if (onOpenDraft) onOpenDraft(t.ref); },
            }, "Open “" + t.name + "”…"),
          )),
        ) : null,
        // ---- 1. Channels and destination ----
        h("section", { key: "s1", className: "jw-orgsec", "aria-label": "Channels and destination" },
          sectionHead("1", taskGroup ? "Destination" : "Channels and destination"),
          taskGroup ? h("div", { className: "jw-task-scope" },
            h("span", { className: "jw-task-scope-line" },
              h("strong", null, "“" + taskGroup.name + "”"),
              " — " + scopeRefs.length + " channel" + (scopeRefs.length === 1 ? "" : "s") + " in this group"),
            h("button", {
              className: "jw-btn jw-btn-ghost jw-btn-small",
              "aria-expanded": advancedScope ? "true" : "false",
              onClick: () => setAdvancedScope((v) => !v),
            }, advancedScope ? "Use the whole group" : "Change selection…"),
          ) : null,
          (!taskGroup || advancedScope) ? h("div", { className: "jw-chip-row", role: "radiogroup", "aria-label": "Scope" },
            [
              ["selection", "Current selection" + (selectionIds && selectionIds.size ? " (" + selectionIds.size + ")" : "")],
              ["group", "An entire group"],
              ["interval", "A number interval"],
              ["filter", "The current filter"],
            ].map(([key, label]) => h("button", {
              key,
              className: "jw-chip" + (scopeType === key ? " jw-chip-active" : ""),
              "aria-pressed": scopeType === key ? "true" : "false",
              disabled: (key === "selection" && !(selectionIds && selectionIds.size))
                || (key === "filter" && !(filteredIds && filteredIds.length))
                || (action === "shift_interval" && key !== "interval"),
              onClick: () => setScopeType(key),
            }, label))) : null,
          (!taskGroup || advancedScope) && scopeType === "group" ? h("div", { className: "jw-field" },
            h("label", { className: "jw-field-label", htmlFor: "jw-org-group" }, "Group"),
            h("select", {
              id: "jw-org-group", className: "jw-input", value: scopeGroupId,
              onChange: (e) => setScopeGroupId(e.target.value),
            }, (groups || []).map((g) => h("option", { key: g.id, value: g.id }, g.name))),
          ) : null,
          (!taskGroup || advancedScope) && (scopeType === "interval" || action === "shift_interval") ? h("div", { className: "jw-fieldrow" },
            numInput("jw-org-scstart", "From number", scopeStart, setScopeStart, 1, 899),
            numInput("jw-org-scend", "To number", scopeEnd, setScopeEnd, 1, 899),
          ) : null,
          seed && seed.scopeCount != null && seed.scopeCount !== scopeRefs.length && scopeType === "group"
            ? h("p", { className: "jw-note-warn", role: "status" },
                "This group's membership changed since the plan was staged (was " + seed.scopeCount
                + ", now " + scopeRefs.length + ") — review the new scope before staging again.")
            : null,
          (!taskGroup || advancedScope) ? h("p", { className: "jw-scope-line" },
            action === "shift_interval"
              ? (Number.isInteger(scopeStart) && Number.isInteger(scopeEnd)
                  ? "Every channel currently in " + Math.min(scopeStart, scopeEnd) + "–" + Math.max(scopeStart, scopeEnd)
                    + " moves — the preview resolves the exact ids before you stage."
                  : "Pick the interval to shift.")
              : scopeRefs.length + " channel" + (scopeRefs.length === 1 ? "" : "s") + " in scope"
                + (scopeTempRefs.length ? " (" + scopeTempRefs.length + " not yet on the server — placed as drafts)" : "")
                + (hiddenInScope ? " — includes " + hiddenInScope + " hidden by the current filter" : "")) : null,
          (!taskGroup || advancedScope) && mixedScope && action !== "assign_group"
            ? h("p", { className: "jw-note-warn" },
                "The scope mixes My Channels (1–99) and networks (100–899) — a number arrangement needs one band. "
                + "Split the selection (via Select channels) and arrange each band separately — nothing is omitted silently.")
            : null,
          (!taskGroup || advancedScope) ? h("div", { className: "jw-chip-row", role: "radiogroup", "aria-label": "Action" },
            ORG_ACTIONS.map((a) => h("button", {
              key: a.key,
              className: "jw-chip" + (action === a.key ? " jw-chip-active" : ""),
              "aria-pressed": action === a.key ? "true" : "false",
              onClick: () => setAction(a.key),
            }, a.label))) : null,
          action === "assign_group" ? h("div", null,
            h("div", { className: "jw-field" },
              h("label", { className: "jw-field-label", htmlFor: "jw-org-dest" }, "Move to existing group"),
              h("select", {
                id: "jw-org-dest", className: "jw-input", value: destGroupId,
                disabled: !!newGroupName.trim(),
                onChange: (e) => setDestGroupId(e.target.value),
              }, (groups || []).map((g) => h("option", { key: g.id, value: g.id }, g.name))),
            ),
            h("div", { className: "jw-field" },
              h("label", { className: "jw-field-label", htmlFor: "jw-org-newgroup" }, "…or create a new group and move there"),
              h("input", {
                id: "jw-org-newgroup", className: "jw-input", placeholder: "New group name",
                value: newGroupName, onChange: (e) => setNewGroupName(e.target.value),
              }),
            ),
          ) : null,
          action === "arrange_range" ? h("div", { className: "jw-fieldrow" },
            numInput("jw-org-rstart", "Range start", rangeStart, setRangeStart, 1, 899),
            numInput("jw-org-rend", "Range end", rangeEnd, setRangeEnd, 1, 899),
          ) : null,
          action === "arrange_range" ? h("div", { className: "jw-field" },
            h("label", { className: "jw-check-line" },
              h("input", {
                type: "checkbox", checked: alsoAssign,
                onChange: (e) => setAlsoAssign(e.target.checked),
              }),
              " Also assign the selection to a group (same reviewed packet)"),
            alsoAssign ? h("div", { className: "jw-fieldrow", style: { marginTop: "6px" } },
              h("div", { className: "jw-field" },
                h("label", { className: "jw-field-label", htmlFor: "jw-org-dest2" }, "Group"),
                h("select", {
                  id: "jw-org-dest2", className: "jw-input", value: destGroupId,
                  disabled: !!newGroupName.trim(),
                  onChange: (e) => setDestGroupId(e.target.value),
                }, (groups || []).map((g) => h("option", { key: g.id, value: g.id }, g.name))),
              ),
              h("div", { className: "jw-field" },
                h("label", { className: "jw-field-label", htmlFor: "jw-org-newgroup2" }, "…or new group name"),
                h("input", {
                  id: "jw-org-newgroup2", className: "jw-input", placeholder: "New group name",
                  value: newGroupName, onChange: (e) => setNewGroupName(e.target.value),
                }),
              ),
            ) : null,
          ) : null,
          action === "move_block"
            ? numInput("jw-org-bstart", "Starting number", blockStart, setBlockStart, 1, 899)
            : null,
          action === "shift_interval"
            ? numInput("jw-org-offset", "Offset (e.g. 10 or -25)", offset, setOffset, -898, 898)
            : null,
          action !== "shift_interval" && action !== "assign_group" ? h("div", { className: "jw-field" },
            h("span", { className: "jw-field-label" }, "Order"),
            h("div", { className: "jw-chip-row", role: "radiogroup", "aria-label": "Order" },
              h("button", {
                className: "jw-chip" + (order === "number" ? " jw-chip-active" : ""),
                "aria-pressed": order === "number" ? "true" : "false",
                onClick: () => setOrder("number"),
              }, "Keep current number order"),
              h("button", {
                className: "jw-chip" + (order === "name" ? " jw-chip-active" : ""),
                "aria-pressed": order === "name" ? "true" : "false",
                onClick: () => setOrder("name"),
              }, "Alphabetical (A–Z, ties by channel id)")),
          ) : null,
        ),
        // ---- 2. Other channels in this range (conflicts) ----
        h("section", { key: "s2", className: "jw-orgsec", "aria-label": "Conflicts" },
          sectionHead("2", action === "arrange_range" ? "Other channels in this range" : "Conflicts"),
          action === "arrange_range" ? h("div", null,
            h("div", { className: "jw-chip-row", role: "radiogroup", "aria-label": "Outsider strategy" },
              h("button", {
                className: "jw-chip" + (strategy === "useAvailable" ? " jw-chip-active" : ""),
                "aria-pressed": strategy === "useAvailable" ? "true" : "false",
                onClick: () => setStrategy("useAvailable"),
              }, "Keep other channels in place"),
              h("button", {
                className: "jw-chip" + (strategy === "exclusive" ? " jw-chip-active" : ""),
                "aria-pressed": strategy === "exclusive" ? "true" : "false",
                onClick: () => setStrategy("exclusive"),
              }, "Move other channels out of this range")),
            strategy === "useAvailable"
              ? h("p", { className: "jw-hint" },
                  "Your channels take the free positions; channels already inside the range keep their numbers.")
              : h("div", null,
                  h("p", { className: "jw-hint" },
                    "Every other channel in the range moves to free numbers you pick below. "
                    + "This is a one-time arrangement — the range is never reserved."),
                  h("div", { className: "jw-fieldrow" },
                    numInput("jw-org-ostart", "Relocate other channels to: from", outStart, setOutStart, 1, 899),
                    numInput("jw-org-oend", "to", outEnd, setOutEnd, 1, 899),
                  ),
                ),
            occupancy ? h("div", { className: "jw-occupancy-wrap" },
              occupancy.tooWide || !occupancy.cells
                ? h("p", { className: "jw-hint" },
                    "Range " + occupancy.lo + "–" + occupancy.hi + " (" + occupancy.total.toLocaleString()
                    + " slots) is too wide for the visual strip — the Review below lists every affected channel.")
                : h("div", { className: "jw-occupancy", role: "img", "aria-label": "Occupancy of range " + occupancy.lo + "–" + occupancy.hi },
                    occupancy.cells.map((c) => h("span", {
                      key: c.n, className: "jw-occ jw-occ-" + c.kind, title: c.title,
                    }))),
              !occupancy.tooWide && occupancy.cells
                ? h("p", { className: "jw-hint" },
                    "Range " + occupancy.lo + "–" + occupancy.hi + ": " + occupancy.inRange + " selected · "
                    + occupancy.blockers + " other channel" + (occupancy.blockers === 1 ? "" : "s") + " · "
                    + occupancy.free + " free"
                    + (occupancy.blockerList.length
                        ? " — occupied by: " + occupancy.blockerList.join(", ") + (occupancy.blockers > 6 ? " …" : "")
                        : ""))
                : null,
            ) : null,
            capacityLine ? h("p", {
              className: "jw-scope-line" + (capacity && capacity.shortfall ? " jw-error-text" : ""),
              role: capacity && capacity.shortfall ? "alert" : undefined,
            }, capacityLine) : null,
          ) : h("p", { className: "jw-hint" },
            action === "shift_interval"
              ? "Shift never moves unselected blockers — collisions come back as typed errors you resolve one by one (for example with Move / insert…)."
              : "Displaced channels keep their relative order and push through free numbers; every one of them is listed in the Review below."),
        ),
      ],
      // ---- 3. Review ----
      h("section", { className: "jw-orgsec", "aria-label": "Review" },
        reviewStaged ? null : sectionHead("3", "Review"),
        reviewStaged && reviewStaged.label ? h("p", { className: "jw-scope-line" }, reviewStaged.label) : null,
        bandMixedError
          ? h("div", { className: "jw-review-errors", role: "alert" },
              h("div", { className: "jw-error-text" }, bandMixedError.message),
              onSplitSelection ? h("div", { className: "jw-chip-row" },
                h("button", {
                  className: "jw-btn jw-btn-small",
                  onClick: () => onSplitSelection("net"),
                }, "Use the network channels only"),
                h("button", {
                  className: "jw-btn jw-btn-small",
                  onClick: () => onSplitSelection("ch"),
                }, "Use the My Channels rows only")) : null)
          : null,
        !bandMixedError && respErrors.length
          ? h("div", { className: "jw-review-errors", role: "alert" },
              respErrors.map((e, i) => h("div", { key: i, className: "jw-error-text" },
                (e.path ? e.path + ": " : "") + (e.message || String(e.code || e)))))
          : null,
        warnings.length
          ? h("div", { className: "jw-note-warn" },
              // UX6: routine warnings read as everyday consequences; the raw
              // server diagnostics live in an expandable details area.
              warnings.map((w, i) => h("div", { key: i }, warningPlainText(w))),
              warnings.some((w) => !WARNING_COPY[w && w.code])
                ? h("details", { className: "jw-warn-details" },
                    h("summary", null, "Technical details"),
                    warnings.filter((w) => !WARNING_COPY[w && w.code]).map((w, i) => h("div", {
                      key: i, className: "jw-hint",
                    }, (w.code ? w.code + " — " : "") + (w.message || "")))) : null)
          : null,
        noop ? h("p", { className: "jw-hint" }, "Nothing would change with these settings.") : null,
        !resp && busy ? h("p", { className: "jw-hint" }, "Planning…") : null,
        !resp && !busy && !loadError && !intent && !reviewStaged
          ? h("p", { className: "jw-hint" }, "Complete the sections above to preview the plan.")
          : null,
        loadError ? h("p", { className: "jw-error-text", role: "alert" }, loadError) : null,
        resp && reviewRows.length
          ? h("div", { className: "jw-review-xscroll" },
              // UX5: a sticky affected-count summary + the review split into
              // "Your selected channels" vs "Other channels that move".
              h("div", { className: "jw-review-sticky" },
                h("div", { className: "jw-review-tools" },
                  h("input", {
                    type: "search", className: "jw-input jw-chip-filter",
                    placeholder: "Filter review rows…", "aria-label": "Filter review rows",
                    value: reviewFilter, onChange: (e) => setReviewFilter(e.target.value),
                  }),
                  h("span", { className: "jw-hint" },
                    selectedRows.length + " selected channel" + (selectedRows.length === 1 ? "" : "s") + " move"
                    + (displacedRows.length
                        ? " · " + displacedRows.length + " other" + (displacedRows.length === 1 ? " moves" : "s move")
                        : ""))),
              ),
              h("div", { className: "jw-review-head" },
                h("span", { className: "jw-review-h-channel" }, "Channel"),
                h("span", { className: "jw-review-h-move" }, "Number"),
                h("span", { className: "jw-review-h-group" }, "Group"),
                h("span", { className: "jw-review-h-reason" }, "Why"),
              ),
              selectedRows.length
                ? h("div", { className: "jw-review-subhead", role: "heading", "aria-level": "4" },
                    "Your selected channels (" + selectedRows.length + ")")
                : null,
              selectedRows.length
                ? h(WindowedList, {
                    items: selectedRows, itemHeight: 36, ariaLabel: "Selected channels in this plan",
                    className: "jw-review-list", resetKey: intentKey + "|sel", listStyle: { height: "190px" },
                    render: renderReviewRow,
                  })
                : null,
              displacedRows.length
                ? h("div", { className: "jw-review-subhead", role: "heading", "aria-level": "4" },
                    "Other channels that move (" + displacedRows.length + ")")
                : null,
              displacedRows.length
                ? h(WindowedList, {
                    items: displacedRows, itemHeight: 36, ariaLabel: "Other channels displaced by this plan",
                    className: "jw-review-list", resetKey: intentKey + "|dis", listStyle: { height: "160px" },
                    render: renderReviewRow,
                  })
                : null,
            )
          : null,
      ),
    );
  }

  // ------------------------------------------------------------------
  // Arrangement bar: the scoped sticky bar for a staged arrangement.
  // Scope stays explicit next to the editor's own Apply channel bar.
  // Submission lifecycle (R6): the pending entry carries
  // state "submitting" | "polling" | "outcome_unknown"; committed/rejected
  // resolve through the durable receipt. A transport failure NEVER leaves
  // the button stuck on "Applying…" — it offers Check result / Retry.
  // ------------------------------------------------------------------

  function ArrangementBar({ staged, pending, libRevision, busy, onApply, onCheckResult, onRetry, onForgetPending, onReview, onDiscard, onUndo, onRedo, canUndo, canRedo, onReplan }) {
    const lifecycle = pending ? (pending.state || "submitting") : "idle";
    // UX7: Undo/Redo stay discoverable even with nothing staged (the stacks
    // remember prior stagings until something replaces them). F3: an
    // unresolved submission keeps the bar visible so Check result / Retry /
    // Forget stay reachable with NOTHING staged.
    if (!staged && !canUndo && lifecycle !== "outcome_unknown") return null;
    const ba = staged ? (staged.beforeAfter || {}) : {};
    const refs = Object.keys(ba);
    const renumbered = refs.filter((r) => ba[r] && ba[r].number).length;
    const regrouped = refs.filter((r) => ba[r] && ba[r].group).length;
    const displacedSet = new Set(((((staged || {}).response || {}).displaced) || [])
      .map((nc) => nc.channelId || nc.tempRef));
    if (!displacedSet.size && staged) {
      for (const nc of ((staged.response || {}).numberChanges) || []) {
        if (nc.selected === false) displacedSet.add(nc.channelId || nc.tempRef);
      }
    }
    const placed = staged ? Object.keys(staged.tempRefs || {}).length : 0;
    const moveCount = renumbered + regrouped;
    const staleBase = !!staged && libRevision != null && staged.baseRevision != null && libRevision !== staged.baseRevision;
    const parts = [];
    if (renumbered) parts.push(renumbered + " channel" + (renumbered === 1 ? "" : "s") + " renumbered");
    if (regrouped) parts.push(regrouped + " channel" + (regrouped === 1 ? "" : "s") + " regrouped");
    if (displacedSet.size) parts.push(displacedSet.size + " other channel" + (displacedSet.size === 1 ? "" : "s") + " relocated");
    if (placed) parts.push(placed + " new channel" + (placed === 1 ? "" : "s") + " placed");
    if (staged && !parts.length) parts.push(staged.label || "Arrangement staged");
    const applyLabel = moveCount
      ? "Apply " + moveCount + " channel move" + (moveCount === 1 ? "" : "s")
      : "Apply arrangement";
    if (!staged) {
      return h("div", { className: "jw-arrange-bar jw-arrange-bar-empty", role: "region", "aria-label": "Arrangement history" },
        h("span", { className: "jw-arrange-label" },
          h("strong", null, lifecycle === "outcome_unknown" ? "Apply outcome unknown" : "Nothing staged"),
          h("span", { className: "jw-arrange-sublabel" },
            lifecycle === "outcome_unknown"
              ? "The packet and request id are kept — resolve it before staging anything new."
              : "Undo brings back a staged plan until something replaces it.")),
        h("span", { style: { flex: 1 } }),
        lifecycle === "outcome_unknown"
          ? [
              h("button", {
                key: "check", className: "jw-btn jw-btn-small",
                onClick: onCheckResult,
                title: "Ask the server once for the durable receipt of the kept request id",
              }, "Check result"),
              h("button", {
                key: "retry", className: "jw-btn jw-btn-small jw-btn-primary",
                onClick: onRetry,
                title: "Resubmit the exact same packet and request id — byte-identical retries can never commit twice",
              }, "Retry same Apply"),
              h("button", {
                key: "forget", className: "jw-btn jw-btn-ghost jw-btn-small",
                onClick: onForgetPending,
                title: "Give up on the kept request — only do this if Check result keeps coming back empty and you accept the risk",
              }, "Forget kept request"),
            ]
          : null,
        canUndo ? h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", onClick: onUndo, title: "Undo (Ctrl+Z)" }, "Undo") : null,
        canRedo ? h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", onClick: onRedo, title: "Redo (Ctrl+Shift+Z)" }, "Redo") : null,
      );
    }
    return h("div", { className: "jw-arrange-bar", role: "region", "aria-label": "Staged arrangement" },
      h("span", { className: "jw-arrange-label" },
        h("strong", null, parts.join(" · ")),
        staged.label ? h("span", { className: "jw-arrange-sublabel" }, staged.label) : null),
      lifecycle === "outcome_unknown"
        ? h("span", { className: "jw-note-warn jw-arrange-stale", role: "alert" },
            "The last Apply's outcome is unknown — the packet and request id are kept.")
        : null,
      staleBase
        ? h("span", { className: "jw-note-warn jw-arrange-stale", role: "status" },
            "Based on r" + staged.baseRevision + " — the library is now r" + libRevision + ".")
        : null,
      h("span", { style: { flex: 1 } }),
      canUndo ? h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", onClick: onUndo, title: "Undo (Ctrl+Z)" }, "Undo") : null,
      canRedo ? h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", onClick: onRedo, title: "Redo (Ctrl+Shift+Z)" }, "Redo") : null,
      h("button", { className: "jw-btn jw-btn-small", onClick: onReview }, "Review…"),
      lifecycle === "submitting" || lifecycle === "polling"
        ? h("span", { className: "jw-pill jw-pill-applying", role: "status", title: "The receipt resolves in the background — leaving the page does not interrupt it" },
            lifecycle === "submitting" ? "Submitting…" : "Applying…")
        : null,
      lifecycle === "outcome_unknown"
        ? [
            h("button", {
              key: "check", className: "jw-btn jw-btn-small",
              onClick: onCheckResult,
              title: "Ask the server once for the durable receipt of the kept request id",
            }, "Check result"),
            h("button", {
              key: "retry", className: "jw-btn jw-btn-small jw-btn-primary",
              onClick: onRetry,
              title: "Resubmit the exact same packet and request id — byte-identical retries can never commit twice",
            }, "Retry same Apply"),
            h("button", {
              key: "forget", className: "jw-btn jw-btn-ghost jw-btn-small",
              onClick: onForgetPending,
              title: "Give up on the kept request — only do this if Check result keeps coming back empty and you accept the risk",
            }, "Forget kept request"),
          ]
        : null,
      lifecycle === "idle" || lifecycle === "outcome_unknown"
        ? h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", disabled: busy,
            onClick: onDiscard,
          }, "Discard")
        : null,
      staleBase
        ? h("button", { className: "jw-btn jw-btn-primary", onClick: onReplan }, "Reload and replan")
        : lifecycle === "idle"
          ? h("button", {
              className: "jw-btn jw-btn-primary", disabled: busy,
              onClick: onApply,
            }, applyLabel)
          : null,
    );
  }

  // ------------------------------------------------------------------
  // Editor pane — the Option A layout + the Apply state machine
  //
  //   clean -> dirty -> validating -> applying -> applied
  //              |         |            |
  //              +-- invalid   +-- revision_conflict / transport
  //  (the draft always survives; edits typed while applying stay draft and
  //   need a second Apply)
  // ------------------------------------------------------------------

  function EditorPane({
    channelId, lib, getRevision, drafts, confirm, toast, arrangement,
    submitOps, submitApply, onApplied, onCreated, libraryVersion,
  }) {
    const isTemp = String(channelId).startsWith("temp-");
    // arrangement = { available, staged, openResolution(subject) } — the
    // number-resolution sheet entry point; null-guarded for old backends.
    const arrange = arrangement || { available: false, staged: null, openResolution: null };

    const [stored, setStored] = useState(null);   // server definition (channel record)
    const [summary, setSummary] = useState([]);   // server plain-language lines
    const [draft, setDraft] = useState(null);
    const [phase, setPhase] = useState("clean");  // clean|dirty|validating|applying|applied
    const [applyError, setApplyError] = useState(null); // {kind, message, errors, currentRevision}
    const [lastAppliedRev, setLastAppliedRev] = useState(null);
    // Honest post-Apply refresh state, read from the durable status op:
    // {state: "pending"|"failed"|"ready"|"unknown", since?, offAir?, missing?}
    // Never cleared on a timer — it changes when the durable state changes.
    const [refreshState, setRefreshState] = useState(null);
    // Set when the pre-flight check itself could not run: Apply stays safe
    // (the server validates at commit) but the UI must not imply it pre-checked.
    const [preflightNote, setPreflightNote] = useState(null);
    const [rebaseNotice, setRebaseNotice] = useState(null);
    const [preview, setPreview] = useState(null);
    const [previewState, setPreviewState] = useState("idle"); // idle|loading|ok|error
    const [menuOpen, setMenuOpen] = useState(false);
    const [glyphOpen, setGlyphOpen] = useState(false);
    const [picker, setPicker] = useState(null);   // {kind, title, facets...}
    const [historyOpen, setHistoryOpen] = useState(false);
    const [loadError, setLoadError] = useState(null);
    const [nameTick, setNameTick] = useState(0);  // re-render chips after name resolution

    const draftRef = useRef(null);
    const storedRef = useRef(null);
    const phaseRef = useRef("clean");
    const inFlightRef = useRef(false);
    const applyEnabledRef = useRef(false); // fresh every render — the Apply chord reads this
    const pendingRef = useRef(null);   // {requestId, snapshot, expected} — kept on transport failure
    const previewSeqRef = useRef(0);
    const aliveRef = useRef(true);
    const refreshTimerRef = useRef(null);
    const refreshPollsLeftRef = useRef(0);
    const menuRef = useOutsideClose(menuOpen, () => setMenuOpen(false));
    const glyphRef = useOutsideClose(glyphOpen, () => setGlyphOpen(false));

    draftRef.current = draft;
    storedRef.current = stored;
    phaseRef.current = phase;

    useEffect(() => {
      aliveRef.current = true;
      resolveGlyphs();
      return () => {
        aliveRef.current = false;
        if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
      };
    }, []);

    // Ctrl/Cmd+Enter commits the draft when Apply is enabled; the ref is
    // refreshed on every render so the chord always sees current validity.
    // While any dialog overlay is open the chord stays silent — Enter belongs
    // to the dialog.
    useEffect(() => {
      const onKey = (e) => {
        if (e.key !== "Enter" || (!e.ctrlKey && !e.metaKey)) return;
        if (!applyEnabledRef.current) return;
        if (typeof document.querySelector === "function" && document.querySelector(".jw-overlay")) return;
        e.preventDefault();
        void doApply();
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // (navigation is free — no phase reporting to the app; an in-flight
    // apply continues in the background and announces itself via toasts)

    // Local mirror of the rules text-search input. The DRAFT is written
    // synchronously on every keystroke — only the preview stays debounced —
    // so an immediate Apply commits exactly the text that was visible
    // (audit E6). The mirror exists so the input is never re-formatted while
    // typing; it converges on blur / channel switch / external rebase.
    const committedQ = draft && draft.source && draft.source.q ? String(draft.source.q) : "";
    const [qLocal, setQLocal] = useState(null);
    useEffect(() => {
      const el = document.getElementById("f-qsearch");
      if (el && document.activeElement === el) return; // typing: the mirror is authoritative
      setQLocal(null);
    }, [committedQ, channelId]);

    // Same pattern for the two duration minute fields: typed text is kept
    // verbatim until blur; the draft carries whole SECONDS (E8: decimals
    // allowed, explicit 0 preserved, blank = absent).
    const committedDur = draft && draft.source ? (draft.source.duration || null) : null;
    const durMinCommitted = committedDur && committedDur.min != null ? committedDur.min : null;
    const durMaxCommitted = committedDur && committedDur.max != null ? committedDur.max : null;
    const [durText, setDurText] = useState({}); // {min?, max?} typed mirrors; absent = follow draft
    useEffect(() => {
      if (document.activeElement === document.getElementById("f-dur-min")
          || document.activeElement === document.getElementById("f-dur-max")) return;
      setDurText({});
    }, [durMinCommitted, durMaxCommitted, channelId]);

    // Minutes in, whole SECONDS in the draft. Decimals are accepted (stored
    // to the nearest second); blank clears the bound; an explicit 0 is a
    // real bound (presence-based — never erased as "empty"). Meaningless
    // values a browser can still yield (negative, huge) stay in the draft so
    // the typed validation errors below the field fire instead of silently
    // normalizing away (audit E8).
    const setDurationHalf = (half, raw) => {
      setDurText((t) => Object.assign({}, t, { [half]: raw }));
      edit((d) => {
        const spec = Object.assign({}, d.source.duration || {});
        const v = String(raw).trim();
        if (v === "") delete spec[half];
        else {
          const minutes = Number(v);
          if (Number.isFinite(minutes)) spec[half] = Math.round(minutes * 60);
        }
        if (spec.min == null && spec.max == null) delete d.source.duration;
        else d.source.duration = spec;
        return d;
      });
    };

    // ---- init / retarget ----
    useEffect(() => {
      let alive = true;
      setLoadError(null);
      setPreview(null);
      setPreviewState("idle");
      setApplyError(null);
      setLastAppliedRev(null);
      setRefreshState(null);
      setPreflightNote(null);
      setRebaseNotice(null);
      pendingRef.current = null;
      (async () => {
        let storedChannel = null;
        let serverSummary = [];
        if (isTemp) {
          const saved = drafts.get(channelId);
          if (!saved) {
            if (alive) setLoadError("This draft no longer exists. Discard it and start again.");
            return;
          }
          storedChannel = null; // nothing on the server yet
        } else {
          try {
            const def = await runOp("GetChannelDefinition", { channelId });
            if (!alive) return;
            storedChannel = def.channel;
            serverSummary = def.summary || [];
          } catch (e) {
            if (alive) setLoadError(String((e && e.message) || e));
            return;
          }
        }
        const saved = drafts.get(channelId);
        let base = saved && saved.base ? clone(saved.base) : clone(storedChannel);
        let working = saved ? clone(saved.draft) : clone(storedChannel);
        let notice = null;
        if (saved && !isTemp && storedChannel) {
          // The draft was authored against `base`; the server record may have
          // moved (another tab/editor/reload). Merge non-overlapping server
          // changes in, keep local edits, and NAME same-field conflicts —
          // never borrow the global revision to authorize a full-record
          // overwrite (audit C8).
          const rebased = rebaseDraft(base, storedChannel, working);
          if (!deepEqual(rebased.draft, working)) working = rebased.draft;
          if (rebased.conflicts.length) {
            notice = "The server also changed " + rebased.conflicts.join(", ")
              + ". Your draft keeps your values — review them before Apply.";
          }
          base = clone(storedChannel);
        }
        if (saved && saved.conflict === "definition_unavailable" && !isTemp) {
          // F2: the arrangement committed this channel but its fresh
          // definition could not be fetched — the draft is kept, flagged
          // conflicted; the owner must reload before editing further.
          notice = (notice ? notice + " " : "")
            + "The committed definition could not be loaded after the last arrangement Apply — "
            + "reload this channel (Reload library) before editing further.";
        }
        if (!alive) return;
        setStored(storedChannel);
        setSummary(serverSummary);
        setDraft(working);
        draftRef.current = working;
        setPhase(saved ? "dirty" : "clean");
        phaseRef.current = saved ? "dirty" : "clean";
        setRebaseNotice(notice);
        if (saved && (saved.base !== undefined || notice)) {
          drafts.setExtras(channelId, { base });
        }
        // resume an apply that outlived the previous editor instance
        const infl = inflightApplies.get(channelId)
          || (saved && saved.pending ? {
            requestId: saved.pending.requestId,
            snapshot: saved.pending.snapshot,
            expected: saved.pending.expected,
            promise: pollApplyReceipt(saved.pending.requestId),
          } : null);
        if (infl) {
          inFlightRef.current = true;
          pendingRef.current = { requestId: infl.requestId, snapshot: infl.snapshot, expected: infl.expected };
          setPhase("applying");
          infl.promise.then((receipt) => {
            finalize(receipt, infl.snapshot, true, { name: infl.name, requestId: infl.requestId });
          }).catch(() => {});
        }
        void loadPreview();
        resolveNamesFor(working && working.source);
        scheduleRefreshPolls(); // recover pending/failed refresh state durably
      })();
      return () => {
        alive = false;
        schedulePreview.cancel();       // no stale preview for the next channel
        if (refreshTimerRef.current) {  // ditto for the refresh poll loop
          clearTimeout(refreshTimerRef.current);
          refreshTimerRef.current = null;
        }
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [channelId]);

    // A library reload or an unrelated commit bumped the server state: rebase
    // the draft against the FRESH definition (same three-way rule as init).
    useEffect(() => {
      if (!libraryVersion || libraryVersion === 0 || isTemp) return undefined;
      let alive = true;
      (async () => {
        const saved = drafts.get(channelId);
        if (!saved) {
          // clean editor: adopt the fresh record wholesale
          try {
            const def = await runOp("GetChannelDefinition", { channelId });
            if (!alive || !deepEqual(def.channel, storedRef.current)) {
              if (!alive) return;
              setStored(def.channel);
              setSummary(def.summary || []);
              if (!drafts.get(channelId) && phaseRef.current !== "applying") {
                const fresh = clone(def.channel);
                draftRef.current = fresh;
                setDraft(fresh);
              }
            }
          } catch (e) { /* offline: keep local state */ }
          return;
        }
        try {
          const def = await runOp("GetChannelDefinition", { channelId });
          if (!alive) return;
          // base falls back to the record this editor loaded (the acknowledged
          // state for entries authored before base-pinning existed)
          const base = saved.base !== undefined ? saved.base
            : (storedRef.current ? clone(storedRef.current) : null);
          const rebased = rebaseDraft(base, def.channel, saved.draft);
          if (!deepEqual(rebased.draft, saved.draft) || rebased.conflicts.length) {
            drafts.put(channelId, rebased.draft, { base: clone(def.channel) });
            if (phaseRef.current !== "applying") {
              draftRef.current = clone(rebased.draft);
              setDraft(clone(rebased.draft));
            }
            if (rebased.conflicts.length) {
              setRebaseNotice("The server also changed " + rebased.conflicts.join(", ")
                + ". Your draft keeps your values — review them before Apply.");
            }
            setStored(def.channel);
            setSummary(def.summary || []);
          }
        } catch (e) { /* offline: the draft stays as-is */ }
      })();
      return () => { alive = false; };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [libraryVersion]);

    // Re-resolve summary names whenever the stored summary changes
    useEffect(() => {
      if (!summary.length) return undefined;
      let alive = true;
      const ids = [];
      const scan = (kind, list) => list.forEach((id) => ids.push([kind, id]));
      (summary.join(" ").match(/#[0-9]+/g) || []).forEach((m) => {
        // summary placeholders render "#id" — resolution needs kinds; the
        // rules summary below re-derives names client-side instead.
        void m;
      });
      void scan;
      const t = setTimeout(() => { if (alive) setNameTick((x) => x + 1); }, 400);
      return () => { alive = false; clearTimeout(t); };
    }, [summary]);

    function resolveNamesFor(source) {
      const s = source || {};
      const jobs = [
        ["tag", [...idsOf(s.tags), ...idsOf(s.tagsAny), ...idsOf(s.excludeTags)]],
        ["performer", [...idsOf(s.performers), ...idsOf(s.performersAny), ...idsOf(s.excludePerformers)]],
        ["studio", [...idsOf(s.studios), ...idsOf(s.studiosAny), ...idsOf(s.excludeStudios)]],
      ];
      Promise.all(jobs.map(([kind, ids]) => resolveEntityNames(kind, ids))).then(() => {
        if (aliveRef.current) setNameTick((x) => x + 1);
      });
    }

    // ---- draft plumbing ----
    function isDirty() {
      const d = draftRef.current;
      const s = storedRef.current;
      if (!d) return false;
      if (!s) return true; // temp channel: nothing stored yet
      if (deepEqual(d, s)) return false;
      // Rule editing may add empty canonical keys and programming touches may
      // add explicit defaults — equivalent values are not a change (mirrors
      // the server's canonical stored record).
      const a = Object.assign({}, d, { source: undefined, programming: undefined });
      const b = Object.assign({}, s, { source: undefined, programming: undefined });
      const headEqual = deepEqual(a, b)
        && deepEqual(canonicalProgramming(d.programming), canonicalProgramming(s.programming));
      return !headEqual || !sourcesEquivalent(d.source, s.source);
    }

    function edit(mutator) {
      const cur = draftRef.current;
      if (!cur) return;
      const next = mutator(clone(cur));
      if (next == null || deepEqual(next, cur)) return; // a no-op edit never dirties
      draftRef.current = next;
      setDraft(next);
      // ALWAYS persist — including while an earlier snapshot is applying.
      // The submitted snapshot is immutable; this newer draft must survive
      // navigation, remounts and the receipt (audit C8). The FIRST edit pins
      // the acknowledged base (the server record this draft was authored
      // against) so later rebases know which fields are locally touched.
      const entry = drafts.get(channelId);
      const extras = entry && entry.base !== undefined
        ? {}
        : { base: storedRef.current ? clone(storedRef.current) : null };
      drafts.put(channelId, clone(next), extras);
      if (phaseRef.current === "clean") {
        setPhase("dirty");
        phaseRef.current = "dirty";
      }
      if (applyError) setApplyError(null);
      if (preflightNote) setPreflightNote(null);
      schedulePreview();
      resolveNamesFor(next.source);
    }

    const schedulePreview = useMemo(() => debounce(() => { void loadPreview(); }, PREVIEW_DEBOUNCE_MS), []);

    async function loadPreview() {
      const d = draftRef.current;
      if (!d || !d.source) return;
      const seq = ++previewSeqRef.current;
      setPreviewState("loading");
      try {
        const args = {
          // The preview sees exactly what Apply sends (audit E1).
          source: JSON.stringify(toWireSource(d.source)),
          sort: d.sort || "shuffle",
          sampleLimit: "10",
          includePaths: true,
        };
        if (storedRef.current && storedRef.current.seed != null) args.seed = String(storedRef.current.seed);
        const result = await runOp("PreviewChannelPool", args);
        if (!aliveRef.current || seq !== previewSeqRef.current) return; // stale response
        setPreview(result);
        setPreviewState("ok");
      } catch (e) {
        if (!aliveRef.current || seq !== previewSeqRef.current) return;
        setPreview({ status: "error", message: String((e && e.message) || e) });
        setPreviewState("error");
      }
    }

    // ---- honest refresh status (durable, never timer-cleared) ----
    // GetChannelRefreshStatus reads the two durable stores: the pending
    // INTENT journal and the published health snapshot. A pending entry means
    // work is outstanding; healthStatus "unavailable" means the last check
    // FAILED (retryable via RequeueChannelRefresh). Readiness is never
    // synthesized — a channel with no durable record reports "unknown".
    async function pollRefreshOnce() {
      if (isTemp) return false;
      let out = null;
      try {
        out = await runOp("GetChannelRefreshStatus", { channelId });
      } catch (e) {
        diagEvent("refresh_status", { channelId, outcome: "unreadable" });
        return refreshPollsLeftRef.current > 1; // transient: retry within the budget
      }
      if (!aliveRef.current || !out || typeof out !== "object") return false;
      if (out.pending) {
        setRefreshState({ state: "pending", since: out.pending.enqueuedAt || null });
        return true; // keep polling
      }
      const health = out.health;
      if (!health || typeof health !== "object") {
        setRefreshState({ state: "unknown" });
        return false;
      }
      const status = health.healthStatus;
      if (status === "unavailable") {
        setRefreshState({ state: "failed" });
        return false;
      }
      setRefreshState({
        state: "ready",
        offAir: status === "offAir",
        missing: status === "missingSource",
      });
      return false;
    }

    function scheduleRefreshPolls() {
      if (isTemp) return;
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
      refreshPollsLeftRef.current = REFRESH_POLL_MAX;
      const tick = async () => {
        refreshTimerRef.current = null;
        if (!aliveRef.current) return;
        const outstanding = await pollRefreshOnce();
        if (outstanding && aliveRef.current && --refreshPollsLeftRef.current > 0) {
          refreshTimerRef.current = setTimeout(tick, REFRESH_POLL_MS);
        }
      };
      refreshTimerRef.current = setTimeout(tick, 300);
    }

    const retryRefresh = async () => {
      try {
        await runOp("RequeueChannelRefresh", { channelId });
        diagEvent("refresh_requeue", { channelId, outcome: "queued" });
        scheduleRefreshPolls();
      } catch (e) {
        diagEvent("refresh_requeue", { channelId, outcome: "failed" });
        toast("Could not queue the refresh — try again.", "err");
      }
    };

    function validateDraft() {
      const d = draftRef.current;
      const errors = [];
      if (!d) return errors;
      const name = String(d.name || "").trim();
      if (!name) errors.push({ field: "name", message: "Name can't be empty." });
      else if (name.length > MAX_NAME_LEN) errors.push({ field: "name", message: "Name must be " + MAX_NAME_LEN + " characters or fewer." });
      if (!d.groupId) errors.push({ field: "group", message: "Pick a group." });
      else if (!(lib.groups || []).some((g) => g.id === d.groupId)) {
        // Inline "Create group…" sentinel: the group is created in the same
        // Apply packet — it needs a name.
        if (!String(d.pendingGroupName || "").trim()) {
          errors.push({ field: "group", message: "Name the new group." });
        }
      }
      const band = BANDS[d.kind || "net"];
      const num = d.number;
      if (!Number.isInteger(num) || num < band[0] || num > band[1]) {
        errors.push({ field: "number", message: "Number must be " + band[0] + "–" + band[1] + " for this band." });
      } else {
        const occupant = (lib.channels || []).find((c) => c.number === num && c.id !== channelId);
        if (occupant) {
          // A staged arrangement that lands this channel on its intended
          // number resolves the conflict — the arrangement Apply commits it.
          let resolvedByStaging = false;
          if (arrange.staged) {
            const stagedBa = arrange.staged.beforeAfter ? arrange.staged.beforeAfter[channelId] : null;
            resolvedByStaging = !!(stagedBa && Array.isArray(stagedBa.number) && stagedBa.number[1] === num);
            if (!resolvedByStaging && arrange.staged.tempRefs && arrange.staged.tempRefs[channelId]) {
              resolvedByStaging = arrange.staged.tempRefs[channelId].number === num;
            }
          }
          if (!resolvedByStaging) {
            errors.push({
              field: "number",
              message: "Channel " + num + " is taken by “" + occupant.name + "”."
                + (arrange.available ? " Use Resolve conflict…" : " Pick a free number."),
            });
          }
        }
      }
      if (isRuleSource(d.source)) {
        // Validate exactly what preview/validate/apply will send (audit E1):
        // the wire source. An explicit duration 0 counts as a rule; a facet
        // the author emptied does not.
        const wire = toWireSource(d.source);
        for (const e of ruleSourceClientErrors(wire)) errors.push(e);
        const hasRule = FACET_KEYS.some((k) => wire[k] && wire[k].length)
          || wire.date || wire.duration || wire.createdAt || wire.q
          || wire.studioSceneCount || wire.performerSceneCount;
        if (!hasRule && d.source.type === "criteria") {
          errors.push({ field: "source", code: "empty_rules", message: "Add at least one rule — an empty criteria pool matches nothing." });
        }
      }
      return errors;
    }

    function fieldError(field) {
      if (applyError && Array.isArray(applyError.errors)) {
        const hit = applyError.errors.find((e) => {
          const p = String(e.path || "");
          return p === "ops[0]." + field || p.endsWith("." + field) || p === field
            || (field === "group" && p.endsWith("groupId"))
            || (field === "source" && p.includes(".source") && !p.includes(".source."));
        });
        if (hit) return hit.message;
      }
      const local = validateDraft().find((e) => e.field === field);
      return local ? local.message : null;
    }

    function rulesErrors() {
      const out = [];
      if (applyError && Array.isArray(applyError.errors)) {
        for (const e of applyError.errors) {
          const p = String(e.path || "");
          if (p.includes(".source")) out.push(e);
        }
      }
      for (const e of validateDraft()) if (String(e.field || "").startsWith("source")) out.push(e);
      return out;
    }

    // ---- the Apply state machine ----
    // Number conflicts are resolved through staged ARRANGEMENTS (the
    // number-resolution sheet / Organize sheet), never through an immediate
    // swap — the intended number stays in the draft while resolving.
    function buildOps(snapshot) {
      const ops = [];
      // A group created inline ("Create group…" sentinel) rides the SAME
      // Apply packet ahead of the channel op — create+assign is atomic.
      // (pendingGroupName is client plumbing, stripped from the wire
      // channel by toWireChannel — read it from the draft itself.)
      const liveDraft = draftRef.current;
      if (liveDraft && liveDraft.pendingGroupName != null) {
        const gid = liveDraft.groupId;
        const name = String(liveDraft.pendingGroupName || "").trim();
        if (gid && name && !(lib.groups || []).some((g) => g.id === gid)) {
          const maxPos = Math.max(0, ...(lib.groups || []).map((g) => g.position || 0));
          ops.push({ op: "group.put", group: { id: gid, name, position: maxPos + 1 } });
        }
      }
      if (isTemp) {
        const channel = clone(snapshot);
        delete channel.id;
        delete channel.seed;
        delete channel.provenance;
        ops.push({ op: "channel.create", tempId: channelId, channel });
        return ops;
      }
      const channel = clone(snapshot);
      ops.push({ op: "channel.put", channel });
      return ops;
    }

    async function doApply(expectedOverride) {
      if (inFlightRef.current) return;
      setApplyError(null);
      const invalid = validateDraft();
      setPhase("validating");
      if (invalid.length) {
        setPhase("dirty");
        return;
      }
      // The submitted snapshot is the WIRE channel: this exact JSON is what
      // preview showed, what Validate checked, and what the idempotent retry
      // replays — one immutable form (audit E1/E6).
      const snapshot = toWireChannel(clone(draftRef.current));
      const applyName = (String((draftRef.current && draftRef.current.name) || "").trim() || channelId).slice(0, 48);
      const ops = buildOps(snapshot);
      const expected = expectedOverride != null ? expectedOverride : getRevision();
      // Pre-flight against the live document: precise typed errors before the
      // task queue is involved (no writes; revision races are still handled
      // by the receipt). If it CANNOT run we continue — the server validates
      // at commit — but the UI says so instead of implying a clean pre-check.
      try {
        const check = await runOp("ValidateChannelChanges", { ops: JSON.stringify(ops) });
        if (!aliveRef.current) return;
        setPreflightNote(null);
        if (check && check.valid === false && (check.errors || []).length) {
          setPhase("dirty");
          setApplyError({ kind: "validation", errors: check.errors, message: "the server rejected these changes" });
          diagEvent("apply_preflight", { channelId, outcome: "invalid", count: check.errors.length });
          return;
        }
      } catch (e) {
        if (aliveRef.current) {
          setPreflightNote(String((e && e.message) || e));
          diagEvent("apply_preflight", { channelId, outcome: "unavailable" });
        }
      }
      if (!aliveRef.current) return;
      // Reuse the pending requestId ONLY for a byte-identical retry on the
      // wire (same serialized ops, same expectedRevision) — that is the
      // idempotent-replay case.
      const pending = pendingRef.current;
      const reuse = pending && pending.expected === expected
        && deepEqual(toWireChannel(clone(draftRef.current)), pending.snapshot);
      const requestId = reuse ? pending.requestId : newRequestId();
      pendingRef.current = { requestId, snapshot, expected };
      // The immutable submitted request survives navigation/remount/reload;
      // a fresh mount resumes by polling this exact receipt (audit C8/C9).
      drafts.setExtras(channelId, { pending: { requestId, expected, snapshot: clone(snapshot) } });
      setPhase("applying");
      inFlightRef.current = true;

      // R9: channel Applies join the ONE submission coordinator
      // (enqueueSubmit) — they serialize with bulk/groups/arrangement, and
      // the coordinator adopts the committed revision BEFORE the queue
      // releases, so the next submission never reads a stale revision.
      const promise = (submitApply || ((fn) => fn()))(() => applyChannelChanges(requestId, expected, ops));
      inflightApplies.set(channelId, { requestId, snapshot, expected, promise, name: applyName });
      inflightNotify();
      let receipt;
      try {
        receipt = await promise;
      } catch (e) {
        // Transport failure: the draft AND the requestId survive. Retrying the
        // unchanged draft replays or lands the same transaction.
        inFlightRef.current = false;
        inflightApplies.delete(channelId);
        inflightNotify();
        if (!aliveRef.current) return;
        setPhase("dirty");
        setApplyError({ kind: "transport", message: "Submit failed (" + String((e && e.message) || e) + ")." });
        toast("Apply failed to reach the server — draft kept. Apply again to retry the same request.", "err");
        return;
      }
      finalize(receipt, snapshot, false, { name: applyName, requestId });
    }

    async function finalize(receipt, snapshot, resumed, meta) {
      inFlightRef.current = false;
      inflightApplies.delete(channelId);
      inflightNotify();
      if (!receipt) return;
      const name = (meta && meta.name) || channelId;
      // Two observers can share one receipt (a stale closure plus a remount
      // that re-attached to the same promise): only the first may toast.
      const dup = markSettled((meta && meta.requestId) || channelId + ":" + String(receipt.status));
      const away = !aliveRef.current; // user navigated elsewhere: toast, don't just set bar state
      const transportLike = receipt.status === "unknown";

      if (receipt.status === "committed") {
        pendingRef.current = null;
        drafts.setExtras(channelId, { pending: null });
        // Did the draft move on while the request was in flight? Compare on
        // the wire: an edit that only re-filled empty facet arrays in the
        // editing model is not a change (the snapshot is already wire-form).
        const currentDraft = aliveRef.current && draftRef.current
          ? draftRef.current
          : (drafts.get(channelId) ? drafts.get(channelId).draft : snapshot);
        const untouched = deepEqual(toWireChannel(clone(currentDraft)), snapshot);
        const finalId = (receipt.idMap && receipt.idMap[channelId]) || channelId;
        // The fresh server record becomes the newer draft's acknowledged base.
        let freshStored = null;
        let freshSummary = [];
        if (!isTemp || receipt.idMap) {
          try {
            const def = await runOp("GetChannelDefinition", { channelId: finalId });
            freshStored = def.channel;
            freshSummary = def.summary || [];
          } catch (e) { /* channel vanished? keep local draft state */ }
        }
        if (untouched) {
          drafts.drop(channelId);
        } else if (!(isTemp && receipt.idMap && finalId === channelId)) {
          // newer edits survive the commit, based on the fresh record; for a
          // committed CREATION the newer draft remaps to the final channel id
          // (stored under the final id BEFORE the temp entry is dropped).
          const persistId = isTemp && receipt.idMap ? finalId : channelId;
          drafts.put(persistId, clone(currentDraft),
                     { base: freshStored ? clone(freshStored) : null, pending: null });
        }
        onApplied(receipt, { channelId, isTemp, untouched, idMap: receipt.idMap });
        if (!aliveRef.current && !dup) {
          toast("Applied “" + name + "” at r" + receipt.revision + ".", "ok");
          return;
        }
        if (!aliveRef.current) return; // registry side effects already done
        setStored(freshStored);
        setSummary(freshSummary);
        setLastAppliedRev(receipt.revision);
        diagEvent("apply_commit", { channelId, outcome: "committed", revision: receipt.revision });
        // Honest post-commit state: poll the durable refresh status until no
        // work is outstanding (or the budget rests). The pill changes when
        // the DURABLE state changes — never on a timer.
        scheduleRefreshPolls();
        if (untouched) {
          const nextDraft = freshStored ? clone(freshStored) : clone(snapshot);
          draftRef.current = nextDraft;
          setDraft(nextDraft);
          setPhase("applied");
          phaseRef.current = "applied";
        } else {
          draftRef.current = currentDraft;
          setDraft(currentDraft);
          setPhase("dirty");
          phaseRef.current = "dirty";
          if (!dup) toast("Applied your earlier snapshot — newer edits are still draft.", "");
        }
        if (resumed && !dup) toast("Applied “" + name + "” at r" + receipt.revision + " (recovered receipt).", "ok");
      } else if (receipt.error === "revision_conflict") {
        if (!transportLike) { pendingRef.current = null; drafts.setExtras(channelId, { pending: null }); }
        setPhase("dirty");
        setApplyError({ kind: "conflict", currentRevision: receipt.currentRevision, message: receipt.message });
        diagEvent("apply_commit", { channelId, outcome: "revision_conflict" });
        if (away && !dup) toast("Apply for “" + name + "” hit a revision conflict — the library moved to r"
          + receipt.currentRevision + ". Your draft is intact.", "err");
      } else if (receipt.error === "validation_failed") {
        if (!transportLike) { pendingRef.current = null; drafts.setExtras(channelId, { pending: null }); }
        setPhase("dirty");
        setApplyError({ kind: "validation", errors: receipt.errors || [], message: receipt.message });
        diagEvent("apply_commit", { channelId, outcome: "validation_failed" });
        if (away && !dup) toast("Apply for “" + name + "” was rejected by server validation — your draft is intact.", "err");
      } else if (transportLike) {
        // unresolved: KEEP the pending requestId — a retry of the unchanged
        // draft is the idempotent path the server documents.
        setPhase("dirty");
        setApplyError({ kind: "transport", message: receipt.message || "the server never reported a result" });
        diagEvent("apply_commit", { channelId, outcome: "unknown" });
        if (!dup) toast("No receipt yet for “" + name + "” — draft kept. Apply again to reuse the same requestId.", "err");
      } else {
        pendingRef.current = null;
        drafts.setExtras(channelId, { pending: null });
        setPhase("dirty");
        setApplyError({ kind: "rejected", message: receipt.message || receipt.error || "rejected" });
        diagEvent("apply_commit", { channelId, outcome: "rejected" });
        if (away && !dup) toast("Apply for “" + name + "” was rejected: "
          + (receipt.message || receipt.error || "rejected") + " — your draft is intact.", "err");
      }
    }

    function discardDraft() {
      confirm({
        title: "Discard draft?",
        message: "Your unsaved changes to this channel will be thrown away.",
        confirmLabel: "Discard", danger: true,
      }).then((ok) => {
        if (!ok) return;
        drafts.drop(channelId);
        pendingRef.current = null;
        setRebaseNotice(null);
        if (isTemp) { onCreated(null, channelId); return; }
        const fresh = storedRef.current;
        draftRef.current = clone(fresh);
        setDraft(clone(fresh));
        setApplyError(null);
        setPhase("clean");
        phaseRef.current = "clean";
      });
    }

    // ---- per-channel menu actions ----
    const patchChannel = async (patch, label) => {
      const receipt = await submitOps(
        [{ op: "channels.patch", channelIds: [channelId], patch }],
        { busyChannel: true },
      );
      if (receipt.status === "committed") {
        toast(label + " — applied at r" + receipt.revision + ".", "ok");
        // adopt the server's record: wholesale when clean, kept-draft otherwise
        try {
          const def = await runOp("GetChannelDefinition", { channelId });
          if (!aliveRef.current) return;
          setStored(def.channel);
          setSummary(def.summary || []);
          if (!drafts.get(channelId)) {
            const fresh = clone(def.channel);
            draftRef.current = fresh;
            setDraft(fresh);
          } else {
            toast("Your draft for this channel was kept — it now differs from the server.", "");
          }
        } catch (e) { /* keep local state */ }
      }
    };

    const duplicate = () => {
      setMenuOpen(false);
      const base = storedRef.current || draftRef.current;
      if (!base) return;
      const band = base.kind || "net";
      const number = nextFreeNumber(lib.channels || [], band);
      if (number == null) { toast("No free number left in " + BANDS[band][0] + "–" + BANDS[band][1] + ".", "err"); return; }
      const copy = clone(base);
      delete copy.id;
      delete copy.seed;
      delete copy.provenance;
      copy.name = (String(base.name || "Channel").trim() + " (copy)").slice(0, MAX_NAME_LEN);
      copy.number = number;
      copy.archived = false;
      copy.paused = false;
      copy.enabled = true;
      const tempId = newTempId();
      drafts.put(tempId, copy);
      // The lowest free number on the SERVER can still collide with another
      // pending draft (the draft store is not part of lib.channels) — route
      // that collision through the same NumberResolutionSheet as create
      // (plan §3.2 lists duplicate as a sheet consumer).
      const draftClash = drafts.ids().some((id) => {
        if (id === tempId || id === channelId) return false;
        const entry = drafts.get(id);
        return !!(entry && entry.draft && entry.draft.number === number
          && (entry.draft.kind || "net") === band);
      });
      onCreated(tempId, draftClash ? { resolveNumber: number } : null);
      toast("Copy staged as a draft — Apply in the editor to commit it.", "");
    };

    if (loadError) {
      return h("div", { className: "jw-editor" },
        h("div", { className: "jw-editor-empty" },
          h("div", { className: "jw-empty-title" }, "This channel can't be edited right now."),
          h("div", { className: "jw-empty-sub" }, loadError),
          h("button", { className: "jw-btn", onClick: discardDraft, style: { marginTop: "10px" } }, "Discard draft"),
        ),
      );
    }

    if (!draft) {
      return h("div", { className: "jw-editor", role: "status", "aria-label": "Loading channel" },
        h("div", { className: "jw-editor-scroll" },
          skelCard(2), skelCard(4), skelCard(3)));
    }

    const groups = (lib.groups || []).slice().sort((a, b) => a.position - b.position);
    const groupById = new Map(groups.map((g) => [g.id, g]));
    const band = BANDS[draft.kind || "net"];
    const numberErr = fieldError("number");
    const occupant = Number.isInteger(draft.number)
      ? (lib.channels || []).find((c) => c.number === draft.number && c.id !== channelId) : null;
    const invalid = validateDraft();
    const rErrors = rulesErrors();

    // ---------- sub-components (inline for draft access) ----------

    const identityCard = h("div", { className: "jw-card" },
      h("h3", { className: "jw-card-title" }, "Identity"),
      h("div", { className: "jw-fieldrow" },
        h("div", { className: "jw-field" + (fieldError("name") ? " jw-field-invalid" : "") },
          h("label", { className: "jw-field-label", htmlFor: "f-name" }, "Name"),
          h("input", {
            id: "f-name", className: "jw-input", value: draft.name || "", maxLength: MAX_NAME_LEN + 10,
            "aria-invalid": fieldError("name") ? "true" : "false",
            "aria-describedby": fieldError("name") ? "err-name" : null,
            onChange: (e) => edit((d) => { d.name = e.target.value; return d; }),
          }),
          fieldError("name") ? h("div", { className: "jw-error-text", id: "err-name" }, fieldError("name")) : null,
        ),
        h("div", { className: "jw-field jw-field-number" + (numberErr ? " jw-field-invalid" : "") },
          h("label", { className: "jw-field-label", htmlFor: "f-number" }, "Number (" + band[0] + "–" + band[1] + ")"),
          h("input", {
            id: "f-number", className: "jw-input jw-num-wide", type: "number",
            min: band[0], max: band[1],
            value: draft.number != null ? draft.number : "",
            "aria-invalid": numberErr ? "true" : "false",
            "aria-describedby": numberErr ? "err-number" : null,
            onChange: (e) => edit((d) => { d.number = e.target.value === "" ? null : parseInt(e.target.value, 10); return d; }),
          }),
          numberErr ? h("div", { className: "jw-error-text", id: "err-number" }, numberErr) : null,
          occupant && !numberErr
            ? h("div", { className: "jw-hint" },
                draft.number + " is “" + occupant.name + "”. ",
                arrange.available
                  ? h("button", {
                      className: "jw-link",
                      onClick: () => arrange.openResolution({ ref: channelId, kind: draft.kind || "net", isNew: isTemp }, draft.number),
                    }, "Resolve conflict…")
                  : "Pick a free number in " + band[0] + "–" + band[1] + ".")
            : null,
          arrange.available
            ? h("div", { className: "jw-hint" },
                h("button", {
                  className: "jw-link",
                  onClick: () => arrange.openResolution(
                    { ref: channelId, kind: draft.kind || "net", isNew: isTemp },
                    Number.isInteger(draft.number) ? draft.number : band[0]),
                }, "Move / insert…"))
            : null,
          arrange.staged && arrange.staged.beforeAfter && arrange.staged.beforeAfter[channelId]
            && arrange.staged.beforeAfter[channelId].number
            ? h("div", { className: "jw-hint" },
                "Number resolves through the staged arrangement — Apply arrangement commits it.")
            : null,
        ),
      ),
      h("div", { className: "jw-field" },
        h("span", { className: "jw-field-label" }, "Brand color"),
        h("div", { className: "jw-swatch-row", role: "group", "aria-label": "Brand color" },
          PALETTE.map((c) => h("button", {
            key: c, className: "jw-swatch" + (draft.color === c ? " jw-swatch-active" : ""),
            style: { background: c }, title: c,
            "aria-pressed": draft.color === c ? "true" : "false",
            "aria-label": "Color " + c,
            onClick: () => edit((d) => { d.color = c; return d; }),
          })),
          h("label", { className: "jw-swatch jw-swatch-custom", title: "Custom color" },
            h("input", {
              type: "color", value: /^#[0-9a-fA-F]{6}$/.test(draft.color || "") ? draft.color : "#455A64",
              "aria-label": "Custom color",
              style: { opacity: 0, position: "absolute", width: 1, height: 1 },
              onChange: (e) => edit((d) => { d.color = e.target.value.toUpperCase(); return d; }),
            }),
            "…",
          ),
        ),
      ),
      h("div", { className: "jw-field" },
        h("span", { className: "jw-field-label" }, "Glyph (optional — the TV renders the number without one)"),
        h("div", { style: { position: "relative", display: "inline-block" }, ref: glyphRef },
          h("button", {
            className: "jw-btn jw-glyph-btn", onClick: () => setGlyphOpen((v) => !v),
            "aria-expanded": glyphOpen ? "true" : "false",
          },
            h(GlyphTile, { codepoint: draft.glyph, color: draft.color, size: 28, fallback: draft.number }),
            h("span", null, draft.glyph ? glyphName(draft.glyph) : "No glyph"),
            h("span", { "aria-hidden": "true", style: { color: "var(--jw-faint)", fontSize: ".7rem" } },
              glyphOpen ? "▴" : "▾"),
          ),
          glyphOpen ? h("div", { className: "jw-glyph-pop" },
            h("div", { className: "jw-glyph-grid" },
              GLYPH_POOL.map((g) => h("button", {
                key: g, className: "jw-glyph-cell" + (draft.glyph === g ? " jw-glyph-active" : ""),
                title: glyphName(g), "aria-label": glyphName(g),
                "aria-pressed": draft.glyph === g ? "true" : "false",
                onClick: () => { edit((d) => { d.glyph = g; return d; }); setGlyphOpen(false); },
              }, h(Glyph, { codepoint: g, size: 16 })))),
            h("button", {
              className: "jw-btn jw-btn-small", style: { marginTop: "6px" },
              onClick: () => { edit((d) => { d.glyph = null; return d; }); setGlyphOpen(false); },
            }, "Clear glyph"),
          ) : null,
        ),
      ),
      draft.archived ? h("p", { className: "jw-note-warn" }, "Archived — not playable until restored.") : null,
      draft.paused ? h("p", { className: "jw-note-warn" }, "Paused — definition kept, removed from the guide.") : null,
    );

    const groupCard = h("div", { className: "jw-card" },
      h("h3", { className: "jw-card-title" }, "Group"),
      h("div", { className: "jw-field" + (fieldError("group") ? " jw-field-invalid" : "") },
        h("label", { className: "jw-field-label", htmlFor: "f-group" }, "Belongs to exactly one group"),
        h("select", {
          id: "f-group", className: "jw-input", value: draft.groupId || "",
          onChange: (e) => {
            const v = e.target.value;
            if (v === "__create__") {
              // Inline group creation: the group rides the SAME Apply
              // packet (buildOps prepends group.put) — never a second write.
              const gid = newGroupId();
              edit((d) => { d.groupId = gid; d.pendingGroupName = ""; return d; });
            } else {
              edit((d) => { d.groupId = v; delete d.pendingGroupName; return d; });
            }
          },
        },
          groups.map((g) => h("option", { key: g.id, value: g.id }, g.name)),
          draft.pendingGroupName != null && !groups.some((g) => g.id === draft.groupId)
            ? h("option", { value: draft.groupId },
                String(draft.pendingGroupName || "").trim()
                  ? "New group: " + String(draft.pendingGroupName).trim()
                  : "New group (name it below)")
            : null,
          h("option", { value: "__create__" }, "Create group…"),
        ),
        draft.pendingGroupName != null && !groups.some((g) => g.id === draft.groupId)
          ? h("input", {
            className: "jw-input", style: { marginTop: "6px" },
            placeholder: "New group name", "aria-label": "New group name",
            value: draft.pendingGroupName || "",
            onChange: (e) => edit((d) => { d.pendingGroupName = e.target.value; return d; }),
          })
          : null,
        fieldError("group") ? h("div", { className: "jw-error-text" }, fieldError("group")) : null,
        h("p", { className: "jw-hint" }, "Groups organize this library and the guide. Membership never changes what airs."),
      ),
    );

    // ---------- rules (What airs) ----------

    const facetRow = (facet) => {
      const cfg = {
        tags: { kind: "tag", allKey: "tags", anyKey: "tagsAny", exclKey: "excludeTags", noun: "tags", icon: "faTags", note: "Sub-tags count automatically. ALL = a scene must carry every tag; ANY = at least one." },
        performers: { kind: "performer", allKey: "performers", anyKey: "performersAny", exclKey: "excludePerformers", sceneKey: "performerSceneCount", noun: "performers", icon: "faUser" },
        studios: { kind: "studio", allKey: "studios", anyKey: "studiosAny", exclKey: "excludeStudios", sceneKey: "studioSceneCount", noun: "studios", icon: "faBuilding", note: "Sub-studios count (hierarchy included)." },
      }[facet];
      const src = draft.source;
      const all = idsOf(src[cfg.allKey]);
      const any = idsOf(src[cfg.anyKey]);
      const excl = idsOf(src[cfg.exclKey]);
      const scene = src[cfg.sceneKey] || null;
      const union = [...all, ...any];

      const setFacet = (mutate) => edit((d) => {
        const s = d.source;
        mutate(s);
        return d;
      });

      const toggleLogic = (mode) => {
        setFacet((s) => {
          // Move the ids of BOTH lists into the chosen storage field (sparse
          // sources may omit the unused list entirely).
          const merged = sortedIds([...idsOf(s[cfg.allKey]), ...idsOf(s[cfg.anyKey])]);
          s[cfg.allKey] = mode === "all" ? merged : [];
          s[cfg.anyKey] = mode === "any" ? merged : [];
          return s;
        });
        toast("Switched to " + mode.toUpperCase() + " — same " + cfg.noun + ", "
          + (mode === "all" ? "a scene must match every one" : "a scene needs at least one") + ".", "");
      };

      const clearIncludes = async () => {
        if (union.length > CLEAR_CONFIRM_MIN) {
          const ok = await confirm({
            title: "Clear " + cfg.noun,
            message: "Remove all " + union.length.toLocaleString() + " chosen " + cfg.noun + " from this rule?",
            confirmLabel: "Clear all",
          });
          if (!ok) return;
        }
        setFacet((s) => { s[cfg.allKey] = []; s[cfg.anyKey] = []; return s; });
      };

      const clearExclusions = async () => {
        if (excl.length > CLEAR_CONFIRM_MIN) {
          const ok = await confirm({
            title: "Clear excluded " + cfg.noun,
            message: "Stop excluding all " + excl.length.toLocaleString() + " " + cfg.noun + "?",
            confirmLabel: "Clear all",
          });
          if (!ok) return;
        }
        setFacet((s) => { s[cfg.exclKey] = []; return s; });
      };

      const pressedAll = all.length > 0 && any.length === 0;
      const pressedAny = any.length > 0 && all.length === 0;

      const pickInto = (key) => setPicker({
        kind: cfg.kind,
        title: (key === cfg.exclKey ? "Choose excluded " : "Choose ") + cfg.noun,
        selected: key === cfg.exclKey ? excl : union,
        onDone: (ids) => {
          const target = key === cfg.exclKey
            ? cfg.exclKey
            : (src[cfg.allKey] && src[cfg.allKey].length ? cfg.allKey : cfg.anyKey);
          setFacet((s) => { s[target] = ids; return s; });
        },
      });

      const setScene = (half, raw) => setFacet((s) => {
        const spec = Object.assign({}, s[cfg.sceneKey] || {});
        const v = parseInt(raw, 10);
        if (Number.isNaN(v)) delete spec[half];
        else spec[half] = v;
        if (spec.min == null && spec.max == null) delete s[cfg.sceneKey];
        else s[cfg.sceneKey] = spec;
        return s;
      });

      const sceneErr = rErrors.find((e) => String(e.path || e.field || "").includes(cfg.sceneKey));
      // Map server typed errors onto THIS facet's row — including its
      // exclusion list (a newly-authored nonexistent exclusion fails the
      // same way a positive id does, with the same source.<key> path).
      const facetErr = rErrors.find((e) => {
        const p = String(e.path || e.field || "");
        return !sceneErr && [cfg.allKey, cfg.anyKey, cfg.exclKey].some((k) => p.includes("." + k));
      });

      return h("div", { key: facet, className: "jw-rule-row" + (facetErr ? " jw-rule-row-invalid" : "") },
        h("div", { className: "jw-rule-head" },
          h("span", { className: "jw-rule-name" },
            h(FaIcon, { name: cfg.icon, size: 13, className: "jw-facet-icon" }),
            facet.charAt(0).toUpperCase() + facet.slice(1)),
          h("span", { className: "jw-logic", role: "group", "aria-label": facet + " match logic" },
            h("button", {
              "aria-pressed": pressedAll ? "true" : "false",
              className: pressedAll ? "jw-logic-on" : "",
              disabled: union.length === 0,
              title: union.length === 0
                ? "Pick " + cfg.noun + " first, then choose the match logic"
                : "A scene must match every one of these",
              onClick: () => toggleLogic("all"),
            }, "ALL"),
            h("button", {
              "aria-pressed": pressedAny ? "true" : "false",
              className: pressedAny ? "jw-logic-on" : "",
              disabled: union.length === 0,
              title: union.length === 0
                ? "Pick " + cfg.noun + " first, then choose the match logic"
                : "A scene matches at least one of these",
              onClick: () => toggleLogic("any"),
            }, "ANY"),
          ),
          union.length === 0
            ? h("span", { className: "jw-hint" }, "match logic applies once you choose " + cfg.noun)
            : null,
          h("div", { className: "jw-rule-actions" },
            h("button", { className: "jw-btn jw-btn-small", onClick: () => pickInto("main") },
              "Choose (" + (union.length ? union.length.toLocaleString() : "none") + ")"),
            h("label", { className: "jw-exclude-label" },
              "exclude",
              h("button", { className: "jw-btn jw-btn-small", onClick: () => pickInto(cfg.exclKey) },
                excl.length ? excl.length.toLocaleString() : "none"),
            ),
          ),
        ),
        union.length
          ? h("div", null,
              h("span", { className: "jw-chip-scope" },
                pressedAll ? "all of" : pressedAny ? "any of" : "matching"),
              all.length > 0 && any.length > 0
                // transient both-lists state the model allows before
                // validation: show each list under its own label
                ? h("div", null,
                    h("span", { className: "jw-chip-scope" }, "all of"),
                    h(FacetChipList, {
                      ids: all, kind: cfg.kind, variant: "include", noun: cfg.noun, confirm,
                      onRemove: (ids) => setFacet((s) => {
                        const rm = new Set(ids);
                        s[cfg.allKey] = (s[cfg.allKey] || []).filter((x) => !rm.has(x));
                        return s;
                      }),
                      onClearAll: () => setFacet((s) => { s[cfg.allKey] = []; return s; }),
                    }),
                    h("span", { className: "jw-chip-scope" }, "any of"),
                    h(FacetChipList, {
                      ids: any, kind: cfg.kind, variant: "include", noun: cfg.noun, confirm,
                      onRemove: (ids) => setFacet((s) => {
                        const rm = new Set(ids);
                        s[cfg.anyKey] = (s[cfg.anyKey] || []).filter((x) => !rm.has(x));
                        return s;
                      }),
                      onClearAll: () => setFacet((s) => { s[cfg.anyKey] = []; return s; }),
                    }))
                : h(FacetChipList, {
                    ids: union, kind: cfg.kind, variant: "include", noun: cfg.noun, confirm,
                    onRemove: (ids) => setFacet((s) => {
                      const rm = new Set(ids);
                      s[cfg.allKey] = (s[cfg.allKey] || []).filter((x) => !rm.has(x));
                      s[cfg.anyKey] = (s[cfg.anyKey] || []).filter((x) => !rm.has(x));
                      return s;
                    }),
                    onClearAll: () => void clearIncludes(),
                  }),
            )
          : h("p", { className: "jw-hint" }, "none — every " + cfg.noun.replace(/s$/, "") + " matches until you narrow it"),
        excl.length
          ? h("div", null,
              h("span", { className: "jw-chip-scope" }, "without"),
              h(FacetChipList, {
                ids: excl, kind: cfg.kind, variant: "exclude", noun: cfg.noun, confirm,
                onRemove: (ids) => setFacet((s) => {
                  const rm = new Set(ids);
                  s[cfg.exclKey] = (s[cfg.exclKey] || []).filter((x) => !rm.has(x));
                  return s;
                }),
                onClearAll: () => void clearExclusions(),
              }))
          : null,
        cfg.sceneKey ? h("div", { className: "jw-dynamic-row" },
          h("span", { className: "jw-dynamic-label" }, "…or match by activity:"),
          h("label", { className: "jw-dynamic-num" },
            h("input", {
              type: "number", min: 0, style: { width: "72px" },
              "aria-label": cfg.noun + " with N or more scenes",
              value: scene && scene.min != null ? scene.min : "",
              onChange: (e) => setScene("min", e.target.value),
            }),
            "or more scenes",
          ),
          h("label", { className: "jw-dynamic-num" },
            h("input", {
              type: "number", min: 1, style: { width: "72px" },
              "aria-label": cfg.noun + " with fewer than N scenes",
              value: scene && scene.max != null ? scene.max : "",
              onChange: (e) => setScene("max", e.target.value),
            }),
            "or fewer scenes",
          ),
          h("span", { className: "jw-hint jw-dynamic-hint" },
            "dynamic — membership updates itself as the library grows"),
        ) : null,
        cfg.note ? h("p", { className: "jw-hint" }, cfg.note) : null,
        facetErr || sceneErr
          ? h("div", { className: "jw-error-text", role: "alert" }, (facetErr || sceneErr).message)
          : null,
      );
    };

    const sceneDetailsRow = (() => {
      const src = draft.source;
      const setMeta = (mutate) => edit((d) => { mutate(d.source); return d; });
      const dateFrom = src.date && src.date.from ? src.date.from : "";
      const dateTo = src.date && src.date.to ? src.date.to : "";
      const durMin = durText.min != null ? durText.min : minutesText(durMinCommitted);
      const durMax = durText.max != null ? durText.max : minutesText(durMaxCommitted);
      const within = src.createdAt && src.createdAt.withinDays ? src.createdAt.withinDays : "";
      const qErr = rErrors.find((e) => String(e.path || e.field || "").includes("duration"));
      const dateErr = rErrors.find((e) => String(e.path || e.field || "").includes("date"));
      const qValue = qLocal != null ? qLocal : committedQ;
      return h("div", { className: "jw-rule-row" + (qErr || dateErr ? " jw-rule-row-invalid" : "") },
        h("div", { className: "jw-rule-head" },
          h("span", { className: "jw-rule-name" }, "Scene details"),
        ),
        h("div", { className: "jw-rule-dates" },
          h("label", { className: "jw-dynamic-num" }, "Released from",
            h("input", {
              type: "date", "aria-label": "Scene date from", value: dateFrom,
              onChange: (e) => setMeta((s) => {
                s.date = { from: e.target.value, to: s.date && s.date.to ? s.date.to : "" };
                if (!s.date.from && !s.date.to) delete s.date;
                return s;
              }),
            })),
          h("label", { className: "jw-dynamic-num" }, "to",
            h("input", {
              type: "date", "aria-label": "Scene date to", value: dateTo,
              onChange: (e) => setMeta((s) => {
                s.date = { from: s.date && s.date.from ? s.date.from : "", to: e.target.value };
                if (!s.date.from && !s.date.to) delete s.date;
                return s;
              }),
            })),
          h("label", { className: "jw-dynamic-num" }, "Min length",
            h("input", {
              type: "number", min: 0, step: "any", style: { width: "76px" },
              id: "f-dur-min", "aria-label": "Minimum duration in minutes",
              value: durMin, placeholder: "min",
              onChange: (e) => setDurationHalf("min", e.target.value),
              onBlur: () => setDurText((t) => Object.assign({}, t, { min: null })),
            })),
          h("label", { className: "jw-dynamic-num" }, "Max length",
            h("input", {
              type: "number", min: 0, step: "any", style: { width: "76px" },
              id: "f-dur-max", "aria-label": "Maximum duration in minutes",
              value: durMax, placeholder: "max",
              onChange: (e) => setDurationHalf("max", e.target.value),
              onBlur: () => setDurText((t) => Object.assign({}, t, { max: null })),
            })),
          h("span", { className: "jw-hint" }, "minutes · decimals ok"),
        ),
        h("div", { className: "jw-rule-dates", style: { marginTop: "8px" } },
          h("label", { className: "jw-dynamic-num" }, "Added within last",
            h("input", {
              type: "number", min: 1, style: { width: "76px" }, "aria-label": "Added within days",
              value: within, placeholder: "days",
              onChange: (e) => setMeta((s) => {
                const v = parseInt(e.target.value, 10);
                if (Number.isNaN(v) || v < 1) delete s.createdAt;
                else s.createdAt = { withinDays: v };
                return s;
              }),
            })),
          h("span", { className: "jw-hint" }, "days"),
          h("input", {
            type: "text", id: "f-qsearch", className: "jw-input", style: { flex: 1 },
            "aria-label": "Text search", placeholder: "title/details contain…", value: qValue,
            onChange: (e) => {
              const v = e.target.value;
              setQLocal(v); // the visible mirror…
              edit((d) => { // …and the draft truth, synchronously (E6)
                if (v.trim()) d.source.q = v.trim();
                else delete d.source.q;
                return d;
              });
            },
            onBlur: () => setQLocal(null),
          }),
        ),
        qErr || dateErr ? h("div", { className: "jw-error-text", role: "alert" }, (qErr || dateErr).message) : null,
      );
    })();

    const legacySummary = (() => {
      const src = draft.source || {};
      const kinds = ["savedFilter", "tag", "performer", "studio"];
      if (!kinds.includes(src.type)) return null;
      const notes = {
        savedFilter: "This channel airs from a linked saved search — used verbatim, including its text query. Editing that search in Stash changes membership.",
        tag: "This channel airs a fixed set of tags. Convert to rules to add exclusions, metadata and dynamic rows.",
        performer: "This channel airs one performer. Convert to rules to combine them with other criteria.",
        studio: "This channel airs one studio (sub-studios included). Convert to rules to combine it with other criteria.",
      };
      return h("div", { className: "jw-card" },
        h("h3", { className: "jw-card-title" }, "What airs"),
        h("p", { className: "jw-hint" }, notes[src.type] || ""),
        h("div", { className: "jw-summary-box", "aria-label": "Plain-language rule summary" },
          summarizeLines(src).map((line, i) => h("div", { key: i, className: "jw-rule-sentence" }, line))),
        h("div", { style: { marginTop: "10px" } },
          h("button", {
            className: "jw-btn",
            onClick: () => {
              edit((d) => {
                d.source = convertLegacySource(d.source);
                return d;
              });
              toast(src.type === "savedFilter"
                ? "Converted to editable rules. The saved search's own criteria are not copied — rebuild them below and watch the preview."
                : "Converted to editable rules. Apply to unlink the legacy source.", "");
            },
          }, "Convert to editable rules…"),
        ),
      );
    })();

    // Disclosure headers carry a one-line summary + an error badge, so a
    // collapsed section never hides authored exclusions or error-bearing
    // controls silently. Sections start OPEN — collapse is owner-chosen.
    const rulesSummaryLine = (() => {
      if (!isRuleSource(draft.source)) return null;
      const head = summarizeLines(draft.source)[0] || "";
      const excl = idsOf(draft.source.excludeTags).length
        + idsOf(draft.source.excludePerformers).length
        + idsOf(draft.source.excludeStudios).length;
      return head.replace(/:$/, "") + (excl ? " · excludes " + excl : "");
    })();
    const rulesErrorCount = rErrors.length;
    const rulesCard = isRuleSource(draft.source)
      ? h(Disclosure, { title: "What airs", summary: rulesSummaryLine, errorCount: rulesErrorCount },
          h("p", { className: "jw-hint" }, "Every row must hold at once. Within a row, ALL / ANY chooses how the picks combine — and the activity rule counts too."),
          ["tags", "performers", "studios"].map(facetRow),
          sceneDetailsRow,
          h("div", { className: "jw-summary-box", "aria-label": "Plain-language rule summary", style: { marginTop: "10px" } },
            summarizeLines(draft.source).map((line, i) => h("div", {
              key: i, className: "jw-rule-sentence" + (i === 0 ? " jw-rule-sentence-head" : ""),
            }, line))),
          rErrors.filter((e) => {
            const p = String(e.path || e.field || "");
            return p === "source" || p.endsWith(".source") || p.includes("empty_rules");
          }).length
            ? h("div", { className: "jw-error-text", role: "alert" },
                rErrors.find((e) => {
                  const p = String(e.path || e.field || "");
                  return p === "source" || p.endsWith(".source") || p.includes("empty_rules");
                }).message)
            : null,
        )
      : legacySummary;

    // ---------- programming ----------

    const prog = draft.programming && typeof draft.programming === "object" ? draft.programming : null;
    const progMode = prog ? prog.mode || "fixed" : "fixed";
    // Only modes an engine actually prepares AND serves for this namespace
    // (audit C7): customs run fixed/explore/discovery; networks run fixed or
    // continuing (operator rollout-gated). A stored legacy mode outside the
    // namespace stays selectable until deliberately changed.
    const isNet = (draft.kind || "net") === "net";
    const offeredModes = isNet ? ["fixed", "continuing"] : ["fixed", "explore", "discovery"];
    const modeOffered = offeredModes.includes(progMode) || progMode === "continuing";
    const setProgramming = (values) => edit((d) => {
      d.programming = fullProgramming(Object.assign({}, canonicalProgramming(d.programming), values));
      return d;
    });
    const progSummaryLine = (() => {
      const sortLabel = (SORTS.find((s) => s.key === (draft.sort || "shuffle")) || {}).label || (draft.sort || "shuffle");
      const modeLabel = { fixed: "Fixed loop", explore: "Explore", discovery: "Discovery", continuing: "Continuing" }[progMode] || progMode;
      return modeLabel + " · " + sortLabel + (isNet ? "" : " · spacing " + canonicalProgramming(prog).spacing);
    })();
    const programmingCard = h(Disclosure, { title: "Programming", summary: progSummaryLine, errorCount: 0 },
      h("div", { className: "jw-fieldrow" },
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "f-mode" }, "Mode"),
          h("select", {
            id: "f-mode", className: "jw-input", value: progMode,
            onChange: (e) => setProgramming({ mode: e.target.value }),
          },
            h("option", { value: "fixed" }, "Fixed loop — the rotation repeats"),
            !isNet ? h("option", { value: "explore" }, "Explore — shuffled, no recent repeats") : null,
            !isNet ? h("option", { value: "discovery" }, "Discovery — pushes unheard content") : null,
            isNet ? h("option", { value: "continuing" },
              "Continuing — full library like a broadcast") : null,
            !modeOffered ? h("option", { value: progMode },
              progMode + " (stored — not servable for this channel; pick another)") : null,
          ),
          isNet
            ? h("p", { className: "jw-hint" },
                "Continuing airs only where the operator rollout activates it; an authored fixed pin always wins.")
            : null,
        ),
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "f-spacing" }, "Spacing"),
          h("select", {
            id: "f-spacing", className: "jw-input", value: canonicalProgramming(prog).spacing,
            onChange: (e) => setProgramming({ spacing: Number(e.target.value) }),
          }, Array.from({ length: 11 }, (_, n) => h("option", { key: n, value: n },
            n ? n + (n === 1 ? " program apart" : " programs apart") : "Follow play order"))),
        ),
      ),
      h("div", { className: "jw-field" },
        h("span", { className: "jw-field-label" }, "Play order"),
        h("div", { className: "jw-chip-row", role: "group", "aria-label": "Play order" },
          SORTS.map((s) => h("button", {
            key: s.key,
            className: "jw-chip" + ((draft.sort || "shuffle") === s.key ? " jw-chip-active" : ""),
            "aria-pressed": ((draft.sort || "shuffle") === s.key) ? "true" : "false",
            onClick: () => edit((d) => { d.sort = s.key; return d; }),
          }, s.label)),
          draft.sort && !SORTS.some((s) => s.key === draft.sort)
            ? h("span", {
                className: "jw-chip jw-chip-active",
                title: "Stored value this editor does not offer — picking a chip replaces it",
              }, draft.sort + " (stored)")
            : null),
        h("p", { className: "jw-hint" },
          "The order scenes enter the on-air loop. Shuffle re-rolls only when the rules change.")),
      progMode !== "fixed" ? h("div", { className: "jw-fieldrow" },
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "f-repeat" }, "No repeats within"),
          h("select", {
            id: "f-repeat", className: "jw-input", value: canonicalProgramming(prog).repeatHours,
            onChange: (e) => setProgramming({ repeatHours: Number(e.target.value) }),
          }, [0, 12, 24, 48, 72, 168].map((n) => h("option", { key: n, value: n },
            n ? n + " hours" : "One complete pass"))),
        ),
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "f-spotlight" }, "Weekly spotlight"),
          h("select", {
            id: "f-spotlight", className: "jw-input", value: canonicalProgramming(prog).spotlight,
            onChange: (e) => setProgramming({ spotlight: e.target.value }),
          },
            h("option", { value: "none" }, "No spotlight"),
            h("option", { value: "studio" }, "Studio spotlight"),
            h("option", { value: "performer" }, "Performer double feature")),
        ),
      ) : null,
      h("p", { className: "jw-hint" },
        "Renaming, regrouping, or re-branding never resets what's playing. Changing rules re-prepares the schedule without touching aired history."),
    );

    // ---------- preview ----------

    const previewCard = h("div", { className: "jw-card" },
      h("div", { className: "jw-card-head" },
        h("h3", { className: "jw-card-title" }, "Preview"),
        h("button", {
          className: "jw-btn jw-btn-ghost jw-btn-small",
          disabled: previewState === "loading",
          title: "Re-run the preview against the current draft",
          onClick: () => { schedulePreview.cancel(); void loadPreview(); },
        }, previewState === "loading" ? "Checking…" : "Refresh"),
      ),
      previewState === "loading" ? h("p", { className: "jw-hint" }, "Checking the current draft…") : null,
      previewState === "error"
        ? h("p", { className: "jw-error-text", role: "alert" },
            "Preview failed: " + (preview && preview.message ? preview.message : "unknown error") + " ",
            h("button", { className: "jw-link", onClick: () => void loadPreview() }, "Retry"))
        : null,
      previewState === "ok" && preview && preview.status === "missing_source"
        ? h("p", { className: "jw-error-text", role: "alert" }, preview.message || "This source no longer resolves.")
        : null,
      previewState === "ok" && preview && preview.status === "ok" ? h("div", null,
        h("div", { className: "jw-count-line" },
          h("div", { className: "jw-count-block" },
            h("div", { className: "jw-count-n" }, fmtCount(preview.poolCount)),
            h("div", { className: "jw-count-l" }, "source pool (matches rules)")),
          h("div", { className: "jw-count-block" },
            h("div", { className: "jw-count-n" }, fmtCount(preview.rotationSize)),
            h("div", { className: "jw-count-l" }, "on-air rotation (max " + ROTATION_SIZE + ")")),
        ),
        preview.poolCount === 0
          ? h("p", { className: "jw-hint", role: "status" },
              "0 scenes match these rules — the channel would read Off Air. That's a valid rule set; "
              + "adjust the bounds to bring scenes back.")
          : h("div", { className: "jw-preview-strip" },
              (preview.sample || []).map((sItem) => h("div", { key: sItem.id, className: "jw-sample-card" },
                h("div", {
                  className: "jw-sample-art", "aria-hidden": "true",
                  style: sItem.preview ? { backgroundImage: "url('" + sItem.preview + "')" } : null,
                },
                  sItem.preview ? "" : "▶",
                  sItem.duration != null
                    ? h("span", { className: "jw-sample-dur" }, fmtDuration(sItem.duration))
                    : null),
                h("div", { className: "jw-sample-cap" },
                  h("div", { className: "jw-sample-title" }, sItem.title || "Untitled"),
                  [sItem.studio, sItem.date].filter(Boolean).join(" · ")),
              ))),
        preview.rotationComplete === false
          ? h("p", { className: "jw-hint" }, "The scan bound (" + ROTATION_SCAN_LIMIT.toLocaleString() + " rows) capped this rotation.")
          : null,
      ) : null,
      previewState === "idle" ? h("p", { className: "jw-hint" }, "No preview yet.") : null,
    );

    // ---------- action bar ----------

    const dirty = isDirty();
    const applyEnabled = dirty && invalid.length === 0 && phase !== "applying"
      && phase !== "validating" && !inFlightRef.current;
    applyEnabledRef.current = applyEnabled;
    const barState = (() => {
      if (phase === "applying") {
        return h("span", { className: "jw-apply-state" },
          "Applying… — you can keep editing or switch channels; we’ll report when it lands.");
      }
      if (phase === "validating") return h("span", { className: "jw-apply-state" }, "Validating…");
      if (applyError && applyError.kind === "conflict") {
        return h("span", { className: "jw-apply-state jw-apply-state-err" },
          "Revision conflict — someone else applied r" + applyError.currentRevision + ". Your draft is intact.");
      }
      if (applyError && applyError.kind === "transport") {
        return h("span", { className: "jw-apply-state jw-apply-state-err" },
          applyError.message + " — your draft is intact. Apply again to retry with the same requestId.");
      }
      if (applyError && applyError.kind === "validation") {
        return h("span", { className: "jw-apply-state jw-apply-state-err" },
          (applyError.errors.length) + " server validation error(s) — shown under the fields. Draft intact.");
      }
      if (applyError && applyError.kind === "rejected") {
        return h("span", { className: "jw-apply-state jw-apply-state-err" },
          "Rejected: " + applyError.message + " — draft intact.");
      }
      if (phase === "applied") {
        return h("span", { className: "jw-apply-state jw-apply-state-ok" }, "Applied at r" + lastAppliedRev + ".");
      }
      if (invalid.length) return h("span", { className: "jw-apply-state jw-apply-state-err" }, invalid[0].message);
      if (dirty) {
        return h("span", { className: "jw-apply-state" },
          "Unsaved draft — Apply to commit.",
          h("kbd", { className: "jw-kbd", title: "Commit this draft" }, APPLY_SHORTCUT));
      }
      return h("span", { className: "jw-apply-state" }, "All changes applied.");
    })();

    // The durable refresh pill: pending / failed(+Retry) / ready(+off-air,
    // +missing). State comes from GetChannelRefreshStatus and changes only
    // when that durable state changes — never an 8-second timer.
    const refreshPill = (() => {
      if (!refreshState || refreshState.state === "unknown") return null;
      if (refreshState.state === "pending") {
        return h("span", {
          className: "jw-pill jw-pill-dirty", title: "Membership changed — the rotation and programming are being recomputed",
        }, "Programming refresh pending…");
      }
      if (refreshState.state === "failed") {
        return h("span", {
          className: "jw-pill jw-pill-err",
          title: "The last refresh could not reach Stash. Last-known numbers stay shown; Retry queues the recompute for the next pass.",
        },
          "Refresh failed (Stash unavailable) ",
          h("button", { className: "jw-link", onClick: () => void retryRefresh() }, "Retry"));
      }
      if (refreshState.offAir) {
        return h("span", {
          className: "jw-pill jw-pill-dirty",
          title: "The rules are valid but nothing matches them right now — raising the duration past everything lands here, and it stays saved",
        }, "Off air — 0 scenes match");
      }
      if (refreshState.missing) {
        return h("span", {
          className: "jw-pill jw-pill-err",
          title: "The linked source no longer resolves — relink it or edit the rules",
        }, "Source missing");
      }
      return h("span", { className: "jw-pill jw-pill-clean", title: "No refresh work is outstanding for this channel" }, "Ready");
    })();

    const conflicting = applyError && applyError.kind === "conflict";

    const copyErrorDetails = () => {
      const bundle = diagBundle({
        stage: "apply", kind: applyError ? applyError.kind : null, channelId,
        requestId: (pendingRef.current && pendingRef.current.requestId) || null,
      });
      copyText(bundle).then((ok) => {
        toast(ok ? "Error details copied — keep them with the request id when reporting."
                 : "Could not copy — use “Download diagnostics” in the toolbar.", ok ? "" : "err");
      });
    };

    const actionBar = h("div", { className: "jw-editor-actions" },
      rebaseNotice ? h("div", { className: "jw-apply-row jw-rebase-note", role: "status" },
        rebaseNotice) : null,
      h("div", { className: "jw-apply-row" },
        h("span", { className: "jw-apply-wrap", role: "status", "aria-live": "polite" },
          barState,
          preflightNote ? h("span", {
            className: "jw-apply-state",
            title: preflightNote,
          }, "Pre-check unavailable — Apply validates on commit.") : null),
        refreshPill,
        applyError ? h("button", {
          className: "jw-link", onClick: copyErrorDetails, title: "Copy a redacted, correlated event bundle for reporting",
        }, "Copy error details") : null,
        h("span", { style: { flex: 1 } }),
        h("button", { className: "jw-btn", onClick: discardDraft, disabled: !dirty && !isTemp || phase === "applying" }, "Discard"),
        h("button", {
          className: "jw-btn jw-btn-primary" + (applyEnabled ? " jw-apply-hot" : ""), id: "apply-btn",
          disabled: !applyEnabled,
          title: "Commit this draft to the library" + (applyEnabled ? " (" + APPLY_SHORTCUT + ")" : ""),
          onClick: () => void doApply(),
        }, phase === "applying" ? "Applying…" : phase === "validating" ? "Validating…" : "Apply"),
      ),
      conflicting ? h("div", { className: "jw-apply-row", style: { marginTop: "8px" } },
        h("button", {
          className: "jw-btn",
          title: "Dismiss this notice — nothing is sent",
          onClick: () => setApplyError(null),
        }, "Keep editing my draft"),
        h("button", {
          className: "jw-btn jw-btn-primary",
          title: "Commit my draft on top of revision " + applyError.currentRevision,
          onClick: () => void doApply(applyError.currentRevision),
        }, "Overwrite r" + applyError.currentRevision + " with my draft"),
      ) : null,
    );

    // ---------- editor header + menu ----------

    const menuItems = [];
    if (arrange.available) {
      menuItems.push({
        label: "Move / insert…",
        action: () => arrange.openResolution(
          { ref: channelId, kind: draft.kind || "net", isNew: isTemp },
          Number.isInteger(draft.number) ? draft.number : BANDS[draft.kind || "net"][0]),
      });
    }
    menuItems.push({ label: "Duplicate…", action: duplicate });
    if (draft.paused) menuItems.push({ label: "Resume", action: () => patchChannel({ paused: false }, "Resumed") });
    else menuItems.push({ label: "Pause", action: () => patchChannel({ paused: true }, "Paused") });
    if (draft.archived) menuItems.push({ label: "Restore from archive", action: () => patchChannel({ archived: false }, "Restored") });
    else menuItems.push({ label: "Archive", action: () => patchChannel({ archived: true }, "Archived") });
    if (drafts.get(channelId)) menuItems.push({ label: "Discard draft", action: discardDraft });
    if (!isTemp) menuItems.push({ label: "History…", action: () => { setMenuOpen(false); setHistoryOpen(true); } });

    const head = h("div", { className: "jw-editor-head" },
      h(GlyphTile, { codepoint: draft.glyph, color: draft.color, size: 36, fallback: draft.number }),
      h("div", { className: "jw-editor-head-id" },
        h("h2", { className: "jw-editor-head-name" }, draft.name || "(unnamed)"),
        h("div", { className: "jw-editor-head-sub" },
          [
            "Channel " + (draft.number != null ? draft.number : "—"),
            groupById.get(draft.groupId) ? groupById.get(draft.groupId).name : "",
            SOURCE_LABELS[draft.source && draft.source.type] || "",
            draft.provenance && draft.provenance.origin === "v4-final-proposal" ? "from v4-final proposal" : (isTemp ? "new — not on the server yet" : "custom"),
          ].filter(Boolean).join(" · ")),
      ),
      h("div", { className: "jw-editor-head-badges" },
        drafts.get(channelId) ? h(StatusChip, { kind: "warn" }, "draft") : null,
        draft.archived ? h(StatusChip, { kind: "dim" }, "archived") : null,
        draft.paused ? h(StatusChip, { kind: "dim" }, "paused") : null,
      ),
      h("div", { className: "jw-menu-host", ref: menuRef },
        h("button", {
          className: "jw-btn jw-btn-small", "aria-haspopup": "menu",
          "aria-expanded": menuOpen ? "true" : "false",
          "aria-label": "Channel actions", onClick: () => setMenuOpen((v) => !v),
        }, "⋯"),
        menuOpen ? h("div", { className: "jw-menu", role: "menu" },
          menuItems.map((item) => h("button", {
            key: item.label, role: "menuitem", className: "jw-menu-item",
            onClick: () => { setMenuOpen(false); item.action(); },
          }, item.label)),
        ) : null,
      ),
    );

    return h("div", { className: "jw-editor" },
      head,
      h("div", { className: "jw-editor-scroll" },
        identityCard,
        groupCard,
        rulesCard,
        programmingCard,
        previewCard,
      ),
      actionBar,
      picker ? h(EntityPickerDialog, {
        kind: picker.kind, title: picker.title, selectedIds: picker.selected,
        onClose: () => setPicker(null),
        onDone: (ids) => { picker.onDone(ids); },
      }) : null,
      historyOpen ? h(HistoryDialog, {
        channelId, channelName: draft.name || channelId,
        fetchPage: ({ offset: pageOffset, limit: pageLimit }) => runOp("GetChannelHistory", {
          offset: pageOffset, limit: pageLimit, includeDefinitions: true,
        }),
        onClose: () => setHistoryOpen(false),
        onRestore: (version) => {
          edit((d) => Object.assign(d, version));
          toast("Revision staged as your draft — Apply to commit it as a new change.", "");
        },
      }) : null,
    );
  }

  // ------------------------------------------------------------------
  // Dial list (left rail): grouped, collapsible, windowed
  // ------------------------------------------------------------------

  function buildDialItems({ channels, groups, query, groupFilter, collapsed }) {
    const items = [];
    const q = query.trim().toLowerCase();
    const searching = !!q || !!groupFilter;
    for (const g of groups) {
      if (groupFilter && g.id !== groupFilter) continue;
      const members = channels.filter((c) => c.groupId === g.id);
      if (!members.length && q) continue;
      // UX8: the header shows the member count AND the actual number span /
      // gap summary (current truth, never an implied reservation).
      let spanText = "";
      let spanTitle = members.length + " channel" + (members.length === 1 ? "" : "s");
      if (members.length) {
        const nums = members.map((c) => c.number).filter((n) => Number.isInteger(n)).sort((a, b) => a - b);
        if (nums.length) {
          const gaps = (nums[nums.length - 1] - nums[0] + 1) - nums.length;
          spanText = nums[0] + "–" + nums[nums.length - 1] + (gaps > 0 ? " · " + gaps + " gap" + (gaps === 1 ? "" : "s") : "");
          spanTitle += " · numbers " + nums[0] + "–" + nums[nums.length - 1]
            + (gaps > 0 ? " · " + gaps + " free number" + (gaps === 1 ? "" : "s") + " inside the span" : " · no gaps");
        }
      }
      items.push({ type: "header", g, count: members.length, spanText, spanTitle });
      if (!(collapsed.has(g.id) && !searching)) {
        for (const c of members) items.push({ type: "ch", c });
      }
    }
    return items;
  }

  function DialRow({ ch, selected, bulkMode, checked, hasDraft, groupsById, onSelect, onCheck, onInsert }) {
    const shiftRef = useRef(false);
    const [insertOpen, setInsertOpen] = useState(false);
    const insertRef = useOutsideClose(insertOpen, () => setInsertOpen(false));
    const ariaLabel = (ch.number != null ? ch.number + " · " : "") + ch.name
      + (ch.archived ? ", archived" : ch.paused ? ", paused" : "") + (ch.staged ? ", staged number" : "");
    return h("div", {
      className: "jw-dial-row" + (selected ? " jw-dial-row-selected" : "") + (ch.paused || ch.archived ? " jw-dial-row-muted" : ""),
      style: selected ? { boxShadow: "inset 3px 0 0 " + (ch.color || "#455A64") } : null,
      role: "option", "aria-selected": (bulkMode ? checked : selected) ? "true" : "false", tabIndex: 0,
      "aria-label": ariaLabel,
      onClick: (e) => (bulkMode ? onCheck(ch.id, !checked, e.shiftKey) : onSelect(ch.id)),
      onKeyDown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (bulkMode) onCheck(ch.id, !checked, e.shiftKey);
          else onSelect(ch.id);
        }
      },
    },
      bulkMode ? h("input", {
        type: "checkbox", "aria-label": "Select " + ch.name, checked,
        onClick: (e) => { e.stopPropagation(); shiftRef.current = e.shiftKey; },
        onChange: (e) => onCheck(ch.id, e.target.checked, shiftRef.current),
      }) : null,
      h("div", {
        className: "jw-dial-number" + (ch.staged ? " jw-dial-number-staged" : ""),
        title: ch.staged ? "Staged — becomes this number when you Apply the arrangement" : undefined,
      }, ch.number != null ? String(ch.number) : "—"),
      h(GlyphTile, { codepoint: ch.glyph, color: ch.color, size: 30, fallback: ch.number }),
      h("div", { className: "jw-dial-meta" },
        // The number cell carries the number — never duplicated in the name
        // (the full "N · Name" stays in storage and the row aria-label).
        h("div", { className: "jw-dial-name" }, ch.name),
        h("div", { className: "jw-dial-sub" }, describeRow(ch, groupsById)),
      ),
      h("div", { className: "jw-dial-badges" },
        hasDraft ? h(StatusChip, { kind: "warn" }, "draft") : null,
        ch.staged ? h(StatusChip, { kind: "accent" }, "staged") : null,
        ch.archived ? h(StatusChip, { kind: "dim" }, "archived") : null,
        !ch.archived && ch.paused ? h(StatusChip, { kind: "dim" }, "paused") : null,
        ch.temp ? h(StatusChip, { kind: "accent" }, "new") : null,
      ),
      // UX4: contextual insert actions — a new channel before/after this
      // row prefills the placement sheet (the conflict resolver).
      onInsert && !bulkMode ? h("span", {
        className: "jw-menu-host jw-insert-host", ref: insertRef,
        onClick: (e) => e.stopPropagation(),
        onKeyDown: (e) => e.stopPropagation(),
      },
        h("button", {
          className: "jw-btn jw-btn-ghost jw-group-menu-btn jw-insert-btn",
          "aria-haspopup": "menu", "aria-expanded": insertOpen ? "true" : "false",
          "aria-label": "Insert a new channel next to " + ch.name,
          title: "New channel before/after this",
          onClick: (e) => { e.stopPropagation(); setInsertOpen((v) => !v); },
        }, "＋"),
        insertOpen ? h("span", { className: "jw-menu jw-group-menu", role: "menu" },
          h("button", {
            role: "menuitem", className: "jw-menu-item",
            onClick: (e) => { e.stopPropagation(); setInsertOpen(false); onInsert(ch, "before"); },
          }, "New channel before this"),
          h("button", {
            role: "menuitem", className: "jw-menu-item",
            onClick: (e) => { e.stopPropagation(); setInsertOpen(false); onInsert(ch, "after"); },
          }, "New channel after this"),
        ) : null,
      ) : null,
    );
  }

  // Group-header contextual actions (plan §3.1): Select group /
  // Arrange numbers… / Edit group…. 28 px inside the 44 px row — the dial
  // geometry is untouched.
  function GroupHeaderMenu({ group, arrangeAvailable, onAction }) {
    const [open, setOpen] = useState(false);
    const ref = useOutsideClose(open, () => setOpen(false));
    const item = (label, fn) => h("button", {
      key: label, role: "menuitem", className: "jw-menu-item",
      onClick: (e) => { e.stopPropagation(); setOpen(false); fn(); },
    }, label);
    return h("span", {
      className: "jw-menu-host jw-group-menu-host", ref,
      onClick: (e) => e.stopPropagation(),
      onKeyDown: (e) => e.stopPropagation(),
    },
      h("button", {
        className: "jw-btn jw-btn-ghost jw-group-menu-btn",
        "aria-haspopup": "menu", "aria-expanded": open ? "true" : "false",
        "aria-label": "Actions for group " + group.name,
        onClick: (e) => { e.stopPropagation(); setOpen((v) => !v); },
      }, "⋯"),
      open ? h("span", { className: "jw-menu jw-group-menu", role: "menu" },
        item("Select group", () => onAction("select")),
        arrangeAvailable ? item("Arrange numbers…", () => onAction("arrange")) : null,
        item("Edit group…", () => onAction("edit")),
      ) : null,
    );
  }

  // ------------------------------------------------------------------
  // App
  // ------------------------------------------------------------------

  function App() {
    const [lib, setLib] = useState(null);
    const [bootError, setBootError] = useState(null);
    const [selectedId, setSelectedId] = useState(null);
    const [query, setQuery] = useState("");
    const [groupFilter, setGroupFilter] = useState("");
    const [collapsed, setCollapsed] = useState(() => new Set());
    const [bulkMode, setBulkMode] = useState(false);
    const [bulkSelected, setBulkSelected] = useState(() => new Set());
    const [bulkAnchor, setBulkAnchor] = useState(null);
    const [features, setFeatures] = useState(null); // Capabilities.features (null = old plugin)
    const [numRes, setNumRes] = useState(null);     // {subject, target, onStaged}
    const [organize, setOrganize] = useState(null); // {scope?, intent?, reviewStaged?}
    const [orgTick, setOrgTick] = useState(0);
    const [arrBusy, setArrBusy] = useState(false);
    const [live, setLive] = useState(null);         // aria-live announcements
    const [dialog, setDialog] = useState(null); // "groups" | "new" | "shortcuts"
    const [moreOpen, setMoreOpen] = useState(false);
    const moreRef = useOutsideClose(moreOpen, () => setMoreOpen(false));
    const [actionsOpen, setActionsOpen] = useState(false);
    const actionsRef = useOutsideClose(actionsOpen, () => setActionsOpen(false));
    const [confirmSpec, setConfirmSpec] = useState(null);
    const [toasts, setToasts] = useState([]);
    const [inflights, setInflights] = useState([]); // [{channelId, name}] — background applies

    useEffect(() => onInflightChange((snap) => setInflights(snap)), []);
    const [draftCount, setDraftCount] = useState(0);
    const [editorKeyBump, setEditorKeyBump] = useState(0);
    const [reloadTick, setReloadTick] = useState(0);
    const [libraryVersion, setLibraryVersion] = useState(0);

    const draftsRef = useRef(null);
    const libRef = useRef(null);
    const liveRevisionRef = useRef(null); // F4: revision adopted SYNCHRONOUSLY on fetch, before any render
    const selectedIdRef = useRef(null);
    const searchInputRef = useRef(null);
    const toastSeq = useRef(0);
    const orgRef = useRef(null);          // makeOrgStore(libraryId), created at boot
    const organizeFormRef = useRef(null); // UX7: partial organize-sheet settings survive closing
    const liveSeq = useRef(0);

    libRef.current = lib;
    selectedIdRef.current = selectedId;

    const announce = useCallback((message) => {
      liveSeq.current += 1;
      setLive({ n: liveSeq.current, message });
    }, []);

    const toast = useCallback((message, kind, opts) => {
      const id = ++toastSeq.current;
      setToasts((cur) => [...cur, { id, message, kind, action: opts && opts.action }]);
      setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== id)), kind === "err" ? 7000 : 4200);
    }, []);

    const arrangementAvailable = !!(features && features.arrangement);

    const confirm = useCallback((spec) => new Promise((resolve) => {
      setConfirmSpec(Object.assign({}, spec, { resolve: (ok) => { setConfirmSpec(null); resolve(ok); } }));
    }), []);

    const fetchLibrary = useCallback(async () => {
      let out = await runOp("GetChannelLibrary", { limit: 1000 });
      const channels = (out.channels || []).slice();
      while (channels.length < out.total && channels.length < 6000) {
        const more = await runOp("GetChannelLibrary", { limit: 1000, offset: channels.length });
        if (!more.channels || !more.channels.length) break;
        channels.push(...more.channels);
      }
      out = Object.assign({}, out, { channels });
      return out;
    }, []);

    // ---- boot ----
    useEffect(() => {
      resolveGlyphs();
      let alive = true;
      (async () => {
        try {
          // Capability handshake rides the boot: features.arrangement gates
          // the organize surface; absent/failed -> basic editing only.
          fetchCapabilities().then((caps) => {
            if (alive) setFeatures(caps && caps.features ? caps.features : null);
          });
          const data = await fetchLibrary();
          if (!alive) return;
          const libraryId = data.libraryId || "default";
          draftsRef.current = makeDraftStore(libraryId);
          draftsRef.current.subscribe(() => setDraftCount(draftsRef.current.count()));
          setDraftCount(draftsRef.current.count());
          orgRef.current = makeOrgStore(libraryId);
          orgRef.current.subscribe(() => setOrgTick((x) => x + 1));
          setOrgTick((x) => x + 1);
          setLib(data);
          liveRevisionRef.current = typeof data.revision === "number" ? data.revision : null;
          const first = (data.channels || [])[0];
          if (first) setSelectedId(first.id);
          recoverPending(libraryId);
        } catch (e) {
          if (alive) setBootError(String((e && e.message) || e));
        }
      })();
      return () => { alive = false; };
    }, [fetchLibrary]);

    // Receipt recovery after a reload (both scopes share the one pending
    // slot, frozen contract decision 6): an in-flight Apply at reload time
    // resolves to exactly one commit — never a duplicated resubmission.
    const recoverPending = useCallback((libraryId) => {
      const entry = readOpsPending(libraryId);
      if (!entry || !entry.requestId) return;
      void entry.expected; // the receipt carries the truth
      pollApplyReceipt(entry.requestId).then(async (receipt) => {
        if (!receipt || receipt.status === "unknown") {
          // R6: the submission identity survives — the bar must offer
          // Check result / Retry, not a phantom "Applying…".
          if (entry.scope === "arrangement" && orgRef.current && orgRef.current.get().pending
              && orgRef.current.get().pending.requestId === entry.requestId) {
            orgRef.current.setPendingState("outcome_unknown");
          }
          return; // keep for a manual retry
        }
        clearOpsPending(libraryId);
        if (receipt.status === "committed") {
          const org = orgRef.current;
          const staged = org ? org.get().staged : null;
          if (entry.scope === "arrangement" && org && org.get().pending
              && org.get().pending.requestId === entry.requestId) {
            // Reconcile the SUBMITTED snapshot (R2), not whatever is staged.
            finishArrangementCommit(receipt, org.get().pending);
          } else {
            await refreshLibrary();
            toast((entry.label || "Apply") + " — committed at r" + receipt.revision + " (recovered).", "ok");
          }
          void staged;
        } else if (receipt.error === "revision_conflict") {
          if (entry.scope === "arrangement" && orgRef.current) orgRef.current.clearPending();
          await refreshLibrary();
          toast("A queued Apply hit a revision conflict while away — nothing changed; review and Apply again.", "err");
        } else if (entry.scope === "arrangement" && orgRef.current) {
          orgRef.current.clearPending();
        }
      }).catch(() => { /* offline: the pending record stays for the next visit */ });
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // "/" focuses search; beforeunload guards drafts
    useEffect(() => {
      const onKey = (e) => {
        if (e.key !== "/" ) return;
        const tag = document.activeElement && document.activeElement.tagName;
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
        e.preventDefault();
        if (searchInputRef.current) { searchInputRef.current.focus(); searchInputRef.current.select(); }
      };
      document.addEventListener("keydown", onKey);
      const onUnload = (e) => {
        if (draftsRef.current && draftsRef.current.count() > 0) {
          e.preventDefault();
          e.returnValue = "";
        }
      };
      window.addEventListener("beforeunload", onUnload);
      return () => {
        document.removeEventListener("keydown", onKey);
        window.removeEventListener("beforeunload", onUnload);
      };
    }, []);

    const refreshLibrary = useCallback(async () => {
      try {
        const data = await fetchLibrary();
        // F4: adopt the fetched revision into the ref-level slot the moment
        // the response arrives — render timing must not decide what the
        // next queued submission stamps as expectedRevision.
        if (data && typeof data.revision === "number") liveRevisionRef.current = data.revision;
        setLib(data);
        setLibraryVersion((x) => x + 1); // editors rebase their drafts on fresh state
        return data;
      } catch (e) {
        toast("Could not reload the library: " + String((e && e.message) || e), "err");
        return null;
      }
    }, [fetchLibrary, toast]);

    // ---- shared submit for NON-editor applies (bulk, groups, patches) ----
    // ONE coordinator for every mutation path (audit C9): the request
    // identity is persisted through reload, an identical retry reuses it
    // (exactly one commit), and a transport failure is UNKNOWN — never
    // announced as "nothing changed".
    // Submissions serialize per tab (frozen contract decision 6) — a second
    // Apply waits for the first RECEIPT, so expectedRevision is never
    // guessed. Editing during flight stays free; newer edits remain dirty.
    const submitOps = useCallback((ops, opts) => enqueueSubmit(async () => {
      const options = opts || {};
      const libraryId = libRef.current ? (libRef.current.libraryId || "default") : "default";
      // F4: stamp expectedRevision from the synchronously-adopted slot —
      // never from render-timing state that a just-released queue item may
      // not have seen yet.
      const expected = options.expectedOverride != null
        ? options.expectedOverride
        : (liveRevisionRef.current != null ? liveRevisionRef.current
          : (libRef.current ? libRef.current.revision : 0));
      const opsDigest = JSON.stringify(ops);
      const pending = readOpsPending(libraryId);
      const requestId = options.requestIdOverride
        || (pending && pending.opsDigest === opsDigest && pending.expected === expected
          ? pending.requestId : newRequestId());
      if (!pending || pending.requestId !== requestId) {
        writeOpsPending(libraryId, {
          requestId, expected, opsDigest,
          label: options.label || "",
          scope: options.scope || "channel",
        });
      }
      let receipt;
      try {
        receipt = await applyChannelChanges(requestId, expected, ops, options.onSubmitted);
      } catch (e) {
        toast("The Apply may or may not have committed — its outcome is unknown. "
              + "Apply again to reuse the same request (it cannot commit twice).", "err");
        return { status: "transport", error: "transport" };
      }
      clearOpsPending(libraryId);
      if (receipt.status === "committed") {
        if (options.scope === "arrangement") {
          // finishArrangementCommit announces, rebases and refreshes.
        } else {
          const affected = countAffected(ops);
          toast((options.label || "Applied") + (affected ? " (" + affected + " channel" + (affected === 1 ? "" : "s") + ")" : "")
            + " — committed at r" + receipt.revision + ".", "ok");
          await refreshLibrary();
        }
      } else if (receipt.error === "revision_conflict") {
        await refreshLibrary();
        toast("Revision conflict — the library moved to r" + receipt.currentRevision + ". Nothing changed; try again.", "err");
      } else if (receipt.error === "validation_failed" && receipt.errors && receipt.errors.length) {
        toast("Rejected: " + receipt.errors[0].message, "err");
      } else {
        toast("Rejected: " + (receipt.message || receipt.error || "unknown error"), "err");
      }
      return receipt;
    }), [refreshLibrary, toast]);

    // R9: the editor's channel Apply rides the SAME coordinator. The wrapper
    // adopts the committed revision (refreshLibrary) before the queue
    // releases, so a follow-up bulk/arrangement Apply never guesses a stale
    // expectedRevision.
    const submitChannelApply = useCallback((fn) => enqueueSubmit(async () => {
      const receipt = await fn();
      if (receipt && receipt.status === "committed") await refreshLibrary();
      return receipt;
    }), [refreshLibrary]);

    // ---- arrangement Apply (scope: "arrangement") ----
    // Joins the SAME coordinator: the frozen packet is submitted verbatim,
    // an identical retry reuses the same requestId (exactly-once), and the
    // durable receipt is the only success signal.
    //
    // R2: `submitted` is the IMMUTABLE pending snapshot (packet +
    // beforeAfter + correlationToken) captured at submission — completion
    // reconciles ONLY that, never the editable staged draft. A NEWER
    // staging survives the commit as dirty; it is re-reviewed/re-planned
    // against the new revision, never silently cleared and never applied to
    // content drafts.
    const finishArrangementCommit = useCallback(async (receipt, submitted) => {
      const org = orgRef.current;
      const snap = submitted || (org ? org.get().pending : null);
      if (!snap) return;
      const packetOps = (snap.packet && snap.packet.ops) || [];
      const idMap = receipt.idMap || {};
      if (draftsRef.current) {
        // R3: compare each temp draft to its SUBMITTED wire snapshot. Drop
        // only an unchanged, fully-acknowledged draft; remap newer edits
        // onto receipt.idMap[tempId] with the fresh acknowledged base
        // (pending editor state rides along).
        for (const op of packetOps) {
          if (op.op !== "channel.create" || !op.tempId) continue;
          const finalId = idMap[op.tempId];
          if (!finalId) continue;
          const entry = draftsRef.current.get(op.tempId);
          const current = entry && entry.draft;
          if (current && draftDiffersFromSubmitted(current, op.channel)) {
            // F2: the remapped draft must carry the SUBMITTED plan's final
            // number/groupId for this tempRef — otherwise it would diff
            // from the committed base as a phantom pending renumber and
            // the next Apply would silently move the channel back.
            const remapped = clone(current);
            if (op.channel && op.channel.number != null) remapped.number = op.channel.number;
            if (op.channel && op.channel.groupId != null) remapped.groupId = op.channel.groupId;
            let fresh = null;
            try {
              fresh = (await runOp("GetChannelDefinition", { channelId: finalId })).channel;
            } catch (e) { /* definition unavailable — handled below */ }
            if (fresh) {
              draftsRef.current.put(finalId, remapped,
                { base: clone(fresh), pending: (entry && entry.pending) || null });
            } else {
              // F2: never store a REAL-channel-keyed draft with base:null
              // (temp-channel semantics). Keep the draft, mark it
              // conflicted — the editor tells the owner to reload the
              // channel before editing further.
              draftsRef.current.put(finalId, remapped,
                { conflict: "definition_unavailable", pending: (entry && entry.pending) || null });
            }
          }
          draftsRef.current.drop(op.tempId);
          if (selectedIdRef.current === op.tempId) setSelectedId(finalId);
        }
        // Groups created in the packet: drafts that referenced them as
        // pending lose the sentinel — the group now exists server-side.
        for (const op of packetOps) {
          if (op.op !== "group.put" || !op.group || !op.group.id) continue;
          for (const id of draftsRef.current.ids()) {
            const entry = draftsRef.current.get(id);
            const d = entry && entry.draft;
            if (d && d.pendingGroupName != null && d.groupId === op.group.id) {
              const next = clone(d);
              delete next.pendingGroupName;
              draftsRef.current.put(id, next, { base: entry.base !== undefined ? entry.base : null });
            }
          }
        }
        // Field-level rebase for bystander content drafts (never a
        // wholesale channel.put) — against the SUBMITTED before/after map.
        if (snap.beforeAfter) rebaseDraftsAfterArrangement(draftsRef.current, snap.beforeAfter);
      }
      if (org) {
        org.clearPending();
        // Clear the staging ONLY if it is the one that was submitted; a
        // newer staging stays dirty and the bar flags it stale (its
        // baseRevision no longer matches the new library revision) so the
        // owner re-plans it explicitly.
        const stagedNow = org.get().staged;
        if (stagedNow && stagedNow.correlationToken === snap.correlationToken) org.clearStagedOnly();
      }
      await refreshLibrary();
      announce("Arrangement applied at r" + receipt.revision + ".");
      studioEmit("applied", { scope: "arrangement", revision: receipt.revision });
      // Post-commit Stage reversal: inverse map + FRESH validation + a NEW
      // Apply — never a blind replay, created rows never auto-deleted.
      const stagedSnapshot = snap;
      toast("Arrangement applied at r" + receipt.revision + ".", "ok", {
        action: {
          label: "Stage reversal",
          onClick: () => { void stageReversal(stagedSnapshot); },
        },
      });
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [refreshLibrary, toast, announce]);

    const stageReversal = useCallback(async (staged) => {
      const cur = libRef.current;
      if (!cur || !orgRef.current) return;
      const rev = buildReversal(staged, cur.channels || []);
      if (!rev.ops.length) {
        toast("Nothing to reverse — every touched row has moved since (created channels and groups are kept; remove them from their editors if unwanted).", "");
        return;
      }
      // Fresh validation against the CURRENT revision before staging the
      // reversal — later edits may collide; the server decides.
      try {
        const check = await runOp("ValidateChannelChanges", { ops: JSON.stringify(rev.ops) });
        if (check && check.valid === false && (check.errors || []).length) {
          toast("The reversal no longer applies cleanly: " + check.errors[0].message, "err");
          return;
        }
      } catch (e) { /* pre-check unavailable — Apply validates at commit */ }
      const skipped = rev.skippedMoved.length
        ? " · " + rev.skippedMoved.length + " row" + (rev.skippedMoved.length === 1 ? "" : "s") + " moved since and stay as they are"
        : "";
      const created = rev.createdCount
        ? " · " + rev.createdCount + " created channel" + (rev.createdCount === 1 ? "" : "s") + " kept (never auto-deleted)"
        : "";
      // Synthesize review rows from the inverse map so the bar's Review
      // shows the reversal's full before/after, like any other plan.
      const reviewChanges = [];
      const reviewGroups = [];
      for (const ref of Object.keys(rev.beforeAfter)) {
        const ba = rev.beforeAfter[ref];
        if (ba.number) {
          reviewChanges.push({ channelId: ref, tempRef: null, from: ba.number[0], to: ba.number[1], selected: true, reason: "relocate" });
        }
        if (ba.group) reviewGroups.push({ channelId: ref, tempRef: null, from: ba.group[0], to: ba.group[1] });
      }
      orgRef.current.stage({
        baseRevision: cur.revision,
        correlationToken: newCorrelationToken(),
        label: "Reversal of: " + (staged.label || "arrangement") + skipped + created,
        intent: { type: "reversal" },
        response: {
          revision: cur.revision, numberChanges: reviewChanges, groupChanges: reviewGroups,
          created: [], displaced: [], warnings: [], errors: [], valid: true, noop: false,
        },
        packet: { expectedRevision: cur.revision, ops: rev.ops, requestId: newRequestId() },
        beforeAfter: rev.beforeAfter,
        tempRefs: {},
        stagedAt: Date.now(),
      });
      announce("Reversal staged — review and Apply arrangement.");
    }, [toast, announce]);

    const submitArrangement = useCallback(async () => {
      const org = orgRef.current;
      const staged = org ? org.get().staged : null;
      if (!org || !staged || !staged.packet) return;
      // R9: a packet reviewed at an older revision is EXPLICITLY stale —
      // never silently re-based onto the current revision. F4: read the
      // live slot, not render state.
      const liveRev = liveRevisionRef.current != null ? liveRevisionRef.current
        : (libRef.current ? libRef.current.revision : null);
      if (liveRev != null && staged.baseRevision != null && liveRev !== staged.baseRevision) {
        toast("This plan was reviewed at r" + staged.baseRevision + " and the library is now r" + liveRev
          + " — Reload and replan to review it against the current revision before Applying.", "err");
        return;
      }
      const ops = clone(staged.packet.ops);
      const expected = staged.packet.expectedRevision;
      const opsDigest = JSON.stringify(ops);
      // Exact frozen retry: same ops/revision/requestId, byte-identical.
      const priorPending = org.get().pending;
      const requestId = priorPending && priorPending.opsDigest === opsDigest && priorPending.expected === expected
        ? priorPending.requestId
        : (staged.packet.requestId || newRequestId());
      setArrBusy(true);
      try {
        org.setPending({
          requestId, expected, opsDigest,
          correlationToken: staged.correlationToken,
          packet: { expectedRevision: expected, ops, requestId },
          beforeAfter: staged.beforeAfter || {},
          tempRefs: staged.tempRefs || {},
          label: staged.label || "",
          state: "submitting",
          submittedAt: priorPending && priorPending.opsDigest === opsDigest ? priorPending.submittedAt : Date.now(),
        });
        announce("Applying arrangement…");
        const receipt = await submitOps(ops, {
          label: staged.label || "Arrangement",
          scope: "arrangement",
          expectedOverride: expected,
          requestIdOverride: requestId,
          onSubmitted: () => org.setPendingState("polling"),
        });
        // F1: an exhausted poll budget (status "unknown") is an UNKNOWN
        // OUTCOME, exactly like a transport failure — keep the full
        // snapshot (packet/requestId), offer Check result / Retry, never
        // clearPending, never label it Rejected.
        if (!receipt || receipt.status === "transport" || receipt.status === "unknown") {
          // R6: transport failure — KEEP the exact packet + requestId and
          // surface an actionable unknown outcome. The button must never
          // stay stuck on "Applying…" and never require discarding.
          org.setPendingState("outcome_unknown");
          toast("The arrangement Apply may or may not have committed — its outcome is unknown. "
            + "Use “Check result” or “Retry same Apply” (a byte-identical retry can never commit twice).", "err");
          return;
        }
        if (receipt.status === "committed") {
          // Reconcile ONLY the submitted snapshot — even if the owner
          // staged a newer arrangement while this one was in flight.
          await finishArrangementCommit(receipt, org.get().pending);
        } else if (receipt.error === "revision_conflict") {
          org.clearPending();
          await refreshLibrary();
          // The staged plan survives, flagged stale — re-plan against the
          // new revision; never blindly replay the old packet.
          toast("Revision conflict — the staged arrangement is intact. Reload and replan to re-preview against r"
            + (receipt.currentRevision != null ? receipt.currentRevision : "the current revision") + ".", "err");
        } else {
          org.clearPending();
          toast("Rejected: " + (receipt.message || receipt.error || "unknown error")
            + " — the staged arrangement is intact; review it or edit and re-stage.", "err");
        }
      } finally {
        setArrBusy(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [submitOps, finishArrangementCommit, refreshLibrary, toast, announce]);

    // R6 recovery: one honest receipt poll for a kept unknown-outcome
    // request. Committed → reconcile; rejected → report and release.
    const checkArrangementResult = useCallback(async () => {
      const org = orgRef.current;
      const pending = org ? org.get().pending : null;
      if (!pending) return;
      let receipt = null;
      try {
        receipt = await runOp("GetChannelApplyResult", { requestId: pending.requestId });
      } catch (e) {
        toast("Could not reach the server — try Check result again.", "err");
        return;
      }
      if (!receipt || receipt.status === "unknown") {
        toast("Still no result for this request — the server never saw it or it is still running. "
          + "“Retry same Apply” is safe: the identical request cannot commit twice.", "");
        return;
      }
      if (receipt.status === "committed") {
        await finishArrangementCommit(receipt, pending);
      } else {
        if (org) org.clearPending();
        await refreshLibrary();
        if (receipt.error === "revision_conflict") {
          toast("The Apply hit a revision conflict — nothing changed. The staged plan is intact; Reload and replan.", "err");
        } else {
          toast("The Apply was rejected: " + (receipt.message || receipt.error || "unknown error")
            + " — the staged arrangement is intact.", "err");
        }
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [finishArrangementCommit, refreshLibrary, toast]);

    // R6 recovery: resubmit the exact kept packet under the exact kept
    // requestId (the server's idempotent replay — exactly one commit).
    const retryArrangement = useCallback(async () => {
      const org = orgRef.current;
      const pending = org ? org.get().pending : null;
      if (!pending) return;
      let ops = null;
      try { ops = JSON.parse(pending.opsDigest); } catch (e) { ops = null; }
      if (!Array.isArray(ops) || !ops.length) {
        toast("The kept packet could not be restored — stage the plan again.", "err");
        if (org) org.clearPending();
        return;
      }
      setArrBusy(true);
      try {
        org.setPending(Object.assign({}, pending, { state: "submitting" }));
        const receipt = await submitOps(ops, {
          label: pending.label || "Arrangement",
          scope: "arrangement",
          expectedOverride: pending.expected,
          requestIdOverride: pending.requestId,
          onSubmitted: () => org.setPendingState("polling"),
        });
        // F1: "unknown" after the poll budget is an unknown outcome, never
        // a rejection — keep the snapshot, keep Check result / Retry alive.
        if (!receipt || receipt.status === "transport" || receipt.status === "unknown") {
          org.setPendingState("outcome_unknown");
          toast("Still unknown — the same request id is kept; try Check result again in a moment.", "err");
          return;
        }
        if (receipt.status === "committed") {
          await finishArrangementCommit(receipt, org.get().pending);
        } else if (receipt.error === "revision_conflict") {
          org.clearPending();
          await refreshLibrary();
          toast("The retry hit a revision conflict — the staged plan is intact. Reload and replan.", "err");
        } else {
          org.clearPending();
          toast("The retry was rejected: " + (receipt.message || receipt.error || "unknown error")
            + " — the staged arrangement is intact.", "err");
        }
      } finally {
        setArrBusy(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [submitOps, finishArrangementCommit, refreshLibrary, toast]);

    function countAffected(ops) {
      let n = 0;
      for (const op of ops) {
        if (op.op === "channels.move" || op.op === "channels.patch") n += (op.channelIds || []).length;
        else if (op.op === "channel.put" || op.op === "channel.create" || op.op === "channel.swap") n += 1;
      }
      return n;
    }

    // ---- editor callbacks ----
    const handleApplied = useCallback(async (receipt, info) => {
      await refreshLibrary();
      if (info && info.isTemp && receipt.idMap && receipt.idMap[info.channelId]) {
        const finalId = receipt.idMap[info.channelId];
        draftsRef.current.drop(info.channelId);
        // Follow the new id only if the user is still on the temp channel —
        // async Apply means the receipt can land while they edit elsewhere.
        const stillThere = selectedIdRef.current === info.channelId;
        setSelectedId((cur) => (cur === info.channelId ? finalId : cur));
        if (stillThere) toast("Channel created as " + finalId + " at r" + receipt.revision + ".", "ok");
      }
    }, [refreshLibrary, toast]);

    const handleCreated = useCallback((tempId, discardedId) => {
      if (tempId) {
        setSelectedId(tempId);
        // Create at an occupied number: the draft keeps the intended number
        // and the resolution sheet opens right away (never an auto-swap).
        if (discardedId && discardedId.resolveNumber && orgRef.current) {
          setNumRes({
            subject: { ref: tempId, isNew: true },
            target: discardedId.resolveNumber,
          });
        }
      } else if (discardedId) {
        // temp draft discarded: select the first real channel
        const first = (libRef.current && libRef.current.channels || [])[0];
        setSelectedId(first ? first.id : null);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // UX4: "New channel before/after this" — stage a draft at the target
    // and open the placement sheet (the existing conflict resolver). The
    // sheet refuses to Stage until the new channel's rules are complete.
    const insertChannelNear = useCallback((ch, dir) => {
      if (!draftsRef.current) return;
      const kind = ch.kind || "net";
      const band = BANDS[kind] || BANDS.net;
      let target = dir === "before" ? ch.number : (Number.isInteger(ch.number) ? ch.number + 1 : null);
      if (!Number.isInteger(target) || target < band[0] || target > band[1]) {
        target = nextFreeNumber(libRef.current ? libRef.current.channels || [] : [], kind);
      }
      if (target == null) { toast("No free number left in " + band[0] + "–" + band[1] + ".", "err"); return; }
      const tempId = newTempId();
      draftsRef.current.put(tempId, {
        kind, number: target, name: "", glyph: null,
        color: pickDefaultColor(target), groupId: ch.groupId, sort: "shuffle",
        enabled: true, archived: false, paused: false,
        source: { type: "criteria" }, sourceLabel: "", programming: null,
      });
      setSelectedId(tempId);
      toast("New draft at " + target + " — finish its rules, then place it with this sheet.", "");
      if (arrangementAvailable) {
        setNumRes({ subject: { ref: tempId, isNew: true, kind }, target });
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [arrangementAvailable, toast]);

    // ---- organization glue ----

    const stageArrangement = useCallback((staged, subjectFinalNumber) => {
      if (!orgRef.current) return;
      // F3: a new Stage must NEVER silently overwrite an unresolved
      // submitted packet. The owner resolves it explicitly — Check result
      // (committed/rejected), Retry same Apply, or Forget the kept request.
      const pendingNow = orgRef.current.get().pending;
      if (pendingNow) {
        toast("An arrangement Apply is still unresolved — use “Check result” or “Retry same Apply” on the bar, "
          + "or “Forget kept request”, before staging a new plan.", "err");
        return;
      }
      // UX7: say so when a new Stage replaces an earlier unsubmitted plan
      // (the store pushes the old one onto the undo stack).
      const prev = orgRef.current.get().staged;
      const replaced = !!(prev && prev.correlationToken !== staged.correlationToken);
      orgRef.current.stage(staged);
      studioEmit("staged", { label: staged.label, baseRevision: staged.baseRevision });
      announce("Arrangement staged: " + (staged.label || "review and Apply when ready.") + " Nothing is written until Apply arrangement.");
      if (replaced) {
        toast("Staged “" + (staged.label || "new plan") + "” — it replaces the earlier unstaged plan. Undo brings the old one back.", "");
      }
      // A number-resolution staged for a draft channel: the subject's final
      // number lands in its draft so a later channel Apply agrees with the
      // committed renumber map.
      if (subjectFinalNumber != null && staged.intent && staged.intent.subject && draftsRef.current) {
        const ref = staged.intent.subject;
        const entry = draftsRef.current.get(ref);
        if (entry && entry.draft && entry.draft.number !== subjectFinalNumber) {
          draftsRef.current.put(ref, Object.assign({}, entry.draft, { number: subjectFinalNumber }));
        }
      }
      setNumRes(null);
      setOrganize(null);
    }, [announce, toast]);

    // F3: the explicit "give up on the kept request" action — the only way
    // an unresolved submission stops blocking a new Stage.
    const forgetPendingSubmission = useCallback(async () => {
      const org = orgRef.current;
      const pending = org ? org.get().pending : null;
      if (!pending) return;
      const ok = await confirm({
        title: "Forget the kept request?",
        message: "The last arrangement Apply's outcome is unknown; its packet and request id are kept so it can be "
          + "checked or retried safely. Forgetting means future Applies use a NEW request id — if the kept request "
          + "actually committed, its changes stay committed and you lose the correlation.",
        confirmLabel: "Forget kept request", danger: true,
      });
      if (!ok) return;
      if (org) org.clearPending();
      const libraryId = libRef.current ? (libRef.current.libraryId || "default") : "default";
      clearOpsPending(libraryId);
      toast("The kept request was forgotten — Apply again only if you are sure the earlier one did not commit.", "err");
    }, [confirm, toast]);

    const openResolution = useCallback((subject, target) => {
      setNumRes({ subject, target });
    }, []);

    const discardStaged = useCallback(async () => {
      const org = orgRef.current;
      if (!org || !org.get().staged) return;
      const ok = await confirm({
        title: "Discard the staged arrangement?",
        message: "The staged number/group changes are thrown away. Channel drafts are unaffected. (Undo can bring it back until you stage something else.)",
        confirmLabel: "Discard", danger: true,
      });
      if (ok) { org.discardStaged(); announce("Staged arrangement discarded."); }
    }, [confirm, announce]);

    const replanStaged = useCallback(async () => {
      const org = orgRef.current;
      const staged = org ? org.get().staged : null;
      if (!staged) return;
      await refreshLibrary();
      // Re-run the SAME intent against the fresh revision — the new
      // displacement review shows before any re-Apply.
      if (staged.intent && staged.intent.type === "reversal") {
        // R8: a stale reversal is rebuilt and re-validated FRESH against
        // the current library — never reused as-is.
        void stageReversal(staged);
        return;
      }
      if (staged.intent && staged.intent.type === "number-resolution") {
        setNumRes({
          subject: { ref: staged.intent.subject, isNew: String(staged.intent.subject).startsWith("temp-") },
          target: staged.intent.number,
        });
        return;
      }
      setOrganize({ intent: staged.intent || null });
    }, [refreshLibrary, toast]);

    const selectChannel = useCallback((id) => {
      if (id === selectedId) return;
      // Navigation during an apply is free: the receipt poll outlives the
      // editor (module-level registry + durable pending draft), so switching
      // channels never interrupts an Apply — a top-bar pill keeps it visible.
      setSelectedId(id);
    }, [selectedId]);

    // ---- dial data ----
    const applyQuery = useMemo(() => debounce((v) => setQuery(v), 140), []);
    const filtering = !!(query.trim() || groupFilter);
    const clearFilters = useCallback(() => {
      if (searchInputRef.current) searchInputRef.current.value = "";
      applyQuery.cancel();
      setQuery("");
      setGroupFilter("");
    }, [applyQuery]);
    // Groups staged inline ("Create group…" sentinel in a draft) show on the
    // dial before they exist server-side — the Apply packet creates them.
    const pendingGroups = useMemo(() => {
      if (!draftsRef.current) return [];
      // F5: ONE implementation (pendingGroupsFromDrafts) — the dial's chrome
      // (position/staged) is applied on top of the shared extraction.
      return pendingGroupsFromDrafts(draftsRef.current, lib ? lib.groups : [])
        .map((g) => ({ id: g.id, name: g.name || "(new group)", position: 9000, staged: true }));
      // recompute on draft count changes
    }, [draftCount, lib]);

    const groupsSorted = useMemo(() => {
      const base = lib ? (lib.groups || []).slice().sort((a, b) => a.position - b.position) : [];
      return base.concat(pendingGroups);
    }, [lib, pendingGroups]);
    const groupsById = useMemo(() => new Map(groupsSorted.map((g) => [g.id, g])), [groupsSorted]);

    // The staged arrangement overlays number/group on the dial preview —
    // selection and reveal track ID, never number (a renumber never loses
    // the selected row), and the WindowedList resetKey does NOT change.
    const orgState = orgRef.current ? orgRef.current.get() : { staged: null, pending: null, undo: [], redo: [] };
    void orgTick; // re-renders on org store changes
    const orgOverlay = useMemo(() => orgOverlayOf(orgState.staged), [orgState.staged]);

    const visibleChannels = useMemo(() => {
      if (!lib) return [];
      const q = query.trim().toLowerCase();
      let list = (lib.channels || []);
      if (groupFilter) list = list.filter((c) => c.groupId === groupFilter);
      if (q) {
        list = list.filter((c) => c.name.toLowerCase().includes(q) || String(c.number).includes(q)
          || (c.sourceLabel || "").toLowerCase().includes(q));
      }
      return list.slice().sort((a, b) => a.number - b.number);
    }, [lib, query, groupFilter]);

    // temp (draft) channels appear too
    const tempChannels = useMemo(() => {
      if (!draftsRef.current) return [];
      return draftsRef.current.ids()
        .filter((id) => String(id).startsWith("temp-"))
        .map((id) => {
          const entry = draftsRef.current.get(id);
          const d = entry.draft;
          return {
            id, temp: true, kind: d.kind, number: d.number, name: d.name || "(unnamed draft)",
            glyph: d.glyph, color: d.color, groupId: d.groupId, sort: d.sort,
            enabled: true, archived: false, paused: false,
            sourceType: d.source && d.source.type, sourceLabel: d.sourceLabel || "",
          };
        });
      // recompute on draft count changes
    }, [draftCount, lib]);

    const allRowsBase = useMemo(() => {
      const seen = new Set();
      const rows = [];
      for (const c of visibleChannels) { rows.push(c); seen.add(c.id); }
      for (const t of tempChannels) {
        if (!seen.has(t.id) && (!groupFilter || t.groupId === groupFilter)) {
          const q = query.trim().toLowerCase();
          if (!q || t.name.toLowerCase().includes(q) || String(t.number).includes(q)) rows.push(t);
        }
      }
      return rows;
    }, [visibleChannels, tempChannels, groupFilter, query]);

    const allRows = useMemo(() => {
      if (!orgOverlay) return allRowsBase;
      const rows = allRowsBase.map((c) => {
        const num = orgOverlay.numbers.get(c.id);
        const grp = orgOverlay.groups.get(c.id);
        if (num == null && grp == null) return c;
        return Object.assign({}, c, {
          number: num != null ? num : c.number,
          groupId: grp != null ? grp : c.groupId,
          staged: true,
        });
      });
      return rows.slice().sort((a, b) => (a.number != null ? a.number : Infinity) - (b.number != null ? b.number : Infinity));
    }, [allRowsBase, orgOverlay]);

    // The FULL filtered collection as an ordered id list — the selection
    // model's range basis (virtualization is irrelevant to selection math).
    const filteredIds = useMemo(() => allRows.map((c) => c.id), [allRows]);
    const filteredIdsRef = useRef(filteredIds);
    filteredIdsRef.current = filteredIds;

    const dialItems = useMemo(() => buildDialItems({
      channels: allRows, groups: groupsSorted, query, groupFilter, collapsed,
    }), [allRows, groupsSorted, query, groupFilter, collapsed]);

    const hasDraft = (id) => !!(draftsRef.current && draftsRef.current.get(id));

    // ---- selection model (Set<channelId> + anchor over the filtered id
    // list — ID-keyed, never DOM nodes or dial row indexes) ----
    const toggleSelect = useCallback((id, checked, shiftKey) => {
      const ids = filteredIdsRef.current;
      setBulkSelected((cur) => {
        const next = new Set(cur);
        if (shiftKey && bulkAnchor && ids.includes(bulkAnchor) && ids.includes(id)) {
          const a = ids.indexOf(bulkAnchor);
          const b = ids.indexOf(id);
          const lo = Math.min(a, b);
          const hi = Math.max(a, b);
          for (const rid of ids.slice(lo, hi + 1)) {
            if (checked) next.add(rid);
            else next.delete(rid);
          }
        } else if (checked) next.add(id);
        else next.delete(id);
        return next;
      });
      if (!shiftKey) setBulkAnchor(id);
    }, [bulkAnchor]);

    const selectAllMatching = useCallback(() => {
      // Temp rows are excluded — new drafts have no number on the server
      // yet; Apply them first. (A temp row can still be a range anchor.)
      const temps = allRows.filter((c) => c.temp).length;
      setBulkSelected(new Set(allRows.filter((c) => !c.temp).map((c) => c.id)));
      if (temps) toast(temps + " new draft" + (temps === 1 ? "" : "s") + " left out — Apply them first.", "");
    }, [allRows, toast]);

    // Hidden selections are never silently dropped by filtering: they stay
    // in the count with an explicit Show / Clear hidden.
    const hiddenSelected = useMemo(() => {
      const shown = new Set(filteredIds);
      return [...bulkSelected].filter((id) => !shown.has(id));
    }, [bulkSelected, filteredIds]);

    // Group-header actions: Select group / Arrange numbers… / Edit group….
    const groupHeaderAction = useCallback((g, action) => {
      if (action === "select") {
        setBulkMode(true);
        const memberIds = allRows.filter((c) => c.groupId === g.id && !c.temp).map((c) => c.id);
        setBulkSelected((cur) => new Set([...cur, ...memberIds]));
        if (memberIds.length) setBulkAnchor(memberIds[0]);
      } else if (action === "arrange") {
        setOrganize({ scope: { type: "group", groupId: g.id } });
      } else if (action === "edit") {
        setDialog("groups");
      }
    }, [allRows]);

    // Keyboard: Ctrl/Cmd+Z undo / Ctrl+Shift+Z redo while the staged bar
    // holds focus or the organize sheet is open; Ctrl/Cmd+A selects all
    // matching from the dial; Escape exits Select mode (drafts unaffected).
    useEffect(() => {
      const onKey = (e) => {
        const tag = document.activeElement && document.activeElement.tagName;
        const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
        if ((e.ctrlKey || e.metaKey) && !typing && (e.key === "z" || e.key === "Z")) {
          const inBar = document.activeElement && document.activeElement.closest
            && document.activeElement.closest(".jw-arrange-bar");
          if (!organize && !inBar) return;
          if (!orgRef.current) return;
          e.preventDefault();
          if (e.shiftKey) orgRef.current.redo();
          else orgRef.current.undo();
          return;
        }
        // Ctrl/Cmd+A while the dial has focus: select all matching channels.
        if ((e.ctrlKey || e.metaKey) && !typing && (e.key === "a" || e.key === "A") && bulkMode) {
          const inDial = document.activeElement && document.activeElement.closest
            && document.activeElement.closest(".jw-dial");
          if (!inDial) return;
          e.preventDefault();
          selectAllMatching();
          return;
        }
        // Escape exits Select mode — drafts and staged arrangements are
        // untouched; dialogs consume their own Escape first.
        if (e.key === "Escape" && bulkMode && !typing) {
          if (typeof document.querySelector === "function" && document.querySelector(".jw-overlay")) return;
          setBulkMode(false);
          setBulkSelected(new Set());
          setBulkAnchor(null);
        }
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [organize, bulkMode, selectAllMatching]);

    // Selection announcements + the extension hook's "selection" event.
    useEffect(() => {
      if (!bulkMode) return;
      studioEmit("selection", { count: bulkSelected.size });
    }, [bulkSelected, bulkMode]);

    // Extension hook context (extras/): view notifications + announce only.
    // No mutation surface exists — snippets can never bypass Apply.
    useEffect(() => {
      studioCtxRef.current = {
        route: ROUTE_PATH,
        on(event, cb) {
          const set = studioExt.listeners[event];
          if (!set) return () => {};
          set.add(cb);
          return () => set.delete(cb);
        },
        getViewState() {
          const l = libRef.current;
          const org = orgRef.current ? orgRef.current.get() : null;
          return {
            route: ROUTE_PATH,
            revision: l ? l.revision : null,
            channelCount: l ? (l.channels || []).length : 0,
            selection: bulkSelected.size,
            stagedArrangement: !!(org && org.staged),
            arrangementAvailable: !!(features && features.arrangement),
          };
        },
        announce,
      };
      return () => {
        studioCtxRef.current = null;
        studioRunCleanups(); // route leave: every extension cleans up
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [features, bulkSelected]);

    // ---- bulk actions ----
    const bulkIds = [...bulkSelected];
    const bulkAction = async (makeOps, title, line, skipConfirm) => {
      if (!bulkIds.length) return;
      if (!skipConfirm) {
        const ok = await confirm({
          title,
          message: bulkIds.length + (bulkIds.length === 1 ? " channel" : " channels") + " " + line,
          confirmLabel: "Apply",
        });
        if (!ok) return;
      }
      const ops = makeOps(bulkIds);
      const receipt = await submitOps(ops, { label: title });
      if (receipt.status === "committed") {
        // writes land in stored drafts of moved channels too (they stay draft)
        if (ops[0] && ops[0].op === "channels.move" && draftsRef.current) {
          const target = ops[0].groupId;
          for (const id of ops[0].channelIds) {
            const entry = draftsRef.current.get(id);
            if (entry) draftsRef.current.put(id, Object.assign({}, entry.draft, { groupId: target }));
          }
        }
        setBulkSelected(new Set());
      }
    };

    const moveDialog = async () => {
      if (!bulkIds.length) return;
      let target = groupsSorted[0] ? groupsSorted[0].id : null;
      let createName = "";
      const ok = await new Promise((resolve) => {
        setConfirmSpec({
          title: "Move " + bulkIds.length + (bulkIds.length === 1 ? " channel" : " channels"),
          message: bulkIds.length + (bulkIds.length === 1 ? " channel moves" : " channels move") + " in one Apply.",
          confirmLabel: "Apply move",
          resolve: (v) => { setConfirmSpec(null); resolve(v); },
          detail: h("div", null,
            h("div", { className: "jw-field" },
              h("label", { className: "jw-field-label", htmlFor: "jw-bulk-dest" }, "Move to existing group"),
              h("select", {
                id: "jw-bulk-dest", className: "jw-input", defaultValue: target,
                onChange: (e) => { target = e.target.value; },
              }, groupsSorted.map((g) => h("option", { key: g.id, value: g.id }, g.name))),
            ),
            h("div", { className: "jw-field" },
              h("label", { className: "jw-field-label", htmlFor: "jw-bulk-newgroup" }, "…or create a new group and move there"),
              h("input", {
                id: "jw-bulk-newgroup", className: "jw-input", placeholder: "New group name",
                onChange: (e) => { createName = e.target.value.trim(); },
              }),
            ),
          ),
        });
      });
      if (!ok) return;
      // ONE Apply: the server validates channels.move against a group created
      // EARLIER IN THE SAME TRANSACTION, so create+move commits atomically —
      // a failure leaves no half-moved group behind (audit C9).
      let ops;
      if (createName) {
        const gid = newGroupId();
        const maxPos = Math.max(0, ...groupsSorted.map((g) => g.position));
        ops = [
          { op: "group.put", group: { id: gid, name: createName, position: maxPos + 1 } },
          { op: "channels.move", channelIds: bulkIds.slice(), groupId: gid },
        ];
        target = gid;
      } else {
        ops = [{ op: "channels.move", channelIds: bulkIds.slice(), groupId: target }];
      }
      const receipt = await submitOps(ops, { label: "Move to group" });
      if (receipt.status === "committed") {
        if (draftsRef.current) {
          for (const id of ops[0].op === "channels.move" ? ops[0].channelIds : ops[1].channelIds) {
            const entry = draftsRef.current.get(id);
            if (entry) draftsRef.current.put(id, Object.assign({}, entry.draft, { groupId: target }));
          }
        }
        setBulkSelected(new Set());
        if (arrangementAvailable) {
          toast("Moved. Arrange the group's numbers next if you like.", "", {
            action: {
              label: "Arrange numbers…",
              onClick: () => setOrganize({ scope: { type: "group", groupId: target } }),
            },
          });
        }
      }
    };

    // ---- export CSV ----
    const exportCsv = () => {
      const rows = [["number", "id", "kind", "name", "group", "sourceType", "sourceLabel", "sort", "programmingMode", "enabled", "paused", "archived"]];
      for (const c of allRows) {
        const g = groupsById.get(c.groupId);
        rows.push([
          c.number, c.id, c.kind || "", c.name, g ? g.name : "",
          c.sourceType || "", c.sourceLabel || "", c.sort || "",
          c.programmingMode || "", c.enabled ? "true" : "false",
          c.paused ? "true" : "false", c.archived ? "true" : "false",
        ]);
      }
      const esc = (v) => {
        const s = String(v == null ? "" : v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      };
      const csv = rows.map((r) => r.map(esc).join(",")).join("\r\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "channel-library-r" + (lib ? lib.revision : 0) + ".csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    };

    // ---- render ----

    if (bootError) {
      return h("div", { className: "jw-page jw-studio" },
        h("h1", { className: "jw-title" }, "Channel Studio"),
        h("div", { className: "jw-missing-note", role: "alert" },
          "The channel library is not available on this deployment: " + bootError),
      );
    }

    if (!lib) {
      return h("div", { className: "jw-page jw-studio" },
        h("header", { className: "jw-topbar" },
          h("div", { className: "jw-topbar-title" },
            h("h1", { className: "jw-title" }, "Channel Studio"),
            h("span", { className: "jw-tagline" }, "Your library, on the air — edits apply when you say so."))),
        h("main", { className: "jw-columns", role: "status", "aria-label": "Loading channel studio" },
          h("section", { className: "jw-dial", "aria-hidden": "true" },
            Array.from({ length: 12 }, (_, i) => h("div", {
              key: i, className: "jw-skel jw-skel-dial-row" + (i % 3 === 2 ? " short" : ""),
            }))),
          h("div", { className: "jw-editor", "aria-hidden": "true" },
            h("div", { className: "jw-editor-scroll" },
              skelCard(3), skelCard(5), skelCard(4)))));
    }

    const dirtyPill = draftCount > 0
      ? h("span", { className: "jw-pill jw-pill-dirty", title: "Drafts persist for this browser session until applied or discarded" },
          draftCount + " unsaved draft" + (draftCount === 1 ? "" : "s"))
      : h("span", { className: "jw-pill jw-pill-clean" }, "no drafts");

    const applyingPill = inflights.length
      ? h("span", {
          className: "jw-pill jw-pill-applying", role: "status",
          title: "Applies run in the background — their receipts land even if you switch channels or reload",
        }, "Applying " + inflights.map((i) => "“" + i.name + "”").join(", ") + "…")
      : null;

    const reload = async () => {
      // Safe even mid-apply: an in-flight receipt is durable server-side and
      // the pending requestId is re-polled when the editor remounts.
      const data = await refreshLibrary();
      if (!data) return;
      const keptDraft = selectedId && draftsRef.current ? !!draftsRef.current.get(selectedId) : false;
      if (keptDraft) {
        toast("Library reloaded (r" + data.revision + "). Your draft was kept.", "");
      } else {
        setEditorKeyBump((x) => x + 1); // remount a clean editor on fresh data
        toast("Library reloaded at r" + data.revision + ".", "ok");
      }
      setReloadTick((x) => x + 1);
    };

    const dialList = h(WindowedList, {
      items: dialItems,
      itemHeight: DIAL_ITEM_HEIGHT,
      resetKey: query + "|" + groupFilter + "|" + reloadTick,
      ariaLabel: "Channel dial",
      className: "jw-dial-list",
      render: (item) => {
        if (item.type === "header") {
          const isCollapsed = collapsed.has(item.g.id) && !query && !groupFilter;
          return h("div", {
            key: "h-" + item.g.id,
            className: "jw-group-header", role: "button", tabIndex: 0,
            "aria-expanded": isCollapsed ? "false" : "true",
            onClick: () => setCollapsed((cur) => {
              const next = new Set(cur);
              if (next.has(item.g.id)) next.delete(item.g.id);
              else next.add(item.g.id);
              return next;
            }),
            onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.currentTarget.click(); } },
          },
            h("span", null, item.g.name),
            h("span", { className: "jw-group-count", title: item.spanTitle }, String(item.count)),
            item.spanText
              ? h("span", { className: "jw-group-span", title: item.spanTitle }, item.spanText)
              : null,
            h(GroupHeaderMenu, {
              group: item.g, arrangeAvailable: arrangementAvailable,
              onAction: (action) => groupHeaderAction(item.g, action),
            }),
          );
        }
        const ch = item.c;
        return h(DialRow, {
          key: ch.id, ch,
          selected: ch.id === selectedId,
          bulkMode,
          checked: bulkSelected.has(ch.id),
          hasDraft: hasDraft(ch.id),
          groupsById,
          onSelect: (id) => { void selectChannel(id); },
          onCheck: toggleSelect,
          onInsert: insertChannelNear,
        });
      },
    });

    const selected = selectedId || "";
    const editor = selected
      ? h(EditorPane, {
          key: selected + ":" + editorKeyBump,
          channelId: selected,
          lib,
          getRevision: () => (liveRevisionRef.current != null ? liveRevisionRef.current
            : (libRef.current ? libRef.current.revision : 0)),
          drafts: draftsRef.current,
          confirm,
          toast,
          arrangement: {
            available: arrangementAvailable,
            staged: orgState.staged,
            openResolution,
          },
          submitOps,
          submitApply: submitChannelApply,
          onApplied: handleApplied,
          onCreated: handleCreated,
          libraryVersion,
        })
      : null;

    return h("div", { className: "jw-page jw-studio" },
      h("header", { className: "jw-topbar" },
        h("div", { className: "jw-topbar-title" },
          h("h1", { className: "jw-title" }, "Channel Studio"),
          h("span", { className: "jw-tagline" }, "Your library, on the air — edits apply when you say so.")),
        h("div", { className: "jw-searchbox" },
          h("span", { className: "jw-search-icon", "aria-hidden": "true" }, "⌕"),
          h("input", {
            ref: searchInputRef, type: "search", className: "jw-input jw-search-input",
            placeholder: "Search " + ((lib.channels || []).length + tempChannels.length) + " channels… (press /)",
            "aria-label": "Search channels",
            onChange: (e) => applyQuery(e.target.value),
          }),
        ),
        h("select", {
          className: "jw-input jw-group-filter", "aria-label": "Filter by group",
          value: groupFilter, onChange: (e) => setGroupFilter(e.target.value),
        },
          h("option", { value: "" }, "All groups"),
          groupsSorted.map((g) => h("option", { key: g.id, value: g.id }, g.name))),
        h("div", { className: "jw-topbar-side" },
          // Wide chrome: every organization action is a plain labeled
          // button — never hidden behind an unlabeled ellipsis again.
          // UX8: exactly ONE New-channel action per context — the dial
          // action on desktop (below), the topbar action only when the
          // rail goes compact (CSS shows .jw-topbar-new under 761px).
          h("span", { className: "jw-topbar-wide-actions" },
            h("button", {
              className: "jw-btn" + (bulkMode ? " jw-btn-primary" : ""),
              "aria-pressed": bulkMode ? "true" : "false",
              onClick: () => { setBulkMode((v) => !v); setBulkSelected(new Set()); setBulkAnchor(null); },
            }, bulkMode ? "Done selecting" : "Select channels"),
            arrangementAvailable
              ? h("button", {
                  className: "jw-btn",
                  onClick: () => setOrganize({ scope: bulkSelected.size ? { type: "selection" } : null }),
                }, "Organize channels…")
              : h("span", {
                  className: "jw-pill jw-pill-clean jw-arrange-gate",
                  title: "Arrangement tools need a plugin update — basic editing (numbers, groups, rules) still works.",
                }, "Arrangement needs a plugin update"),
            h("button", { className: "jw-btn", onClick: () => setDialog("groups") }, "Groups…"),
          ),
          // Narrow chrome (CSS shows these under 761px): creation stays
          // visible; organization actions live in ONE labeled menu — never
          // an unlabeled ellipsis.
          h("button", {
            className: "jw-btn jw-btn-primary jw-topbar-new", onClick: () => setDialog("new"),
          }, "+ New channel"),
          h("div", { className: "jw-menu-host jw-actions-menu-host", ref: actionsRef },
            h("button", {
              className: "jw-btn", "aria-haspopup": "menu",
              "aria-expanded": actionsOpen ? "true" : "false",
              onClick: () => setActionsOpen((v) => !v),
            }, "Actions ▾"),
            actionsOpen ? h("div", { className: "jw-menu", role: "menu" },
              [
                [bulkMode ? "Done selecting" : "Select channels", () => { setBulkMode((v) => !v); setBulkSelected(new Set()); setBulkAnchor(null); }],
                arrangementAvailable ? ["Organize channels…", () => setOrganize({ scope: bulkSelected.size ? { type: "selection" } : null })] : null,
                ["Groups…", () => setDialog("groups")],
              ].filter(Boolean).map(([label, fn]) => h("button", {
                key: label, className: "jw-menu-item", role: "menuitem",
                onClick: () => { setActionsOpen(false); fn(); },
              }, label))) : null,
          ),
          h("div", { className: "jw-menu-host", ref: moreRef },
            h("button", {
              className: "jw-btn", "aria-haspopup": "menu",
              "aria-expanded": moreOpen ? "true" : "false", "aria-label": "More actions",
              title: "Export, shortcuts, diagnostics, reload",
              onClick: () => setMoreOpen((v) => !v),
            }, "⋯"),
            moreOpen ? h("div", { className: "jw-menu", role: "menu" }, [
              ["Export CSV", () => void exportCsv()],
              ["Keyboard shortcuts…", () => setDialog("shortcuts")],
              ["Download diagnostics", () => downloadDiagnostics()],
              ["Reload library", () => void reload()],
            ].map(([label, fn]) => h("button", {
              key: label, className: "jw-menu-item", role: "menuitem",
              onClick: () => { setMoreOpen(false); fn(); },
            }, label))) : null,
          ),
          h("span", { className: "jw-revision-chip" }, "r" + lib.revision),
          dirtyPill,
          applyingPill,
        ),
      ),
      bulkMode ? h("div", { className: "jw-bulk-bar", role: "toolbar", "aria-label": "Selection actions" },
        h("strong", null, bulkSelected.size + " selected"),
        h("span", { className: "jw-scope-line" },
          "of " + filteredIds.length + " shown"
          + (filtering ? " (filter: " + (groupFilter ? (groupsById.get(groupFilter) || {}).name || "group" : "search") + ")" : "")
          + (hiddenSelected.length ? " · includes " + hiddenSelected.length + " hidden by the current filter" : "")),
        hiddenSelected.length
          ? h("span", { className: "jw-hidden-note" },
              hiddenSelected.length + " selected " + (hiddenSelected.length === 1 ? "is" : "are") + " hidden by the current filter",
              h("button", { className: "jw-link", onClick: clearFilters }, "Show"),
              h("button", {
                className: "jw-link",
                onClick: () => setBulkSelected((cur) => {
                  const next = new Set(cur);
                  for (const id of hiddenSelected) next.delete(id);
                  return next;
                }),
              }, "Clear hidden"))
          : null,
        h("button", {
          className: "jw-btn jw-btn-small", disabled: !allRows.length,
          title: "Select every channel in the current search/filter (new drafts stay out — Apply them first)",
          onClick: selectAllMatching,
        }, "Select all " + allRows.filter((c) => !c.temp).length + " matching channels"),
        arrangementAvailable
          ? h("button", {
              className: "jw-btn jw-btn-small", disabled: !bulkSelected.size,
              title: "Arrange numbers or assign a group for the selection",
              onClick: () => setOrganize({ scope: { type: "selection" } }),
            }, "Organize selection…")
          : null,
        h("button", { className: "jw-btn jw-btn-small", disabled: !bulkSelected.size, onClick: () => void moveDialog() }, "Move to group…"),
        h("button", {
          className: "jw-btn jw-btn-small", disabled: !bulkSelected.size,
          onClick: () => void bulkAction((ids) => [{ op: "channels.patch", channelIds: ids, patch: { paused: true } }], "Pause", "will pause"),
        }, "Pause"),
        h("button", {
          className: "jw-btn jw-btn-small", disabled: !bulkSelected.size,
          onClick: () => void bulkAction((ids) => [{ op: "channels.patch", channelIds: ids, patch: { paused: false } }], "Resume", "will resume"),
        }, "Resume"),
        h("button", {
          className: "jw-btn jw-btn-small", disabled: !bulkSelected.size,
          onClick: () => void bulkAction((ids) => [{ op: "channels.patch", channelIds: ids, patch: { archived: true } }], "Archive", "will be archived"),
        }, "Archive"),
        h("button", {
          className: "jw-btn jw-btn-small", disabled: !bulkSelected.size,
          onClick: () => void bulkAction((ids) => [{ op: "channels.patch", channelIds: ids, patch: { archived: false } }], "Restore", "will be restored"),
        }, "Restore"),
        h("button", { className: "jw-btn jw-btn-ghost jw-btn-small", onClick: () => setBulkSelected(new Set()) }, "Clear"),
      ) : null,
      h(ArrangementBar, {
        staged: orgState.staged,
        pending: orgState.pending,
        libRevision: lib.revision,
        busy: arrBusy,
        canUndo: !!(orgState.undo && orgState.undo.length),
        canRedo: !!(orgState.redo && orgState.redo.length),
        onApply: () => void submitArrangement(),
        onCheckResult: () => void checkArrangementResult(),
        onRetry: () => void retryArrangement(),
        onForgetPending: () => void forgetPendingSubmission(),
        onReview: () => orgState.staged && setOrganize({ reviewStaged: orgState.staged }),
        onDiscard: () => void discardStaged(),
        onUndo: () => orgRef.current && orgRef.current.undo(),
        onRedo: () => orgRef.current && orgRef.current.redo(),
        onReplan: () => void replanStaged(),
      }),
      h("main", { className: "jw-columns" },
        h("section", { className: "jw-dial", "aria-label": "Channel dial" },
          h("div", { className: "jw-dial-tools" },
            h("button", { className: "jw-btn jw-btn-primary jw-new-channel", onClick: () => setDialog("new") }, "+ New channel…"),
            filtering
              ? h("div", { className: "jw-dial-count" },
                  "Showing " + allRows.length + " of " + ((lib.channels || []).length + tempChannels.length),
                  h("button", { className: "jw-link jw-dial-clear", onClick: clearFilters }, "Clear search & filter"))
              : null,
          ),
          dialItems.length === 0
            ? h("div", { className: "jw-empty" },
                h("div", { className: "jw-empty-title" }, "Nothing matches"),
                h("div", { className: "jw-empty-sub" }, "Adjust the search, or create a channel."),
                filtering
                  ? h("button", {
                      className: "jw-btn jw-btn-small", style: { marginTop: "10px" },
                      onClick: clearFilters,
                    }, "Clear search & filter")
                  : null)
            : dialList,
        ),
        editor || h("div", { className: "jw-editor jw-editor-empty" },
          h("div", { className: "jw-empty-title" }, "Nothing selected"),
          h("div", { className: "jw-empty-sub" }, "Pick a channel on the dial, or start a new one."),
          h("button", {
            className: "jw-btn jw-btn-primary",
            style: { marginTop: "12px", alignSelf: "flex-start" },
            onClick: () => setDialog("new"),
          }, "+ New channel…"),
        ),
      ),
      dialog === "groups"
        ? h(GroupsManagerDialog, {
            lib, channels: lib.channels || [], drafts: draftsRef.current,
            onClose: () => setDialog(null), submitOps, toast,
            refreshLibrary: () => void refreshLibrary(),
          })
        : null,
      dialog === "new"
        ? h(NewChannelDialog, {
            lib, drafts: draftsRef.current, arrangementAvailable,
            onClose: () => setDialog(null),
            onCreate: (tempId, opts) => { setDialog(null); handleCreated(tempId, opts); },
          })
        : null,
      numRes
        ? h(NumberResolutionSheet, {
            lib, drafts: draftsRef.current,
            subject: numRes.subject, target: numRes.target,
            onStage: stageArrangement,
            onClose: () => setNumRes(null),
          })
        : null,
      organize
        ? h(OrganizeSheet, {
            lib, drafts: draftsRef.current, groups: groupsSorted,
            tempRows: tempChannels, filteredIds, selectionIds: bulkSelected,
            initialScope: organize.scope || null,
            initialIntent: organize.intent || null,
            reviewStaged: organize.reviewStaged || null,
            cachedForm: organizeFormRef.current,
            onStage: stageArrangement,
            onFormChange: (form) => { organizeFormRef.current = form; },
            onBackToEdit: orgState.staged
              ? () => {
                // F6: a reversal has no editable form — Back to edit opens
                // its read-only annotated review instead of a wrong form.
                if (orgState.staged.intent && orgState.staged.intent.type === "reversal") {
                  setOrganize({ reviewStaged: orgState.staged });
                  return;
                }
                setOrganize({ intent: orgState.staged.intent || null });
              }
              : null,
            onOpenDraft: (ref) => { setOrganize(null); setSelectedId(ref); },
            onReplan: () => void replanStaged(),
            onSplitSelection: (kind) => {
              // band_mixed split: re-scope through Select channels so the
              // dial and bar reflect the narrower selection explicitly.
              // Kind resolves for committed AND temp rows (a draft row can
              // sit in the selection); every dropped row is counted and
              // announced — the other band never leaves the scope silently.
              const kindOf = (id) => {
                const c = (lib.channels || []).find((x) => x.id === id);
                if (c) return c.kind || "net";
                const t = tempChannels.find((x) => x.id === id);
                return t ? (t.kind || "net") : null;
              };
              const keep = new Set([...bulkSelected].filter((id) => kindOf(id) === kind));
              const dropped = bulkSelected.size - keep.size;
              setBulkMode(true);
              setBulkSelected(keep);
              setOrganize({ scope: { type: "selection" } });
              const keptBand = kind === "ch" ? "My Channels (1–99)" : "network (100–899)";
              const droppedBand = kind === "ch" ? "network" : "My Channels";
              const msg = "Scope narrowed to " + keep.size + " " + keptBand + " row" + (keep.size === 1 ? "" : "s")
                + (dropped
                    ? " — " + dropped + " " + droppedBand + " row" + (dropped === 1 ? "" : "s")
                      + " left the selection (re-select to arrange that band)."
                    : ".");
              toast(msg, dropped ? "" : "ok");
              announce(msg);
            },
            onClose: () => setOrganize(null),
          })
        : null,
      dialog === "shortcuts"
        ? h(Dialog, {
            title: "Keyboard shortcuts", onClose: () => setDialog(null),
            footer: h("button", { className: "jw-btn", onClick: () => setDialog(null) }, "Close"),
          },
            SHORTCUT_ROWS.map(([keys, desc]) => h("div", { key: desc, className: "jw-shortcut-row" },
              h("span", null, desc),
              h("span", { className: "jw-shortcut-keys" },
                keys.map((k) => h("kbd", { key: k, className: "jw-kbd" }, k))))))
        : null,
      h(ConfirmDialog, { spec: confirmSpec }),
      h(Toasts, { items: toasts, onDismiss: (id) => setToasts((cur) => cur.filter((t) => t.id !== id)) }),
      live ? h("div", {
        key: live.n, className: "jw-visually-hidden", role: "status", "aria-live": "polite",
      }, live.message) : null,
    );
  }

  // ------------------------------------------------------------------
  // Boot: nav link + route
  // ------------------------------------------------------------------

  try {
    const RRDOM = (api.libraries && api.libraries.ReactRouterDOM) || {};
    const FAS = (api.libraries && api.libraries.FontAwesomeSolid) || {};
    const IconCmp = (api.components && api.components.Icon) || null;
    const NavLink = RRDOM.NavLink;
    if (api.patch && typeof api.patch.before === "function" && NavLink) {
      // MenuItems is the primary nav group — the Scenes/Images/…/Tags icon
      // row the owner navigates by (UtilityItems is the Donate/Statistics/
      // Settings group and the wrong neighborhood for an owner page). The
      // tile copies the stock item markup so it renders identically in the
      // row, including the narrow-layout collapse.
      api.patch.before("MainNavBar.MenuItems", function (props) {
        try {
          const existing = props && props.children != null ? props.children : null;
          const icon = FAS.faTv && typeof IconCmp === "function"
            ? h(IconCmp, { icon: FAS.faTv, className: "nav-menu-icon d-block d-xl-inline mb-2 mb-xl-0" })
            : null;
          const tile = h("div", { key: "stash-justwatch-nav", className: "col-4 col-sm-3 col-md-2 col-lg-auto" },
            h(NavLink, {
              exact: true, to: ROUTE_PATH, activeClassName: "active",
              className: "minimal p-4 p-xl-2 d-flex d-xl-inline-block flex-column justify-content-between align-items-center btn btn-primary",
            }, icon, h("span", null, "Channel Studio")),
          );
          return [{ children: h(React.Fragment, null, existing, tile) }];
        } catch (e) {
          return [props || {}];
        }
      });
    }
  } catch (e) {
    console.error("[stash-justwatch] nav patch failed", e);
  }

  try {
    api.register.route(ROUTE_PATH, App);
    api.register.route(LEGACY_ROUTE_PATH, App);
  } catch (e) {
    console.error("[stash-justwatch] route registration failed", e);
  }
})();
