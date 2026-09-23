// Prototype A — Channel Studio.
// Workflow: a searchable, grouped dial on the left; the focused channel's
// editor on the right. You find a channel like you'd find it on the TV, edit
// it in place, and Apply. Drafts survive browsing; nothing autosaves.

import { createMockApi, draftStore, SCENARIOS } from "../shared/mock-api.js";
import { topBar, createEditorPane, guardUnload } from "../shared/editor-sections.js";
import { bulkMoveFlow, groupsManagerFlow } from "../shared/bulk.js";
import { el, clear, tile, virtualList, toast, statusChip, fmtCount, debounce } from "../shared/ui.js";

const api = createMockApi();
const drafts = draftStore("studio");
guardUnload(api, drafts);

const state = {
  query: "",
  selectedId: null,
  collapsed: new Set(),
  bulkMode: false,
  selected: new Set(),
};

const dialTools = el("div", { class: "dial-tools" });
const dialListHost = el("div", { style: "flex:1;min-height:0;display:flex" });
const dial = el("section", { class: "dial", "aria-label": "Channel dial" }, dialTools, dialListHost);
const editorPane = el("section", { class: "editor-pane", "aria-label": "Channel editor" });
const main = el("main", { class: "studio-main" }, dial, editorPane);

let list = null;
let editor = null;

// ---------- dial list ----------
function channelRow(ch) {
  const sub = describeSub(ch);
  const row = el("div", {
    class: "row", role: "option", tabindex: "0",
    "aria-selected": String(state.selectedId === ch.id),
    onclick: () => selectChannel(ch.id),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectChannel(ch.id); } },
  },
    state.bulkMode ? el("input", {
      type: "checkbox", "aria-label": `Select ${ch.name}`, checked: state.selected.has(ch.id),
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? state.selected.add(ch.id) : state.selected.delete(ch.id); renderBulkBar(); },
    }) : null,
    tile(ch, 32),
    el("div", { class: "meta" },
      el("div", { class: "name" }, `${ch.number} · ${ch.name}`),
      el("div", { class: "sub" }, sub)),
    el("div", { class: "badges" },
      drafts.get(ch.id) ? statusChip("warn", "draft") : null,
      ch.archived ? statusChip("dim", "archived") : ch.paused ? statusChip("dim", "paused") : null),
  );
  return row;
}

function describeSub(ch) {
  const g = api.groups().find((x) => x.id === ch.groupId)?.name || "";
  if (ch.source?.type === "savedFilter") return `${g} · saved search`;
  if (ch.source?.type === "tag") return `${g} · tag set (${(ch.source.ids || [ch.source.id]).length} tags)`;
  if (ch.source?.type === "performer") return `${g} · performer`;
  if (ch.source?.type === "studio") return `${g} · studio`;
  return `${g} · rules`;
}

function filteredChannels() {
  const q = state.query.trim().toLowerCase();
  const channels = api.getLibrary().channels;
  if (!q) return channels;
  return channels.filter((c) =>
    c.name.toLowerCase().includes(q) || String(c.number).includes(q)
    || (c.sourceLabel || "").toLowerCase().includes(q));
}

function renderDial() {
  clear(dialListHost);
  const channels = filteredChannels();
  const groups = [...api.groups()].sort((a, b) => a.position - b.position);
  const itemHeight = 44;
  const rows = [];
  for (const g of groups) {
    const members = channels.filter((c) => c.groupId === g.id);
    if (!members.length && state.query) continue;
    const collapsed = state.collapsed.has(g.id) && !state.query;
    rows.push({ type: "header", g, count: members.length });
    if (!collapsed) for (const c of members) rows.push({ type: "ch", c });
  }
  const orphaned = channels.filter((c) => !groups.some((g) => g.id === c.groupId));
  for (const c of orphaned) rows.push({ type: "ch", c });

  list = virtualList({
    items: rows,
    itemHeight,
    render: (item) => {
      if (item.type === "header") {
        const h = el("div", {
          class: "group-header", role: "button", tabindex: "0",
          "aria-expanded": String(!state.collapsed.has(item.g.id)),
          onclick: () => { state.collapsed.has(item.g.id) ? state.collapsed.delete(item.g.id) : state.collapsed.add(item.g.id); renderDial(); },
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); h.click(); } },
        },
          el("span", {}, item.g.name), el("span", { class: "count" }, `${item.count}`));
        return h;
      }
      return channelRow(item.c);
    },
  });
  list.node.style.height = "100%";
  dialListHost.append(list.node);
}

