// Bulk operations shared by all variants: group management + bulk move,
// each committed as ONE explicit Apply transaction (touched records only).

import { el, openDialog, toast, tile } from "./ui.js";

let bulkCounter = 0;

function newRequestId() {
  return `bulk-${Date.now().toString(36)}-${(bulkCounter++).toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// Opens the move dialog for `channelIds`. Commits:
//   [group.put (when creating)] + [channels.move]  — one Apply, one revision.
export function bulkMoveFlow({ api, drafts, channelIds, onApplied }) {
  if (!channelIds.length) { toast("Select at least one channel first.", "err"); return; }
  const groups = api.groups();
  let targetGroupId = groups[0]?.id;
  let createName = "";
  const dirtyIds = channelIds.filter((id) => drafts.get(id));

  const radioExisting = el("input", { type: "radio", name: "move-target", id: "mv-existing", checked: "true" });
  const select = el("select", { "aria-label": "Destination group", style: "width:100%", onchange: () => { targetGroupId = select.value; } },
    groups.map((g) => el("option", { value: g.id }, g.name)));
  select.addEventListener("focus", () => { radioExisting.checked = true; });
  select.addEventListener("change", () => { radioExisting.checked = true; });

  const radioNew = el("input", { type: "radio", name: "move-target", id: "mv-new" });
  const nameInput = el("input", {
    type: "text", placeholder: "New group name", "aria-label": "New group name", style: "width:100%",
    oninput: () => { createName = nameInput.value; radioNew.checked = true; },
    onfocus: () => { radioNew.checked = true; },
  });

  const affected = el("p", { style: "color:var(--text-dim)" },
    el("strong", {}, `${channelIds.length}`), ` channel${channelIds.length === 1 ? "" : "s"} will move in one Apply.`,
    dirtyIds.length ? el("span", {}, ` ${dirtyIds.length} of them have unsaved drafts — the move is written into those drafts too (they stay draft).`) : null);

  const applyBtn = el("button", { class: "btn primary", onclick: () => void commit() }, "Apply move");
  const dlg = openDialog({
    title: `Move ${channelIds.length} channel${channelIds.length === 1 ? "" : "s"}`,
    body: el("div", {},
      affected,
      el("div", { class: "field" },
        el("div", { style: "display:flex;gap:8px;align-items:center" }, radioExisting, el("label", { for: "mv-existing" }, "Move to existing group:")),
        el("div", { style: "margin:6px 0 12px 24px" }, select)),
      el("div", { class: "field" },
        el("div", { style: "display:flex;gap:8px;align-items:center" }, radioNew, el("label", { for: "mv-new" }, "Create a new group and move there:")),
        el("div", { style: "margin:6px 0 0 24px" }, nameInput)),
    ),
    actions: [{ label: "Cancel" }],
    onClose: () => {},
  });
  dlg.node.querySelector("footer").append(applyBtn);

  async function commit() {
    const ops = [];
    let groupId = targetGroupId;
    if (radioNew.checked) {
      if (!createName.trim()) { toast("Name the new group first.", "err"); return; }
      groupId = `grp_${(bulkCounter++).toString(36)}${Date.now().toString(36).slice(-4)}`;
      const maxPos = Math.max(0, ...groups.map((g) => g.position));
      ops.push({ op: "group.put", group: { id: groupId, name: createName.trim(), position: maxPos + 1 } });
    }
    ops.push({ op: "channels.move", channelIds: [...channelIds], groupId });
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    try {
      const receipt = await api.apply(newRequestId(), api.getLibrary().revision, ops);
      if (receipt.status === "committed") {
        for (const id of dirtyIds) {
          const d = drafts.get(id);
          if (d) { d.draft.groupId = groupId; drafts.put(id, d); }
        }
        toast(`Moved ${channelIds.length} channel${channelIds.length === 1 ? "" : "s"}${radioNew.checked ? ` into new group “${createName.trim()}”` : ""} — applied.`, "ok");
        dlg.close();
        onApplied?.(receipt, groupId);
      } else {
        applyBtn.disabled = false;
        applyBtn.textContent = "Apply move";
        toast(`Move rejected: ${receipt.error}${receipt.currentRevision ? ` (server at r${receipt.currentRevision})` : ""}. Nothing changed — try again.`, "err");
      }
    } catch (err) {
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply move";
      toast(`Move failed to reach the server (${err.message}). Nothing changed.`, "err");
    }
  }
}

// Groups manager: create / rename / reorder / delete-with-destination.
export function groupsManagerFlow({ api, drafts, onApplied }) {
  function render() {
    const groups = api.groups();
    const rows = groups.map((g, i) => {
      const members = api.getLibrary().channels.filter((c) => c.groupId === g.id);
      const nameInput = el("input", {
        type: "text", value: g.name, "aria-label": `Group name ${g.name}`, style: "flex:1",
        onchange: () => void commit({ op: "group.put", group: { id: g.id, name: nameInput.value, position: g.position } }),
      });
      return el("div", { class: "entity-row", style: "border-bottom:1px solid var(--line)" },
        el("span", { class: "eid" }, `#${g.position}`),
        nameInput,
        el("span", { class: "eid" }, `${members.length} ch`),
        el("button", {
          class: "btn ghost small", "aria-label": `Move ${g.name} up`, disabled: i === 0,
          onclick: () => void reorder(g, -1),
        }, "↑"),
        el("button", {
          class: "btn ghost small", "aria-label": `Move ${g.name} down`, disabled: i === groups.length - 1,
          onclick: () => void reorder(g, +1),
        }, "↓"),
        el("button", {
          class: "btn ghost small", "aria-label": `Delete group ${g.name}`,
          onclick: () => void removeGroup(g, members.length),
        }, "🗑"),
      );
    });

    const newList = el("div", { class: "entity-list" }, rows);
    const nameInput = el("input", { type: "text", placeholder: "New group name", "aria-label": "New group name", style: "flex:1" });
    const addBtn = el("button", {
      class: "btn primary small",
      onclick: () => {
        if (!nameInput.value.trim()) { toast("Give the group a name.", "err"); return; }
        void commit({ op: "group.put", group: { id: `grp_new${Date.now().toString(36)}`, name: nameInput.value.trim(), position: groups.length + 1 } });
      },
    }, "Create");
    const dlg = openDialog({
      title: "Groups",
      body: el("div", {},
        el("p", { class: "hint", style: "color:var(--text-dim);margin:0 0 8px" },
          "Every channel belongs to exactly one group. Renames and reorders apply immediately (one revision each); deleting a populated group asks where its channels go."),
        newList,
        el("div", { style: "display:flex;gap:8px;margin-top:10px" }, nameInput, addBtn),
      ),
      actions: [{ label: "Close", kind: "primary" }],
      onClose: () => onApplied?.(),
    });
    return dlg;
  }

  async function commit(...ops) {
    try {
      const r = await api.apply(newRequestId(), api.getLibrary().revision, ops);
      if (r.status === "committed") { toast("Applied.", "ok"); render(); }
      else toast(`Rejected: ${r.error}.`, "err");
    } catch (err) { toast(`Failed: ${err.message}`, "err"); }
  }

  async function reorder(g, dir) {
    const groups = [...api.groups()].sort((a, b) => a.position - b.position);
    const i = groups.findIndex((x) => x.id === g.id);
    const j = i + dir;
    if (j < 0 || j >= groups.length) return;
    [groups[i], groups[j]] = [groups[j], groups[i]];
    const ops = groups.map((x, idx) => ({ op: "group.put", group: { id: x.id, name: x.name, position: idx + 1 } }));
    await commit(...ops);
  }

  async function removeGroup(g, memberCount) {
    const groups = api.groups();
    if (groups.length <= 1) { toast("Cannot remove the last group.", "err"); return; }
    const select = el("select", { "aria-label": "Destination for members", style: "width:100%" },
      groups.filter((x) => x.id !== g.id).map((x) => el("option", { value: x.id }, x.name)));
    const dlg = openDialog({
      title: `Delete “${g.name}”`,
      body: el("div", {},
        el("p", {}, `${memberCount} channel${memberCount === 1 ? "" : "s"} will move to:`),
        select),
      actions: [{ label: "Cancel" }, { label: "Delete group", kind: "danger", value: true }],
      onClose: (ok) => {
        if (ok) void commit({ op: "group.delete", id: g.id, moveTo: select.value });
      },
    });
  }

  return render();
}
