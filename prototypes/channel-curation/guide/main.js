// Prototype D — Visual Guide.
// Workflow: browse the lineup the way it airs — a guide grid with group bands
// and simulated programs. Click a program for context; edit the channel from
// a side panel without losing your place. Bulk organization lives behind the
// Manage toggle. Group tabs filter the grid; arrows move between programs.
//
// The grid renders only the rows in view (519 channels × ~12 blocks would
// otherwise blow up the renderer); horizontal scroll is shared with the
// timeline via a scroll sync.

import { createMockApi, draftStore } from "../shared/mock-api.js";
import { topBar, createEditorPane, guardUnload } from "../shared/editor-sections.js";
import { bulkMoveFlow, groupsManagerFlow } from "../shared/bulk.js";
import { el, clear, tile, toast, statusChip, hashOf, debounce } from "../shared/ui.js";

const api = createMockApi();
const drafts = draftStore("guide");
guardUnload(api, drafts);

const SLOT_MIN = 30;
const WINDOW_H = 6;
const PX_PER_MIN = 3.4;
const WINDOW_PX = WINDOW_H * 60 * PX_PER_MIN;
const LABEL_W = 210;
const ROW_H = 46;

const state = { groupFilter: "all", query: "", manage: false, selected: new Set(), nowOffsetMin: hashOf(String(new Date().getHours() || 12)) % 30 };

const main = el("main", { class: "gd-main" });
const tools = el("div", { class: "gd-tools" });
const gridHost = el("div", { class: "grid-host" });
const gridCol = el("div", { style: "flex:1;min-width:0;display:flex;flex-direction:column" });
const timeline = el("div", { style: "overflow:hidden;flex:none;border-bottom:1px solid var(--line-strong);background:var(--bg-raise)" });
const epgViewport = el("div", { style: "flex:1;min-height:0;overflow:auto;position:relative" });
const epgCanvas = el("div", { style: "position:relative;width:100%" });
epgViewport.append(epgCanvas);
gridCol.append(timeline, epgViewport);
const ctxPanel = el("aside", { class: "ctx-panel", "aria-label": "Channel editor" });
gridHost.append(gridCol, ctxPanel);
main.append(tools, gridHost);

let ctxEditor = null;
let rowItems = [];

// ---------- simulated schedule (deterministic, clearly labeled) ----------
const TITLES = ["Late Shift", "Quiet Hours", "Double Feature", "Backlot", "After Curfew",
  "First Take", "Encore", "Matinee", "Nightcap", "Cold Open", "Director's Cut", "Intermission",
  "Primetime", "Rerun Alley", "The Long Take", "Overture", "Marathon", "Rewind"];
function programsFor(ch, windowMinutes) {
  const rng = hashOf(ch.id + ch.sort);
  let t = 0;
  const out = [];
  let i = 0;
  while (t < windowMinutes && i < 24) {
    // NB: >>> (unsigned) — >> is signed and flips negatives on large hashes.
    const dur = 30 + ((rng >>> (i % 22)) % 3) * 30; // 30/60/90 min
    const title = TITLES[(rng >>> ((i * 3) % 26)) % TITLES.length];
    out.push({ start: t, dur, title: `${title}${i % 4 === 3 ? " (encore)" : ""}` });
    t += dur;
    i++;
  }
  return out;
}

// ---------- virtualized grid ----------
function buildRowItems() {
  const groups = [...api.groups()].sort((a, b) => a.position - b.position)
    .filter((g) => state.groupFilter === "all" || state.groupFilter === g.id);
  const q = state.query.trim().toLowerCase();
  rowItems = [];
  for (const g of groups) {
    let channels = api.getLibrary().channels.filter((c) => c.groupId === g.id);
    if (q) channels = channels.filter((c) => c.name.toLowerCase().includes(q) || String(c.number).includes(q));
    if (!channels.length) continue;
    rowItems.push({ type: "band", g, count: channels.length });
    for (const ch of channels) rowItems.push({ type: "ch", ch });
  }
}

function renderTimeline() {
  clear(timeline);
  const inner = el("div", { style: `display:flex;width:${LABEL_W + WINDOW_PX}px` },
    el("div", { style: `width:${LABEL_W}px;flex:none;padding:4px 8px;font-size:11px;color:var(--text-faint)` }, "SIMULATED"),
    Array.from({ length: WINDOW_H * 2 }, (_, i) => {
      const mins = i * SLOT_MIN;
      const hh = String(Math.floor((9 * 60 + mins) / 60) % 24).padStart(2, "0");
      return el("div", { style: `width:${SLOT_MIN * PX_PER_MIN}px;flex:none;font-size:11px;color:var(--text-dim);font-family:var(--mono);padding:4px 6px;border-left:1px solid var(--line)` }, `${hh}:${mins % 60 ? "30" : "00"}`);
    }));
  timeline.append(inner);
}

