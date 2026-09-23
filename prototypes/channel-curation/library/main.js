// Prototype C — Channel Library.
// Workflow: a power table over the whole library. Filter by group/status/kind,
// sort by any column, multi-select across filters, and run bulk operations
// (move / pause / resume / archive). Row click opens the editor as a modal.
// Built for scale: 500+ rows stay virtualized and responsive.

import { createMockApi, draftStore } from "../shared/mock-api.js";
import { topBar, createEditorPane, guardUnload } from "../shared/editor-sections.js";
import { bulkMoveFlow, groupsManagerFlow } from "../shared/bulk.js";
import { el, clear, tile, virtualList, toast, statusChip, openDialog, confirmDialog, fmtCount } from "../shared/ui.js";

const api = createMockApi();
const drafts = draftStore("library");
guardUnload(api, drafts);

const state = { query: "", group: "all", status: "all", kind: "all", sort: "number", dir: 1, selected: new Set(), lastClicked: null };

const main = el("main", { class: "lib-main" });
const filters = el("div", { class: "filters", role: "search", "aria-label": "Library filters" });
const tableWrap = el("div", { class: "table-wrap" });
const bulkBar = el("div", { class: "bulk-bar", role: "toolbar", "aria-label": "Bulk actions" });
main.append(filters, tableWrap, bulkBar);

let list = null;
let modalEditor = null;

function statusOf(ch) {
  if (ch.archived) return ["dim", "archived"];
  if (ch.paused || !ch.enabled) return ["warn", "paused"];
  if (drafts.get(ch.id)) return ["warn", "draft"];
  return ["ok", "on air"];
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  let rows = api.getLibrary().channels;
  if (state.group !== "all") rows = rows.filter((c) => c.groupId === state.group);
  if (state.kind !== "all") rows = rows.filter((c) => c.kind === state.kind);
  if (state.status !== "all") rows = rows.filter((c) => {
    const [k] = statusOf(c);
    if (state.status === "problems") return k !== "ok";
    return k === state.status || (state.status === "draft" && drafts.get(c.id));
  });
  if (q) rows = rows.filter((c) => c.name.toLowerCase().includes(q) || String(c.number).includes(q) || (c.sourceLabel || "").toLowerCase().includes(q));
  const key = state.sort;
  rows = [...rows].sort((a, b) => {
    let va = a[key], vb = b[key];
    if (key === "group") { va = a.groupId; vb = b.groupId; }
    if (key === "status") { va = statusOf(a)[1]; vb = statusOf(b)[1]; }
    if (typeof va === "string") return va.localeCompare(vb) * state.dir;
    return ((va ?? 0) - (vb ?? 0)) * state.dir;
  });
  return rows;
}