function renderDialTools() {
  clear(dialTools);
  const input = el("input", {
    type: "search", placeholder: "Search 519 channels… (press /)", value: state.query,
    "aria-label": "Search channels", style: "width:100%",
    oninput: debounce(() => { state.query = input.value; renderDial(); }, 140),
  });
  dialTools.append(
    el("div", { class: "searchbox", style: "width:100%" }, el("span", { class: "icon" }, "⌕"), input),
    el("div", { style: "display:flex; gap:8px; flex-wrap:wrap" },
      el("button", {
        class: `btn small ${state.bulkMode ? "primary" : ""}`,
        "aria-pressed": String(state.bulkMode),
        onclick: () => { state.bulkMode = !state.bulkMode; if (!state.bulkMode) state.selected.clear(); renderDialTools(); renderDial(); renderBulkBar(); },
      }, state.bulkMode ? "Selecting… done" : "Select…"),
      el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: () => renderDial() }) }, "Groups…"),
    ),
  );
  document.onkeydown = (e) => {
    if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      e.preventDefault(); input.focus(); input.select();
    }
  };
}

function renderBulkBar() {
  const existing = document.getElementById("studio-bulk");
  if (existing) existing.remove();
  if (!state.bulkMode) return;
  const bar = el("div", { class: "bulk-bar active", id: "studio-bulk", role: "toolbar", "aria-label": "Bulk actions" },
    el("strong", {}, `${state.selected.size} selected`),
    el("button", {
      class: "btn small", disabled: !state.selected.size,
      onclick: () => bulkMoveFlow({ api, drafts, channelIds: [...state.selected], onApplied: () => { state.selected.clear(); renderDial(); renderBulkBar(); } }),
    }, "Move to group…"),
    el("button", {
      class: "btn small", disabled: !state.selected.size,
      onclick: async () => {
        const ids = [...state.selected];
        const r = await api.apply(`bulk-${Date.now()}`, api.getLibrary().revision,
          [{ op: "channels.patch", channelIds: ids, patch: { paused: true, enabled: false } }]);
        r.status === "committed" ? toast(`Paused ${ids.length}.`, "ok") : toast(`Rejected: ${r.error}`, "err");
        state.selected.clear(); renderDial(); renderBulkBar();
      },
    }, "Pause"),
    el("button", { class: "btn ghost small", onclick: () => { state.selected.clear(); renderDial(); renderBulkBar(); } }, "Clear"),
  );
  dial.prepend(bar);
}

function selectChannel(id) {
  state.selectedId = id;
  renderDial();
  if (editor) editor.retarget(id);
  else editor = createEditorPane({ api, drafts, channelId: id, onChanged: () => renderDial() });
  renderEditorHead();
  if (window.innerWidth <= 760) editorPane.scrollIntoView({ behavior: "smooth" });
}

let headNode = null;
function renderEditorHead() {
  if (headNode) headNode.remove();
  const ch = api.getDefinition(state.selectedId);
  if (!ch) return;
  headNode = el("div", { class: "editor-head" },
    tile(ch, 36),
    el("div", {},
      el("h2", { style: "font-size:16px" }, ch.name),
      el("div", { class: "sub", style: "color:var(--text-dim);font-size:12px" },
        `Channel ${ch.number} · seed count ${fmtCount(ch.seedCount)} (historical, simulated) · ${ch.provenance?.origin === "v4-final-proposal" ? "from v4-final proposal" : "custom"}`)),
    el("span", { class: "spacer", style: "flex:1" }),
    ch.archived ? el("button", {
      class: "btn small", onclick: async () => {
        const r = await api.apply(`arch-${Date.now()}`, api.getLibrary().revision, [{ op: "channel.put", channel: { ...clone(ch), archived: false } }]);
        if (r.status === "committed") { toast("Restored to the guide.", "ok"); refreshAll(); }
      },
    }, "Restore") : null,
  );
  editorPane.prepend(headNode);
}

function clone(v) { return JSON.parse(JSON.stringify(v)); }

function refreshAll() {
  renderDialTools();
  renderDial();
  renderEditorHead();
  if (editor) { editor.refreshStored(); }
}

// ---------- boot ----------
state.selectedId = "net_04685144"; // start on the tier's first channel
const bar = topBar({
  title: "Prototype A", sub: "Channel Studio",
  api, drafts, variant: "studio",
  extra: [el("span", { class: "pill sim", title: "All counts, previews and artwork in these prototypes are simulated" }, "simulated data")],
  onReset: () => { state.selected.clear(); refreshAll(); if (editor) editor.retarget(state.selectedId); },
});
editor = createEditorPane({ api, drafts, channelId: state.selectedId, onChanged: () => { renderEditorHead(); renderDial(); } });
editorPane.append(editor.node);
document.getElementById("app").append(bar, main);
renderDialTools();
renderDial();
renderEditorHead();
