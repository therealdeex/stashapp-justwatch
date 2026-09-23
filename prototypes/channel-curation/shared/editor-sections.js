// Shared editing sections + the Apply/Discard state machine used by every
// variant. One editor body, embedded at different sizes by the variants:
//
//   clean -> dirty -> validating -> applying -> applied
//              |         |            |
//              +-- invalid      +-- revision_conflict / transport error
//   (draft always survives; a newer edit while applying stays dirty)
//
// Applying submits an immutable snapshot of the draft. Edits typed while the
// request is in flight are NOT in that snapshot; they keep the editor dirty
// and need another Apply.

import { el, clear, tile, statusChip, debounce, clone, deepEqual, toast, confirmDialog, sectionCard, fmtCount, fmtDuration } from "./ui.js";
import { ruleEditor, summaryBox, summarizeLines, sourcesEquivalent } from "./rules.js";

const PALETTE = ["#E91E63", "#D32F2F", "#F57C00", "#F9A825", "#AFB42B", "#3883C",
  "#00897B", "#00ACC1", "#3949AB", "#5E35B1", "#7B1FA2", "#455A64"];

const SORTS = [
  ["shuffle", "Shuffle"], ["newest", "Newest"], ["oldest", "Oldest"],
  ["top_rated", "Top rated"], ["longest", "Longest"], ["shortest", "Shortest"],
];

// Simulated rollout gate: which channels may select continuing programming.
const CONTINUING_AVAILABLE = new Set(["net_461ab3e3", "net_04685144"]);

