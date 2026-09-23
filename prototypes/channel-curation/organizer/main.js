// Prototype B — Group Organizer.
// Workflow: groups first. Pick a group, see its channels as cards, organize —
// drag cards between groups, multi-select and move, manage groups (rename,
// reorder, delete-with-destination). Channel editing opens a drawer so the
// organization view is never lost. Everything is one explicit Apply.

import { createMockApi, draftStore } from "../shared/mock-api.js";
import { topBar, createEditorPane, guardUnload } from "../shared/editor-sections.js";
import { bulkMoveFlow, groupsManagerFlow } from "../shared/bulk.js";
import { el, clear, tile, virtualList, toast, statusChip, fmtCount, debounce } from "../shared/ui.js";

const api = createMockApi();
const drafts = draftStore("organizer");
guardUnload(api, drafts);

const state = { groupId: null, query: "", selected: new Set(), drawerId: null };
let editor = null;

const groupRail = el("nav", { class: "group-rail", "aria-label": "Groups" });
const members = el("section", { class: "members", "aria-label": "Channels in group" });
const main = el("main", { class: "org-main" }, groupRail, members);

// ---------- group rail ----------
function renderRail() {
  clear(groupRail);
  const groups = [...api.groups()].sort((a, b) => a.position - b.position);
  if (!state.groupId) state.groupId = groups[0]?.id;
  for (const g of groups) {
    const count = api.getLibrary().channels.filter((c) => c.groupId === g.id && !c.archived).length;
    const row = el("div", {
      class: "g-row", role: "option", tabindex: "0",
      "aria-selected": String(state.groupId === g.id),
      onclick: () => { state.groupId = g.id; state.selected.clear(); renderRail(); renderMembers(); },
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); row.click(); } },
    },
      el("span", { class: "g-name" }, g.name),
      drafts.ids().some((id) => api.getDefinition(id)?.groupId === g.id) ? statusChip("warn", "draft") : null,
      el("span", { class: "g-count" }, String(count)));
    groupRail.append(row);
  }
  groupRail.append(el("div", { class: "g-tools" },
    el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: () => { renderRail(); renderMembers(); } }) }, "Manage groups…"),
    el("button", {
      class: "btn small", onclick: () => {
        state.drawerId = "__new__";
        openDrawer("__new__");
      },
    }, "New channel…"),
  ));
}

// ---------- member cards ----------
function cardFor(ch) {
  const card = el("div", {
    class: "card", role: "option", tabindex: "0", draggable: "true",
    "aria-selected": String(state.selected.has(ch.id)),
    onclick: (e) => {
      if (e.target.closest("input")) return;
      state.drawerId = ch.id;
      openDrawer(ch.id);
    },
    onkeydown: (e) => { if (e.key === "Enter") { state.drawerId = ch.id; openDrawer(ch.id); } },
  },
    el("input", {
      type: "checkbox", "aria-label": `Select ${ch.name}`, checked: state.selected.has(ch.id),
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? state.selected.add(ch.id) : state.selected.delete(ch.id); renderBulkHint(); },
    }),
    tile(ch, 32),
    el("div", { style: "min-width:0;flex:1" },
      el("div", { class: "c-name" }, `${ch.number} · ${ch.name}`),
      el("div", { class: "c-sub" }, `${ch.archived ? "archived · " : ch.paused ? "paused · " : ""}${describeSource(ch)}`)),
    drafts.get(ch.id) ? statusChip("warn", "draft") : null,
  );
  card.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/jw-channel", ch.id); e.dataTransfer.effectAllowed = "move"; });
  return card;
}

function describeSource(ch) {
  const s = ch.source || {};
  if (s.type === "savedFilter") return "saved search";
  if (s.type === "tag") return `tag set · ${(s.ids || [s.id]).length} tag(s)`;
  if (s.type === "performer") return "performer";
  if (s.type === "studio") return "studio";
  if (s.type === "filter") {
    const parts = [];
    for (const k of ["tags", "tagsAny", "performers", "performersAny", "studios", "studiosAny"]) {
      if (Array.isArray(s[k]) && s[k].length) parts.push(`${k.replace("Any", " (any)")}: ${s[k].length}`);
    }
    return parts.length ? `rules · ${parts.join(" · ")}` : "rules";
  }
  return s.type || "?";
}

function renderMembers() {
  clear(members);
  const groups = api.groups();
  const g = groups.find((x) => x.id === state.groupId) || groups[0];
  const q = state.query.trim().toLowerCase();
  const all = api.getLibrary().channels.filter((c) => c.groupId === g.id);
  const shown = q ? all.filter((c) => c.name.toLowerCase().includes(q) || String(c.number).includes(q)) : all;

  const search = el("input", {
    type: "search", value: state.query, placeholder: `Search in ${g.name}…`, "aria-label": `Search in ${g.name}`,
    style: "width:220px",
    oninput: debounce(() => { state.query = search.value; renderMembers(); }, 140),
  });
  const bulkHint = el("span", { class: "pill clean", id: "bulk-hint" }, `${state.selected.size} selected`);
  members.append(
    el("div", { class: "m-head" },
      el("h2", { style: "font-size:16px" }, g.name),
      el("span", { class: "sub", style: "color:var(--text-dim)" }, `${all.length} channels (numbers stay put when groups change)`),
      el("span", { class: "spacer", style: "flex:1" }),
      search,
      bulkHint,
      el("button", {
        class: "btn small", id: "org-move-btn", disabled: !state.selected.size,
        onclick: () => bulkMoveFlow({ api, drafts, channelIds: [...state.selected], onApplied: () => { state.selected.clear(); renderRail(); renderMembers(); } }),
      }, "Move selected…"),
    ),
  );
  const grid = el("div", { class: "cards" });
  for (const ch of shown) grid.append(cardFor(ch));
  members.append(grid);
  if (!shown.length) members.append(el("div", { class: "drop-empty" }, "Nothing here — drop cards or move channels in."));

  // group drop target: dropping anywhere on the members pane moves into g
  members.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
  members.addEventListener("drop", (e) => {
    const id = e.dataTransfer.getData("text/jw-channel");
    if (!id) return;
    e.preventDefault();
    void moveTo([id], g.id);
  });
}

