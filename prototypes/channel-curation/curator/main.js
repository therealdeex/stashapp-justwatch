// Prototype E — Guided Curator.
// Workflow: progressive editing. Pick a channel (or start a new one), then walk
// identity → group → rules → programming, and meet a review step that shows the
// exact diff before a single Apply. Nothing commits early; drafts survive
// leaving and coming back. Bulk organizing is its own guided branch.

import { createMockApi, draftStore } from "../shared/mock-api.js";
import { topBar, guardUnload } from "../shared/editor-sections.js";
import { bulkMoveFlow, groupsManagerFlow } from "../shared/bulk.js";
import { ruleEditor, summaryBox } from "../shared/rules.js";
import { el, clear, tile, virtualList, toast, statusChip, confirmDialog, sectionCard, fmtCount, debounce, openDialog } from "../shared/ui.js";

const api = createMockApi();
const drafts = draftStore("curator");
guardUnload(api, drafts);

const STEPS = [
  { key: "pick", label: "Choose channel", sub: "find or create" },
  { key: "identity", label: "Identity", sub: "name, number, brand" },
  { key: "group", label: "Group", sub: "one group per channel" },
  { key: "rules", label: "What airs", sub: "rules & preview" },
  { key: "programming", label: "Programming", sub: "mode & pacing" },
  { key: "review", label: "Review & Apply", sub: "the only commit" },
];

const state = { step: "pick", channelId: null, pickedTitle: null };
let rulesCtl = null;

const main = el("main", { class: "cur-main" });
const stepsNav = el("nav", { class: "steps", "aria-label": "Curation steps" });
const stageHead = el("div", { class: "stage-head" });
const stageBody = el("div", { class: "stage-body scroll" });
const stageFoot = el("div", { class: "stage-foot" });
const stage = el("section", { class: "stage", "aria-label": "Editing stage" }, stageHead, stageBody, stageFoot);
main.append(stepsNav, stage);

function draftOrNull() {
  return state.channelId ? drafts.get(state.channelId)?.draft || api.getDefinition(state.channelId) : null;
}

// ---------- steps nav ----------
function renderSteps() {
  clear(stepsNav);
  const idx = STEPS.findIndex((s) => s.key === state.step);
  STEPS.forEach((s, i) => {
    const blocked = !state.channelId && s.key !== "pick";
    const stepEl = el("div", {
      class: `step ${s.key === state.step ? "current" : ""} ${i < idx ? "done" : ""} ${blocked ? "blocked" : ""}`,
      role: "button", tabindex: blocked ? "-1" : "0",
      "aria-current": s.key === state.step ? "step" : null,
      onclick: () => { if (!blocked) { state.step = s.key; render(); } },
      onkeydown: (e) => { if ((e.key === "Enter" || e.key === " ") && !blocked) { e.preventDefault(); stepEl.click(); } },
    },
      el("span", { class: "dot", "aria-hidden": "true" }, i < idx ? "✓" : String(i + 1)),
      el("span", {},
        el("div", { class: "st-label" }, s.label),
        el("div", { class: "st-sub" }, blocked ? "pick a channel first" : s.sub)));
    stepsNav.append(stepEl);
  });
  const ch = draftOrNull();
  stageHead.replaceChildren(
    ch ? el("div", { style: "display:flex;align-items:center;gap:12px" },
      tile(ch, 34),
      el("div", {},
        el("h1", { style: "font-size:17px" }, ch.name || "New channel"),
        el("div", { style: "color:var(--text-dim);font-size:12px" },
          `#${ch.number} · ${api.groups().find((g) => g.id === ch.groupId)?.name || "no group"}`)),
      el("span", { class: "spacer", style: "flex:1" }),
      drafts.get(state.channelId) ? statusChip("warn", "draft") : statusChip("ok", "saved"))
      : el("h1", { style: "font-size:17px" }, "Curate a channel"));
}