function channelRowNode(item) {
  const ch = item.ch;
  const row = el("div", { class: "chan-row", style: `width:${LABEL_W + WINDOW_PX}px;height:${ROW_H - 2}px` });
  const cell = el("div", {
    class: "chan-cell", role: "rowheader", tabindex: "0",
    onclick: () => openContext(ch.id),
    onkeydown: (e) => { if (e.key === "Enter") openContext(ch.id); },
  },
    tile(ch, 28),
    el("div", { style: "min-width:0" },
      el("div", { class: "cname" }, ch.name),
      el("div", { class: "cnum" }, `#${ch.number}${drafts.get(ch.id) ? " · draft" : ""}${ch.archived ? " · archived" : ""}`)),
    state.manage ? el("input", {
      type: "checkbox", "aria-label": `Select ${ch.name}`, checked: state.selected.has(ch.id),
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? state.selected.add(ch.id) : state.selected.delete(ch.id); renderTools(); },
    }) : null);
  row.append(cell);
  for (const p of programsFor(ch, WINDOW_H * 60)) {
    const isOnAir = p.start <= state.nowOffsetMin && state.nowOffsetMin < p.start + p.dur;
    row.append(el("div", {
      class: `prog ${isOnAir ? "onair" : ""}`, role: "gridcell", tabindex: "0",
      "aria-label": `${ch.name}, ${p.title}, ${p.dur} minutes${isOnAir ? ", on air now" : ""}`,
      style: `left:${LABEL_W + p.start * PX_PER_MIN}px;width:${p.dur * PX_PER_MIN - 3}px;height:${ROW_H - 8}px`,
      onclick: (e) => { e.stopPropagation(); openPopover(ch, p, e.currentTarget, isOnAir); },
      onkeydown: (e) => {
        if (e.key === "Enter") { openPopover(ch, p, e.currentTarget, isOnAir); }
        else if (e.key.startsWith("Arrow")) { e.preventDefault(); moveBlockFocus(e.currentTarget, e.key); }
      },
    },
      el("div", { class: "pt" }, p.title),
      el("div", { class: "pm" }, `${p.dur}m${isOnAir ? " · on air" : ""}`)));
  }
  return row;
}

function renderGrid() {
  buildRowItems();
  clear(epgCanvas);
  epgCanvas.style.height = `${rowItems.length * ROW_H}px`;
  const top = epgViewport.scrollTop;
  const viewH = epgViewport.clientHeight || 600;
  const start = Math.max(0, Math.floor(top / ROW_H) - 4);
  const end = Math.min(rowItems.length, Math.ceil((top + viewH) / ROW_H) + 4);
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i++) {
    const item = rowItems[i];
    let node;
    if (item.type === "band") {
      node = el("div", { class: "group-band", style: `height:${ROW_H - 2}px;width:${LABEL_W + WINDOW_PX}px` },
        el("span", {}, item.g.name),
        el("span", { style: "font-weight:400;text-transform:none;letter-spacing:0" }, `${item.count} channels`));
    } else {
      node = channelRowNode(item);
    }
    node.style.position = "absolute";
    node.style.top = `${i * ROW_H}px`;
    frag.append(node);
  }
  epgCanvas.append(frag);
}

epgViewport.addEventListener("scroll", () => {
  timeline.scrollLeft = epgViewport.scrollLeft;
  requestAnimationFrame(renderGrid);
}, { passive: true });

function moveBlockFocus(from, key) {
  const row = from.parentElement;
  const rows = [...epgCanvas.querySelectorAll(".chan-row")];
  if (key === "ArrowRight" || key === "ArrowLeft") {
    const blocks = [...row.querySelectorAll(".prog")];
    const i = blocks.indexOf(from);
    blocks[i + (key === "ArrowRight" ? 1 : -1)]?.focus();
    return;
  }
  const rIdx = rows.indexOf(row);
  const target = rows[rIdx + (key === "ArrowDown" ? 1 : -1)];
  if (!target) return;
  const tblocks = [...target.querySelectorAll(".prog")];
  const left = from.offsetLeft;
  const best = tblocks.reduce((acc, b) => {
    const d = Math.abs(b.offsetLeft - left);
    return !acc || d < acc.d ? { b, d } : acc;
  }, null);
  (best?.b || tblocks[0])?.focus();
}