let applyCounter = 0;
function newRequestId() {
  return `req-${Date.now().toString(36)}-${(applyCounter++).toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
}

export function createEditorPane({ api, drafts, channelId, onChanged, compact = false, focusField = null }) {
  // ---------- state ----------
  let stored = api.getDefinition(channelId);
  let draft = drafts.get(channelId) ? clone(drafts.get(channelId).draft) : clone(stored);
  let phase = "clean"; // clean | dirty | validating | applying | applied
  let applyingSnapshot = null; // the immutable submitted snapshot
  let pendingRequestId = null; // in-flight/lost request awaiting its receipt
  let pendingSnapshot = null;  // the snapshot that request carried
  let applyError = null;       // {kind, message, errors}
  let lastAppliedRevision = null;
  let preparingTimer = null;
  let preview = null;          // latest preview result
  let previewStale = false;
  let destroyed = false;

  const root = el("div", { class: "editor", "aria-label": `Editing ${stored?.name || channelId}` });
  const body = el("div", { class: "editor-body scroll", style: "flex:1" });
  const actionBar = el("div", { class: "editor-actions" });
  root.append(body, actionBar);

  let rules = null;

  function saveDraft() {
    if (phase === "applying" || phase === "applied") return; // snapshot already sent
    drafts.put(channelId, { v: 1, draft: clone(draft), savedAt: Date.now() });
  }

  function markDirty() {
    if (phase === "applying") {
      // The in-flight request keeps its own snapshot; this edit is newer.
      phase = "dirty";
      renderBar();
      return;
    }
    if (phase !== "dirty") { phase = "dirty"; }
    applyError = null;
    saveDraft();
    schedulePreview();
    renderBar();
  }

  const schedulePreview = debounce(() => { void loadPreview(); }, 350);

  async function loadPreview() {
    const mySig = JSON.stringify(draft.source || {});
    previewStale = true;
    renderPreview();
    try {
      const result = await api.preview({ id: channelId, source: draft.source, seedCount: stored?.seedCount, programming: draft.programming });
      if (destroyed) return;
      if (JSON.stringify(draft.source || {}) !== mySig) return; // stale response: ignore
      preview = result;
      previewStale = false;
      renderPreview();
    } catch {
      if (!destroyed) { previewStale = false; preview = { error: "Preview failed — showing last known numbers only.", poolCount: undefined }; renderPreview(); }
    }
  }

  function isDirty() {
    if (!stored) return true;
    if (deepEqual(draft, stored)) return false;
    // Rule editing may add empty canonical keys — equivalent sources are not
    // a change (mirrors the backend's canonical stored source).
    const a = { ...draft, source: undefined };
    const b = { ...stored, source: undefined };
    return !deepEqual(a, b) || !sourcesEquivalent(draft.source, stored.source);
  }

  function validateDraft() {
    const errors = [];
    const name = String(draft.name || "").trim();
    if (!name) errors.push({ field: "name", message: "Name can't be empty." });
    else if (name.length > 60) errors.push({ field: "name", message: "Name must be 60 characters or fewer." });
    if (!draft.groupId) errors.push({ field: "group", message: "Pick a group." });
    const num = draft.number;
    if (!Number.isInteger(num) || num < 1 || num > 899) errors.push({ field: "number", message: "Number must be 1–899." });
    else {
      const clash = api.getLibrary().channels.find((c) => c.number === num && c.id !== channelId);
      if (clash) errors.push({ field: "number", message: `Channel ${num} is taken by “${clash.name}”. Use Swap.` });
    }
    return errors;
  }

  // ---------- sections ----------
  function identitySection() {
    const nameInput = el("input", {
      type: "text", value: draft.name || "", id: "f-name", "aria-label": "Channel name",
      oninput: () => { draft.name = nameInput.value; markDirty(); },
    });
    const numberInput = el("input", {
      type: "number", min: "1", max: "899", value: draft.number ?? "", id: "f-number", "aria-label": "Channel number",
      oninput: () => { draft.number = numberInput.value === "" ? null : parseInt(numberInput.value, 10); markDirty(); },
    });
    const colorRow = el("div", { class: "entity-summary", role: "listbox", "aria-label": "Brand color" });
    for (const c of PALETTE) {
      colorRow.append(el("button", {
        class: "tile", role: "option", "aria-selected": String(draft.color === c),
        "aria-label": `Color ${c}`, style: `width:26px;height:26px;background:${c};border:2px solid ${draft.color === c ? "#fff" : "transparent"}`,
        onclick: () => { draft.color = c; markDirty(); rerender(); },
      }, ""));
    }
    const swapBtn = el("button", {
      class: "btn small", title: "Swap numbers with another channel (one Apply)",
      onclick: () => swapDialog(),
    }, "Swap…");
    return sectionCard("Identity",
      el("div", { class: "fieldrow" },
        el("div", { class: `field ${validateField("name") ? "invalid" : ""}` },
          el("label", { for: "f-name" }, "Name"),
          nameInput,
          validateField("name") ? el("div", { class: "error-msg", id: "err-name" }, validateField("name")) : null),
        el("div", { class: `field ${validateField("number") ? "invalid" : ""}`, style: "flex:0 0 130px" },
          el("label", { for: "f-number" }, "Number"),
          numberInput,
          validateField("number") ? el("div", { class: "error-msg", id: "err-number" }, validateField("number")) : null,
          el("div", { class: "hint" }, swapBtn)),
      ),
      el("div", { class: "field" }, el("label", {}, "Brand color"), colorRow),
      draft.archived ? el("div", { class: "pill warn" }, "Archived — not playable until restored") : null,
      draft.paused ? el("div", { class: "pill warn" }, "Paused — definition kept, removed from the guide") : null,
    );
  }

  function validateField(field) {
    const err = validateDraft().find((e) => e.field === field);
    return err ? err.message : null;
  }

  async function swapDialog() {
    const { openDialog } = await import("./ui.js");
    const others = api.getLibrary().channels
      .filter((c) => c.id !== channelId && (draft.kind === "net" ? c.kind === "net" : c.kind === "ch"))
      .sort((a, b) => Math.abs(a.number - draft.number) - Math.abs(b.number - draft.number))
      .slice(0, 30);
    const list = el("div", { class: "entity-list", role: "listbox", "aria-label": "Swap with" },
      others.map((c) => el("div", {
        class: "entity-row", role: "option", tabindex: "0",
        onclick: () => {
          const theirs = c.number;
          api.apply(newRequestId(), api.getLibrary().revision, [
            { op: "channel.put", channel: { ...clone(stored), number: theirs } },
            { op: "channel.put", channel: { ...clone(c), number: stored.number } },
          ]).then((r) => {
            if (r.status === "committed") { toast(`Swapped ${stored.number} ↔ ${theirs}. Applied.`, "ok"); stored = api.getDefinition(channelId); rerender(); onChanged?.("applied"); }
            else toast(`Swap failed: ${r.error}`, "err");
          });
          dlg.close();
        },
      }, tile(c, 24), el("span", {}, c.name), el("span", { class: "eid" }, `#${c.number}`))));
    const dlg = openDialog({ title: `Swap number ${draft.number} with…`, body: list });
  }

  function groupSection(groups) {
    const select = el("select", { id: "f-group", "aria-label": "Group", onchange: () => { draft.groupId = select.value; markDirty(); } },
      groups.map((g) => el("option", { value: g.id, selected: draft.groupId === g.id }, g.name)));
    return sectionCard("Group",
      el("div", { class: "field" },
        el("label", { for: "f-group" }, "Belongs to exactly one group"),
        select,
        el("div", { class: "hint" }, "Groups organize the guide and this library. Membership never changes what airs."),
      ));
  }

  function rulesSection() {
    const wrap = el("div", {});
    if (stored?.source?.type === "savedFilter" && !draft._converted) {
      wrap.append(sectionCard("Source",
        el("p", { style: "margin:0 0 8px" }, "This channel airs from a linked saved search — used verbatim, including its text query. Editing that search in Stash changes membership."),
        el("div", { class: "summary-box" }, summarizeLines(draft.source).map((l) => el("div", { class: "rule-sentence" }, l))),
        el("div", { style: "margin-top:8px" },
          el("button", {
            class: "btn small", onclick: () => {
              draft._converted = true; markDirty(); rerender();
              toast("Converted to editable rules. The saved search is no longer linked once applied.", "");
            },
          }, "Convert to editable rules…"))));
      return wrap;
    }
    rules = ruleEditor(draft.source, () => {
      draft.source = rules.source(); // write back the edited copy — a real edit
      markDirty();
    });
    wrap.append(sectionCard("What airs (rules)",
      el("p", { class: "hint", style: "margin:0 0 8px; color:var(--text-faint)" },
        "Rows combine with AND. ANY / ALL applies within a row."),
      rules.node,
    ));
    return wrap;
  }

  function programmingSection() {
    const mode = draft.programming?.mode || "fixed";
    const continuingOK = CONTINUING_AVAILABLE.has(channelId);
    const modeSelect = el("select", { id: "f-mode", "aria-label": "Programming mode", onchange: () => {
      draft.programming = { ...clone(draft.programming || {}), mode: modeSelect.value };
      markDirty();
    } },
      el("option", { value: "fixed", selected: mode === "fixed" }, "Fixed loop — the rotation repeats"),
      el("option", { value: "explore", selected: mode === "explore" }, "Explore — shuffled, no recent repeats"),
      el("option", { value: "discovery", selected: mode === "discovery" }, "Discovery — pushes unheard content"),
      el("option", {
        value: "continuing", selected: mode === "continuing", disabled: !continuingOK,
      }, continuingOK ? "Continuing — full library like a broadcast" : "Continuing — not enabled for this channel (server rollout gate)"),
    );
    const spacing = el("input", {
      type: "number", min: "0", style: "width:110px", value: draft.programming?.spacingMinutes ?? "",
      "aria-label": "Minimum spacing between plays of a scene, minutes",
      oninput: () => { draft.programming = { ...clone(draft.programming || {}), spacingMinutes: spacing.value === "" ? undefined : parseInt(spacing.value, 10) }; markDirty(); },
    });
    return sectionCard("Programming",
      el("div", { class: "field" }, el("label", { for: "f-mode" }, "Mode"), modeSelect,
        continuingOK ? null : el("div", { class: "hint" }, "Continuing is activation-gated by the operator rollout; the GUI cannot switch it on.")),
      el("div", { class: "field" }, el("label", {}, "Spacing"), spacing, el("span", { class: "hint" }, " minutes between repeats"),
      ),
      el("p", { class: "hint", style: "color:var(--text-faint)" },
        "Renaming, regrouping, or re-branding never resets what's playing. Changing rules re-prepares the schedule without touching aired history."),
    );
  }

  function previewSection() {
    const holder = el("div", { class: "section-card" }, el("h3", {}, "Preview — simulated"));
    holder.dataset.role = "preview";
    return holder;
  }

  function renderPreview() {
    const holder = root.querySelector('[data-role="preview"]');
    if (!holder) return;
    clear(holder);
    holder.append(el("h3", {}, el("span", {}, "Preview "), el("span", { class: "pill sim" }, "simulated numbers")));
    if (previewStale || !preview) {
      holder.append(el("p", { class: "hint", style: "color:var(--text-faint)" }, previewStale ? "Checking the current draft…" : "No preview yet."));
      return;
    }
    if (preview.error) {
      holder.append(el("p", { class: "apply-state err" }, preview.error));
      return;
    }
    holder.append(
      el("div", { class: "count-line" },
        el("div", { class: "count-block" },
          el("div", { class: "n" }, preview.poolCount === 0 ? "0" : fmtCount(preview.poolCount)),
          el("div", { class: "l" }, "source pool (matches rules)")),
        el("div", { class: "count-block" },
          el("div", { class: "n" }, fmtCount(preview.rotationSize)),
          el("div", { class: "l" }, "on-air rotation (bounded 50)")),
        preview.schedule
          ? el("div", { class: "count-block" },
              el("div", { class: "n" }, `${fmtCount(preview.schedule.coverageHours)}h`),
              el("div", { class: "l" }, `schedule: ${preview.schedule.status}`))
          : null,
      ),
      preview.poolCount === 0
        ? el("p", { class: "apply-state err" }, "Empty pool — nothing matches these rules yet. The channel would be off air.")
        : el("div", { class: "preview-strip" },
            preview.sample.map((s) => el("div", { class: "sample-card" },
              el("div", { class: "art", "aria-hidden": "true" }, "▶"),
              el("div", { class: "cap" },
                el("div", { class: "t" }, s.title),
                `${s.studio} · ${fmtDuration(s.duration)} · ${s.date}`)))),
      el("p", { class: "hint", style: "color:var(--text-faint);margin-top:6px" },
        "Counts and artwork are simulated for the prototype. First item is not “now” — live numbers come from the pool count above."),
    );
  }

  // ---------- action bar / state machine ----------
  function renderBar() {
    clear(actionBar);
    const dirty = isDirty();
    const invalid = validateDraft();
    let stateNode;
    if (phase === "applying") {
      stateNode = el("span", { class: "apply-state" }, "Applying… (edits you type now stay draft)");
    } else if (applyError?.kind === "conflict") {
      stateNode = el("span", { class: "apply-state err" },
        `Revision conflict — someone else applied r${applyError.currentRevision}. Your draft is intact.`);
    } else if (applyError?.kind === "transport") {
      stateNode = el("span", { class: "apply-state err" }, `${applyError.message} — your draft is intact. Retry with Apply.`);
    } else if (applyError?.kind === "validation") {
      stateNode = el("span", { class: "apply-state err" },
        `${applyError.errors.length} server validation error(s) — shown below the fields. Draft intact.`);
    } else if (phase === "applied") {
      stateNode = el("span", { class: "apply-state ok" },
        `Applied at r${lastAppliedRevision}.`);
    } else if (invalid.length) {
      stateNode = el("span", { class: "apply-state err" }, invalid[0].message);
    } else if (dirty) {
      stateNode = el("span", { class: "apply-state" }, "Unsaved draft — Apply to commit.");
    } else {
      stateNode = el("span", { class: "apply-state" }, "All changes saved.");
    }

    const preparing = phase === "applied" && preparingTimer;
    // NB: native .append(null) writes the string "null" — compose through el().
    actionBar.replaceChildren(
      el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap;width:100%" },
        stateNode,
        el("span", { class: "spacer", style: "flex:1" }),
        preparing ? el("span", { class: "pill" }, "Preparing programming…") : null,
        phase === "applied" && !preparing && !dirty ? el("span", { class: "pill clean" }, "Ready") : null,
        el("button", {
          class: "btn", disabled: !dirty || phase === "applying",
          onclick: async () => {
            if (await confirmDialog("Discard draft?", "Your unsaved changes to this channel will be thrown away.", "Discard", true)) {
              drafts.drop(channelId);
              draft = clone(stored);
              applyError = null;
              phase = "clean";
              rerender();
              onChanged?.("discarded");
            }
          },
        }, "Discard"),
        el("button", {
          class: "btn primary", id: "apply-btn",
          disabled: !dirty || invalid.length > 0 || phase === "applying",
          onclick: () => void doApply(),
        }, phase === "applying" ? "Applying…" : "Apply"),
      ),
    );
    if (applyError?.kind === "conflict") {
      actionBar.append(
        el("button", { class: "btn", onclick: () => { applyError = null; renderBar(); } }, "Keep draft"),
        el("button", {
          class: "btn primary", onclick: () => void doApply(), // fresh look at revision
        }, `Apply onto r${applyError.currentRevision}`),
      );
    }
    if (applyError?.kind === "validation") {
      const list = el("div", { role: "alert", style: "position:absolute;bottom:64px;right:16px;background:var(--bg-panel);border:1px solid var(--err);border-radius:8px;padding:10px 14px;max-width:420px" },
        el("strong", {}, "Server rejected the edit:"),
        el("ul", { style: "margin:6px 0 0;padding-left:18px" },
          applyError.errors.map((e) => el("li", {}, `${e.path ? e.path + ": " : ""}${e.message}`))),
        el("div", { style: "margin-top:6px" },
          el("button", { class: "btn small", onclick: (e) => { e.currentTarget.closest("div[role=alert]").remove(); } }, "Dismiss")));
      root.style.position = "relative";
      actionBar.append(list);
    }
  }

  async function doApply() {
    applyError = null;
    phase = "validating";
    renderBar();
    const invalid = validateDraft();
    if (invalid.length) {
      phase = "dirty";
      renderBar();
      return;
    }
    // Snapshot — immutable from here. Later edits re-dirty the editor.
    applyingSnapshot = clone(draft);
    // A previous attempt that died in transport keeps its request id: retrying
    // an unchanged draft reuses it, so a request that actually committed
    // server-side resolves to the SAME receipt instead of applying twice.
    const reusePending = pendingRequestId && deepEqual(applyingSnapshot, pendingSnapshot);
    const requestId = reusePending ? pendingRequestId : newRequestId();
    const sourceTouched = !sourcesEquivalent(stored?.source, applyingSnapshot.source);
    const ops = [{ op: "channel.put", channel: applyingSnapshot, _sourceTouched: sourceTouched }];
    phase = "applying";
    renderBar();
    let receipt;
    try {
      receipt = await api.apply(requestId, api.getLibrary().revision, ops);
    } catch (err) {
      if (destroyed) return;
      pendingRequestId = requestId;       // resolved by receipt lookup on retry
      pendingSnapshot = applyingSnapshot;
      phase = "dirty";
      applyError = { kind: "transport", message: `Submit failed (${err.message}).` };
      renderBar();
      toast("Apply failed to reach the server — draft kept. Try Apply again.", "err");
      return;
    }
    pendingRequestId = null;
    pendingSnapshot = null;
    if (destroyed) return;
    // Edits typed during the request re-dirtied the draft; only then do they
    // differ from the (applied) snapshot.
    if (receipt.status === "committed") {
      stored = api.getDefinition(channelId);
      drafts.drop(channelId);
      lastAppliedRevision = receipt.revision;
      applyError = null;
      if (deepEqual(draft, applyingSnapshot)) {
        draft = clone(stored);
        phase = "applied";
        preparingTimer = setTimeout(() => {
          preparingTimer = null;
          if (!destroyed) { phase = "applied"; renderBar(); toast(`“${stored.name}” applied and programming ready.`, "ok"); }
        }, 2200);
      } else {
        phase = "dirty"; // newer edits remain: another Apply is needed
        toast("Applied your earlier snapshot. Newer edits are still draft.", "");
      }
      renderBar();
      rerenderBody();
      onChanged?.("applied");
    } else if (receipt.error === "revision_conflict") {
      phase = "dirty";
      applyError = { kind: "conflict", currentRevision: receipt.currentRevision, message: receipt.message };
      renderBar();
    } else if (receipt.error === "validation_failed") {
      phase = "dirty";
      applyError = { kind: "validation", errors: receipt.errors };
      renderBar();
    } else {
      phase = "dirty";
      applyError = { kind: "transport", message: receipt.message || "rejected" };
      renderBar();
    }
  }

  // ---------- render ----------
  function rerenderBody() {
    clear(body);
    const groups = api.groups();
    if (!stored) {
      body.append(el("p", { class: "apply-state err" }, "This channel no longer exists on the server."));
      return;
    }
    body.append(
      identitySection(),
      groupSection(groups),
      rulesSection(),
      programmingSection(),
      previewSection(),
    );
    rules?.setOnChange?.(() => {});
    renderPreview();
    if (focusField) body.querySelector(`#f-${focusField}`)?.focus();
  }

  function rerender() {
    rerenderBody();
    renderBar();
  }

  rerender();
  if (isDirty()) { phase = "dirty"; renderBar(); }
  void loadPreview();

  return {
    node: root,
    destroy() {
      destroyed = true;
      if (preparingTimer) clearTimeout(preparingTimer);
    },
    get phase() { return phase; },
    get isDirty() { return isDirty(); },
    refreshStored() { stored = api.getDefinition(channelId); },
    /** Switch to editing another channel id in place. */
    retarget(newChannelId) {
      channelId = newChannelId;
      stored = api.getDefinition(channelId);
      draft = drafts.get(channelId) ? clone(drafts.get(channelId).draft) : clone(stored);
      phase = drafts.get(channelId) ? "dirty" : "clean";
      applyError = null;
      preview = null;
      rerender();
      void loadPreview();
    },
  };
}