// ---------- step: pick ----------
function renderPick() {
  clear(stageBody);
  const q = el("input", {
    type: "search", placeholder: "Search 519 channels… ( / )", "aria-label": "Search channels",
    style: "width:100%;max-width:640px",
  });
  const listBox = el("div", { class: "picker-list", style: "height:52vh;display:flex;margin-top:10px" });
  stageBody.append(
    el("p", { style: "color:var(--text-dim);max-width:640px" },
      "Choose the channel you want to curate. Edits are staged as a draft across the next steps and commit only at Review."),
    el("div", { style: "display:flex;gap:8px;margin:8px 0 4px;flex-wrap:wrap" },
      el("button", { class: "btn small", onclick: () => createNewFlow() }, "New channel…"),
      el("button", { class: "btn small", onclick: () => bulkMoveFlow({ api, drafts, channelIds: askSelection(), onApplied: () => render() }) },
        "Organize multiple…"),
      el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: () => render() }) }, "Groups…")),
    q, listBox);

  function askSelection() {
    // lightweight multi-pick within this step
    const ids = [];
    const dlg = openDialog({
      title: "Select channels to move",
      body: (() => {
        const input = el("input", { type: "search", placeholder: "Filter…", "aria-label": "Filter", style: "width:100%;margin-bottom:8px" });
        const lst = el("div", { class: "entity-list", style: "height:50vh" });
        const renderL = (query = "") => {
          clear(lst);
          for (const c of api.getLibrary().channels) {
            if (query && !c.name.toLowerCase().includes(query.toLowerCase())) continue;
            lst.append(el("label", { class: "entity-row" },
              el("input", { type: "checkbox", checked: ids.includes(c.id), onchange: (e) => {
                const i = ids.indexOf(c.id);
                if (e.target.checked && i < 0) ids.push(c.id); else if (!e.target.checked && i >= 0) ids.splice(i, 1);
              } }),
              el("span", {}, `${c.number} · ${c.name}`)));
          }
        };
        input.addEventListener("input", debounce(() => renderL(input.value), 120));
        renderL();
        return el("div", {}, input, lst);
      })(),
      actions: [{ label: "Cancel" }, { label: "Continue…", kind: "primary", value: true }],
      onClose: (ok) => { if (!ok) ids.length = 0; },
    });
    return ids;
  }

  const renderList = debounce(() => {
    const query = q.value.trim().toLowerCase();
    const channels = api.getLibrary().channels
      .filter((c) => !query || c.name.toLowerCase().includes(query) || String(c.number).includes(query));
    const vl = virtualList({
      items: channels, itemHeight: 42,
      render: (ch) => el("div", {
        class: "row", role: "option", tabindex: "0",
        "aria-selected": String(state.channelId === ch.id),
        onclick: () => { state.channelId = ch.id; state.step = drafts.get(ch.id) ? "identity" : "identity"; render(); },
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ch && vl && (function () { state.channelId = ch.id; state.step = "identity"; render(); })(); } },
      },
        tile(ch, 30),
        el("div", { class: "meta" },
          el("div", { class: "name" }, `${ch.number} · ${ch.name}`),
          el("div", { class: "sub" }, api.groups().find((g) => g.id === ch.groupId)?.name || "")),
        drafts.get(ch.id) ? statusChip("warn", "draft") : null),
    });
    vl.node.style.height = "100%";
    clear(listBox); listBox.append(vl.node);
  }, 140);
  q.addEventListener("input", renderList);
  renderList();
}