// ---------- popover + context editor ----------
function openPopover(ch, prog, anchor, isOnAir) {
  document.querySelector(".popover")?.remove();
  const rect = anchor.getBoundingClientRect();
  const pop = el("div", { class: "popover", role: "dialog", "aria-label": `${prog.title} on ${ch.name}` },
    el("div", { class: "pop-body" },
      el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:8px" },
        tile(ch, 30),
        el("div", {},
          el("strong", {}, ch.name),
          el("div", { style: "color:var(--text-dim);font-size:12px" }, `#${ch.number} · ${api.groups().find((g) => g.id === ch.groupId)?.name}`))),
      el("div", { style: "font-weight:700;margin-bottom:2px" }, prog.title),
      el("div", { style: "color:var(--text-dim);font-size:12px;margin-bottom:10px" },
        `${prog.dur} min${isOnAir ? " · on air now" : ""} · simulated program`),
      el("div", { style: "display:flex;gap:8px" },
        el("button", { class: "btn small primary", onclick: () => { pop.remove(); openContext(ch.id); } }, "Edit this channel"),
        el("button", { class: "btn small", onclick: () => pop.remove() }, "Close"))));
  document.body.append(pop);
  const top = Math.min(window.innerHeight - 260, rect.bottom + 6);
  pop.style.top = `${Math.max(60, top)}px`;
  pop.style.left = `${Math.min(window.innerWidth - 340, Math.max(8, rect.left))}px`;
  pop.querySelector("button")?.focus();
  document.addEventListener("mousedown", function closer(e) {
    if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("mousedown", closer); }
  });
}

function openContext(channelId) {
  state.ctxId = channelId;
  clear(ctxPanel);
  ctxPanel.classList.add("open");
  const ch = api.getDefinition(channelId);
  ctxPanel.append(
    el("div", { class: "ctx-head" },
      tile(ch, 30),
      el("strong", { style: "flex:1;font-size:14px" }, ch.name),
      el("button", {
        class: "btn ghost small", "aria-label": "Close editor panel",
        onclick: () => { ctxEditor?.destroy(); ctxPanel.classList.remove("open"); state.ctxId = null; },
      }, "✕")));
  ctxEditor = createEditorPane({
    api, drafts, channelId,
    onChanged: () => { renderGrid(); },
  });
  ctxPanel.append(ctxEditor.node);
}

// ---------- tools ----------
function renderTools() {
  clear(tools);
  const groups = api.groups();
  const tabs = el("div", { class: "tabs", role: "group", "aria-label": "Filter by group" },
    el("button", { "aria-pressed": String(state.groupFilter === "all"), onclick: () => { state.groupFilter = "all"; renderTools(); renderGrid(); } }, "All groups"),
    groups.map((g) => el("button", {
      "aria-pressed": String(state.groupFilter === g.id),
      onclick: () => { state.groupFilter = g.id; renderTools(); renderGrid(); },
    }, `${g.name} (${api.getLibrary().channels.filter((c) => c.groupId === g.id).length})`)));
  const search = el("input", {
    type: "search", placeholder: "Jump to channel… ( / )", value: state.query, "aria-label": "Jump to channel",
    style: "width:200px",
    oninput: debounce(() => { state.query = search.value; epgViewport.scrollTop = 0; renderGrid(); }, 150),
  });
  tools.append(
    tabs,
    el("span", { class: "spacer", style: "flex:1" }),
    search,
    el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: () => { renderTools(); renderGrid(); } }) }, "Groups…"),
    el("button", {
      class: `btn small ${state.manage ? "primary" : ""}`, "aria-pressed": String(state.manage),
      onclick: () => { state.manage = !state.manage; if (!state.manage) state.selected.clear(); renderTools(); renderGrid(); },
    }, state.manage ? "Managing… done" : "Manage channels"),
  );
  if (state.manage) {
    tools.append(
      el("strong", {}, `${state.selected.size} selected`),
      el("button", {
        class: "btn small", disabled: !state.selected.size,
        onclick: () => bulkMoveFlow({ api, drafts, channelIds: [...state.selected], onApplied: () => { state.selected.clear(); renderTools(); renderGrid(); } }),
      }, "Move to group…"));
  }
}

// ---------- boot ----------
const bar = topBar({
  title: "Prototype D", sub: "Visual Guide",
  api, drafts, variant: "guide",
  extra: [el("span", { class: "pill sim" }, "simulated schedule")],
  onReset: () => { state.selected.clear(); ctxEditor?.destroy(); ctxPanel.classList.remove("open"); renderTools(); renderGrid(); },
});
document.getElementById("app").append(bar, main);
renderTimeline();
renderTools();
renderGrid();
document.onkeydown = (e) => {
  if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    tools.querySelector("input")?.focus();
  }
};