// ---------- top bar (shared chrome) ----------
export function topBar({ title, sub, api, drafts, variant, extra = [], onReset }) {
  const revisionChip = el("span", { class: "revision-chip" });
  const dirtyPill = el("span", { class: "pill clean" });
  function refresh() {
    revisionChip.textContent = `r${api.getLibrary().revision} · SIMULATED`;
    const n = drafts.ids().length;
    dirtyPill.className = `pill ${n ? "dirty" : "clean"}`;
    dirtyPill.textContent = n ? `${n} unsaved draft${n === 1 ? "" : "s"}` : "no drafts";
  }
  api.subscribe(refresh);
  window.addEventListener("jw-drafts-changed", refresh);
  refresh();
  const scenarioSelect = el("select", {
    class: "pill", "aria-label": "Failure scenario", title: "Inject simulated server behavior",
    onchange: () => { api.setScenario(scenarioSelect.value); toast(`Scenario: ${SCENARIOS_LABEL[scenarioSelect.value]}`); },
  }, Object.entries(SCENARIOS_LABEL).map(([k, v]) => el("option", { value: k }, `Scenario: ${v}`)));
  const bar = el("nav", { class: "topbar" },
    el("a", { href: "../index.html", class: "btn ghost small", "aria-label": "Back to prototype overview" }, "‹ Prototypes"),
    el("span", { class: "title" }, title, sub ? el("span", { class: "sub" }, ` — ${sub}`) : null),
    el("span", { class: "spacer" }),
    dirtyPill,
    revisionChip,
    ...extra,
    scenarioSelect,
    el("button", {
      class: "btn small", title: "Restore the same starting fixture (clears drafts)",
      onclick: async () => {
        if (await confirmDialog("Reset prototype?", "All drafts and applied changes return to the starting fixture.", "Reset")) {
          drafts.clear();
          api.reset();
          onReset?.();
          toast("Prototype reset to the starting fixture.", "ok");
        }
      },
    }, "Reset"),
  );
  return bar;
}
const SCENARIOS_LABEL = {
  normal: "Normal",
  slowApply: "Slow Apply",
  conflict: "Conflict on Apply",
  lostResponse: "Lost response → retry",
  validationError: "Validation error",
};

// Warn before leaving with drafts (browser-level guard).
export function guardUnload(api, drafts) {
  window.addEventListener("beforeunload", (e) => {
    if (drafts.ids().length) { e.preventDefault(); e.returnValue = ""; }
  });
}