function createNewFlow() {
  openDialog({
    title: "New channel",
    body: (() => {
      const name = el("input", { type: "text", placeholder: "Channel name", "aria-label": "Name", style: "width:100%" });
      const num = el("input", { type: "number", value: api.nextFreeNumber(100, 899) ?? "", "aria-label": "Number", style: "width:130px" });
      const wrap = el("div", {},
        el("div", { class: "field" }, el("label", {}, "Name"), name),
        el("div", { class: "field" }, el("label", {}, `Number (next free: ${api.nextFreeNumber(100, 899)})`), num),
        el("p", { class: "hint", style: "color:var(--text-dim)" },
          "The channel is committed when you Apply at Review — identity (id/seed) is assigned then. Until that Apply, nothing exists on the server."));
      wrap._fields = { name, num };
      return wrap;
    })(),
    actions: [{ label: "Cancel" }, { label: "Start draft", kind: "primary", value: true }],
    onClose: (ok) => {
      if (!ok) return;
      const fields = document.querySelector(".dialog .dialog-body div")._fields;
      const id = `ch_draft${Date.now().toString(36)}`;
      const channel = {
        id, kind: "ch", number: parseInt(fields.num.value, 10) || null,
        name: fields.name.value.trim() || "Untitled channel", glyph: "", color: "#3949AB",
        groupId: api.groups()[0]?.id, sort: "shuffle", seed: 0, enabled: true, archived: false,
        paused: false, source: { type: "filter", tags: [], tagsAny: [], excludeTags: [], performers: [], performersAny: [], studios: [], studiosAny: [], excludePerformers: [], excludeStudios: [] },
        sourceLabel: "new rules (draft)", programming: { mode: "fixed" }, seedCount: 0,
        provenance: { origin: "created-in-prototype" },
      };
      drafts.put(id, { v: 1, draft: channel, savedAt: Date.now() });
      state.channelId = id;
      state.step = "identity";
      render();
      toast("Draft started — nothing exists on the server until you Apply at Review.", "");
    },
  });
}

// ---------- step bodies (staged draft edits — no Apply until review) ----------
function stageDraft(mutate) {
  const d = draftOrNull();
  if (!d) return;
  mutate(d);
  drafts.put(state.channelId, { v: 1, draft: d, savedAt: Date.now() });
  renderSteps();
}

function renderIdentity() {
  clear(stageBody);
  const d = draftOrNull();
  const name = el("input", { type: "text", value: d.name, id: "f-name", "aria-label": "Channel name", style: "max-width:420px",
    oninput: () => stageDraft((x) => { x.name = name.value; }) });
  const number = el("input", { type: "number", value: d.number ?? "", id: "f-number", "aria-label": "Number", style: "width:140px",
    oninput: () => stageDraft((x) => { x.number = number.value === "" ? null : parseInt(number.value, 10); }) });
  const colors = el("div", { class: "entity-summary" });
  for (const c of ["#E91E63", "#D32F2F", "#F57C00", "#F9A825", "#3883C", "#00897B", "#00ACC1", "#3949AB", "#5E35B1", "#7B1FA2", "#455A64"]) {
    colors.append(el("button", {
      class: "tile", "aria-label": `Color ${c}`, style: `width:28px;height:28px;background:${c};border:2px solid ${d.color === c ? "#fff" : "transparent"}`,
      onclick: () => stageDraft((x) => { x.color = c; }),
    }));
  }
  stageBody.append(sectionCard("Identity",
    el("div", { class: "field" }, el("label", { for: "f-name" }, "Name"), name),
    el("div", { class: "field" }, el("label", { for: "f-number" }, "Number"), number,
      el("div", { class: "hint" }, "1–99 My Channels · 100–899 Networks. Swaps are atomic.")),
    el("div", { class: "field" }, el("label", {}, "Brand color"), colors),
  ));
}

function renderGroup() {
  clear(stageBody);
  const d = draftOrNull();
  const groups = [...api.groups()].sort((a, b) => a.position - b.position);
  stageBody.append(sectionCard("Group",
    el("p", { style: "margin:0 0 10px;color:var(--text-dim)" }, "Exactly one group per channel. Grouping organizes the guide; it never changes what airs."),
    el("div", { role: "radiogroup", "aria-label": "Group" },
      groups.map((g) => {
        const count = api.getLibrary().channels.filter((c) => c.groupId === g.id).length;
        return el("div", { class: "row", role: "radio", tabindex: "0", "aria-checked": String(d.groupId === g.id),
          onclick: () => stageDraft((x) => { x.groupId = g.id; }),
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stageDraft((x) => { x.groupId = g.id; }); } },
        },
          el("input", { type: "radio", name: "grp", checked: d.groupId === g.id, style: "accent-color:var(--accent)", onchange: () => stageDraft((x) => { x.groupId = g.id; }) }),
          el("div", { class: "meta" }, el("div", { class: "name" }, g.name), el("div", { class: "sub" }, `${count} channels`)));
      })),
    el("div", { style: "margin-top:10px" },
      el("button", { class: "btn small", onclick: () => groupsManagerFlow({ api, drafts, onApplied: () => render() }) }, "Create / manage groups…"))));
}