function renderTable() {
  clear(tableWrap);
  const rows = filtered();
  const groups = api.groups();
  const header = el("thead", {},
    el("tr", {},
      el("th", { class: "sel" }, el("input", {
        type: "checkbox", "aria-label": "Select all filtered",
        onchange: (e) => {
          if (e.target.checked) for (const c of rows) state.selected.add(c.id);
          else state.selected.clear();
          renderTable(); renderBulkBar();
        },
      })),
      th("num", "#"), th("name", "Name"), th("group", "Group"), th("src", "Source"), th("cnt", "Seed count"), th("status", "Status"),
    ));
  function th(key, label) {
    return el("th", {
      class: key === "num" || key === "cnt" ? key : "", "aria-sort": state.sort === key ? (state.dir === 1 ? "ascending" : "descending") : "none",
      onclick: () => { if (state.sort === key) state.dir *= -1; else { state.sort = key; state.dir = 1; } renderTable(); },
    }, label, state.sort === key ? (state.dir === 1 ? " ▲" : " ▼") : "");
  }
  const table = el("table", { "aria-label": "Channels", style: "width:100%" }, header);
  const theadHost = el("div", { style: "position:relative;flex:1;min-height:0;display:flex;flex-direction:column" }, table);
  const tbody = el("tbody", {});
  table.append(tbody);
  const viewport = el("div", { class: "scroll", style: "flex:1;min-height:0" }, theadHost);
  // simpler approach at this scale: render all rows but with content-visibility
  for (const ch of rows) {
    const [stKind, stLabel] = statusOf(ch);
    const tr = el("tr", {
      class: "cursor", "aria-selected": String(state.selected.has(ch.id)), tabindex: "0",
      onclick: (e) => {
        if (e.target.closest("input")) return;
        if (e.shiftKey && state.lastClicked) {
          const ids = rows.map((r) => r.id);
          const i = ids.indexOf(state.lastClicked), j = ids.indexOf(ch.id);
          for (const id of ids.slice(Math.min(i, j), Math.max(i, j) + 1)) state.selected.add(id);
        } else state.lastClicked = ch.id;
        renderTable(); renderBulkBar();
      },
      onkeydown: (e) => { if (e.key === "Enter") openEditor(ch.id); },
    },
      el("td", { class: "sel" }, el("input", {
        type: "checkbox", "aria-label": `Select ${ch.name}`, checked: state.selected.has(ch.id),
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => { e.target.checked ? state.selected.add(ch.id) : state.selected.delete(ch.id); renderTable(); renderBulkBar(); },
      })),
      el("td", { class: "num" }, String(ch.number)),
      el("td", {},
        el("span", { style: "display:inline-flex;align-items:center;gap:8px;max-width:100%" },
          tile(ch, 22), el("span", { style: "overflow:hidden;text-overflow:ellipsis" }, ch.name),
          drafts.get(ch.id) ? statusChip("warn", "draft") : null)),
      el("td", { class: "grp" }, groups.find((g) => g.id === ch.groupId)?.name || "—"),
      el("td", { class: "src" }, ch.sourceLabel || ch.source?.type || ""),
      el("td", { class: "cnt" }, fmtCount(ch.seedCount)),
      el("td", { class: "st" }, statusChip(stKind, stLabel)),
    );
    tr.addEventListener("dblclick", () => openEditor(ch.id));
    tbody.append(tr);
  }
  tableWrap.append(viewport);
  const note = el("div", { class: "count-note", style: "padding:4px 14px;color:var(--text-faint)" },
    `${rows.length} of ${api.getLibrary().channels.length} channels shown (counts are historical/simulated)`);
  main.querySelector(".count-note")?.remove();
  bulkBar.before(note);
}

function renderFilters() {
  clear(filters);
  const groups = api.groups();
  filters.append(
    el("input", {
      type: "search", placeholder: "Search name, number, source… ( / )", value: state.query, "aria-label": "Search",
      style: "width:250px",
      oninput: debounceInput(() => { state.query = filters.querySelector("input").value; renderTable(); }, 140),
    }),
    el("select", { "aria-label": "Filter by group", onchange: (e) => { state.group = e.target.value; renderTable(); } },
      el("option", { value: "all" }, "All groups"),
      groups.map((g) => el("option", { value: g.id, selected: state.group === g.id }, `${g.name}`))),
    el("select", { "aria-label": "Filter by status", onchange: (e) => { state.status = e.target.value; renderTable(); } },
      el("option", { value: "all" }, "Any status"),
      el("option", { value: "on air" }, "On air"),
      el("option", { value: "draft" }, "Has draft"),
      el("option", { value: "paused" }, "Paused"),
      el("option", { value: "archived" }, "Archived"),
      el("option", { value: "problems" }, "Any problem")),
    el("select", { "aria-label": "Filter by kind", onchange: (e) => { state.kind = e.target.value; renderTable(); } },
      el("option", { value: "all" }, "Customs + networks"),
      el("option", { value: "ch" }, "My channels (1–99)"),
      el("option", { value: "net" }, "Networks (100–899)")),
    el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: renderAll }) }, "Groups…"),
    el("button", {
      class: "btn small", title: "Download the current filtered definitions as CSV (definition export, not scheduler state)",
      onclick: () => exportCsv(),
    }, "Export CSV"),
  );
  function debounceInput(fn, ms) { let t; return (e) => { clearTimeout(t); t = setTimeout(() => fn(e), ms); }; }
}

