// Just Watch — Channel Studio.
//
// Registers `/plugin/stash-justwatch` through Stash's PluginApi route surface
// and renders the editor for the TV app's custom channels (1-99):
//
//   Master-detail: the left rail IS the dial (guide-row anatomy: number,
//   glyph tile in brand color, name + summary). The editor pane edits the
//   selected channel. Creation and source-change share one sheet with
//   always-visible grouped results. Everything autosaves; no Save button.
//
// Language is the TV fiction: lineup, airing from, play order, on air,
// off air, loops. Never "filters", "JSON", or "plugin operations".
//
// Design invariants (stash-tag-curator precedent):
//   * XSS: no raw-HTML injection anywhere; server strings are React children.
//   * No second React: React + libraries come from PluginApi.
//   * Writes dispatch through Stash's job queue (runPluginTask); the result
//     is read back from the plugin's save_result.json side-channel, matched
//     by a client-generated requestId.

(() => {
  const api = window.PluginApi;
  if (!api || !api.React || !api.register || typeof api.register.route !== "function") {
    console.warn("[stash-justwatch] PluginApi with React + register.route unavailable; UI not registered.");
    return;
  }

  const React = api.React;
  const h = React.createElement;
  const { useState, useEffect, useRef, useCallback } = React;

  // ------------------------------------------------------------------
  // Constants
  // ------------------------------------------------------------------

  const ROUTE_PATH = "/plugins/stash-justwatch";
  // Legacy path kept registered: early bookmarks/links used it. The server
  // only serves the app shell for /plugins/* (its /plugin mount owns assets
  // and /javascript), so the plural path is the only deep-linkable one.
  const LEGACY_ROUTE_PATH = "/plugin/stash-justwatch";
  const ASSET_BASE = "/plugin/stash-justwatch/assets/";
  const POLL_INTERVAL_MS = 800;
  const AUTOSAVE_DEBOUNCE_MS = 700;
  const SEARCH_DEBOUNCE_MS = 300;
  const PREVIEW_COUNT = 10;
  const THIN_LINEUP = 10;

  const SORTS = [
    { key: "shuffle", label: "Shuffle" },
    { key: "newest", label: "Newest" },
    { key: "oldest", label: "Oldest" },
    { key: "top_rated", label: "Top rated" },
    { key: "longest", label: "Longest" },
    { key: "shortest", label: "Shortest" },
  ];

  const SOURCE_KINDS = {
    savedFilter: { label: "Custom lineup", glyph: "\uf02d" },
    studio: { label: "Studio", glyph: "\uf1ad" },
    tag: { label: "Tag", glyph: "\uf02c" },
    performer: { label: "Performer", glyph: "\uf007" },
  };

  // A tag channel airs from a SET of tags (any-of union). Canonical shape:
  // {type:"tag", id:<first>, ids:[sorted]} — mirrors the server's normalize.
  // Names resolve lazily (this Stash has no ids filter on findTags); null
  // marks a tag deleted in Stash, known only after it has been looked up.
  const TAG_NAMES = new Map();

  function tagIdsOf(source) {
    const src = source || {};
    if (src.type !== "tag") return [];
    const pool = [...new Set((Array.isArray(src.ids) ? src.ids : []).map(String).filter((x) => /^\d+$/.test(x)))];
    if (pool.length) return pool.sort((a, b) => Number(a) - Number(b));
    return /^\d+$/.test(String(src.id || "")) ? [String(src.id)] : [];
  }

  function tagSource(ids) {
    const clean = [...new Set(ids.map(String))].sort((a, b) => Number(a) - Number(b));
    return { type: "tag", id: clean[0], ids: clean };
  }

  function joinLabels(names) {
    return names.length <= 2 ? names.join(", ") : names.slice(0, 2).join(", ") + " +" + (names.length - 2);
  }

  async function fetchTagNames(ids) {
    const missing = [...new Set(ids)].filter((id) => !TAG_NAMES.has(id));
    await Promise.all(missing.map(async (id) => {
      try {
        const r = await gql("query JwTagName($id: ID!) { findTag(id: $id) { name } }", { id });
        TAG_NAMES.set(id, ((r || {}).findTag || {}).name || null);
      } catch (e) {
        TAG_NAMES.set(id, "#" + id);
      }
    }));
  }

  // Curated brand palette (one row of swatches; a custom color is allowed too).
  const PALETTE = [
    "#E91E63", "#D32F2F", "#F57C00", "#F9A825", "#AFB42B", "#388E3C",
    "#00897B", "#00ACC1", "#3949AB", "#5E35B1", "#7B1FA2", "#455A64",
  ];

  // The shared glyph set from the plugin contract (the TV dial's brand
  // glyphs). DEFAULT_GLYPHS is the quick-pick pool for new channels.
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
  const DEFAULT_GLYPHS = [
    "\uf111", "\uf005", "\uf008", "\uf06d", "\uf1b0", "\uf236",
    "\uf3a5", "\uf4d8", "\uf5e4", "\uf0a1", "\uf11b", "\uf1da",
  ];

  // Section glyphs mirror the TV app's auto-channel branding; auto channels use
  // the neutral slate so the owner's custom channels stay visually special.
  const SECTION_GLYPHS = { general: "\uf02b", studios: "\uf1ad", performers: "\uf007" };
  const AUTO_SLATE = "#546E7A";
  const GROUP_SLATE = "#455A64";

  const TERMINAL_STATUSES = new Set([
    "FINISHED", "COMPLETE", "COMPLETED", "FAILED", "CANCELLED", "CANCELED", "REMOVED", "ABORTED",
  ]);

  // ------------------------------------------------------------------
  // Transport
  // ------------------------------------------------------------------

  async function gql(query, variables) {
    const resp = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ query, variables: variables || {} }),
    });
    const body = await resp.json();
    if (body.errors && body.errors.length) {
      throw new Error(body.errors[0].message || "GraphQL error");
    }
    return body.data;
  }

  // runPluginOperation with an explicitly-typed Map: absent keys arrive as
  // null and the plugin treats them as "not provided".
  async function runOp(mode, args) {
    const a = args || {};
    const data = await gql(
      "mutation JwOp($mode: String!, $channelId: String, $page: String, $perPage: String, $channel: String, $catalog: String, $expectedRevision: String, $requestId: String) " +
      '{ runPluginOperation(plugin_id: "stash-justwatch", args: {mode: $mode, channelId: $channelId, page: $page, perPage: $perPage, channel: $channel, catalog: $catalog, expectedRevision: $expectedRevision, requestId: $requestId}) }',
      {
        mode,
        channelId: a.channelId != null ? String(a.channelId) : null,
        page: a.page != null ? String(a.page) : null,
        perPage: a.perPage != null ? String(a.perPage) : null,
        channel: a.channel != null ? String(a.channel) : null,
        catalog: a.catalog != null ? String(a.catalog) : null,
        expectedRevision: a.expectedRevision != null ? String(a.expectedRevision) : null,
        requestId: a.requestId != null ? String(a.requestId) : null,
      },
    );
    return data.runPluginOperation;
  }

  async function runTask(taskName, args) {
    const flat = {};
    Object.keys(args || {}).forEach((k) => { flat[k] = String(args[k]); });
    const data = await gql(
      "mutation JwTask($name: String!, $a: Map!) { runPluginTask(plugin_id: \"stash-justwatch\", task_name: $name, args_map: $a) }",
      { name: taskName, a: flat },
    );
    return data.runPluginTask; // job id
  }

  // Bounded: a job that never reaches a terminal state leaves an actionable
  // "save failed — retry" state, not an endless Saving indicator.
  const JOB_POLL_MAX = 150; // ~2 min at POLL_INTERVAL_MS

  async function pollJob(jobId) {
    for (let i = 0; i < JOB_POLL_MAX; i++) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      let data = null;
      try {
        data = await gql(
          "query JwJob($input: FindJobInput!) { findJob(input: $input) { status } }",
          { input: { id: jobId } },
        );
      } catch (e) {
        continue; // transient query failure: keep waiting for the job
      }
      const job = (data || {}).findJob;
      if (job == null) return "FINISHED"; // pruned from the queue = done
      const status = String(job.status || "").toUpperCase();
      if (TERMINAL_STATUSES.has(status)) return status;
    }
    throw new Error("the save is taking unusually long — you can retry");
  }

  // Results are stored per requestId by the plugin (a small retention-capped
  // list), so two tabs saving close together each find their own outcome
  // instead of whoever wrote last.
  async function readSaveResult(requestId, attempts) {
    for (let i = 0; i < (attempts || 12); i++) {
      try {
        const resp = await fetch(ASSET_BASE + "snapshots/save_result.json?r=" + Date.now(), {
          credentials: "same-origin",
        });
        if (resp.ok) {
          const body = await resp.json();
          const results = Array.isArray(body && body.results) ? body.results : [];
          const match = results.find((r) => r && r.requestId === requestId);
          if (match) return match;
        }
      } catch (e) { /* retry */ }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
    throw new Error("the server never reported the save result");
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
      // IconDefinition shape: {prefix, iconName, icon: [w, h, ligatures, unicode, path]}
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
    const key = fasKeyByCodepoint.get(codepoint.codePointAt(0));
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
    }, codepoint);
  }

  function GlyphTile({ codepoint, color, size }) {
    const s = size || 28;
    return h("div", {
      className: "jw-glyph-tile",
      style: {
        width: s + "px", height: s + "px", borderRadius: Math.max(6, s / 5) + "px",
        background: color || "#455A64",
      },
    }, h(Glyph, { codepoint, size: Math.round(s * 0.5) }));
  }

  // ------------------------------------------------------------------
  // Formatting helpers
  // ------------------------------------------------------------------

  function formatCount(n) {
    if (n == null) return "";
    return n.toLocaleString() + (n === 1 ? " scene" : " scenes");
  }

  function formatLoop(seconds) {
    if (seconds == null) return "";
    const hours = seconds / 3600;
    if (hours >= 1) return "~" + (Math.round(hours * 10) / 10) + " h before it loops";
    return "~" + Math.max(1, Math.round(seconds / 60)) + " min before it loops";
  }

  function lowestFreeNumber(channels) {
    const taken = new Set(channels.map((c) => c.number));
    for (let n = 1; n <= 99; n++) if (!taken.has(n)) return n;
    return null;
  }

  function pickDefaultGlyph(name) {
    let hash = 0;
    for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
    return DEFAULT_GLYPHS[hash % DEFAULT_GLYPHS.length];
  }

  function newChannelId() {
    const bytes = new Uint8Array(4);
    (window.crypto || { getRandomValues: (b) => b.map((_, i) => (Math.random() * 256) | 0).forEach((v, i) => { b[i] = v; }) })
      .getRandomValues(bytes);
    return "ch_" + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function summarize(channel) {
    const src = channel.source || {};
    const kind = SOURCE_KINDS[src.type] || { label: "" };
    const ids = tagIdsOf(src);
    const kindLabel = ids.length > 1 ? "Tags" : kind.label;
    const base = kind.label === "Custom lineup"
      ? (channel.sourceLabel || "Custom lineup")
      : kindLabel + " · " + (channel.sourceLabel || "?");
    if (channel.sourceMissing) return base + " · lineup missing";
    const missing = ids.filter((id) => TAG_NAMES.get(id) === null).length;
    const missingNote = !channel.sourceMissing && missing
      ? " · " + missing + (missing === 1 ? " tag missing" : " tags missing") : "";
    if (channel.sceneCount != null) return base + missingNote + " · " + formatCount(channel.sceneCount);
    return base + missingNote;
  }

  // ------------------------------------------------------------------
  // API surface used by components
  // ------------------------------------------------------------------

  function loadCatalog() {
    return runOp("GetCatalog");
  }

  function loadFullDirectory() {
    return runOp("FullDirectory");
  }

  function previewLineup(channel, perPage) {
    return runOp("PreviewLineup", {
      channel: JSON.stringify(channel),
      perPage: String(perPage || PREVIEW_COUNT),
    });
  }

  let saveSeq = 0;

  function newRequestId() {
    // Random UUID: safe across tabs and sessions, unlike wall-clock sequences.
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return "req-" + window.crypto.randomUUID();
    }
    return "req-" + Date.now() + "-" + Math.random().toString(36).slice(2) + "-" + (++saveSeq);
  }

  // After a successful save the server has refreshed labels + health counts,
  // so re-pull the catalog instead of letting local guesses linger. The
  // caller decides what to adopt — a newer local draft is never clobbered.
  async function commitAndReload(catalog, expectedRevision) {
    const requestId = newRequestId();
    const jobId = await runTask("Save Channel Edit", {
      mode: "SaveCatalog",
      catalog: JSON.stringify(catalog),
      expectedRevision: String(expectedRevision),
      requestId,
    });
    const status = await pollJob(jobId);
    if (status === "FAILED" || status === "ABORTED" || status === "CANCELLED" || status === "CANCELED") {
      throw new Error("the server reported the save failed");
    }
    const result = await readSaveResult(requestId);
    if (result.saved) {
      try { return { result, fresh: await loadCatalog() }; } catch (e) { return { result, fresh: null }; }
    }
    return { result, fresh: null };
  }

  async function fetchSources(q) {
    const filter = { q: q || undefined, per_page: 8, sort: "scenes_count", direction: "DESC" };
    const [saved, tags, performers, studios] = await Promise.all([
      gql("query JwSaved { findSavedFilters(mode: SCENES) { id name } }").catch(() => null),
      gql("query JwTags($f: FindFilterType!) { findTags(filter: $f) { tags { id name scene_count } } }", { f: filter }).catch(() => null),
      gql("query JwPerformers($f: FindFilterType!) { findPerformers(filter: $f) { performers { id name scene_count } } }", { f: filter }).catch(() => null),
      gql("query JwStudios($f: FindFilterType!) { findStudios(filter: $f) { studios { id name scene_count } } }", { f: filter }).catch(() => null),
    ]);
    const savedRows = ((saved || {}).findSavedFilters || [])
      .filter((r) => !q || r.name.toLowerCase().includes(q.toLowerCase()))
      .slice(0, 8)
      .map((r) => ({ type: "savedFilter", id: String(r.id), name: r.name, count: null }));
    const norm = (rows, type) => (rows || []).map((r) => ({
      type, id: String(r.id), name: r.name, count: r.scene_count != null ? r.scene_count : null,
    }));
    return {
      savedFilters: savedRows,
      tags: norm((((tags || {}).findTags || {}).tags) || [], "tag"),
      performers: norm((((performers || {}).findPerformers || {}).performers) || [], "performer"),
      studios: norm((((studios || {}).findStudios || {}).studios) || [], "studio"),
    };
  }

  // ------------------------------------------------------------------
  // Components
  // ------------------------------------------------------------------

  function SaveIndicator({ state, onRetry }) {
    if (state === "saving") return h("span", { className: "jw-save-indicator" }, "Saving…");
    if (state === "error") {
      return h("span", { className: "jw-save-indicator jw-save-error" },
        "Save failed — your changes are kept. ",
        h("button", { className: "jw-link", onClick: onRetry }, "Retry now"),
      );
    }
    if (state === "saved") return h("span", { className: "jw-save-indicator" }, "Saved");
    return null;
  }

  function RailRow({ row, selected, onSelect }) {
    return h("div", {
      className: "jw-rail-row" + (selected ? " jw-selected" : "") + (row.paused ? " jw-row-paused" : ""),
      style: selected ? { boxShadow: "inset 3px 0 0 " + row.color } : null,
      onClick: () => onSelect(row.sel),
      onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(row.sel); } },
      tabIndex: 0,
      role: "button",
    },
      h("div", { className: "jw-rail-number", style: selected ? { color: row.color } : null }, row.number != null ? String(row.number) : "—"),
      h(GlyphTile, { codepoint: row.glyph, color: row.color }),
      h("div", { className: "jw-rail-body" },
        h("div", { className: "jw-rail-name" }, row.name),
        h("div", { className: "jw-rail-sub" }, row.sub),
      ),
      row.badges && row.badges.length ? h("div", { className: "jw-rail-badges" }, row.badges) : null,
    );
  }

  /** The whole lineup: My Channels (editable) + the TV's automatic sections. */
  function buildRailSections(catalog, fullDir) {
    const sections = [];
    const customRows = (catalog.channels || [])
      .slice()
      .sort((a, b) => a.number - b.number)
      .map((channel) => {
        const badges = [];
        if (channel.sourceMissing) {
          badges.push(h("span", { key: "m", className: "jw-badge jw-badge-missing", title: "This lineup's source was deleted in Stash. Relink it." }, "relink"));
        } else if (channel.sceneCount === 0) {
          badges.push(h("span", { key: "o", className: "jw-badge jw-badge-off" }, "off air"));
        } else if (channel.sceneCount != null && channel.sceneCount < THIN_LINEUP) {
          badges.push(h("span", { key: "t", className: "jw-badge jw-badge-thin", title: "A lineup this small repeats all evening." }, "thin"));
        }
        if (!channel.enabled) badges.push(h("span", { key: "d", className: "jw-badge jw-badge-off" }, "paused"));
        return {
          sel: channel.id,
          number: channel.number,
          name: channel.name,
          glyph: channel.glyph,
          color: channel.color,
          sub: summarize(channel),
          badges,
          channel,
        };
      });
    sections.push({ label: "My Lineup", rows: customRows });

    if (fullDir) {
      const autoRow = (row, section) => {
        const badges = [];
        if (row.offAir) badges.push(h("span", { key: "o", className: "jw-badge jw-badge-off" }, "off air"));
        else if (row.count === 0 && row.count != null) badges.push(h("span", { key: "o", className: "jw-badge jw-badge-off" }, "off air"));
        else if (row.count != null && row.count < THIN_LINEUP) badges.push(h("span", { key: "t", className: "jw-badge jw-badge-thin", title: "A lineup this small repeats all evening." }, "thin"));
        const countText =
          row.count != null ? formatCount(row.count)
            : row.members != null ? row.members + (row.members === 1 ? " member" : " members")
              : "";
        const group = row.kind && /Group|Spillover$/.test(row.kind);
        return {
          sel: "auto:" + section + ":" + row.number + ":" + row.name,
          number: row.number,
          name: row.name,
          glyph: SECTION_GLYPHS[section] || "\uf111",
          color: group ? GROUP_SLATE : AUTO_SLATE,
          sub: countText ? countText + " · automatic" : "automatic",
          badges,
          auto: { section, row },
        };
      };
      sections.push({
        label: "General",
        note: fullDir.curatedDialNote,
        rows: (fullDir.general && fullDir.general.tagChannels || []).map((r) => autoRow(r, "general")),
      });
      sections.push({
        label: "Studios",
        rows: (fullDir.studios && fullDir.studios.channels || []).map((r) => autoRow(r, "studios")),
      });
      sections.push({
        label: "Performers",
        rows: (fullDir.performers && fullDir.performers.channels || []).map((r) => autoRow(r, "performers")),
      });
    }
    return sections;
  }

  function DialRail({ railSections, selectedId, onSelect, onNew, channelCount }) {
    return h("div", { className: "jw-rail" },
      h("div", { className: "jw-rail-head" },
        h("span", { className: "jw-section-label" }, "My Lineup"),
        h("span", { className: "jw-rail-count" }, channelCount + (channelCount === 1 ? " channel" : " channels")),
      ),
      h("button", { className: "jw-btn jw-btn-primary jw-new-channel", onClick: onNew }, "+ New channel"),
      railSections.every((s) => s.rows.length === 0) && !railSections.some((s) => s.note)
        ? h("div", { className: "jw-empty" },
            h("div", { className: "jw-empty-title" }, "Your dial starts here"),
            h("div", { className: "jw-empty-sub" }, "Channels you create here air on numbers 1–99, ahead of the built-in dial on your TV."),
          )
        : railSections.map((section) =>
            section.rows.length === 0 && !section.note
              ? null
              : h("div", { key: section.label, className: "jw-rail-section" },
                  h("div", { className: "jw-rail-section-label" }, section.label),
                  section.note ? h("div", { className: "jw-rail-note" }, section.note) : null,
                  h("div", { className: "jw-rail-list" },
                    section.rows.map((row) =>
                      h(RailRow, { key: row.sel, row, selected: row.sel === selectedId, onSelect })),
                  ),
                ),
          ),
    );
  }

  function NumberStepper({ channel, channels, onAssign }) {
    const [raw, setRaw] = useState(String(channel.number));
    useEffect(() => { setRaw(String(channel.number)); }, [channel.id, channel.number]);

    const parsed = parseInt(raw, 10);
    const valid = parsed >= 1 && parsed <= 99;
    const occupant = valid && parsed !== channel.number
      ? channels.find((c) => c.id !== channel.id && c.number === parsed) : null;

    const step = (delta) => {
      const taken = new Set(channels.filter((c) => c.id !== channel.id).map((c) => c.number));
      let n = channel.number;
      do { n += delta; } while (n >= 1 && n <= 99 && taken.has(n));
      if (n >= 1 && n <= 99) onAssign(n, null);
    };

    return h("div", { className: "jw-number-stepper" },
      h("div", { className: "jw-number-line" },
        h("button", { className: "jw-btn jw-btn-ghost jw-step", onClick: () => step(-1), title: "Previous free number" }, "–"),
        h("input", {
          className: "jw-number-input" + (!valid ? " jw-input-bad" : ""),
          value: raw,
          onChange: (e) => setRaw(e.target.value.replace(/[^0-9]/g, "").slice(0, 2)),
          onBlur: () => {
            if (valid && parsed !== channel.number && !occupant) onAssign(parsed, null);
            else setRaw(String(channel.number));
          },
          onKeyDown: (e) => { if (e.key === "Enter") e.target.blur(); },
        }),
        h("button", { className: "jw-btn jw-btn-ghost jw-step", onClick: () => step(1), title: "Next free number" }, "+"),
      ),
      occupant
        ? h("div", { className: "jw-swap-note" },
            h("span", null, parsed + " is " + occupant.name + ". "),
            h("button", { className: "jw-link", onClick: () => { onAssign(parsed, occupant.id); setRaw(String(parsed)); } }, "Swap channels"),
          )
        : (!valid ? h("div", { className: "jw-swap-note" }, "Channel numbers run 1–99.") : null),
    );
  }

  function OnAirStrip({ channel, isDraft }) {
    const [state, setState] = useState({ loading: true, items: null, total: null, sourceTotal: null, error: null });
    const spec = JSON.stringify(channel);

    useEffect(() => {
      let alive = true;
      setState({ loading: true, items: null, total: null, sourceTotal: null, error: null });
      const t = setTimeout(async () => {
        try {
          const result = await previewLineup(JSON.parse(spec), PREVIEW_COUNT + 1);
          if (!alive) return;
          setState({
            loading: false, items: result.items || [], total: result.total,
            sourceTotal: result.sourceTotal, error: null,
          });
        } catch (e) {
          if (alive) setState({ loading: false, items: null, total: null, sourceTotal: null, error: String((e && e.message) || e) });
        }
      }, 250);
      return () => { alive = false; clearTimeout(t); };
    }, [spec]);

    const missing = channel.sourceMissing && !isDraft;
    // The preview count IS the rotation: the same bounded loop the TV plays,
    // not the size of the library behind it (that's shown separately).
    const countBits = [];
    if (!state.loading && !missing) {
      if (state.total != null) countBits.push(formatCount(state.total) + " in rotation");
      if (!isDraft && state.sourceTotal != null && state.sourceTotal > state.total) {
        countBits.push(formatCount(state.sourceTotal) + " in your library");
      }
      if (!isDraft && channel.loopSeconds != null) countBits.push(formatLoop(channel.loopSeconds));
      if (!isDraft && channel.loopCapped) countBits.push("sampled from a very long lineup");
    }
    return h("div", { className: "jw-editor-section" },
      h("div", { className: "jw-onair-head" },
        h("span", { className: "jw-section-label" }, "On air tonight"),
        h("span", { className: "jw-onair-meta" }, countBits.join(" · ")),
      ),
      missing
        ? h("div", { className: "jw-missing-note" }, "This lineup's source was deleted in Stash. Use “Airing from” above to relink it.")
        : state.loading
          ? h("div", { className: "jw-thumb-strip" }, [0, 1, 2, 3].map((i) => h("div", { key: i, className: "jw-thumb jw-thumb-skeleton" })))
          : state.error
            ? h("div", { className: "jw-missing-note" }, "Could not load a preview: " + state.error)
            : (state.items || []).length === 0
              ? h("div", { className: "jw-missing-note" }, "Nothing matches this lineup yet — it goes live as the library grows.")
              : h("div", { className: "jw-thumb-strip" },
                  state.items.slice(0, PREVIEW_COUNT).map((item, i) => h("div", { key: item.id, className: "jw-thumb-wrap" },
                    i === 0 ? h("span", { className: "jw-now-badge" }, "NOW") : null,
                    h("div", {
                      className: "jw-thumb",
                      style: item.preview ? { backgroundImage: "url('" + item.preview + "')" } : null,
                    }),
                  )),
                  state.total != null && state.total > PREVIEW_COUNT
                    ? h("div", { className: "jw-thumb-more" }, "+" + (state.total - PREVIEW_COUNT).toLocaleString())
                    : null,
                ),
    );
  }

  function GlyphPicker({ current, onPick, onClose }) {
    const ref = useRef(null);
    useEffect(() => {
      const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
      document.addEventListener("mousedown", onDoc);
      return () => document.removeEventListener("mousedown", onDoc);
    }, [onClose]);
    return h("div", { className: "jw-glyph-pop", ref },
      h("div", { className: "jw-glyph-grid" },
        GLYPH_POOL.map((g) => h("button", {
          key: g,
          className: "jw-glyph-cell" + (g === current ? " jw-glyph-active" : ""),
          title: glyphName(g),
          onClick: () => onPick(g),
        }, h(Glyph, { codepoint: g, size: 16 }))),
      ),
    );
  }

  function ProgrammingControls({ channel, onPatch }) {
    const p = channel.programming || { mode: "fixed", spacing: 0, repeatHours: 0, spotlight: "none", spotlightDay: 5, spotlightHour: 20 };
    const patch = (values) => onPatch({ programming: Object.assign({}, p, values) });
    return h("div", { className: "jw-editor-section" },
      h("div", { className: "jw-section-label" }, "Keep the channel moving"),
      h("div", { className: "jw-chip-row" }, [
        ["fixed", "Favorite rotation"], ["explore", "Explore the library"], ["discovery", "Discovery"]
      ].map(([mode, label]) => h("button", { key: mode, className: "jw-chip" + (p.mode === mode ? " jw-chip-active" : ""),
        onClick: () => patch({ mode }) }, label))),
      h("p", { className: "jw-settings-hint" }, p.mode === "fixed"
        ? "A familiar rotation of up to 50 scenes, on repeat."
        : p.mode === "discovery" ? "Give overlooked scenes a turn. Uses scheduled airtime, never your watch history."
        : "A continuing schedule through this source. Every scene gets a turn before the next pass."),
      p.mode !== "fixed" ? h("div", { className: "jw-programming" },
        h("label", { className: "jw-field" }, "Space performers and studios",
          h("select", { className: "jw-search", value: p.spacing || 0, onChange: e => patch({ spacing: Number(e.target.value) }) },
            [0, 1, 2, 3, 5].map(n => h("option", { key: n, value: n }, n ? n + (n === 1 ? " program apart when possible" : " programs apart when possible") : "Follow play order")))),
        h("label", { className: "jw-field" }, "Prefer no repeats within",
          h("select", { className: "jw-search", value: p.repeatHours || 0, onChange: e => patch({ repeatHours: Number(e.target.value) }) },
            [0, 12, 24, 48, 72, 168].map(n => h("option", { key: n, value: n }, n ? n + " hours" : "One complete library pass")))),
        h("label", { className: "jw-field" }, "Weekly double feature",
          h("select", { className: "jw-search", value: p.spotlight || "none", onChange: e => patch({ spotlight: e.target.value }) },
            [["none", "No spotlight"], ["studio", "Studio spotlight"], ["performer", "Performer double feature"]].map(([v, label]) => h("option", { key: v, value: v }, label)))),
        p.spotlight !== "none" ? h("div", { className: "jw-chip-row" },
          h("select", { className: "jw-search", "aria-label": "Spotlight day in UTC", value: p.spotlightDay == null ? 5 : p.spotlightDay, onChange: e => patch({ spotlightDay: Number(e.target.value) }) },
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => h("option", { key: d, value: i }, d))),
          h("select", { className: "jw-search", "aria-label": "Spotlight hour in UTC", value: p.spotlightHour == null ? 20 : p.spotlightHour, onChange: e => patch({ spotlightHour: Number(e.target.value) }) },
            Array.from({ length: 24 }, (_, i) => h("option", { key: i, value: i }, String(i).padStart(2, "0") + ":00 UTC")))) : null,
      ) : null);
  }

  function PublishedSchedule({ channel }) {
    const [tomorrow, setTomorrow] = useState(false);
    const [data, setData] = useState(null);
    const [desk, setDesk] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const spec = JSON.stringify([channel.id, channel.programming, channel.sort, channel.source, channel.programmingVersion]);
    useEffect(() => {
      let alive = true;
      setData(null); setError(null);
      const at = Date.now() + (tomorrow ? 86400000 : 0);
      Promise.all([
        gql('mutation($a: Map!) { runPluginOperation(plugin_id: "stash-justwatch", args: $a) }', { a: { mode: "Schedule", channelId: channel.id, at: String(at), limit: "20" } }),
        runOp("ProgrammingDesk"),
      ]).then(([result, overview]) => { if (alive) { setData(result.runPluginOperation); setDesk(overview); } })
        .catch(e => { if (alive) setError(e.message); });
      return () => { alive = false; };
    }, [spec, tomorrow, refresh]);
    if (!channel.programming || channel.programming.mode === "fixed") return null;
    const summary = desk && (desk.channels || []).find(c => c.id === channel.id);
    const prepare = async () => {
      setBusy(true); setError(null);
      try {
        const id = await runTask("Prepare Programming", { mode: "PrepareProgramming", channelId: channel.id });
        const status = await pollJob(id);
        if (status !== "FINISHED" && status !== "COMPLETE" && status !== "COMPLETED") throw new Error("Programming could not be prepared. Try again.");
        setRefresh(v => v + 1);
      } catch (e) { setError(e.message); } finally { setBusy(false); }
    };
    return h("div", { className: "jw-editor-section jw-published" },
      h("div", { className: "jw-section-label" }, "Your programming desk"),
      h("div", { className: "jw-chip-row" },
        h("button", { className: "jw-chip" + (!tomorrow ? " jw-chip-active" : ""), onClick: () => setTomorrow(false) }, "On now"),
        h("button", { className: "jw-chip" + (tomorrow ? " jw-chip-active" : ""), onClick: () => setTomorrow(true) }, "Tomorrow"),
        h("button", { className: "jw-chip", disabled: busy, onClick: prepare }, busy ? "Preparing…" : "Prepare upcoming programming")),
      error ? h("p", { role: "alert", className: "jw-missing-note" }, error) : null,
      summary ? h("p", { className: "jw-settings-hint" }, formatCount(summary.sourceTotal) + " in this source · " + summary.scheduledUnique + " have a place in the schedule · Ready through " + new Date(summary.preparedThrough).toLocaleString()) : null,
      summary && summary.overlap && summary.overlap.length ? h("p", { className: "jw-settings-hint" }, "Shared programming: " + summary.overlap.map(o => o.percent + "% also belongs to " + o.name).join(" · ")) : null,
      data && (data.warnings || []).map(w => h("p", { key: w, className: "jw-settings-hint" }, w)),
      data && data.status === "preparing" ? h("p", null, "Your first schedule is being prepared. Save your channel, then prepare upcoming programming.") : null,
      data && data.status === "repeat" ? h("p", { className: "jw-settings-hint" }, "Encore programming is airing until the next schedule is ready.") : null,
      h("div", { className: "jw-airings" }, data && (data.programs || []).map(p => h("div", { className: "jw-airing", key: p.airingId },
        h("time", null, new Date(p.startEpochMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
        h("div", null, h("strong", null, p.item.title || "Untitled"),
          h("div", { className: "jw-settings-hint" }, [p.block, p.item.studio, Math.round((p.endEpochMs - p.startEpochMs) / 60000) + " min"].filter(Boolean).join(" · ")))))),
    );
  }

  function ProgrammingTrial({ channel, onApply, onClose }) {
    const [draft, setDraft] = useState(() => JSON.parse(JSON.stringify(channel)));
    const [result, setResult] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    const [tested, setTested] = useState(null);
    const ref = useRef(null);
    useEffect(() => {
      const previous = document.activeElement;
      ref.current.querySelector("button").focus();
      const keys = e => {
        if (e.key === "Escape") { e.preventDefault(); onClose(); }
        if (e.key === "Tab") {
          const nodes = Array.from(ref.current.querySelectorAll("button:not(:disabled), select"));
          const first = nodes[0], last = nodes[nodes.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      };
      document.addEventListener("keydown", keys);
      return () => { document.removeEventListener("keydown", keys); if (previous && previous.isConnected) previous.focus(); };
    }, []);
    const preview = async () => {
      setBusy(true); setError(null);
      try {
        const answer = await runOp("PreviewProgramming", { channel: JSON.stringify(draft) });
        setResult(answer); setTested(JSON.stringify(draft.programming));
      } catch (e) { setError(e.message); } finally { setBusy(false); }
    };
    return h("div", { className: "jw-overlay" }, h("div", { className: "jw-sheet", ref, role: "dialog", "aria-modal": true, "aria-label": "Try different programming" },
      h("div", { className: "jw-sheet-head" }, h("strong", null, "Try different programming"), h("button", { className: "jw-btn", onClick: onClose }, "Close")),
      h("p", { className: "jw-settings-hint" }, "Experiment here before changing what airs. The current program always finishes."),
      h(ProgrammingControls, { channel: draft, onPatch: patch => setDraft(Object.assign({}, draft, patch)) }),
      h("button", { className: "jw-btn", disabled: busy || draft.programming.mode === "fixed", onClick: preview }, busy ? "Preparing preview…" : "Preview this programming"),
      error ? h("p", { role: "alert" }, error) : null,
      result && result.message ? h("p", null, result.message) : null,
      result && result.effectiveAt ? h("p", { className: "jw-settings-hint" }, "This preview changes programming from " + new Date(result.effectiveAt).toLocaleString() + ". Playback continues through the published boundary.") : null,
      result && (result.warnings || []).map(w => h("p", { key: w }, w)),
      h("div", { className: "jw-airings" }, result && (result.programs || []).slice(0,8).map(p => h("div", { className: "jw-airing", key: p.airingId },
        h("time", null, new Date(p.startEpochMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), h("span", null, p.item.title || "Untitled")))),
      h("div", { className: "jw-sheet-foot" }, h("button", { className: "jw-btn jw-btn-primary", disabled: !result || result.status !== "preview" || tested !== JSON.stringify(draft.programming),
        onClick: () => { onApply({ programming: draft.programming }); onClose(); } }, "Use this programming")),
    ));
  }

  /** The tag-set "Airing from" field: one pill per tag, plus add/switch. */
  function TagPillRow({ channel, onRemoveTag, onAddTags, onChangeSource }) {
    const ids = tagIdsOf(channel.source);
    const [, setNamesTick] = useState(0);
    useEffect(() => {
      let alive = true;
      fetchTagNames(ids).then(() => { if (alive) setNamesTick((t) => t + 1); });
      return () => { alive = false; };
    }, [ids.join(",")]);
    const [confirmSwitch, setConfirmSwitch] = useState(false);
    return h("div", { className: "jw-field jw-field-wide" },
      h("div", { className: "jw-field-label" }, "Airing from"),
      h("div", { className: "jw-chip-row" },
        ids.map((id) => {
          const name = TAG_NAMES.get(id);
          const missing = name === null;
          return h("span", {
            key: id,
            className: "jw-pill" + (missing ? " jw-pill-missing" : ""),
            title: missing ? "This tag was deleted in Stash — remove it from the channel." : null,
          },
            h("span", { className: "jw-pill-name" }, missing ? "Deleted tag" : name || ("#" + id)),
            h("button", {
              className: "jw-pill-remove",
              disabled: ids.length === 1,
              "aria-label": "Remove tag " + (name || id),
              title: ids.length === 1 ? "A channel needs at least one tag." : "Remove this tag",
              onClick: () => onRemoveTag(id),
            }, "×"),
          );
        }),
        h("button", { className: "jw-pill-add", onClick: onAddTags }, "+ Add tag"),
      ),
      ids.length > 1
        ? h("div", { className: "jw-swap-note" }, "Airs scenes tagged with any of these.")
        : h("div", { className: "jw-swap-note" }, "A channel needs at least one tag — add more, or switch to a different kind of lineup below."),
      confirmSwitch
        ? h("div", { className: "jw-confirm-row" },
            h("span", null, "Switching to a different kind of lineup removes these " + ids.length + " tags."),
            h("button", { className: "jw-btn jw-btn-danger", onClick: () => { setConfirmSwitch(false); onChangeSource(); } }, "Switch"),
            h("button", { className: "jw-btn jw-btn-ghost", onClick: () => setConfirmSwitch(false) }, "Keep tags"),
          )
        : h("button", { className: "jw-link", onClick: () => (ids.length > 1 ? setConfirmSwitch(true) : onChangeSource()) },
            "Switch to a different kind of lineup…"),
    );
  }

  function EditorPane({ channel, channels, onPatch, onAssignNumber, onChangeSource, onRemoveTag, onAddTags, onRemove }) {
    const [confirmRemove, setConfirmRemove] = useState(false);
    const [glyphOpen, setGlyphOpen] = useState(false);
    const [trialOpen, setTrialOpen] = useState(false);
    if (!channel) return null;

    return h("div", { className: "jw-editor" },
      h("div", { className: "jw-network-card" },
        h("div", { style: { position: "relative" } },
          h("div", { onClick: () => setGlyphOpen(!glyphOpen), style: { cursor: "pointer" }, title: "Choose a logo" },
            h(GlyphTile, { codepoint: channel.glyph, color: channel.color, size: 56 }),
          ),
          glyphOpen ? h(GlyphPicker, {
            current: channel.glyph,
            onPick: (g) => { onPatch({ glyph: g }); setGlyphOpen(false); },
            onClose: () => setGlyphOpen(false),
          }) : null,
        ),
        h("div", { className: "jw-network-id" },
          h("input", {
            className: "jw-name-input",
            value: channel.name,
            maxLength: 60,
            onChange: (e) => onPatch({ name: e.target.value }),
            onBlur: (e) => { const v = e.target.value.trim(); if (v && v !== channel.name) onPatch({ name: v }); },
          }),
          h(NumberStepper, { channel, channels, onAssign: onAssignNumber }),
        ),
      ),

      h("div", { className: "jw-editor-section" },
        h("div", { className: "jw-section-label" }, "Programming"),
        h("div", { className: "jw-programming" },
          (channel.source || {}).type === "tag"
            ? h(TagPillRow, { channel, onRemoveTag, onAddTags, onChangeSource })
            : h("div", { className: "jw-field" },
                h("div", { className: "jw-field-label" }, "Airing from"),
                h("button", {
                  className: "jw-source-chip" + (channel.sourceMissing ? " jw-source-missing" : ""),
                  onClick: onChangeSource,
                  title: "Change what this channel airs",
                },
                  h(Glyph, { codepoint: SOURCE_KINDS[(channel.source || {}).type] ? SOURCE_KINDS[(channel.source || {}).type].glyph : "\uf111", size: 12, color: "inherit" }),
                  h("span", null, channel.sourceMissing
                    ? "Lineup missing — relink"
                    : (channel.sourceLabel || (SOURCE_KINDS[(channel.source || {}).type] || {}).label || "?")),
                ),
              ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Play order"),
            h("div", { className: "jw-chip-row" }, SORTS.map((s) =>
              h("button", {
                key: s.key,
                className: "jw-chip" + (channel.sort === s.key ? " jw-chip-active" : ""),
                onClick: () => onPatch({ sort: s.key }),
              }, s.label))),
          ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Channel is"),
            h("button", {
              className: "jw-chip" + (channel.enabled ? " jw-chip-active" : ""),
              onClick: () => onPatch({ enabled: !channel.enabled }),
            }, channel.enabled ? "On air" : "Paused"),
          ),
        ),
      ),

      h(ProgrammingControls, { channel, onPatch }),
      channel.programming && channel.programming.mode !== "fixed" ? h("button", { className: "jw-btn", onClick: () => setTrialOpen(true) }, "Try different programming…") : null,
      trialOpen ? h(ProgrammingTrial, { channel, onApply: onPatch, onClose: () => setTrialOpen(false) }) : null,
      h(PublishedSchedule, { channel }),
      h("div", { className: "jw-editor-section" },
        h("div", { className: "jw-section-label" }, "Appearance"),
        h("div", { className: "jw-appearance" },
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Color"),
            h("div", { className: "jw-swatch-row" },
              PALETTE.map((c) => h("button", {
                key: c,
                className: "jw-swatch" + (channel.color === c ? " jw-swatch-active" : ""),
                style: { background: c },
                onClick: () => onPatch({ color: c }),
                title: c,
              })),
              h("label", { className: "jw-swatch jw-swatch-custom", title: "Custom color" },
                h("input", {
                  type: "color",
                  value: channel.color,
                  style: { opacity: 0, position: "absolute", width: 1, height: 1 },
                  onChange: (e) => onPatch({ color: e.target.value.toUpperCase() }),
                }),
                "…",
              ),
            ),
          ),
        ),
      ),

      (!channel.programming || channel.programming.mode === "fixed") ? h(OnAirStrip, { channel, isDraft: false }) : null,

      h("div", { className: "jw-editor-section jw-danger" },
        confirmRemove
          ? h("div", { className: "jw-confirm-row" },
              h("span", null, "Remove " + channel.name + " from the dial?"),
              h("button", { className: "jw-btn jw-btn-danger", onClick: onRemove }, "Remove"),
              h("button", { className: "jw-btn jw-btn-ghost", onClick: () => setConfirmRemove(false) }, "Keep"),
            )
          : h("button", { className: "jw-link jw-link-danger", onClick: () => setConfirmRemove(true) }, "Remove this channel…"),
      ),
    );
  }

  /** Read-only detail for an automatic (library-derived) channel. */
  function AutoChannelPane({ row }) {
    const info = row.auto;
    const r = info.row;
    const soloSource =
      r.kind === "studio" ? { type: "studio", id: r.id }
        : r.kind === "performer" ? { type: "performer", id: r.id }
          : null;
    const draft = soloSource
      ? {
          id: "draft", number: r.number || 1, name: r.name,
          glyph: SECTION_GLYPHS[info.section] || "\uf111", color: AUTO_SLATE,
          source: soloSource, sort: "shuffle", seed: 0, enabled: true,
        }
      : null;

    return h("div", { className: "jw-editor" },
      h("div", { className: "jw-network-card" },
        h(GlyphTile, { codepoint: SECTION_GLYPHS[info.section] || "\uf111", color: r.kind && /Group|Spillover$/.test(r.kind) ? GROUP_SLATE : AUTO_SLATE, size: 56 }),
        h("div", { className: "jw-network-id" },
          h("div", { className: "jw-name-input jw-name-readonly" }, r.name),
          h("div", { className: "jw-swap-note" }, r.number != null ? "Channel " + r.number : "Unnumbered"),
        ),
      ),
      h("div", { className: "jw-editor-section" },
        h("div", { className: "jw-section-label" }, "Programming"),
        h("div", { className: "jw-programming" },
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Airing from"),
            h("div", { className: "jw-field-value jw-auto-note" },
              r.kind === "tags" ? "Tags matching this channel's theme"
                : r.kind === "studio" ? "Studio"
                  : r.kind === "performer" ? "Performer"
                    : r.count != null || r.members != null ? "A group of related " + (info.section === "studios" ? "studios" : "performers") : ""),
          ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Size"),
            h("div", { className: "jw-field-value jw-auto-note" },
              r.count != null ? formatCount(r.count)
                : r.members != null ? r.members + (r.members === 1 ? " member" : " members")
                  : ""),
          ),
        ),
      ),
      r.offAir
        ? h("div", { className: "jw-editor-section" },
            h("div", { className: "jw-missing-note" }, "Off air — nothing in your library matches this channel yet. It goes live as the library grows."),
          )
        : null,
      draft
        ? h(OnAirStrip, { channel: draft, isDraft: true })
        : h("div", { className: "jw-editor-section" },
            h("div", { className: "jw-missing-note" },
              r.kind === "tags"
                ? "This channel pools every tag matching its theme; open the TV guide to see what's playing."
                : "This channel pools several members; open the TV guide to see what's playing."),
          ),
      h("div", { className: "jw-editor-section" },
        h("div", { className: "jw-missing-note" }, "Generated automatically from your library — shape it with the thresholds in Tuning (⚙), or create a custom channel to take over a number."),
      ),
    );
  }

  function CreateChannelSheet({ onClose, onCreate, editingChannel }) {
    const [query, setQuery] = useState("");
    const [results, setResults] = useState(null);
    const [picked, setPicked] = useState(editingChannel
      ? { type: editingChannel.source.type, id: String(editingChannel.source.id), name: editingChannel.sourceLabel || "" }
      : null);
    const [name, setName] = useState(editingChannel ? editingChannel.name : "");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    const inputRef = useRef(null);

    useEffect(() => { if (inputRef.current) inputRef.current.focus(); }, []);
    useEffect(() => {
      let alive = true;
      const t = setTimeout(async () => {
        try {
          const r = await fetchSources(query.trim());
          if (alive) setResults(r);
        } catch (e) {
          if (alive) setResults({ savedFilters: [], tags: [], performers: [], studios: [] });
        }
      }, query.trim() ? SEARCH_DEBOUNCE_MS : 0);
      return () => { alive = false; clearTimeout(t); };
    }, [query]);

    useEffect(() => {
      if (picked && !name && picked.name) setName(picked.name);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [picked]);

    const groups = [];
    if (results) {
      if (results.savedFilters.length) groups.push({ title: "Custom lineups", glyph: SOURCE_KINDS.savedFilter.glyph, rows: results.savedFilters, hint: "saved search" });
      if (results.studios.length) groups.push({ title: "Studios", glyph: SOURCE_KINDS.studio.glyph, rows: results.studios });
      if (results.tags.length) groups.push({ title: "Tags", glyph: SOURCE_KINDS.tag.glyph, rows: results.tags });
      if (results.performers.length) groups.push({ title: "Performers", glyph: SOURCE_KINDS.performer.glyph, rows: results.performers });
    }

    const submit = async () => {
      if (!picked || busy) return;
      setBusy(true);
      setError(null);
      try {
        await onCreate({
          source: picked.type === "tag"
            ? { type: "tag", id: picked.id, ids: [picked.id] }
            : { type: picked.type, id: picked.id },
          name: (name || picked.name || "New channel").trim().slice(0, 60),
        });
        onClose();
      } catch (e) {
        setError(String((e && e.message) || e));
        setBusy(false);
      }
    };

    return h("div", { className: "jw-overlay", onMouseDown: (e) => { if (e.target === e.currentTarget) onClose(); } },
      h("div", { className: "jw-sheet" },
        h("div", { className: "jw-sheet-head" },
          h("span", { className: "jw-sheet-title" }, editingChannel ? "Change what airs" : "New channel"),
          h("button", { className: "jw-btn jw-btn-ghost", onClick: onClose }, "✕"),
        ),
        !picked ? [
          h("input", {
            key: "search", ref: inputRef, className: "jw-search",
            placeholder: "Search saved searches, studios, tags, performers…",
            value: query, onChange: (e) => setQuery(e.target.value),
          }),
          results == null
            ? h("div", { className: "jw-sheet-loading" }, "Loading…")
            : groups.length === 0
              ? h("div", { className: "jw-sheet-loading" }, "Nothing matches “" + query + "”")
              : groups.map((g) => h("div", { key: g.title, className: "jw-result-group" },
                  h("div", { className: "jw-section-label" }, g.title),
                  g.rows.map((row) => h("button", {
                    key: row.type + "-" + row.id,
                    className: "jw-result-row",
                    onClick: () => setPicked(row),
                  },
                    h(Glyph, { codepoint: g.glyph, size: 13, color: "rgba(235,235,240,.6)" }),
                    h("span", { className: "jw-result-name" }, row.name),
                    h("span", { className: "jw-result-count" + (row.count === 0 ? " jw-off-air-text" : "") },
                      row.count == null ? (g.hint || "") : (row.count === 0 ? "off air" : formatCount(row.count))),
                  )),
                )),
        ] : [
          h("div", { key: "picked", className: "jw-picked-row" },
            h("span", { className: "jw-field-label" }, "Airing from"),
            h("span", { className: "jw-picked-name" }, picked.name || picked.id),
            h("button", { className: "jw-link", onClick: () => { setPicked(null); setName(editingChannel ? editingChannel.name : ""); } }, "change"),
          ),
          h("div", { key: "name", className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Channel name"),
            h("input", {
              className: "jw-search", value: name, maxLength: 60,
              placeholder: picked.name || "New channel",
              onChange: (e) => setName(e.target.value),
              onKeyDown: (e) => { if (e.key === "Enter") submit(); },
              autoFocus: true,
            }),
          ),
          error ? h("div", { key: "err", className: "jw-missing-note" }, error) : null,
          h("div", { key: "foot", className: "jw-sheet-foot" },
            h("button", { className: "jw-btn jw-btn-ghost", onClick: onClose }, "Cancel"),
            h("button", { className: "jw-btn jw-btn-primary", disabled: busy, onClick: submit },
              busy ? "Working…" : (editingChannel ? "Relink channel" : "Create channel")),
          ),
        ],
      ),
    );
  }

  /** Multi-pick tag sheet: toggle membership; Done applies the whole set. */
  function TagPickSheet({ channel, onClose, onDone }) {
    const [query, setQuery] = useState("");
    const [results, setResults] = useState(null);
    const [picked, setPicked] = useState(tagIdsOf(channel.source));
    const [busy, setBusy] = useState(false);
    const inputRef = useRef(null);

    useEffect(() => { if (inputRef.current) inputRef.current.focus(); }, []);
    useEffect(() => { fetchTagNames(picked); }, [picked.join(",")]);
    useEffect(() => {
      let alive = true;
      const t = setTimeout(async () => {
        try {
          const r = await fetchSources(query.trim());
          if (alive) setResults(r.tags);
        } catch (e) {
          if (alive) setResults([]);
        }
      }, query.trim() ? SEARCH_DEBOUNCE_MS : 0);
      return () => { alive = false; clearTimeout(t); };
    }, [query]);

    const toggle = (id) => setPicked((cur) => cur.includes(id)
      ? cur.filter((x) => x !== id)
      : [...cur, id].sort((a, b) => Number(a) - Number(b)));
    const done = () => {
      if (!picked.length || busy) return;
      setBusy(true);
      onDone(picked);
      onClose();
    };

    return h("div", { className: "jw-overlay", onMouseDown: (e) => { if (e.target === e.currentTarget) onClose(); } },
      h("div", { className: "jw-sheet" },
        h("div", { className: "jw-sheet-head" },
          h("span", { className: "jw-sheet-title" }, "Tags this channel airs from"),
          h("button", { className: "jw-btn jw-btn-ghost", onClick: onClose }, "✕"),
        ),
        h("div", { className: "jw-settings-hint" }, "Pick any number — the channel airs scenes matching any of them."),
        picked.length
          ? h("div", { className: "jw-chip-row jw-picked-tags" },
              picked.map((id) => {
                const name = TAG_NAMES.get(id);
                return h("span", { key: id, className: "jw-pill" },
                  h("span", { className: "jw-pill-name" }, name || "#" + id),
                  h("button", { className: "jw-pill-remove", "aria-label": "Remove tag " + (name || id), onClick: () => toggle(id) }, "×"),
                );
              }),
            )
          : null,
        h("input", {
          ref: inputRef, className: "jw-search",
          placeholder: "Search tags…",
          value: query, onChange: (e) => setQuery(e.target.value),
        }),
        results == null
          ? h("div", { className: "jw-sheet-loading" }, "Loading…")
          : results.length === 0
            ? h("div", { className: "jw-sheet-loading" }, "Nothing matches “" + query + "”")
            : results.map((row) => h("button", {
                key: row.id,
                className: "jw-result-row" + (picked.includes(row.id) ? " jw-result-picked" : ""),
                onClick: () => toggle(row.id),
              },
                h(Glyph, { codepoint: SOURCE_KINDS.tag.glyph, size: 13, color: "rgba(235,235,240,.6)" }),
                h("span", { className: "jw-result-name" }, row.name),
                h("span", { className: "jw-result-count" + (row.count === 0 ? " jw-off-air-text" : "") },
                  row.count === 0 ? "off air" : formatCount(row.count)),
              )),
        h("div", { className: "jw-sheet-foot" },
          h("button", { className: "jw-btn jw-btn-ghost", onClick: onClose }, "Cancel"),
          h("button", { className: "jw-btn jw-btn-primary", disabled: !picked.length || busy, onClick: done,
            title: picked.length ? null : "A channel needs at least one tag." },
            "Done"),
        ),
      ),
    );
  }

  function SettingsSheet({ catalog, onClose, onPatchSettings }) {
    const s = catalog.settings || {};
    const num = (key, def) => h("input", {
      className: "jw-number-input jw-num-wide", type: "number", min: 2, max: 98,
      value: s[key] != null ? s[key] : def,
      onChange: (e) => {
        const v = parseInt(e.target.value, 10);
        if (!isNaN(v)) onPatchSettings({ [key]: v });
      },
    });
    return h("div", { className: "jw-overlay", onMouseDown: (e) => { if (e.target === e.currentTarget) onClose(); } },
      h("div", { className: "jw-sheet jw-sheet-settings" },
        h("div", { className: "jw-sheet-head" },
          h("span", { className: "jw-sheet-title" }, "Tuning"),
          h("button", { className: "jw-btn jw-btn-ghost", onClick: onClose }, "✕"),
        ),
        h("p", { className: "jw-settings-hint" },
          "These thresholds shape the General, Studios, and Performers sections shown on THIS page. ",
          "Your TV generates its own channels from its own Just Watch settings (TV → Settings → Just Watch); ",
          "the two are not connected. Channels you create here are never touched by either."),
        h("div", { className: "jw-programming" },
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "A studio or performer is listed on its own row at"),
            num("soloThreshold", 10),
          ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Smaller ones are pooled into groups below"),
            num("groupThreshold", 5),
          ),
        ),
        h("p", { className: "jw-settings-hint" },
          "Launch behavior (pick up where you left off vs. a random channel) is set on your TV, under Just Watch settings."),
        h("div", { className: "jw-sheet-foot" },
          h("button", { className: "jw-btn jw-btn-primary", onClick: onClose }, "Done"),
        ),
      ),
    );
  }

  // ------------------------------------------------------------------
  // App root: catalog state + one save coordinator
  // ------------------------------------------------------------------

  // Server-authoritative fields are merged into a draft that moved on while a
  // save ran: only per-channel display facts (labels + health), and only for
  // channels the finished save actually covered with an unchanged source.
  function rebaseAcknowledged(latest, savedDraft, fresh, revision) {
    const out = Object.assign({}, latest, { revision });
    if (!fresh) return out;
    const freshById = new Map((fresh.channels || []).map((c) => [c.id, c]));
    const savedSources = new Map((savedDraft.channels || []).map((c) => [c.id, JSON.stringify(c.source)]));
    out.channels = (latest.channels || []).map((ch) => {
      const saved = freshById.get(ch.id);
      if (!saved) return ch; // brand-new local channel: no server truth yet
      if (savedSources.get(ch.id) !== JSON.stringify(ch.source)) return ch; // source moved on
      return Object.assign({}, ch, {
        sourceLabel: saved.sourceLabel || ch.sourceLabel,
        sceneCount: saved.sceneCount,
        loopSeconds: saved.loopSeconds,
        loopCapped: saved.loopCapped,
        sourceMissing: saved.sourceMissing,
      });
    });
    return out;
  }

  function App() {
    const [catalog, setCatalog] = useState(null);
    const [fullDir, setFullDir] = useState(null);
    const [selectedId, setSelectedId] = useState(null);
    const [sheet, setSheet] = useState(null); // null | {mode: 'new'|'source'|'settings'}
    const [saveState, setSaveState] = useState({ state: "idle" });
    const [toast, setToast] = useState(null);

    // One save coordinator for creation, editing, relinking, renumbering,
    // deletion, and settings. `draftRef` is the newest working copy (possibly
    // ahead of the server); `ackRef` is the revision the server last
    // acknowledged. Saves always submit against ackRef, so edits made while a
    // save is in flight serialize cleanly instead of conflicting with it.
    const catalogRef = useRef(null); // mirrors the draft for event handlers
    const draftRef = useRef(null);
    const ackRef = useRef(0);
    const saveTimer = useRef(null);
    const savingRef = useRef(false);
    const selectedRef = useRef(null);
    const settingsDirtyRef = useRef(false);

    useEffect(() => { resolveGlyphs(); }, []);

    const showToast = useCallback((msg) => {
      setToast(msg);
      setTimeout(() => setToast(null), 5000);
    }, []);

    const adopt = useCallback((next) => {
      draftRef.current = next;
      catalogRef.current = next;
      setCatalog(next);
    }, []);

    // ---- initial load
    useEffect(() => {
      let alive = true;
      (async () => {
        try {
          const c = await loadCatalog();
          if (!alive) return;
          adopt(c);
          ackRef.current = c.revision || 0;
          if (c.channels && c.channels.length) setSelectedId(c.channels[0].id);
        } catch (e) {
          if (!alive) return;
          showToast("Could not reach the Just Watch plugin: " + ((e && e.message) || e));
          adopt({ revision: 0, settings: {}, channels: [] });
        }
        // The full lineup (auto channels) is an enhancement; its absence
        // must never block editing.
        try {
          const fd = await loadFullDirectory();
          if (alive) setFullDir(fd);
        } catch (e) { /* rail shows custom channels only */ }
      })();
      return () => { alive = false; };
    }, [showToast, adopt]);

    const flushSave = useCallback(async () => {
      if (savingRef.current) return;
      const toSave = draftRef.current;
      if (!toSave) return;
      savingRef.current = true;
      setSaveState((s) => (s.state === "conflict" ? s : { state: "saving" }));
      let dirty = false;
      try {
        const { result, fresh } = await commitAndReload(toSave, ackRef.current);
        const latest = draftRef.current;
        if (result.saved) {
          ackRef.current = result.revision;
          if (latest === toSave) {
            // Nothing moved during the save: adopt the server's copy wholesale.
            adopt(fresh || Object.assign({}, toSave, { revision: result.revision }));
            setSaveState({ state: "saved" });
          } else {
            // Edits arrived while saving: keep them, acknowledge under them,
            // and save the newer draft right after.
            adopt(rebaseAcknowledged(latest, toSave, fresh, result.revision));
            setSaveState({ state: "saving" });
            dirty = true;
          }
          if (settingsDirtyRef.current) {
            settingsDirtyRef.current = false;
            loadFullDirectory().then((fd) => setFullDir(fd)).catch(() => {});
          }
        } else if (result.error === "revision_conflict") {
          // Real external change (another tab, a task). The local draft is
          // preserved; the user chooses reload vs deliberate overwrite.
          setSaveState({ state: "conflict", serverRevision: result.currentRevision });
        } else {
          const first = (result.errors && result.errors[0]) || {};
          showToast(first.message || "The server rejected that change.");
          setSaveState({ state: "error" });
        }
      } catch (e) {
        setSaveState({ state: "error" });
      } finally {
        savingRef.current = false;
        if (dirty) scheduleSave();
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [adopt, showToast]);

    const scheduleSave = useCallback((working) => {
      if (working) adopt(working);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => { flushSave(); }, AUTOSAVE_DEBOUNCE_MS);
    }, [adopt, flushSave]);

    const retrySave = useCallback(() => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      flushSave();
    }, [flushSave]);

    const discardDraftAndReload = useCallback(async () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      try {
        const fresh = await loadCatalog();
        ackRef.current = fresh.revision || 0;
        adopt(fresh);
        setSaveState({ state: "idle" });
      } catch (e) {
        showToast("Could not reload: " + ((e && e.message) || e));
      }
    }, [adopt, showToast]);

    const overwriteWithDraft = useCallback(() => {
      // Deliberate overwrite: acknowledge the server's current revision and
      // resubmit the preserved draft against it.
      ackRef.current = saveState.serverRevision || ackRef.current;
      setSaveState({ state: "saving" });
      flushSave();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [flushSave, saveState.serverRevision]);

    const mutate = useCallback((fn) => {
      const next = fn(JSON.parse(JSON.stringify(draftRef.current || { revision: 0, settings: {}, channels: [] })));
      scheduleSave(next);
    }, [scheduleSave]);

    const patchChannel = useCallback((channelId, patch) => {
      mutate((c) => {
        const ch = c.channels.find((x) => x.id === channelId);
        if (ch) Object.assign(ch, patch);
        return c;
      });
    }, [mutate]);

    const assignNumber = useCallback((channelId, number, swapWithId) => {
      mutate((c) => {
        const ch = c.channels.find((x) => x.id === channelId);
        if (!ch) return c;
        if (swapWithId) {
          const other = c.channels.find((x) => x.id === swapWithId);
          if (other) other.number = ch.number;
        }
        ch.number = number;
        c.channels.sort((a, b) => a.number - b.number);
        return c;
      });
    }, [mutate]);

    const createChannel = useCallback(({ source, name }) => {
      const base = draftRef.current;
      const number = lowestFreeNumber(base.channels || []);
      if (number == null) throw new Error("All 99 channel numbers are in use.");
      const channel = {
        id: newChannelId(),
        number,
        name,
        glyph: pickDefaultGlyph(name),
        color: PALETTE[(number - 1) % PALETTE.length],
        source,
        sourceLabel: name,
        sort: "shuffle",
        seed: Math.floor(Math.random() * 2147483647),
        enabled: true,
      };
      mutate((c) => {
        c.channels.push(channel);
        c.channels.sort((a, b) => a.number - b.number);
        return c;
      });
      setSelectedId(channel.id);
    }, [mutate]);

    const relinkChannel = useCallback(({ source, name }) => {
      const target = selectedRef.current;
      mutate((c) => {
        const ch = c.channels.find((x) => x.id === target);
        if (ch) { ch.source = source; ch.sourceLabel = name; ch.sourceMissing = false; }
        return c;
      });
    }, [mutate]);

    const patchSelectedSource = useCallback((source) => {
      const target = selectedRef.current;
      mutate((c) => {
        const ch = c.channels.find((x) => x.id === target);
        if (ch) {
          ch.source = source;
          const names = tagIdsOf(source).map((id) => TAG_NAMES.get(id));
          // Optimistic rail label from known names; the server re-joins on save.
          if (names.length && names.every(Boolean)) ch.sourceLabel = joinLabels(names);
        }
        return c;
      });
    }, [mutate]);

    const removeTagFromSelected = useCallback((tagId) => {
      const ch = (draftRef.current.channels || []).find((x) => x.id === selectedRef.current);
      if (!ch) return;
      const remaining = tagIdsOf(ch.source).filter((x) => x !== tagId);
      if (remaining.length) patchSelectedSource(tagSource(remaining));
    }, [patchSelectedSource]);

    const removeChannel = useCallback(() => {
      const target = selectedRef.current;
      const remaining = (draftRef.current.channels || []).filter((x) => x.id !== target);
      mutate((c) => {
        c.channels = c.channels.filter((x) => x.id !== target);
        return c;
      });
      setSelectedId(remaining.length ? remaining[0].id : null);
    }, [mutate]);

    const patchSettings = useCallback((patch) => {
      settingsDirtyRef.current = true;
      mutate((c) => { Object.assign(c.settings, patch); return c; });
    }, [mutate]);

    useEffect(() => { selectedRef.current = selectedId; }, [selectedId]);

    if (!catalog) {
      return h("div", { className: "jw-page" }, h("div", { className: "jw-loading" }, "Tuning the dial…"));
    }

    const selected = (catalog.channels || []).find((c) => c.id === selectedId) || null;
    const railSections = buildRailSections(catalog, fullDir);
    const selectedRailRow = railSections
      .flatMap((s) => s.rows)
      .find((r) => r.sel === selectedId) || null;

    return h("div", { className: "jw-page" },
      h("div", { className: "jw-header" },
        h("div", null,
          h("h1", { className: "jw-title" }, "Channel Studio"),
          h("div", { className: "jw-tagline" },
            "Your library, on the air. Channels you create here air on numbers 1–99, ahead of the built-in dial on your TV."),
        ),
        h("div", { className: "jw-header-actions" },
          h(SaveIndicator, {
            state: saveState.state,
            onRetry: retrySave,
          }),
          h("button", { className: "jw-btn jw-btn-ghost", title: "Tuning", onClick: () => setSheet({ mode: "settings" }) }, "⚙"),
        ),
      ),
      saveState.state === "conflict"
        ? h("div", { className: "jw-conflict-banner" },
            h("span", null,
              "Your lineup changed in another session. Keep this page's changes, or reload the saved lineup?"),
            h("button", { className: "jw-btn jw-btn-primary", onClick: overwriteWithDraft }, "Keep my changes"),
            h("button", { className: "jw-btn jw-btn-ghost", onClick: discardDraftAndReload }, "Reload saved lineup"),
          )
        : null,
      h("div", { className: "jw-columns" },
        h(DialRail, {
          railSections, selectedId,
          onSelect: setSelectedId, onNew: () => setSheet({ mode: "new" }),
          channelCount: (catalog.channels || []).length,
        }),
        selected
          ? h(EditorPane, {
              channel: selected, channels: catalog.channels || [],
              onPatch: (patch) => patchChannel(selected.id, patch),
              onAssignNumber: (number, swapId) => assignNumber(selected.id, number, swapId),
              onChangeSource: () => setSheet({ mode: "source" }),
              onRemoveTag: removeTagFromSelected,
              onAddTags: () => setSheet({ mode: "tags" }),
              onRemove: removeChannel,
            })
          : selectedRailRow && selectedRailRow.auto
            ? h(AutoChannelPane, { row: selectedRailRow })
            : h("div", { className: "jw-editor jw-editor-empty" },
                h("div", { className: "jw-empty-title" }, "Nothing selected"),
                h("div", { className: "jw-empty-sub" }, "Pick a channel on the left, or create your first one."),
              ),
      ),
      sheet && sheet.mode === "new"
        ? h(CreateChannelSheet, { onClose: () => setSheet(null), onCreate: createChannel })
        : null,
      sheet && sheet.mode === "source"
        ? h(CreateChannelSheet, { onClose: () => setSheet(null), onCreate: relinkChannel, editingChannel: selected })
        : null,
      sheet && sheet.mode === "tags"
        ? h(TagPickSheet, { channel: selected, onClose: () => setSheet(null), onDone: (ids) => patchSelectedSource(tagSource(ids)) })
        : null,
      sheet && sheet.mode === "settings"
        ? h(SettingsSheet, { catalog, onClose: () => setSheet(null), onPatchSettings: patchSettings })
        : null,
      toast ? h("div", { className: "jw-toast" }, toast) : null,
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