function renderRules() {
  clear(stageBody);
  const d = draftOrNull();
  const summaryHolder = el("div", {});
  const previewHolder = el("div", { id: "curator-preview" });
  const refreshSummary = () => {
    summaryHolder.replaceChildren(sectionCard("Plain-language summary", summaryBox(d.source)));
  };
  rulesCtl = ruleEditor(d.source, () => {
    d.source = rulesCtl.source();
    stageDraft(() => {}); // d is already the live draft object; persist it
    refreshSummary();
    void loadPreview();
  });
  stageBody.append(
    sectionCard("What airs", el("p", { class: "hint", style: "color:var(--text-faint);margin:0 0 8px" }, "Rows combine with AND."),
      rulesCtl.node),
    summaryHolder,
    previewHolder,
  );
  refreshSummary();
  void loadPreview();
}

async function loadPreview() {
  const d = draftOrNull();
  const holder = stageBody.querySelector("#curator-preview");
  if (!holder) return;
  holder.replaceChildren(el("p", { class: "hint", style: "color:var(--text-faint)" }, "Checking the draft…"));
  const result = await api.preview({ id: state.channelId, source: d.source, seedCount: d.seedCount, programming: d.programming });
  if (!stageBody.querySelector("#curator-preview")) return;
  clear(holder);
  holder.append(sectionCard("Preview (simulated)",
    el("div", { class: "count-line" },
      el("div", { class: "count-block" }, el("div", { class: "n" }, result.poolCount.toLocaleString()), el("div", { class: "l" }, "source pool")),
      el("div", { class: "count-block" }, el("div", { class: "n" }, result.rotationSize.toLocaleString()), el("div", { class: "l" }, "on-air rotation (≤50)"))),
    result.poolCount === 0 ? el("p", { class: "apply-state err" }, "Empty pool — the channel would be off air.") : null));
}

function renderProgramming() {
  clear(stageBody);
  const d = draftOrNull();
  const continuingOK = state.channelId === "net_461ab3e3" || state.channelId === "net_04685144";
  const mode = el("select", { id: "f-mode", "aria-label": "Mode", onchange: () => stageDraft((x) => { x.programming = { ...x.programming, mode: mode.value }; }) },
    el("option", { value: "fixed", selected: d.programming?.mode === "fixed" }, "Fixed loop"),
    el("option", { value: "explore", selected: d.programming?.mode === "explore" }, "Explore"),
    el("option", { value: "discovery", selected: d.programming?.mode === "discovery" }, "Discovery"),
    el("option", { value: "continuing", selected: d.programming?.mode === "continuing", disabled: !continuingOK },
      continuingOK ? "Continuing (rollout-enabled)" : "Continuing — gated off for this channel"));
  const spacing = el("input", { type: "number", value: d.programming?.spacingMinutes ?? "", "aria-label": "Spacing minutes", style: "width:120px",
    oninput: () => stageDraft((x) => { x.programming = { ...x.programming, spacingMinutes: spacing.value === "" ? undefined : parseInt(spacing.value, 10) }; }) });
  stageBody.append(sectionCard("Programming",
    el("div", { class: "field" }, el("label", { for: "f-mode" }, "Mode"), mode),
    el("div", { class: "field" }, el("label", {}, "Spacing"), spacing, el("span", { class: "hint" }, " minutes between repeats")),
    el("p", { class: "hint", style: "color:var(--text-faint)" }, "Continuing is controlled by the operator rollout — the GUI shows availability truthfully and cannot flip it on.")));
}