function renderBulkHint() {
  const hint = document.getElementById("bulk-hint");
  if (hint) {
    hint.className = `pill ${state.selected.size ? "dirty" : "clean"}`;
    hint.textContent = `${state.selected.size} selected`;
  }
  const moveBtn = document.getElementById("org-move-btn");
  if (moveBtn) moveBtn.disabled = !state.selected.size;
}

async function moveTo(channelIds, groupId) {
  const r = await api.apply(`move-${Date.now()}`, api.getLibrary().revision, [{ op: "channels.move", channelIds, groupId }]);
  if (r.status === "committed") {
    for (const id of channelIds) {
      const d = drafts.get(id);
      if (d) { d.draft.groupId = groupId; drafts.put(id, d); }
    }
    toast(`Moved ${channelIds.length} → ${api.groups().find((x) => x.id === groupId)?.name}. Applied.`, "ok");
  } else toast(`Move rejected: ${r.error}`, "err");
  renderRail(); renderMembers();
}

// ---------- drawer editor ----------
function openDrawer(channelId) {
  document.getElementById("org-drawer")?.remove();
  const ch = channelId === "__new__" ? { name: "New channel" } : api.getDefinition(channelId);
  const drawer = el("aside", { class: "drawer", id: "org-drawer", "aria-label": "Channel editor drawer" });
  const head = el("div", { class: "drawer-head" },
    channelId === "__new__" ? el("span", { class: "tile", style: "width:32px;height:32px;background:#3949AB" }, "+") : tile(ch, 32),
    el("h2", { style: "font-size:15px;flex:1" }, channelId === "__new__" ? "New channel (draft)" : ch.name),
    el("button", { class: "btn ghost small", "aria-label": "Close drawer", onclick: closeDrawer }, "✕"));
  drawer.append(head);
  if (channelId === "__new__") {
    const nameInput = el("input", { type: "text", placeholder: "Channel name", "aria-label": "New channel name", style: "width:100%" });
    const num = api.nextFreeNumber(100, 899);
    const numInput = el("input", { type: "number", value: num ?? "", "aria-label": "Channel number", style: "width:130px" });
    const groups = api.groups();
    const gsel = el("select", { "aria-label": "Group", style: "width:100%" }, groups.map((g) => el("option", { value: g.id }, g.name)));
    const createBtn = el("button", {
      class: "btn primary",
      onclick: () => {
        const name = nameInput.value.trim();
        if (!name) { toast("Name the channel first.", "err"); return; }
        const id = `ch_${Math.random().toString(16).slice(2, 10)}`;
        const channel = {
          id, kind: "ch", number: parseInt(numInput.value, 10), name, glyph: "", color: "#3949AB",
          groupId: gsel.value, sort: "shuffle", seed: Math.floor(Math.random() * 2 ** 31),
          enabled: true, archived: false, paused: false,
          source: { type: "tag", id: "1189", ids: ["1189"] }, sourceLabel: "starter tag set",
          programming: { mode: "fixed" }, seedCount: 0,
          provenance: { origin: "created-in-prototype" },
        };
        api.apply(`create-${Date.now()}`, api.getLibrary().revision, [{ op: "channel.put", channel }])
          .then((r) => {
            if (r.status === "committed") { toast(`Created “${name}” at #${channel.number}. Applied.`, "ok"); closeDrawer(); renderRail(); renderMembers(); openDrawer(id); }
            else toast(`Create rejected: ${r.error}`, "err");
          });
      },
    }, "Create & edit");
    drawer.append(el("div", { class: "editor-body", style: "padding:14px 16px" },
      el("div", { class: "field" }, el("label", {}, "Name"), nameInput),
      el("div", { class: "fieldrow" },
        el("div", { class: "field" }, el("label", {}, "Number (next free in 100–899)"), numInput),
        el("div", { class: "field" }, el("label", {}, "Group"), gsel)),
      el("p", { class: "hint", style: "color:var(--text-dim)" }, "Creation is a committed Apply (identity is assigned on commit), then the drawer switches to editing it."),
      createBtn));
  } else {
    editor = createEditorPane({
      api, drafts, channelId,
      onChanged: () => { renderRail(); renderMembers(); renderHead(); },
    });
    drawer.append(editor.node);
  }
  document.body.append(drawer);
  function renderHead() { /* drawer head refresh is handled by retarget */ }
}
function closeDrawer() {
  document.getElementById("org-drawer")?.remove();
  editor?.destroy();
  editor = null;
  state.drawerId = null;
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.drawerId) closeDrawer(); });

// ---------- boot ----------
const bar = topBar({
  title: "Prototype B", sub: "Group Organizer",
  api, drafts, variant: "organizer",
  extra: [el("span", { class: "pill sim" }, "simulated data")],
  onReset: () => { state.selected.clear(); renderRail(); renderMembers(); closeDrawer(); },
});
document.getElementById("app").append(bar, main);
renderRail();
renderMembers();
