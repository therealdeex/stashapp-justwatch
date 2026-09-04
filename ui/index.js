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

  const ROUTE_PATH = "/plugin/stash-justwatch";
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

  async function pollJob(jobId) {
    for (;;) {
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
  }

  async function readSaveResult(requestId, attempts) {
    for (let i = 0; i < (attempts || 12); i++) {
      try {
        const resp = await fetch(ASSET_BASE + "snapshots/save_result.json?r=" + Date.now(), {
          credentials: "same-origin",
        });
        if (resp.ok) {
          const body = await resp.json();
          if (!requestId || body.requestId === requestId) return body;
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
      const def = FAS[key];
      const unicode = Array.isArray(def) ? def[3] : null;
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
    const kind = SOURCE_KINDS[(channel.source || {}).type] || { label: "" };
    const base = kind.label === "Custom lineup"
      ? (channel.sourceLabel || "Custom lineup")
      : kind.label + " · " + (channel.sourceLabel || "?");
    if (channel.sourceMissing) return base + " · lineup missing";
    if (channel.sceneCount != null) return base + " · " + formatCount(channel.sceneCount);
    return base;
  }

  // ------------------------------------------------------------------
  // API surface used by components
  // ------------------------------------------------------------------

  function loadCatalog() {
    return runOp("GetCatalog");
  }

  function previewLineup(channel, perPage) {
    return runOp("PreviewLineup", {
      channel: JSON.stringify(channel),
      perPage: String(perPage || PREVIEW_COUNT),
    });
  }

  let saveSeq = 0;

  // After a successful save the server has refreshed labels + health counts,
  // so re-pull the catalog instead of letting local guesses linger.
  async function commitAndReload(catalog, expectedRevision) {
    const requestId = "req-" + Date.now() + "-" + (++saveSeq);
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

  function SaveIndicator({ state }) {
    if (state === "saving") return h("span", { className: "jw-save-indicator" }, "Saving…");
    if (state === "error") return h("span", { className: "jw-save-indicator jw-save-error" }, "Save failed — will retry on your next change");
    if (state === "saved") return h("span", { className: "jw-save-indicator" }, "Saved");
    return null;
  }

  function RailRow({ channel, selected, onSelect }) {
    const badges = [];
    if (channel.sourceMissing) {
      badges.push(h("span", { key: "m", className: "jw-badge jw-badge-missing", title: "This lineup's source was deleted in Stash. Relink it." }, "relink"));
    } else if (channel.sceneCount === 0) {
      badges.push(h("span", { key: "o", className: "jw-badge jw-badge-off" }, "off air"));
    } else if (channel.sceneCount != null && channel.sceneCount < THIN_LINEUP) {
      badges.push(h("span", { key: "t", className: "jw-badge jw-badge-thin", title: "A lineup this small repeats all evening." }, "thin"));
    }
    if (!channel.enabled) badges.push(h("span", { key: "d", className: "jw-badge jw-badge-off" }, "paused"));

    return h("div", {
      className: "jw-rail-row" + (selected ? " jw-selected" : "") + (channel.enabled ? "" : " jw-row-paused"),
      style: selected ? { boxShadow: "inset 3px 0 0 " + channel.color } : null,
      onClick: () => onSelect(channel.id),
      onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(channel.id); } },
      tabIndex: 0,
      role: "button",
    },
      h("div", { className: "jw-rail-number", style: selected ? { color: channel.color } : null }, String(channel.number)),
      h(GlyphTile, { codepoint: channel.glyph, color: channel.color }),
      h("div", { className: "jw-rail-body" },
        h("div", { className: "jw-rail-name" }, channel.name),
        h("div", { className: "jw-rail-sub" }, summarize(channel)),
      ),
      badges.length ? h("div", { className: "jw-rail-badges" }, badges) : null,
    );
  }

  function DialRail({ channels, selectedId, onSelect, onNew }) {
    const sorted = channels.slice().sort((a, b) => a.number - b.number);
    return h("div", { className: "jw-rail" },
      h("div", { className: "jw-rail-head" },
        h("span", { className: "jw-section-label" }, "My Lineup"),
        h("span", { className: "jw-rail-count" }, sorted.length + (sorted.length === 1 ? " channel" : " channels")),
      ),
      h("button", { className: "jw-btn jw-btn-primary jw-new-channel", onClick: onNew }, "+ New channel"),
      sorted.length === 0
        ? h("div", { className: "jw-empty" },
            h("div", { className: "jw-empty-title" }, "Your dial starts here"),
            h("div", { className: "jw-empty-sub" }, "Channels you create here air on numbers 1–99, ahead of the built-in dial on your TV."),
          )
        : h("div", { className: "jw-rail-list" },
            sorted.map((c) => h(RailRow, { key: c.id, channel: c, selected: c.id === selectedId, onSelect }))),
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
    const [state, setState] = useState({ loading: true, items: null, total: null, error: null });
    const spec = JSON.stringify(channel);

    useEffect(() => {
      let alive = true;
      setState({ loading: true, items: null, total: null, error: null });
      const t = setTimeout(async () => {
        try {
          const result = await previewLineup(JSON.parse(spec), PREVIEW_COUNT + 1);
          if (!alive) return;
          setState({ loading: false, items: result.items || [], total: result.total, error: null });
        } catch (e) {
          if (alive) setState({ loading: false, items: null, total: null, error: String((e && e.message) || e) });
        }
      }, 250);
      return () => { alive = false; clearTimeout(t); };
    }, [spec]);

    const missing = channel.sourceMissing && !isDraft;
    return h("div", { className: "jw-editor-section" },
      h("div", { className: "jw-onair-head" },
        h("span", { className: "jw-section-label" }, "On air tonight"),
        h("span", { className: "jw-onair-meta" },
          state.loading || missing ? "" : [
            state.total != null ? formatCount(state.total) : "",
            !isDraft && channel.loopSeconds != null ? " · " + formatLoop(channel.loopSeconds) : "",
            !isDraft && channel.loopCapped ? " · very long lineup" : "",
          ].join(""),
        ),
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

  function EditorPane({ channel, channels, onPatch, onAssignNumber, onChangeSource, onRemove }) {
    const [confirmRemove, setConfirmRemove] = useState(false);
    const [glyphOpen, setGlyphOpen] = useState(false);
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
          h("div", { className: "jw-field" },
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

      h(OnAirStrip, { channel, isDraft: false }),

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
          source: { type: picked.type, id: picked.id },
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
          "These tune the channels your TV generates by itself (General, Studios, Performers). Channels you create here are never touched by them."),
        h("div", { className: "jw-programming" },
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "A studio or performer gets its own channel at"),
            num("soloThreshold", 10),
          ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "Smaller ones are pooled into groups at"),
            num("groupThreshold", 5),
          ),
          h("div", { className: "jw-field" },
            h("div", { className: "jw-field-label" }, "When the TV turns on"),
            h("div", { className: "jw-chip-row" },
              h("button", {
                className: "jw-chip" + (s.launchMode !== "random" ? " jw-chip-active" : ""),
                onClick: () => onPatchSettings({ launchMode: "last" }),
              }, "Pick up where I left off"),
              h("button", {
                className: "jw-chip" + (s.launchMode === "random" ? " jw-chip-active" : ""),
                onClick: () => onPatchSettings({ launchMode: "random" }),
              }, "Surprise me"),
            ),
          ),
        ),
        h("div", { className: "jw-sheet-foot" },
          h("button", { className: "jw-btn jw-btn-primary", onClick: onClose }, "Done"),
        ),
      ),
    );
  }

  // ------------------------------------------------------------------
  // App root: catalog state + serialized autosave
  // ------------------------------------------------------------------

  function App() {
    const [catalog, setCatalog] = useState(null);
    const [selectedId, setSelectedId] = useState(null);
    const [sheet, setSheet] = useState(null); // null | {mode: 'new'|'source'|'settings'}
    const [saveState, setSaveState] = useState({ state: "idle" });
    const [toast, setToast] = useState(null);

    const catalogRef = useRef(null); // mirrors state for event handlers
    const saveTimer = useRef(null);
    const pendingRef = useRef(null); // latest working copy while a save is in flight
    const savingRef = useRef(false);
    const selectedRef = useRef(null);

    useEffect(() => { resolveGlyphs(); }, []);

    const showToast = useCallback((msg) => {
      setToast(msg);
      setTimeout(() => setToast(null), 5000);
    }, []);

    // ---- initial load
    useEffect(() => {
      let alive = true;
      (async () => {
        try {
          const c = await loadCatalog();
          if (!alive) return;
          setCatalog(c);
          catalogRef.current = c;
          if (c.channels && c.channels.length) setSelectedId(c.channels[0].id);
        } catch (e) {
          if (!alive) return;
          showToast("Could not reach the Just Watch plugin: " + ((e && e.message) || e));
          const empty = { revision: 0, settings: {}, channels: [] };
          setCatalog(empty);
          catalogRef.current = empty;
        }
      })();
      return () => { alive = false; };
    }, [showToast]);

    // ---- autosave: debounce, serialize saves, handle conflicts
    const scheduleSave = useCallback((working) => {
      pendingRef.current = working;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(async () => {
        if (savingRef.current) return; // in-flight save re-reads pendingRef below
        const toSave = pendingRef.current;
        if (!toSave) return;
        savingRef.current = true;
        setSaveState({ state: "saving" });
        try {
          const { result, fresh } = await commitAndReload(toSave, toSave.revision);
          if (result.saved) {
            const savedCopy = fresh || Object.assign({}, pendingRef.current, { revision: result.revision });
            catalogRef.current = savedCopy;
            setCatalog(savedCopy);
            setSaveState({ state: "saved" });
          } else if (result.error === "revision_conflict") {
            const fresh = await loadCatalog();
            catalogRef.current = fresh;
            setCatalog(fresh);
            setSaveState({ state: "idle" });
            showToast("Your lineup changed elsewhere — reloaded the latest.");
          } else {
            const first = (result.errors && result.errors[0]) || {};
            showToast(first.message || "The server rejected that change.");
            setSaveState({ state: "error" });
          }
        } catch (e) {
          setSaveState({ state: "error" });
        } finally {
          savingRef.current = false;
          // Anything edited while this save ran gets its own save now.
          if (pendingRef.current && pendingRef.current !== toSave) {
            scheduleSave(pendingRef.current);
          } else {
            pendingRef.current = null;
          }
        }
      }, AUTOSAVE_DEBOUNCE_MS);
    }, [showToast]);

    const mutate = useCallback((fn) => {
      const next = fn(JSON.parse(JSON.stringify(catalogRef.current || { revision: 0, settings: {}, channels: [] })));
      catalogRef.current = next;
      setCatalog(next);
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

    const createChannel = useCallback(async ({ source, name }) => {
      const base = catalogRef.current;
      const number = lowestFreeNumber(base.channels || []);
      if (number == null) throw new Error("All 99 channel numbers are in use.");
      const channel = {
        id: newChannelId(),
        number,
        name,
        glyph: pickDefaultGlyph(name),
        color: PALETTE[(number - 1) % PALETTE.length],
        source,
        sourceLabel: "",
        sort: "shuffle",
        seed: Math.floor(Math.random() * 2147483647),
        enabled: true,
      };
      const next = JSON.parse(JSON.stringify(base));
      next.channels.push(channel);
      next.channels.sort((a, b) => a.number - b.number);
      catalogRef.current = next;
      setCatalog(next);
      setSelectedId(channel.id);
      setSaveState({ state: "saving" });
      const { result, fresh } = await commitAndReload(next, next.revision);
      if (result.saved) {
        const savedCopy = fresh || Object.assign({}, next, { revision: result.revision });
        catalogRef.current = savedCopy;
        setCatalog(savedCopy);
        setSaveState({ state: "saved" });
      } else {
        setSaveState({ state: "error" });
        const first = (result.errors && result.errors[0]) || {};
        throw new Error(first.message || "The server rejected the new channel.");
      }
    }, []);

    const relinkChannel = useCallback(async ({ source, name }) => {
      const target = selectedRef.current;
      mutate((c) => {
        const ch = c.channels.find((x) => x.id === target);
        if (ch) { ch.source = source; ch.sourceLabel = name; ch.sourceMissing = false; }
        return c;
      });
    }, [mutate]);

    const removeChannel = useCallback(() => {
      const target = selectedRef.current;
      const remaining = (catalogRef.current.channels || []).filter((x) => x.id !== target);
      mutate((c) => {
        c.channels = c.channels.filter((x) => x.id !== target);
        return c;
      });
      setSelectedId(remaining.length ? remaining[0].id : null);
    }, [mutate]);

    const patchSettings = useCallback((patch) => {
      mutate((c) => { Object.assign(c.settings, patch); return c; });
    }, [mutate]);

    useEffect(() => { selectedRef.current = selectedId; }, [selectedId]);

    if (!catalog) {
      return h("div", { className: "jw-page" }, h("div", { className: "jw-loading" }, "Tuning the dial…"));
    }

    const selected = (catalog.channels || []).find((c) => c.id === selectedId) || null;

    return h("div", { className: "jw-page" },
      h("div", { className: "jw-header" },
        h("div", null,
          h("h1", { className: "jw-title" }, "Channel Studio"),
          h("div", { className: "jw-tagline" },
            "Your library, on the air. Channels you create here air on numbers 1–99, ahead of the built-in dial on your TV."),
        ),
        h("div", { className: "jw-header-actions" },
          h(SaveIndicator, { state: saveState.state }),
          h("button", { className: "jw-btn jw-btn-ghost", title: "Tuning", onClick: () => setSheet({ mode: "settings" }) }, "⚙"),
        ),
      ),
      h("div", { className: "jw-columns" },
        h(DialRail, {
          channels: catalog.channels || [], selectedId,
          onSelect: setSelectedId, onNew: () => setSheet({ mode: "new" }),
        }),
        selected
          ? h(EditorPane, {
              channel: selected, channels: catalog.channels || [],
              onPatch: (patch) => patchChannel(selected.id, patch),
              onAssignNumber: (number, swapId) => assignNumber(selected.id, number, swapId),
              onChangeSource: () => setSheet({ mode: "source" }),
              onRemove: removeChannel,
            })
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
      api.patch.before("MainNavBar.MenuItems", function (props) {
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
  } catch (e) {
    console.error("[stash-justwatch] route registration failed", e);
  }
})();