async function renderReview() {
  clear(stageBody);
  const d = draftOrNull();
  const stored = api.getDefinition(state.channelId);
  const isNew = !stored;
  const fields = [
    ["Name", stored?.name, d.name],
    ["Number", stored?.number, d.number],
    ["Group", api.groups().find((g) => g.id === stored?.groupId)?.name, api.groups().find((g) => g.id === d.groupId)?.name],
    ["Brand color", stored?.color, d.color],
    ["Source", stored?.sourceLabel || JSON.stringify(stored?.source), summarizeInline(d.source)],
    ["Programming", stored?.programming?.mode, d.programming?.mode],
    ["Status", stored?.archived ? "archived" : stored?.paused ? "paused" : "on air", d.archived ? "archived" : d.paused ? "paused" : "on air"],
  ];
  stageBody.append(
    sectionCard("Review",
      el("p", { style: "margin:0 0 10px;color:var(--text-dim)" },
        isNew ? "This is a new channel. Applying creates it and assigns its permanent identity."
          : "Applying submits exactly this snapshot as one revision. Edits after Apply need another Apply."),
      el("table", { class: "diff-table" },
        el("tr", {}, el("th", {}, ""), el("th", {}, "Now (server)"), el("th", {}, "Your draft")),
        fields.map(([label, oldV, newV]) => {
          const changed = JSON.stringify(oldV) !== JSON.stringify(newV);
          return el("tr", {},
            el("th", {}, label),
            el("td", { class: "old" }, formatVal(oldV)),
            el("td", { style: changed ? "color:var(--accent);font-weight:600" : "" },
              formatVal(newV), changed ? " •" : ""));
        })),
      changedCount(fields) === 0 ? el("p", { class: "hint", style: "color:var(--text-faint)" }, "No changes — Apply is disabled.") : null),
    sectionCard("Effect",
      el("ul", { style: "margin:0;padding-left:18px;color:var(--text-dim)" },
        el("li", {}, "Renaming / regrouping / rebranding: no reindex, no playback reset."),
        el("li", {}, "Source changes: only this channel's membership is re-prepared; aired history is preserved."),
        el("li", {}, "Applying one channel never publishes another channel's draft."))),
  );
  function summarizeInline(source) {
    if (source?.type === "savedFilter") return `saved search #${source.id}`;
    const parts = [];
    for (const [k, kind] of [["tags", "tags ALL"], ["tagsAny", "tags ANY"], ["performers", "performers ALL"], ["performersAny", "performers ANY"], ["studios", "studios ALL"], ["studiosAny", "studios ANY"], ["excludeTags", "excl tags"], ["excludePerformers", "excl performers"], ["excludeStudios", "excl studios"]]) {
      if (Array.isArray(source?.[k]) && source[k].length) parts.push(`${kind}×${source[k].length}`);
    }
    if (source?.date?.from || source?.date?.to) parts.push("date range");
    if (source?.duration) parts.push("duration");
    if (source?.createdAt) parts.push("recent");
    if (source?.q) parts.push("text");
    return parts.join(" + ") || "no rules";
  }
  function formatVal(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v);
  }
  function changedCount(fs) { return fs.filter(([, a, b]) => JSON.stringify(a) !== JSON.stringify(b)).length; }
  renderFoot();
  function renderFoot() { /* foot rendered by render() */ }
}

// ---------- footer nav ----------
function renderFoot() {
  const idx = STEPS.findIndex((s) => s.key === state.step);
  const d = state.channelId ? draftOrNull() : null;
  const invalid = d ? validateDraft(d) : [];
  // NB: compose through el() — native .append(null) writes the string "null".
  stageFoot.replaceChildren(
    el("div", { style: "display:flex;gap:10px;align-items:center;width:100%" },
      el("button", {
        class: "btn", disabled: idx === 0,
        onclick: () => { state.step = STEPS[Math.max(0, idx - 1)].key; render(); },
      }, "‹ Back"),
      el("span", { class: "apply-state" }, footHint()),
      el("span", { class: "spacer", style: "flex:1" }),
      drafts.get(state.channelId) ? el("button", {
        class: "btn",
        onclick: async () => {
          if (await confirmDialog("Discard draft?", "All staged changes for this channel will be thrown away.", "Discard", true)) {
            drafts.drop(state.channelId);
            state.step = "pick";
            render();
          }
        },
      }, "Discard draft") : null,
      idx < STEPS.length - 1
        ? el("button", {
            class: "btn primary", disabled: !state.channelId || (invalid.length > 0 && state.step !== "pick"),
            onclick: () => {
              if (invalid.length) { toast(invalid[0].message, "err"); return; }
              state.step = STEPS[idx + 1].key;
              render();
            },
          }, "Next ›")
        : el("button", {
            class: "btn primary", id: "apply-btn", disabled: !d || invalid.length > 0 || changedInReview() === 0,
            onclick: () => void applyAll(),
          }, `Apply${changedInReview() ? ` (${changedInReview()})` : ""}`),
    ),
  );

  function changedInReview() {
    if (state.step !== "review" || !d) return 0;
    const stored = api.getDefinition(state.channelId);
    return stored ? (JSON.stringify(stored) === JSON.stringify(d) ? 0 : 1) : 1;
  }
  function footHint() {
    if (!state.channelId) return "Pick a channel to begin.";
    if (invalid.length) return invalid[0].message;
    if (state.step === "review") return "Review shows exactly what Apply will commit.";
    if (drafts.get(state.channelId)) return "Draft staged — Apply happens at Review.";
    return "No changes yet.";
  }
}

