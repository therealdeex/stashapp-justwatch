// Shared DOM helpers, controls, and behaviors for the prototypes.
// Vanilla ES modules; no framework, no build step. XSS-safe: text goes through
// createTextNode paths only (el() never parses HTML strings).

let toastZone = null;

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "value") node.value = v;
    else if (BOOLEAN_PROPS.has(k)) node[k] = Boolean(v); // property, not attribute:
    // setAttribute("disabled","0") still disables; Boolean() folds numeric 0 to false
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  append(node, children);
  return node;
}

const BOOLEAN_PROPS = new Set([
  "disabled", "readonly", "required", "checked", "selected", "multiple",
  "open", "autofocus", "indeterminate",
]);

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function toast(message, kind = "") {
  if (!toastZone) {
    toastZone = el("div", { class: "toast-zone", role: "status", "aria-live": "polite" });
    document.body.append(toastZone);
  }
  const t = el("div", { class: `toast ${kind}` }, message);
  toastZone.append(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 3600);
}

// Neutral brand tile: color + monogram + channel number. No external images.
export function tile(channel, size = 30) {
  const initials = (channel.name || "?")
    .split(/\s+/).filter((w) => /[a-z0-9]/i.test(w)).slice(0, 2)
    .map((w) => w[0].toUpperCase()).join("") || "?";
  return el("div", {
    class: "tile", "aria-hidden": "true",
    style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.38)}px;background:${channel.color || "#455A64"}`,
  },
    el("span", { class: "num", style: `font-size:${Math.max(8, Math.round(size * 0.3))}px` }, String(channel.number ?? "")),
    initials,
  );
}

export function statusChip(kind, label) {
  return el("span", { class: `status-chip ${kind}` }, label);
}

export function debounce(fn, ms) {
  let h = null;
  const wrapped = (...args) => {
    clearTimeout(h);
    h = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(h);
  return wrapped;
}

export function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

// Virtualized list: renders only the visible window of `items`.
// Returns { node, setItems } — call setItems after filtering.
export function virtualList({ items, render, itemHeight = 40, className = "", overscan = 8 }) {
  const viewport = el("div", { class: `scroll ${className}`, style: "position:relative;flex:1;min-height:0" });
  const canvas = el("div", { style: "position:relative;width:100%" });
  viewport.append(canvas);
  let current = items;
  let focusedIndex = -1;

  function renderWindow() {
    const top = viewport.scrollTop;
    const height = viewport.clientHeight || 480;
    const start = Math.max(0, Math.floor(top / itemHeight) - overscan);
    const end = Math.min(current.length, Math.ceil((top + height) / itemHeight) + overscan);
    clear(canvas);
    canvas.style.height = `${current.length * itemHeight}px`;
    const window = [];
    for (let i = start; i < end; i++) window.push(current[i]);
    const nodes = window.map((item) => render(item));
    canvas.style.paddingTop = `${start * itemHeight}px`;
    for (const n of nodes) canvas.append(n);
  }
  viewport.addEventListener("scroll", () => requestAnimationFrame(renderWindow), { passive: true });
  const ro = new ResizeObserver(() => renderWindow());
  ro.observe(viewport);
  requestAnimationFrame(renderWindow);
  return {
    node: viewport,
    setItems(next) { current = next; viewport.scrollTop = 0; renderWindow(); },
    refresh: renderWindow,
    get length() { return current.length; },
  };
}

// Accessible dialog with focus trap, Esc to cancel, focus return.
export function openDialog({ title, body, actions = [], wide = false, onClose = null }) {
  const previouslyFocused = document.activeElement;
  const overlay = el("div", { class: "overlay", role: "presentation" });
  const dialog = el("div", {
    class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title,
    style: wide ? "max-width:1000px" : "",
  });
  const close = (result) => {
    overlay.remove();
    document.removeEventListener("keydown", keyHandler, true);
    if (previouslyFocused && previouslyFocused.isConnected) previouslyFocused.focus();
    if (onClose) onClose(result);
  };
  const keyHandler = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(null); }
    if (e.key === "Tab") {
      const focusables = dialog.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])");
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", keyHandler, true);
  const footer = actions.length
    ? el("footer", {}, ...actions.map((a) =>
        el("button", {
          class: `btn ${a.kind || ""}`, onclick: () => close(a.value === undefined ? true : a.value),
        }, a.label))) : null;
  dialog.append(
    el("header", {}, el("h2", { style: "font-size:15px;flex:1" }, title),
      el("button", { class: "btn ghost small", "aria-label": "Close dialog", onclick: () => close(null) }, "✕")),
    el("div", { class: "dialog-body" }, body),
    footer,
  );
  overlay.append(dialog);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(null); });
  document.body.append(overlay);
  const firstInput = dialog.querySelector("input, select, textarea, button.btn:not(.ghost)");
  if (firstInput) firstInput.focus();
  return { close, node: dialog };
}

export function confirmDialog(title, message, confirmLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    openDialog({
      title,
      body: el("p", { style: "margin:4px 0 8px" }, message),
      actions: [
        { label: "Cancel" },
        { label: confirmLabel, kind: danger ? "danger" : "primary" },
      ],
      onClose: (result) => resolve(result === true),
    });
  });
}

// Searchable, windowed entity picker over potentially thousands of items.
// `entities`: [{id, name}] — `selected`: Set<string> — `onDone(Set)`.
export function entityPicker({ title, entities, selected, onDone, singleton = false }) {
  const state = { selected: new Set(selected), query: "" };
  let list;
  const searchInput = el("input", {
    type: "text", placeholder: `Search ${entities.length} entries…`,
    "aria-label": `Search ${title}`,
    oninput: debounce(() => { state.query = searchInput.value.trim().toLowerCase(); list.setItems(filtered()); }, 120),
    onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); toggleFirstVisible(); } },
  });
  function filtered() {
    if (!state.query) return entities;
    const out = [];
    for (const item of entities) {
      if (item.name.toLowerCase().includes(state.query) || item.id.includes(state.query)) out.push(item);
      if (out.length >= 2000) break; // windowed search results
    }
    return out;
  }
  function rowNode(item) {
    const checked = state.selected.has(item.id);
    const row = el("div", {
      class: `entity-row ${checked ? "checked" : ""}`, role: "option",
      "aria-selected": checked ? "true" : "false", tabindex: "0",
      onclick: () => { toggle(item); },
      onkeydown: (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(item); } },
    },
      el("span", { style: "width:16px;text-align:center" }, checked ? "✓" : ""),
      el("span", {}, item.name),
      el("span", { class: "eid" }, `#${item.id}`),
    );
    return row;
  }
  function toggle(item) {
    if (singleton) { state.selected = new Set([item.id]); }
    else if (state.selected.has(item.id)) state.selected.delete(item.id);
    else state.selected.add(item.id);
    refreshPicked();
  }
  function toggleFirstVisible() {
    const first = list.node.querySelector(".entity-row");
    if (first) first.click();
  }
  const pickedHead = el("div", { class: "picked-head" });
  const pickedChips = el("div", { class: "entity-summary", style: "max-height:110px" });
  function refreshPicked() {
    const names = new Map(entities.map((x) => [x.id, x.name]));
    const picked = [...state.selected];
    pickedHead.textContent = "";
    pickedHead.append(
      el("span", {}, `${picked.length.toLocaleString()} selected`),
      picked.length
        ? el("button", {
            class: "btn ghost small", onclick: () => { state.selected.clear(); refreshPicked(); list.refresh(); },
          }, "Clear all") : null,
    );
    clear(pickedChips);
    // show at most 200 chips + an overflow note (storage keeps everything)
    for (const id of picked.slice(0, 200)) {
      pickedChips.append(el("span", { class: "entity-chip" },
        names.get(id) || `#${id}`,
        el("button", { "aria-label": `Remove ${names.get(id) || id}`, onclick: () => { state.selected.delete(id); refreshPicked(); list.refresh(); } }, "✕")));
    }
    if (picked.length > 200) {
      pickedChips.append(el("span", { class: "entity-chip" }, `+ ${picked.length - 200} more (all kept)`));
    }
  }
  list = virtualList({ items: filtered(), render: rowNode, itemHeight: 34 });
  list.node.style.height = "46vh";
  const body = el("div", {},
    el("div", { class: "field" }, searchInput),
    list.node,
    el("div", { class: "picked-pane" }, pickedHead, pickedChips),
  );
  refreshPicked();
  openDialog({
    title,
    body,
    wide: true,
    actions: [{ label: "Done", kind: "primary" }],
    onClose: () => onDone(state.selected),
  });
}

export function sectionCard(title, ...content) {
  return el("div", { class: "section-card" }, el("h3", {}, title), ...content);
}

export function fmtCount(n) {
  return typeof n === "number" ? n.toLocaleString("en-US") : "—";
}

export function fmtDuration(seconds) {
  if (!seconds || seconds <= 0) return "0m";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// Deterministic hash → number (for simulated counts/grids).
export function hashOf(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0);
}