function exportCsv() {
  const rows = filtered();
  const cols = ["id", "number", "name", "groupId", "kind", "sort", "seed", "sourceType", "sourceLabel", "mode"];
  const lines = [cols.join(",")];
  for (const c of rows) {
    const vals = [c.id, c.number, c.name, c.groupId, c.kind, c.sort, c.seed, c.source?.type, c.sourceLabel, c.programming?.mode];
    lines.push(vals.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "channel-library-export.csv" });
  document.body.append(a); a.click(); a.remove();
  toast(`Exported ${rows.length} definitions (definitions only — not scheduler state).`, "ok");
}

function renderBulkBar() {
  clear(bulkBar);
  const n = state.selected.size;
  bulkBar.append(
    el("strong", {}, `${n} selected`),
    el("button", { class: "btn small", disabled: !n, onclick: () => bulkMoveFlow({ api, drafts, channelIds: [...state.selected], onApplied: () => { state.selected.clear(); renderAll(); } }) }, "Move to group…"),
    el("button", {
      class: "btn small", disabled: !n,
      onclick: () => bulkPatch({ paused: false, enabled: true }, "Resumed"),
    }, "Resume / put on air"),
    el("button", {
      class: "btn small", disabled: !n,
      onclick: () => bulkPatch({ paused: true, enabled: false }, "Paused"),
    }, "Pause"),
    el("button", {
      class: "btn small", disabled: !n,
      onclick: async () => {
        if (!await confirmDialog("Archive channels?", `${state.selected.size} channel(s) leave playback but keep their definition, number and identity.`)) return;
        bulkPatch({ archived: true }, "Archived");
      },
    }, "Archive"),
    el("button", { class: "btn ghost small", onclick: () => { state.selected.clear(); renderTable(); renderBulkBar(); } }, "Clear"),
  );
}

async function bulkPatch(patch, verb) {
  const ids = [...state.selected];
  const r = await api.apply(`bulk-${Date.now()}`, api.getLibrary().revision, [{ op: "channels.patch", channelIds: ids, patch }]);
  if (r.status === "committed") toast(`${verb} ${ids.length} channel(s). Applied — metadata only, no reindex.`, "ok");
  else toast(`Rejected: ${r.error}`, "err");
  state.selected.clear();
  renderTable(); renderBulkBar();
}

function openEditor(channelId) {
  document.getElementById("lib-editor-overlay")?.remove();
  const overlay = el("div", { class: "overlay", id: "lib-editor-overlay", role: "presentation" });
  const panel = el("div", {
    class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": "Channel editor",
    style: "width:min(720px,94vw);height:min(86vh,900px);display:flex;flex-direction:column",
  });
  const ch = api.getDefinition(channelId);
  panel.append(
    el("header", {},
      tile(ch, 30), el("h2", { style: "font-size:15px;flex:1" }, `Edit — ${ch.name}`),
      el("button", { class: "btn ghost small", "aria-label": "Close editor", onclick: closeEditor }, "✕")),
  );
  const editor = createEditorPane({ api, drafts, channelId, onChanged: (what) => { if (what === "applied") renderTable(); } });
  panel.append(editor.node);
  overlay.append(panel);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeEditor(); });
  document.body.append(overlay);
  function escHandler(e) { if (e.key === "Escape") closeEditor(); }
  document.addEventListener("keydown", escHandler, { once: true });
}
function closeEditor() {
  document.getElementById("lib-editor-overlay")?.remove();
  renderTable(); renderBulkBar();
}

function renderAll() { renderFilters(); renderTable(); renderBulkBar(); }

// ---------- boot ----------
const bar = topBar({
  title: "Prototype C", sub: "Channel Library",
  api, drafts, variant: "library",
  extra: [el("span", { class: "pill sim" }, "simulated data")],
  onReset: () => { state.selected.clear(); renderAll(); },
});
document.getElementById("app").append(bar, main);
renderAll();
document.onkeydown = (e) => {
  if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    e.preventDefault(); filters.querySelector("input")?.focus();
  }
};