function validateDraft(d) {
  const errors = [];
  const name = String(d.name || "").trim();
  if (!name) errors.push({ message: "Name can't be empty." });
  else if (name.length > 60) errors.push({ message: "Name must be 60 characters or fewer." });
  if (!d.groupId) errors.push({ message: "Pick a group." });
  if (!Number.isInteger(d.number) || d.number < 1 || d.number > 899) errors.push({ message: "Number must be 1–899." });
  else {
    const clash = api.getLibrary().channels.find((c) => c.number === d.number && c.id !== state.channelId);
    if (clash) errors.push({ message: `Number ${d.number} is taken by “${clash.name}”.` });
  }
  return errors;
}

async function applyAll() {
  const d = draftOrNull();
  if (!d) return;
  const btn = stageFoot.querySelector("#apply-btn");
  btn.disabled = true;
  btn.textContent = "Applying…";
  const stored = api.getDefinition(state.channelId);
  const ops = [];
  if (stored) {
    // identity fields never travel: server preserves id/seed/kind
    ops.push({ op: "channel.put", channel: d, _sourceTouched: JSON.stringify(stored.source) !== JSON.stringify(d.source) });
  } else {
    // new channel: strip the prototype draft id; the server assigns identity
    const { id, seed, kind, ...clean } = d;
    ops.push({ op: "channel.put", channel: { ...clean, id: null, seed: null } });
  }
  try {
    const receipt = await api.apply(`curator-${Date.now()}`, api.getLibrary().revision, ops);
    if (receipt.status === "committed") {
      if (stored) drafts.drop(state.channelId);
      else {
        // creation receipt would map temp->final id in production; prototype reuses the draft id path
        drafts.drop(state.channelId);
        state.channelId = null;
      }
      toast(`Applied at r${receipt.revision}. Programming prepares in the background.`, "ok");
      state.step = "pick";
      render();
    } else {
      btn.disabled = false;
      btn.textContent = "Apply";
      toast(`Rejected: ${receipt.error}${receipt.errors ? ` — ${receipt.errors[0].message}` : ""}`, "err");
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Apply";
    toast(`Submit failed (${err.message}) — draft kept. Try Apply again.`, "err");
  }
}

// ---------- render dispatch ----------
function render() {
  renderSteps();
  switch (state.step) {
    case "pick": renderPick(); break;
    case "identity": renderIdentity(); break;
    case "group": renderGroup(); break;
    case "rules": renderRules(); break;
    case "programming": renderProgramming(); break;
    case "review": void renderReview(); break;
  }
  renderFoot();
}

const bar = topBar({
  title: "Prototype E", sub: "Guided Curator",
  api, drafts, variant: "curator",
  extra: [el("span", { class: "pill sim" }, "simulated data")],
  onReset: () => { state.channelId = null; state.step = "pick"; render(); },
});
document.getElementById("app").append(bar, main);
render();
document.onkeydown = (e) => {
  if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    stageBody.querySelector("input[type=search]")?.focus();
  }
};
