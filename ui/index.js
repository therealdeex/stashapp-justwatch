// Just Watch — Channel Studio (the owner's channel-library editor).
//
// Registers /plugins/stash-justwatch through Stash's PluginApi route surface
// and edits the plugin's channel library (My Channels 1-99 + the network tier
// 100-899) through the channel-curation operations:
//
//   GetChannelLibrary / GetChannelDefinition / GetChannelHistory     reads
//   PreviewChannelPool                                        draft preview
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
  const TEXT_DEBOUNCE_MS = 300;
  const SEARCH_DEBOUNCE_MS = 250;
  const APPLY_POLL_INTERVAL_MS = 2000;
  const APPLY_POLL_TRIES = 60; // ~2 min: the Apply task refreshes inline
  const REFRESH_NOTE_MS = 8000;
  const MAX_NAME_LEN = 60;
  const BANDS = { ch: [1, 99], net: [100, 899] };
  const DRAFT_COUNT_CAP = 100; // chips shown per facet line

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
  const PICKER_ITEM_HEIGHT = 34;

  const TERMINAL_STATUSES = new Set([
    "FINISHED", "COMPLETE", "COMPLETED", "FAILED", "CANCELLED", "CANCELED", "REMOVED", "ABORTED",
  ]);

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
    const resp = await fetch("/graphql", {
      method: "POST",
      headers: gqlHeaders(),
      credentials: "same-origin",
      body: JSON.stringify({ query, variables: variables || {} }),
    });
    const body = await resp.json();
    if (body.errors && body.errors.length) {
      throw new Error(body.errors[0].message || "GraphQL error");
    }
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

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // A queued task is not an outcome. Poll the plugin's durable receipt store
  // until this requestId resolves; "unknown" past the budget is reported, not
  // invented — the caller keeps its draft and may resubmit the SAME id.
  async function pollApplyReceipt(requestId) {
    for (let i = 0; i < APPLY_POLL_TRIES; i++) {
      await sleep(i === 0 ? 700 : APPLY_POLL_INTERVAL_MS);
      let receipt = null;
      try {
        receipt = await runOp("GetChannelApplyResult", { requestId });
      } catch (e) {
        continue; // transient query failure: the task may still be running
      }
      if (receipt && receipt.status && receipt.status !== "unknown") return receipt;
    }
    return {
      requestId, status: "unknown",
      message: "the server never reported a result for this request — applying again with the same draft reuses the same requestId",
    };
  }

  // THE write path: task submit + receipt correlation.
  async function applyChannelChanges(requestId, expectedRevision, ops) {
    await runTask("Apply Channel Changes", {
      mode: "ApplyChannelChanges",
      requestId,
      expectedRevision: String(expectedRevision),
      ops: JSON.stringify(ops),
    });
    return pollApplyReceipt(requestId);
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
    // Server-side group ids look like grp_<lowercase>; a client-generated id
    // lets channels.move reference a group created in the same transaction.
    const bytes = new Uint8Array(5);
    (window.crypto || { getRandomValues: (b) => b.forEach((_, i) => { b[i] = (Math.random() * 256) | 0; }) })
      .getRandomValues(bytes);
    return "grp" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
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

  // Mirrors the server's programming policy clamp so a field the user touched
  // and reverted compares clean against a shorter stored policy.
  function canonicalProgramming(raw) {
    const p = raw && typeof raw === "object" ? raw : {};
    const int = (key, def, lo, hi) => {
      const v = p[key] != null ? p[key] : def;
      return typeof v === "number" ? Math.max(lo, Math.min(hi, v)) : def;
    };
    return {
      mode: ["fixed", "explore", "discovery", "continuing"].includes(p.mode) ? p.mode : "fixed",
      spacing: int("spacing", 0, 0, 10),
      repeatHours: int("repeatHours", 0, 0, 168),
      spotlight: ["studio", "performer"].includes(p.spotlight) ? p.spotlight : "none",
      spotlightDay: int("spotlightDay", 5, 0, 6),
      spotlightHour: int("spotlightHour", 20, 0, 23),
    };
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
    if (s.duration && s.duration.min) active.push("Running at least " + Math.round(s.duration.min / 60) + " min");
    if (s.duration && s.duration.max) active.push("Running at most " + Math.round(s.duration.max / 60) + " min");
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
    if (s.duration && s.duration.min != null && s.duration.max != null && Number(s.duration.max) < Number(s.duration.min)) {
      errors.push({ field: "source.duration", code: "bad_duration", message: "Maximum duration is below the minimum." });
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
  // Drafts: in-memory + sessionStorage, versioned, never applied
  // automatically. Keyed jw-studio-drafts-v1:<libraryId>.
  // ------------------------------------------------------------------

  function makeDraftStore(libraryId) {
    const key = "jw-studio-drafts-v1:" + libraryId;
    let listeners = [];
    const emit = () => listeners.slice().forEach((fn) => { try { fn(); } catch (e) { /* noop */ } });
    function readAll() {
      try {
        const parsed = JSON.parse(sessionStorage.getItem(key) || "{}");
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch (e) { return {}; }
    }
    function writeAll(all) {
      try { sessionStorage.setItem(key, JSON.stringify(all)); } catch (e) { /* full storage: drafts stay in memory */ }
    }
    return {
      get(id) {
        const entry = readAll()[id];
        return entry && entry.v === 1 && entry.draft ? entry : null;
      },
      put(id, draft) {
        const all = readAll();
        all[id] = { v: 1, draft, savedAt: Date.now() };
        writeAll(all);
        emit();
      },
      drop(id) {
        const all = readAll();
        if (all[id] != null) { delete all[id]; writeAll(all); emit(); }
      },
      ids() { return Object.keys(readAll()); },
      clear() { try { sessionStorage.removeItem(key); } catch (e) { /* noop */ } emit(); },
      subscribe(fn) { listeners.push(fn); return () => { listeners = listeners.filter((f) => f !== fn); }; },
    };
  }

  // ------------------------------------------------------------------
  // In-flight applies: a channel's apply survives the editor being navigated
  // away (poll continues; the receipt lands; drafts/library update).
  // channelId -> { requestId, snapshot, expected, promise }
  // ------------------------------------------------------------------

  const inflightApplies = new Map();

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

  function Dialog({ title, onClose, children, footer, wide }) {
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
        className: "jw-dialog" + (wide ? " jw-dialog-wide" : ""),
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
  function WindowedList({ items, itemHeight, render, overscan, resetKey, ariaLabel, className }) {
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
      role: "listbox", "aria-label": ariaLabel,
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
      }, t.message)),
    );
  }

  function StatusChip({ kind, children }) {
    return h("span", { className: "jw-status-chip jw-status-" + kind }, children);
  }

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
        : h(WindowedList, {
            items: rows, itemHeight: PICKER_ITEM_HEIGHT, render: rowRender,
            resetKey: query + ":" + kind, ariaLabel: "Search results",
            className: "jw-pick-list",
          }),
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
  // Groups manager (each change is its own Apply)
  // ------------------------------------------------------------------

  function GroupsManagerDialog({ lib, drafts, onClose, submitOps }) {
    const [busy, setBusy] = useState(false);
    const [newName, setNewName] = useState("");
    const [confirmDelete, setConfirmDelete] = useState(null); // {group, moveTo}
    const groups = (lib.groups || []).slice().sort((a, b) => a.position - b.position);
    const memberCount = (gid) => (lib.channels || []).filter((c) => c.groupId === gid).length;

    const run = async (ops, label) => {
      if (busy) return;
      setBusy(true);
      try {
        await submitOps(ops, { label });
      } finally {
        setBusy(false);
      }
    };

    const rename = (g, name) => {
      const trimmed = String(name || "").trim();
      if (!trimmed || trimmed === g.name) return;
      void run([{ op: "group.put", group: { id: g.id, name: trimmed, position: g.position } }], "Group renamed");
    };

    const move = (g, dir) => {
      const i = groups.findIndex((x) => x.id === g.id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= groups.length) return;
      const ops = groups.map((x, idx) => {
        const pos = idx === i ? j + 1 : idx === j ? i + 1 : idx + 1;
        return { op: "group.put", group: { id: x.id, name: x.name, position: pos } };
      });
      void run(ops, "Groups reordered");
    };

    const create = () => {
      const trimmed = newName.trim();
      if (!trimmed) return;
      const maxPos = Math.max(0, ...groups.map((g) => g.position));
      setNewName("");
      void run([{ op: "group.put", group: { id: newGroupId(), name: trimmed, position: maxPos + 1 } }], "Group created");
    };

    if (confirmDelete) {
      const g = confirmDelete.group;
      const members = memberCount(g.id);
      const others = groups.filter((x) => x.id !== g.id);
      return h(Dialog, {
        title: "Delete “" + g.name + "”", onClose: () => setConfirmDelete(null),
        footer: [
          h("button", { key: "c", className: "jw-btn", onClick: () => setConfirmDelete(null) }, "Cancel"),
          h("button", {
            key: "d", className: "jw-btn jw-btn-danger", disabled: busy,
            onClick: () => {
              const ops = [{ op: "group.delete", id: g.id }];
              if (members) ops[0].moveTo = confirmDelete.moveTo;
              setConfirmDelete(null);
              // keep stored drafts consistent: their group moved too
              if (drafts && members) {
                for (const c of lib.channels || []) {
                  if (c.groupId !== g.id) continue;
                  const entry = drafts.get(c.id);
                  if (entry) drafts.put(c.id, Object.assign({}, entry.draft, { groupId: confirmDelete.moveTo }));
                }
              }
              void run(ops, "Group deleted");
            },
          }, "Delete group"),
        ],
      },
        h("p", { className: "jw-confirm-message" },
          members
            ? members + (members === 1 ? " channel moves" : " channels move") + " to another group. One Apply."
            : "The group is empty. One Apply."),
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "jw-group-dest" }, "Destination group"),
          h("select", {
            id: "jw-group-dest", className: "jw-input", disabled: !members,
            value: confirmDelete.moveTo, onChange: (e) => setConfirmDelete({ group: g, moveTo: e.target.value }),
          }, others.map((x) => h("option", { key: x.id, value: x.id }, x.name))),
        ),
      );
    }

    return h(Dialog, {
      title: "Groups", onClose,
      footer: [h("button", { key: "done", className: "jw-btn jw-btn-primary", onClick: onClose }, "Done")],
    },
      h("p", { className: "jw-hint" },
        "Every channel belongs to exactly one group. Renames, reorders, creates and deletes each apply immediately as their own revision."),
      h("div", { className: "jw-groups-list", role: "list" },
        groups.map((g, i) => h("div", { key: g.id, className: "jw-groups-row", role: "listitem" },
          h("span", { className: "jw-groups-pos" }, "#" + g.position),
          h("input", {
            className: "jw-input jw-groups-name", defaultValue: g.name,
            "aria-label": "Group name " + g.name,
            onBlur: (e) => rename(g, e.target.value),
            onKeyDown: (e) => { if (e.key === "Enter") e.target.blur(); },
          }),
          h("span", { className: "jw-groups-count" }, memberCount(g.id) + " ch"),
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Move " + g.name + " up",
            disabled: busy || i === 0, onClick: () => move(g, -1),
          }, "↑"),
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Move " + g.name + " down",
            disabled: busy || i === groups.length - 1, onClick: () => move(g, +1),
          }, "↓"),
          h("button", {
            className: "jw-btn jw-btn-ghost jw-btn-small", "aria-label": "Delete group " + g.name,
            disabled: busy || groups.length <= 1, title: groups.length <= 1 ? "Cannot remove the last group." : null,
            onClick: () => setConfirmDelete({ group: g, moveTo: (groups.filter((x) => x.id !== g.id)[0] || {}).id }),
          }, "🗑"),
        )),
      ),
      h("div", { className: "jw-groups-create" },
        h("input", {
          className: "jw-input", placeholder: "New group name", value: newName,
          "aria-label": "New group name",
          onChange: (e) => setNewName(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") create(); },
        }),
        h("button", { className: "jw-btn jw-btn-primary", disabled: busy || !newName.trim(), onClick: create }, "Create"),
      ),
    );
  }

  // ------------------------------------------------------------------
  // New channel dialog → local DRAFT (committed on Apply)
  // ------------------------------------------------------------------

  function NewChannelDialog({ lib, drafts, onClose, onCreate }) {
    const groups = (lib.groups || []).slice().sort((a, b) => a.position - b.position);
    const [kind, setKind] = useState("net");
    const [number, setNumber] = useState(() => nextFreeNumber(lib.channels || [], "net"));
    const [name, setName] = useState("");
    const [groupId, setGroupId] = useState(groups[0] ? groups[0].id : "");
    const [error, setError] = useState(null);

    const band = BANDS[kind];
    const numberOk = Number.isInteger(number) && number >= band[0] && number <= band[1]
      && !(lib.channels || []).some((c) => c.number === number);

    const switchKind = (k) => {
      setKind(k);
      setNumber(nextFreeNumber(lib.channels || [], k));
    };

    const submit = () => {
      const trimmed = name.trim();
      if (!trimmed) { setError("Give the channel a name."); return; }
      if (!numberOk) { setError("Number " + (number || "") + " is not free in " + band[0] + "–" + band[1] + "."); return; }
      if (!groupId) { setError("Pick a group."); return; }
      const tempId = newTempId();
      const draft = {
        kind, number, name: trimmed, glyph: null,
        color: pickDefaultColor(number), groupId, sort: "shuffle",
        enabled: true, archived: false, paused: false,
        source: convertLegacySource({ type: "none" }),
        sourceLabel: "", programming: null,
      };
      drafts.put(tempId, draft);
      onCreate(tempId);
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
        !numberOk && number != null
          ? h("p", { className: "jw-error-text" }, "That number is taken or out of the band.")
          : null,
      ),
      h("div", { className: "jw-field" },
        h("label", { className: "jw-field-label", htmlFor: "jw-new-group" }, "Group"),
        h("select", { id: "jw-new-group", className: "jw-input", value: groupId, onChange: (e) => setGroupId(e.target.value) },
          groups.map((g) => h("option", { key: g.id, value: g.id }, g.name))),
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
  // Swap dialog
  // ------------------------------------------------------------------

  function SwapDialog({ draft, lib, onClose, onPick }) {
    const others = (lib.channels || [])
      .filter((c) => c.id !== draft.id && c.kind === (draft.kind || "net"))
      .sort((a, b) => Math.abs(a.number - draft.number) - Math.abs(b.number - draft.number))
      .slice(0, 30);
    return h(Dialog, {
      title: "Swap number " + draft.number + " with…", onClose,
      footer: [h("button", { key: "c", className: "jw-btn", onClick: onClose }, "Cancel")],
    },
      h("p", { className: "jw-hint" },
        "Both numbers exchange in the SAME Apply — one transaction, no duplicate-number window."),
      h("div", { className: "jw-swap-list", role: "listbox", "aria-label": "Swap with" },
        others.map((c) => h("div", {
          key: c.id, className: "jw-swap-row", role: "option", tabIndex: 0,
          onClick: () => { onPick(c); onClose(); },
          onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(c); onClose(); } },
        },
          h(GlyphTile, { codepoint: c.glyph, color: c.color, size: 24, fallback: c.number }),
          h("span", { className: "jw-swap-name" }, c.name),
          h("span", { className: "jw-pick-eid" }, "#" + c.number),
        )),
      ),
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
    channelId, lib, getRevision, drafts, confirm, toast,
    submitOps, onApplied, onCreated, reportPhase,
  }) {
    const isTemp = String(channelId).startsWith("temp-");

    const [stored, setStored] = useState(null);   // server definition (channel record)
    const [summary, setSummary] = useState([]);   // server plain-language lines
    const [draft, setDraft] = useState(null);
    const [phase, setPhase] = useState("clean");  // clean|dirty|validating|applying|applied
    const [applyError, setApplyError] = useState(null); // {kind, message, errors, currentRevision}
    const [lastAppliedRev, setLastAppliedRev] = useState(null);
    const [refreshNote, setRefreshNote] = useState(null);
    const [preview, setPreview] = useState(null);
    const [previewState, setPreviewState] = useState("idle"); // idle|loading|ok|error
    const [menuOpen, setMenuOpen] = useState(false);
    const [glyphOpen, setGlyphOpen] = useState(false);
    const [picker, setPicker] = useState(null);   // {kind, title, facets...}
    const [swapOpen, setSwapOpen] = useState(false);
    const [historyOpen, setHistoryOpen] = useState(false);
    const [loadError, setLoadError] = useState(null);
    const [nameTick, setNameTick] = useState(0);  // re-render chips after name resolution

    const draftRef = useRef(null);
    const storedRef = useRef(null);
    const phaseRef = useRef("clean");
    const inFlightRef = useRef(false);
    const pendingRef = useRef(null);   // {requestId, snapshot, expected} — kept on transport failure
    const previewSeqRef = useRef(0);
    const aliveRef = useRef(true);
    const refreshTimerRef = useRef(null);
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

    // Phase reporting (the app guards channel switches + Reload while applying)
    useEffect(() => {
      reportPhase({ phase, dirty: isDirty() });
    });

    // Local mirror of the rules text-search input (committed to the draft on a
    // debounce). Null = mirror the committed value.
    const committedQ = draft && draft.source && draft.source.q ? String(draft.source.q) : "";
    const [qLocal, setQLocal] = useState(null);
    useEffect(() => { setQLocal(null); }, [committedQ, channelId]);

    // ---- init / retarget ----
    useEffect(() => {
      let alive = true;
      setLoadError(null);
      setPreview(null);
      setPreviewState("idle");
      setApplyError(null);
      setLastAppliedRev(null);
      setRefreshNote(null);
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
        const base = saved ? saved.draft : clone(storedChannel);
        if (!alive) return;
        setStored(storedChannel);
        setSummary(serverSummary);
        setDraft(base);
        draftRef.current = base;
        setPhase(saved ? "dirty" : "clean");
        phaseRef.current = saved ? "dirty" : "clean";
        // resume an apply that outlived the previous editor instance
        const infl = inflightApplies.get(channelId);
        if (infl) {
          inFlightRef.current = true;
          pendingRef.current = { requestId: infl.requestId, snapshot: infl.snapshot, expected: infl.expected };
          setPhase("applying");
          infl.promise.then((receipt) => { finalize(receipt, infl.snapshot, true); }).catch(() => {});
        }
        void loadPreview();
        resolveNamesFor(base && base.source);
      })();
      return () => { alive = false; };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [channelId]);

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
      if (phaseRef.current !== "applying") {
        setPhase("dirty");
        phaseRef.current = "dirty";
        drafts.put(channelId, clone(next));
      }
      if (applyError) setApplyError(null);
      schedulePreview();
      resolveNamesFor(next.source);
    }

    const schedulePreview = useMemo(() => debounce(() => { void loadPreview(); }, PREVIEW_DEBOUNCE_MS), []);

    // Debounced commit of the rules text-search input.
    const commitQRef = useRef(null);
    const commitQ = useMemo(() => debounce((v) => {
      edit((d) => {
        if (v.trim()) d.source.q = v.trim();
        else delete d.source.q;
        return d;
      });
    }, TEXT_DEBOUNCE_MS), []);
    commitQRef.current = commitQ;

    async function loadPreview() {
      const d = draftRef.current;
      if (!d || !d.source) return;
      const seq = ++previewSeqRef.current;
      setPreviewState("loading");
      try {
        const args = {
          source: JSON.stringify(d.source),
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

    function validateDraft() {
      const d = draftRef.current;
      const errors = [];
      if (!d) return errors;
      const name = String(d.name || "").trim();
      if (!name) errors.push({ field: "name", message: "Name can't be empty." });
      else if (name.length > MAX_NAME_LEN) errors.push({ field: "name", message: "Name must be " + MAX_NAME_LEN + " characters or fewer." });
      if (!d.groupId) errors.push({ field: "group", message: "Pick a group." });
      const band = BANDS[d.kind || "net"];
      const num = d.number;
      if (!Number.isInteger(num) || num < band[0] || num > band[1]) {
        errors.push({ field: "number", message: "Number must be " + band[0] + "–" + band[1] + " for this band." });
      } else {
        const occupant = (lib.channels || []).find((c) => c.number === num && c.id !== channelId);
        if (occupant) {
          errors.push({ field: "number", message: "Channel " + num + " is taken by “" + occupant.name + "”. Use Swap." });
        }
      }
      if (isRuleSource(d.source)) {
        for (const e of ruleSourceClientErrors(d.source)) errors.push(e);
        const canon = canonicalSource(d.source);
        const hasRule = FACET_KEYS.some((k) => canon[k] && canon[k].length)
          || canon.date || canon.duration || canon.createdAt || canon.q
          || canon.studioSceneCount || canon.performerSceneCount;
        if (!hasRule && d.source.type === "criteria") {
          errors.push({ field: "source", code: "empty_rules", message: "Add at least one rule — an empty criteria pool matches nothing." });
        }
      }
      return errors;
    }

    // membership effects of the last pre-flight (drives the refresh-pending note)
    const effectsRef = useRef([]);

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
    // NOTE: swap is NOT part of a draft transaction. This server build cannot
    // statically validate channel.put onto a taken number (its swap-pair
    // exemption crashes: library.py _check_channel_draft references an out-of-
    // scope `swap_pairs`), so swapping numbers is its OWN immediate Apply —
    // exactly the prototype's behavior. The draft never carries a taken number.
    function buildOps(snapshot) {
      if (isTemp) {
        const channel = clone(snapshot);
        delete channel.id;
        delete channel.seed;
        delete channel.provenance;
        return [{ op: "channel.create", tempId: channelId, channel }];
      }
      return [{ op: "channel.put", channel: clone(snapshot) }];
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
      const snapshot = clone(draftRef.current);
      const ops = buildOps(snapshot);
      const expected = expectedOverride != null ? expectedOverride : getRevision();
      // Pre-flight against the live document: precise typed errors before the
      // task queue is involved (no writes; revision races are still handled
      // by the receipt).
      try {
        const check = await runOp("ValidateChannelChanges", { ops: JSON.stringify(ops) });
        if (!aliveRef.current) return;
        if (check && check.valid === false && (check.errors || []).length) {
          setPhase("dirty");
          setApplyError({ kind: "validation", errors: check.errors, message: "the server rejected these changes" });
          return;
        }
        effectsRef.current = (check && check.effects) || [];
      } catch (e) { /* pre-flight is best-effort; the receipt remains the truth */ }
      if (!aliveRef.current) return;
      // Reuse the pending requestId ONLY for a byte-identical retry (same ops,
      // same expectedRevision) — that is the idempotent-replay case.
      const pending = pendingRef.current;
      const reuse = pending && pending.expected === expected && deepEqual(snapshot, pending.snapshot);
      const requestId = reuse ? pending.requestId : newRequestId();
      pendingRef.current = { requestId, snapshot, expected };
      setPhase("applying");
      inFlightRef.current = true;

      const promise = applyChannelChanges(requestId, expected, ops);
      inflightApplies.set(channelId, { requestId, snapshot, expected, promise });
      let receipt;
      try {
        receipt = await promise;
      } catch (e) {
        // Transport failure: the draft AND the requestId survive. Retrying the
        // unchanged draft replays or lands the same transaction.
        inFlightRef.current = false;
        inflightApplies.delete(channelId);
        if (!aliveRef.current) return;
        setPhase("dirty");
        setApplyError({ kind: "transport", message: "Submit failed (" + String((e && e.message) || e) + ")." });
        toast("Apply failed to reach the server — draft kept. Apply again to retry the same request.", "err");
        return;
      }
      finalize(receipt, snapshot, false);
    }

    async function finalize(receipt, snapshot, resumed) {
      inFlightRef.current = false;
      inflightApplies.delete(channelId);
      if (!receipt) return;
      const transportLike = receipt.status === "unknown";

      if (receipt.status === "committed") {
        pendingRef.current = null;
        // Did the draft move on while the request was in flight? Compare the
        // LIVE draft (an editor that died mid-apply falls back to its stored
        // session draft, which by definition matches the snapshot).
        const currentDraft = aliveRef.current && draftRef.current
          ? draftRef.current
          : (drafts.get(channelId) ? drafts.get(channelId).draft : snapshot);
        const untouched = deepEqual(currentDraft, snapshot);
        if (untouched) drafts.drop(channelId);
        onApplied(receipt, { channelId, isTemp, untouched, idMap: receipt.idMap });
        if (!aliveRef.current) return; // registry side effects already done
        // refresh the definition from the server
        let freshStored = null;
        let freshSummary = [];
        if (!isTemp || receipt.idMap) {
          const finalId = (receipt.idMap && receipt.idMap[channelId]) || channelId;
          try {
            const def = await runOp("GetChannelDefinition", { channelId: finalId });
            freshStored = def.channel;
            freshSummary = def.summary || [];
          } catch (e) { /* channel vanished? keep local draft state */ }
        }
        setStored(freshStored);
        setSummary(freshSummary);
        setLastAppliedRev(receipt.revision);
        // The durable receipt (what we poll) does not carry the task's inline
        // refresh map — membership effects come from the pre-flight instead.
        const membershipChanged = effectsRef.current.some((e) => e.reindex);
        const outcomes = receipt.refresh && typeof receipt.refresh === "object"
          ? Object.entries(receipt.refresh) : [];
        if (outcomes.length || membershipChanged) {
          setRefreshNote(outcomes.length
            ? "Background programming refresh pending — " + outcomes.length
              + (outcomes.length === 1 ? " channel" : " channels") + " ("
              + [...new Set(outcomes.map(([, o]) => String(o).split(":")[0]))].join(", ") + ")"
            : "Background programming refresh pending — membership changed.");
          if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
          refreshTimerRef.current = setTimeout(() => {
            if (aliveRef.current) setRefreshNote(null);
          }, REFRESH_NOTE_MS);
        }
        if (untouched) {
          const nextDraft = freshStored ? clone(freshStored) : clone(snapshot);
          draftRef.current = nextDraft;
          setDraft(nextDraft);
          setPhase("applied");
          phaseRef.current = "applied";
        } else {
          setPhase("dirty");
          phaseRef.current = "dirty";
          toast("Applied your earlier snapshot — newer edits are still draft.", "");
        }
        if (resumed) toast("Applied at r" + receipt.revision + " (recovered receipt).", "ok");
      } else if (receipt.error === "revision_conflict") {
        if (!transportLike) pendingRef.current = null;
        setPhase("dirty");
        setApplyError({ kind: "conflict", currentRevision: receipt.currentRevision, message: receipt.message });
      } else if (receipt.error === "validation_failed") {
        if (!transportLike) pendingRef.current = null;
        setPhase("dirty");
        setApplyError({ kind: "validation", errors: receipt.errors || [], message: receipt.message });
      } else if (transportLike) {
        // unresolved: KEEP the pending requestId — a retry of the unchanged
        // draft is the idempotent path the server documents.
        setPhase("dirty");
        setApplyError({ kind: "transport", message: receipt.message || "the server never reported a result" });
        toast("No receipt yet — draft kept. Apply again to reuse the same requestId.", "err");
      } else {
        pendingRef.current = null;
        setPhase("dirty");
        setApplyError({ kind: "rejected", message: receipt.message || receipt.error || "rejected" });
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
      onCreated(tempId);
      toast("Copy staged as a draft — Apply in the editor to commit it.", "");
    };

    // Swap numbers with another channel: one immediate Apply (a swap rides
    // alone — the draft stays out of it and must be clean beforehand).
    const doSwap = async (other) => {
      setSwapOpen(false);
      const d = draftRef.current;
      if (!d || !d.number || d.number === other.number) return;
      if (inFlightRef.current) { toast("An apply is in flight — wait for its receipt.", "err"); return; }
      if (isDirty()) { toast("Apply or discard your draft first — a number swap is its own Apply.", "err"); return; }
      const ok = await confirm({
        title: "Swap numbers " + d.number + " ↔ " + other.number + "?",
        message: "“" + (d.name || "This channel") + "” and “" + other.name + "” exchange numbers. One Apply, atomic — no duplicate-number window.",
        confirmLabel: "Swap numbers",
      });
      if (!ok) return;
      const receipt = await submitOps(
        [{ op: "channel.swap", a: channelId, b: other.id }],
        { label: "Numbers swapped" },
      );
      if (receipt.status === "committed") {
        try {
          const def = await runOp("GetChannelDefinition", { channelId });
          if (!aliveRef.current) return;
          setStored(def.channel);
          setSummary(def.summary || []);
          const fresh = clone(def.channel);
          draftRef.current = fresh;
          setDraft(fresh);
        } catch (e) { /* keep local state */ }
      }
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
      return h("div", { className: "jw-editor" }, h("div", { className: "jw-loading" }, "Loading definition…"));
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
                h("button", { className: "jw-link", onClick: () => void doSwap(occupant) }, "Swap numbers"))
            : null,
          h("div", { className: "jw-hint" },
            h("button", { className: "jw-link", onClick: () => setSwapOpen(true) }, "Swap with a nearby channel…")),
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
            className: "jw-btn", onClick: () => setGlyphOpen((v) => !v),
            "aria-expanded": glyphOpen ? "true" : "false",
          },
            h(GlyphTile, { codepoint: draft.glyph, color: draft.color, size: 22, fallback: draft.number }),
            h("span", { style: { marginLeft: "8px" } }, draft.glyph ? glyphName(draft.glyph) : "No glyph"),
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
          onChange: (e) => edit((d) => { d.groupId = e.target.value; return d; }),
        }, groups.map((g) => h("option", { key: g.id, value: g.id }, g.name))),
        fieldError("group") ? h("div", { className: "jw-error-text" }, fieldError("group")) : null,
        h("p", { className: "jw-hint" }, "Groups organize this library and the guide. Membership never changes what airs."),
      ),
    );

    // ---------- rules (What airs) ----------

    const facetRow = (facet) => {
      const cfg = {
        tags: { kind: "tag", allKey: "tags", anyKey: "tagsAny", exclKey: "excludeTags", note: "Sub-tags always count (hierarchy included). ALL = a scene must carry every tag; ANY = at least one." },
        performers: { kind: "performer", allKey: "performers", anyKey: "performersAny", exclKey: "excludePerformers", sceneKey: "performerSceneCount", noun: "performers" },
        studios: { kind: "studio", allKey: "studios", anyKey: "studiosAny", exclKey: "excludeStudios", sceneKey: "studioSceneCount", noun: "studios", note: "Sub-studios count (hierarchy included)." },
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

      const toggleLogic = (mode) => setFacet((s) => {
        // Move the ids of BOTH lists into the chosen storage field (sparse
        // sources may omit the unused list entirely).
        const merged = sortedIds([...idsOf(s[cfg.allKey]), ...idsOf(s[cfg.anyKey])]);
        s[cfg.allKey] = mode === "all" ? merged : [];
        s[cfg.anyKey] = mode === "any" ? merged : [];
        return s;
      });

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
      const facetErr = rErrors.find((e) => {
        const p = String(e.path || e.field || "");
        return !sceneErr && (p.includes("." + cfg.allKey) || p.includes("." + cfg.anyKey)
          || p.endsWith(".source." + cfg.allKey));
      });

      return h("div", { key: facet, className: "jw-rule-row" + (facetErr ? " jw-rule-row-invalid" : "") },
        h("div", { className: "jw-rule-head" },
          h("span", { className: "jw-rule-name" }, facet.charAt(0).toUpperCase() + facet.slice(1)),
          h("span", { className: "jw-logic", role: "group", "aria-label": facet + " match logic" },
            h("button", {
              "aria-pressed": pressedAll ? "true" : "false",
              className: pressedAll ? "jw-logic-on" : "",
              title: "A scene must match every one of these",
              onClick: () => toggleLogic("all"),
            }, "ALL"),
            h("button", {
              "aria-pressed": pressedAny ? "true" : "false",
              className: pressedAny ? "jw-logic-on" : "",
              title: "A scene matches at least one of these",
              onClick: () => toggleLogic("any"),
            }, "ANY"),
          ),
          h("button", { className: "jw-btn jw-btn-small", onClick: () => pickInto("main") },
            "Choose (" + (union.length ? union.length.toLocaleString() : "none") + ")"),
          h("label", { className: "jw-exclude-label" },
            "exclude",
            h("button", { className: "jw-btn jw-btn-small", onClick: () => pickInto(cfg.exclKey) },
              excl.length ? excl.length.toLocaleString() : "none"),
          ),
        ),
        h("div", { className: "jw-entity-summary" },
          union.length
            ? union.slice(0, DRAFT_COUNT_CAP).map((id) => h("span", { key: id, className: "jw-entity-chip" },
                h("span", null, entityName(cfg.kind, id) + (any.includes(id) ? " (any)" : "")),
                h("button", {
                  "aria-label": "Remove " + entityName(cfg.kind, id),
                  onClick: () => setFacet((s) => {
                    s[cfg.allKey] = (s[cfg.allKey] || []).filter((x) => x !== id);
                    s[cfg.anyKey] = (s[cfg.anyKey] || []).filter((x) => x !== id);
                    return s;
                  }),
                }, "✕"),
              ))
            : h("span", { className: "jw-hint" }, "none"),
          union.length > DRAFT_COUNT_CAP
            ? h("span", { className: "jw-entity-chip" }, "+ " + (union.length - DRAFT_COUNT_CAP) + " more")
            : null,
        ),
        excl.length
          ? h("div", { className: "jw-entity-summary" },
              excl.slice(0, DRAFT_COUNT_CAP).map((id) => h("span", { key: id, className: "jw-entity-chip jw-entity-chip-excl" },
                h("span", null, "without " + entityName(cfg.kind, id)),
                h("button", {
                  "aria-label": "Keep " + entityName(cfg.kind, id),
                  onClick: () => setFacet((s) => { s[cfg.exclKey] = (s[cfg.exclKey] || []).filter((x) => x !== id); return s; }),
                }, "✕"),
              )))
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
      const durMin = src.duration && src.duration.min != null ? Math.round(src.duration.min / 60) : "";
      const durMax = src.duration && src.duration.max != null ? Math.round(src.duration.max / 60) : "";
      const within = src.createdAt && src.createdAt.withinDays ? src.createdAt.withinDays : "";
      const qErr = rErrors.find((e) => String(e.path || e.field || "").includes("duration"));
      const dateErr = rErrors.find((e) => String(e.path || e.field || "").includes("date"));
      const qValue = qLocal != null ? qLocal : committedQ;
      return h("div", { className: "jw-rule-row" + (qErr || dateErr ? " jw-rule-row-invalid" : "") },
        h("div", { className: "jw-rule-head" },
          h("span", { className: "jw-rule-name" }, "Scene details"),
        ),
        h("div", { className: "jw-rule-dates" },
          h("label", { className: "jw-dynamic-num" }, "dated",
            h("input", {
              type: "date", "aria-label": "Scene date from", value: dateFrom,
              onChange: (e) => setMeta((s) => {
                s.date = { from: e.target.value, to: s.date && s.date.to ? s.date.to : "" };
                if (!s.date.from && !s.date.to) delete s.date;
                return s;
              }),
            })),
          h("span", { className: "jw-hint" }, "→"),
          h("input", {
            type: "date", "aria-label": "Scene date to", value: dateTo,
            onChange: (e) => setMeta((s) => {
              s.date = { from: s.date && s.date.from ? s.date.from : "", to: e.target.value };
              if (!s.date.from && !s.date.to) delete s.date;
              return s;
            }),
          }),
          h("label", { className: "jw-dynamic-num" }, "duration ≥",
            h("input", {
              type: "number", min: 0, style: { width: "76px" }, "aria-label": "Minimum duration in minutes",
              value: durMin, placeholder: "min",
              onChange: (e) => setMeta((s) => {
                const v = parseInt(e.target.value, 10);
                const spec = Object.assign({}, s.duration || {});
                if (Number.isNaN(v)) delete spec.min; else spec.min = v * 60;
                if (spec.min == null && spec.max == null) delete s.duration;
                else s.duration = spec;
                return s;
              }),
            })),
          h("label", { className: "jw-dynamic-num" }, "≤",
            h("input", {
              type: "number", min: 0, style: { width: "76px" }, "aria-label": "Maximum duration in minutes",
              value: durMax, placeholder: "max",
              onChange: (e) => setMeta((s) => {
                const v = parseInt(e.target.value, 10);
                const spec = Object.assign({}, s.duration || {});
                if (Number.isNaN(v)) delete spec.max; else spec.max = v * 60;
                if (spec.min == null && spec.max == null) delete s.duration;
                else s.duration = spec;
                return s;
              }),
            })),
          h("span", { className: "jw-hint" }, "min"),
        ),
        h("div", { className: "jw-rule-dates", style: { marginTop: "8px" } },
          h("label", { className: "jw-dynamic-num" }, "added within",
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
            type: "text", className: "jw-input", style: { flex: 1 }, "aria-label": "Text search",
            placeholder: "text search…", value: qValue,
            onChange: (e) => {
              setQLocal(e.target.value);
              commitQRef.current(e.target.value);
            },
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

    const rulesCard = isRuleSource(draft.source)
      ? h("div", { className: "jw-card" },
          h("h3", { className: "jw-card-title" }, "What airs (rules)"),
          h("p", { className: "jw-hint" }, "Rows combine with AND. ALL / ANY applies within a row; ids and the dynamic activity rule combine too."),
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
    const setProgramming = (values) => edit((d) => {
      d.programming = fullProgramming(Object.assign({}, canonicalProgramming(d.programming), values));
      return d;
    });
    const programmingCard = h("div", { className: "jw-card" },
      h("h3", { className: "jw-card-title" }, "Programming"),
      h("div", { className: "jw-fieldrow" },
        h("div", { className: "jw-field" },
          h("label", { className: "jw-field-label", htmlFor: "f-mode" }, "Mode"),
          h("select", {
            id: "f-mode", className: "jw-input", value: progMode,
            onChange: (e) => setProgramming({ mode: e.target.value }),
          },
            h("option", { value: "fixed" }, "Fixed loop — the rotation repeats"),
            h("option", { value: "explore" }, "Explore — shuffled, no recent repeats"),
            h("option", { value: "discovery" }, "Discovery — pushes unheard content"),
            h("option", { value: "continuing", disabled: progMode !== "continuing" },
              progMode === "continuing" ? "Continuing — full library like a broadcast" : "Continuing — operator rollout gate"),
          ),
          progMode !== "continuing"
            ? h("p", { className: "jw-hint" }, "Continuing is activation-gated by the operator rollout; the GUI cannot switch it on.")
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
      h("h3", { className: "jw-card-title" }, "Preview"),
      previewState === "loading" ? h("p", { className: "jw-hint" }, "Checking the current draft…") : null,
      previewState === "error"
        ? h("p", { className: "jw-error-text", role: "alert" }, "Preview failed: " + (preview && preview.message ? preview.message : "unknown error"))
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
          ? h("p", { className: "jw-error-text", role: "alert" },
              "Empty pool — nothing matches these rules yet. The channel would be off air.")
          : h("div", { className: "jw-preview-strip" },
              (preview.sample || []).map((sItem) => h("div", { key: sItem.id, className: "jw-sample-card" },
                h("div", {
                  className: "jw-sample-art", "aria-hidden": "true",
                  style: sItem.preview ? { backgroundImage: "url('" + sItem.preview + "')" } : null,
                }, sItem.preview ? "" : "▶"),
                h("div", { className: "jw-sample-cap" },
                  h("div", { className: "jw-sample-title" }, sItem.title || "Untitled"),
                  [sItem.studio, fmtDuration(sItem.duration), sItem.date].filter(Boolean).join(" · ")),
              ))),
        preview.rotationComplete === false
          ? h("p", { className: "jw-hint" }, "The scan bound (" + ROTATION_SCAN_LIMIT.toLocaleString() + " rows) capped this rotation.")
          : null,
      ) : null,
      previewState === "idle" ? h("p", { className: "jw-hint" }, "No preview yet.") : null,
    );

    // ---------- action bar ----------

    const dirty = isDirty();
    const barState = (() => {
      if (phase === "applying") {
        return h("span", { className: "jw-apply-state" }, "Applying… (edits you type now stay draft)");
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
      if (dirty) return h("span", { className: "jw-apply-state" }, "Unsaved draft — Apply to commit.");
      return h("span", { className: "jw-apply-state" }, "All changes applied.");
    })();

    const conflicting = applyError && applyError.kind === "conflict";

    const actionBar = h("div", { className: "jw-editor-actions" },
      h("div", { className: "jw-apply-row" },
        barState,
        h("span", { style: { flex: 1 } }),
        refreshNote ? h("span", { className: "jw-pill jw-pill-dirty", title: "The receipt's refresh map" }, refreshNote) : null,
        phase === "applied" && !dirty && !refreshNote ? h("span", { className: "jw-pill jw-pill-clean" }, "Ready") : null,
        h("button", { className: "jw-btn", onClick: discardDraft, disabled: !dirty && !isTemp || phase === "applying" }, "Discard"),
        h("button", {
          className: "jw-btn jw-btn-primary", id: "apply-btn",
          disabled: !dirty || invalid.length > 0 || phase === "applying" || phase === "validating",
          onClick: () => void doApply(),
        }, phase === "applying" ? "Applying…" : phase === "validating" ? "Validating…" : "Apply"),
      ),
      conflicting ? h("div", { className: "jw-apply-row", style: { marginTop: "8px" } },
        h("button", { className: "jw-btn", onClick: () => setApplyError(null) }, "Keep draft"),
        h("button", { className: "jw-btn jw-btn-primary", onClick: () => void doApply(applyError.currentRevision) },
          "Apply onto r" + applyError.currentRevision),
      ) : null,
    );

    // ---------- editor header + menu ----------

    const menuItems = [];
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
      swapOpen ? h(SwapDialog, {
        draft, lib, onClose: () => setSwapOpen(false),
        onPick: (c) => { void doSwap(c); },
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
      items.push({ type: "header", g, count: members.length });
      if (!(collapsed.has(g.id) && !searching)) {
        for (const c of members) items.push({ type: "ch", c });
      }
    }
    return items;
  }

  function DialRow({ ch, selected, bulkMode, checked, hasDraft, groupsById, onSelect, onCheck }) {
    return h("div", {
      className: "jw-dial-row" + (selected ? " jw-dial-row-selected" : "") + (ch.paused || ch.archived ? " jw-dial-row-muted" : ""),
      style: selected ? { boxShadow: "inset 3px 0 0 " + (ch.color || "#455A64") } : null,
      role: "option", "aria-selected": selected ? "true" : "false", tabIndex: 0,
      onClick: () => onSelect(ch.id),
      onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(ch.id); } },
    },
      bulkMode ? h("input", {
        type: "checkbox", "aria-label": "Select " + ch.name, checked,
        onClick: (e) => e.stopPropagation(),
        onChange: (e) => onCheck(ch.id, e.target.checked),
      }) : null,
      h("div", { className: "jw-dial-number" }, ch.number != null ? String(ch.number) : "—"),
      h(GlyphTile, { codepoint: ch.glyph, color: ch.color, size: 30, fallback: ch.number }),
      h("div", { className: "jw-dial-meta" },
        h("div", { className: "jw-dial-name" }, ch.number != null ? ch.number + " · " + ch.name : ch.name),
        h("div", { className: "jw-dial-sub" }, describeRow(ch, groupsById)),
      ),
      h("div", { className: "jw-dial-badges" },
        hasDraft ? h(StatusChip, { kind: "warn" }, "draft") : null,
        ch.archived ? h(StatusChip, { kind: "dim" }, "archived") : null,
        !ch.archived && ch.paused ? h(StatusChip, { kind: "dim" }, "paused") : null,
        ch.temp ? h(StatusChip, { kind: "accent" }, "new") : null,
      ),
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
    const [queryLive, setQueryLive] = useState("");
    const [groupFilter, setGroupFilter] = useState("");
    const [collapsed, setCollapsed] = useState(() => new Set());
    const [bulkMode, setBulkMode] = useState(false);
    const [bulkSelected, setBulkSelected] = useState(() => new Set());
    const [dialog, setDialog] = useState(null); // "groups" | "new"
    const [confirmSpec, setConfirmSpec] = useState(null);
    const [toasts, setToasts] = useState([]);
    const [draftCount, setDraftCount] = useState(0);
    const [editorKeyBump, setEditorKeyBump] = useState(0);
    const [reloadTick, setReloadTick] = useState(0);

    const draftsRef = useRef(null);
    const libRef = useRef(null);
    const searchInputRef = useRef(null);
    const editorPhaseRef = useRef({ phase: "clean", dirty: false });
    const toastSeq = useRef(0);

    libRef.current = lib;

    const toast = useCallback((message, kind) => {
      const id = ++toastSeq.current;
      setToasts((cur) => [...cur, { id, message, kind }]);
      setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== id)), kind === "err" ? 7000 : 4200);
    }, []);

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
          const data = await fetchLibrary();
          if (!alive) return;
          draftsRef.current = makeDraftStore(data.libraryId || "default");
          draftsRef.current.subscribe(() => setDraftCount(draftsRef.current.count()));
          setDraftCount(draftsRef.current.count());
          setLib(data);
          const first = (data.channels || [])[0];
          if (first) setSelectedId(first.id);
        } catch (e) {
          if (alive) setBootError(String((e && e.message) || e));
        }
      })();
      return () => { alive = false; };
    }, [fetchLibrary]);

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
        setLib(data);
        return data;
      } catch (e) {
        toast("Could not reload the library: " + String((e && e.message) || e), "err");
        return null;
      }
    }, [fetchLibrary, toast]);

    // ---- shared submit for NON-editor applies (bulk, groups, patches) ----
    const submitOps = useCallback(async (ops, opts) => {
      const options = opts || {};
      const requestId = newRequestId();
      const expected = libRef.current ? libRef.current.revision : 0;
      let receipt;
      try {
        receipt = await applyChannelChanges(requestId, expected, ops);
      } catch (e) {
        toast("Apply failed to reach the server (" + String((e && e.message) || e) + "). Nothing changed.", "err");
        return { status: "transport", error: "transport" };
      }
      if (receipt.status === "committed") {
        const affected = countAffected(ops);
        toast((options.label || "Applied") + (affected ? " (" + affected + " channel" + (affected === 1 ? "" : "s") + ")" : "")
          + " — committed at r" + receipt.revision + ".", "ok");
        await refreshLibrary();
      } else if (receipt.error === "revision_conflict") {
        toast("Revision conflict — the library moved to r" + receipt.currentRevision + ". Nothing changed; try again.", "err");
      } else if (receipt.error === "validation_failed" && receipt.errors && receipt.errors.length) {
        toast("Rejected: " + receipt.errors[0].message, "err");
      } else {
        toast("Rejected: " + (receipt.message || receipt.error || "unknown error"), "err");
      }
      return receipt;
    }, [refreshLibrary, toast]);

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
        setSelectedId(finalId);
        toast("Channel created as " + finalId + " at r" + receipt.revision + ".", "ok");
      }
    }, [refreshLibrary, toast]);

    const handleCreated = useCallback((tempId, discardedId) => {
      if (tempId) setSelectedId(tempId);
      else if (discardedId) {
        // temp draft discarded: select the first real channel
        const first = (libRef.current && libRef.current.channels || [])[0];
        setSelectedId(first ? first.id : null);
      }
    }, []);

    const reportPhase = useCallback((state) => { editorPhaseRef.current = state; }, []);

    const selectChannel = useCallback(async (id) => {
      if (id === selectedId) return;
      const ep = editorPhaseRef.current;
      if (ep.phase === "applying" || ep.phase === "validating") {
        const ok = await confirm({
          title: "An apply is in flight",
          message: "The current channel is being applied. Leaving now is safe — the receipt is polled in the background and your draft survives either way.",
          confirmLabel: "Leave now",
        });
        if (!ok) return;
      }
      setSelectedId(id);
    }, [selectedId, confirm]);

    // ---- dial data ----
    const applyQuery = useMemo(() => debounce((v) => setQuery(v), 140), []);
    const groupsSorted = lib ? (lib.groups || []).slice().sort((a, b) => a.position - b.position) : [];
    const groupsById = useMemo(() => new Map(groupsSorted.map((g) => [g.id, g])), [lib]);

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

    const allRows = useMemo(() => {
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

    const dialItems = useMemo(() => buildDialItems({
      channels: allRows, groups: groupsSorted, query, groupFilter, collapsed,
    }), [allRows, groupsSorted, query, groupFilter, collapsed]);

    const hasDraft = (id) => !!(draftsRef.current && draftsRef.current.get(id));

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
      if (createName) {
        // The server validates channels.move against EXISTING groups only, so
        // a new destination takes two applies: create the group, then move.
        const gid = newGroupId();
        const maxPos = Math.max(0, ...groupsSorted.map((g) => g.position));
        const first = await submitOps(
          [{ op: "group.put", group: { id: gid, name: createName, position: maxPos + 1 } }],
          { label: "Group created", silent: true },
        );
        if (first.status !== "committed") return;
        target = gid;
        await refreshLibrary();
      }
      await bulkAction((ids) => [{ op: "channels.move", channelIds: ids, groupId: target }],
        "Move to group", "will move", true);
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
      return h("div", { className: "jw-page" },
        h("h1", { className: "jw-title" }, "Channel Studio"),
        h("div", { className: "jw-missing-note", role: "alert" },
          "The channel library is not available on this deployment: " + bootError),
      );
    }

    if (!lib) {
      return h("div", { className: "jw-page" }, h("div", { className: "jw-loading" }, "Tuning the dial…"));
    }

    const dirtyPill = draftCount > 0
      ? h("span", { className: "jw-pill jw-pill-dirty", title: "Drafts persist for this browser session until applied or discarded" },
          draftCount + " unsaved draft" + (draftCount === 1 ? "" : "s"))
      : h("span", { className: "jw-pill jw-pill-clean" }, "no drafts");

    const reload = async () => {
      const ep = editorPhaseRef.current;
      if (ep.phase === "applying" || ep.phase === "validating") {
        toast("An apply is in flight — Reload is disabled until its receipt lands.", "err");
        return;
      }
      const data = await refreshLibrary();
      if (!data) return;
      if (editorPhaseRef.current.dirty) {
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
            h("span", { className: "jw-group-count" }, String(item.count)),
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
          onCheck: (id, checked) => setBulkSelected((cur) => {
            const next = new Set(cur);
            if (checked) next.add(id);
            else next.delete(id);
            return next;
          }),
        });
      },
    });

    const selected = selectedId || "";
    const editor = selected
      ? h(EditorPane, {
          key: selected + ":" + editorKeyBump,
          channelId: selected,
          lib,
          getRevision: () => (libRef.current ? libRef.current.revision : 0),
          drafts: draftsRef.current,
          confirm,
          toast,
          submitOps,
          onApplied: handleApplied,
          onCreated: handleCreated,
          reportPhase,
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
        h("button", { className: "jw-btn", onClick: () => setDialog("groups") }, "Groups…"),
        h("button", {
          className: "jw-btn" + (bulkMode ? " jw-btn-primary" : ""),
          "aria-pressed": bulkMode ? "true" : "false",
          onClick: () => { setBulkMode((v) => !v); setBulkSelected(new Set()); },
        }, bulkMode ? "Selecting… done" : "Select…"),
        h("button", { className: "jw-btn", onClick: exportCsv }, "Export CSV"),
        h("button", { className: "jw-btn", onClick: () => void reload(), title: "Refetch the library and revision" }, "Reload"),
        h("span", { className: "jw-revision-chip" }, "r" + lib.revision),
        dirtyPill,
      ),
      bulkMode ? h("div", { className: "jw-bulk-bar", role: "toolbar", "aria-label": "Bulk actions" },
        h("strong", null, bulkSelected.size + " selected"),
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
      h("main", { className: "jw-columns" },
        h("section", { className: "jw-dial", "aria-label": "Channel dial" },
          h("div", { className: "jw-dial-tools" },
            h("button", { className: "jw-btn jw-btn-primary jw-new-channel", onClick: () => setDialog("new") }, "+ New channel…"),
          ),
          dialItems.length === 0
            ? h("div", { className: "jw-empty" },
                h("div", { className: "jw-empty-title" }, "Nothing matches"),
                h("div", { className: "jw-empty-sub" }, "Adjust the search, or create a channel."))
            : dialList,
        ),
        editor || h("div", { className: "jw-editor jw-editor-empty" },
          h("div", { className: "jw-empty-title" }, "Nothing selected"),
          h("div", { className: "jw-empty-sub" }, "Pick a channel on the dial."),
        ),
      ),
      dialog === "groups"
        ? h(GroupsManagerDialog, { lib, drafts: draftsRef.current, onClose: () => setDialog(null), submitOps })
        : null,
      dialog === "new"
        ? h(NewChannelDialog, {
            lib, drafts: draftsRef.current, onClose: () => setDialog(null),
            onCreate: (tempId) => { setDialog(null); handleCreated(tempId); },
          })
        : null,
      h(ConfirmDialog, { spec: confirmSpec }),
      h(Toasts, { items: toasts, onDismiss: (id) => setToasts((cur) => cur.filter((t) => t.id !== id)) }),
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
      // UtilityItems is the nav group Stash always renders (top-right, next to
      // Statistics/Settings); MenuItems collapses on narrow layouts.
      api.patch.before("MainNavBar.UtilityItems", function (props) {
        try {
          if (!props || typeof props !== "object") return [{}];
          const existing = props.children != null ? props.children : null;
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
