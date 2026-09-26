// Content-rule model + visual rule editor + plain-language summary.
//
// The source shape mirrors the plugin's composite `filter` source so prototype
// edits round-trip to the real model:
//   { type:"filter", tags:[], excludeTags:[], performers:[], performersAny:[],
//     studios:[], studiosAny:[], excludePerformers:[], excludeStudios:[],
//     date:{from,to}, duration:{min,max}, createdAt:{withinDays}, q }
// Rows combine with AND; the ANY/ALL toggle applies within a row and picks the
// storage field (performers = ALL-of, performersAny = ANY-of).
// A channel can alternatively be linked to a saved search (savedFilter), used
// verbatim including its text query — shown read-only with a membership note.

import { el, clear, entityPicker, fmtCount, hashOf } from "./ui.js";
import { FIXTURES } from "./fixture-data.js";

const ENT = {
  tags: Object.entries(FIXTURES.entities.tags).map(([id, name]) => ({ id, name })),
  performers: Object.entries(FIXTURES.entities.performers).map(([id, name]) => ({ id, name })),
  studios: Object.entries(FIXTURES.entities.studios).map(([id, name]) => ({ id, name })),
};
for (const kind of Object.keys(ENT)) ENT[kind].sort((a, b) => a.name.localeCompare(b.name));

export function entityName(kind, id) {
  return FIXTURES.entities[kind]?.[id] || `#${id}`;
}

const ids = (v) => (Array.isArray(v) ? v.map(String) : []);

// Minute rendering with authored precision (mirrors criteria._fmt_minutes).
function fmtMinutes(seconds) {
  const m = Number(seconds) / 60;
  if (!Number.isFinite(m)) return "0 min";
  if (Number.isInteger(m)) return `${m} min`;
  return `${Math.round(m * 100) / 100} min`;
}

// Stable canonical form of a source: filter sources always carry every list
// key (empty when unused) so "the editor touched nothing" stays byte-equal to
// the stored record. Mirrors the backend's canonical stored source.
export function canonicalSource(source) {
  const s = source || {};
  if (s.type !== "filter") return s;
  const out = { type: "filter" };
  for (const k of ["tags", "tagsAny", "excludeTags", "performers", "performersAny", "studios", "studiosAny", "excludePerformers", "excludeStudios"]) {
    out[k] = ids(s[k]);
  }
  if (s.date && (s.date.from || s.date.to)) out.date = { from: s.date.from || "", to: s.date.to || "" };
  if (s.duration && (s.duration.min || s.duration.max)) {
    out.duration = { ...(s.duration.min ? { min: s.duration.min } : {}), ...(s.duration.max ? { max: s.duration.max } : {}) };
  }
  if (s.createdAt && s.createdAt.withinDays) out.createdAt = { withinDays: s.createdAt.withinDays };
  if (s.q) out.q = s.q;
  return out;
}

export function sourcesEquivalent(a, b) {
  return JSON.stringify(canonicalSource(a)) === JSON.stringify(canonicalSource(b));
}

// The ONE draft→wire serializer — mirrors production ui/index.js so the
// prototype can never drift from what the plugin accepts (audit E1/E8):
//   * genuinely empty facet arrays are OMITTED (the server rejects a
//     present-but-empty list);
//   * bounds are PRESENCE-based: an explicit 0 is a real bound, blank is
//     absence;
//   * q is trimmed and dropped when blank.
// canonicalSource above stays COMPARISON-ONLY.
export function toWireSource(source) {
  const s = source || {};
  if (s.type !== "filter") return s;
  const out = { type: "filter" };
  for (const k of ["tags", "tagsAny", "excludeTags", "performers", "performersAny", "studios", "studiosAny", "excludePerformers", "excludeStudios"]) {
    const v = ids(s[k]).filter((x) => /^\d+$/.test(x));
    if (v.length) out[k] = v;
  }
  if (s.date && (s.date.from || s.date.to)) out.date = { from: s.date.from || "", to: s.date.to || "" };
  if (s.duration && typeof s.duration === "object") {
    const spec = {};
    if (s.duration.min != null) spec.min = s.duration.min;
    if (s.duration.max != null) spec.max = s.duration.max;
    if (spec.min != null || spec.max != null) out.duration = spec;
  }
  if (s.createdAt && s.createdAt.withinDays != null) out.createdAt = { withinDays: s.createdAt.withinDays };
  if (typeof s.q === "string" && s.q.trim()) out.q = s.q.trim();
  return out;
}

export function toWireChannel(channel) {
  const c = JSON.parse(JSON.stringify(channel));
  c.source = toWireSource(c.source);
  return c;
}

function sortedIds(set) {
  return [...set].map(Number).sort((a, b) => a - b).map(String);
}

// ---------- plain-language summary ----------

function nameList(kind, list, verb) {
  if (!list.length) return null;
  const shown = list.slice(0, 3).map((id) => entityName(kind, id));
  const rest = list.length - shown.length;
  const body = shown.join(", ") + (rest > 0 ? ` + ${rest} more` : "");
  return `${verb} ${body}`;
}

export function summarizeLines(source) {
  const s = source || {};
  if (s.type === "savedFilter") {
    return ["Airs from the saved search linked to this channel — used verbatim, including its text query. Editing that search in Stash changes what airs."];
  }
  if (s.type === "tag") return [nameList("tags", ids(s.ids).length ? s.ids : [s.id], "Scenes tagged")];
  if (s.type === "performer") return [nameList("performers", [s.id], "Scenes featuring")];
  if (s.type === "studio") return ["Scenes from", nameList("studios", [s.id], "studio")].filter(Boolean);
  if (s.type !== "filter") return [`Airs from a ${s.type} source.`];
  const lines = [];
  const joiner = " — then ALL of the following must hold:";
  const active = [];
  if (ids(s.tags).length || ids(s.tagsAny)?.length || ids(s.excludeTags).length) {
    const parts = [];
    if (ids(s.tags).length) parts.push(`scenes tagged with ALL of: ${ids(s.tags).map((id) => entityName("tags", id)).join(", ")}`);
    if (ids(s.tagsAny)?.length) parts.push(`scenes tagged with any of: ${ids(s.tagsAny).slice(0, 3).map((id) => entityName("tags", id)).join(", ")}${s.tagsAny.length > 3 ? ` + ${s.tagsAny.length - 3} more` : ""}`);
    if (ids(s.excludeTags).length) parts.push(`without any of: ${ids(s.excludeTags).map((id) => entityName("tags", id)).join(", ")}`);
    active.push(parts.join(", "));
  }
  if (ids(s.performers).length) active.push(`featuring ALL of: ${ids(s.performers).map((id) => entityName("performers", id)).join(", ")}`);
  if (ids(s.performersAny).length) active.push(`featuring any of: ${ids(s.performersAny).slice(0, 3).map((id) => entityName("performers", id)).join(", ")}${s.performersAny.length > 3 ? ` + ${s.performersAny.length - 3} more` : ""}`);
  if (ids(s.excludePerformers)?.length) active.push(`NOT featuring: ${s.excludePerformers.slice(0, 3).map((id) => entityName("performers", id)).join(", ")}${s.excludePerformers.length > 3 ? ` + ${s.excludePerformers.length - 3} more` : ""}`);
  if (ids(s.studios).length) active.push(`from studios (incl. sub-studios): ${ids(s.studios).map((id) => entityName("studios", id)).join(", ")}`);
  if (ids(s.studiosAny).length) active.push(`from any of: ${ids(s.studiosAny).slice(0, 3).map((id) => entityName("studios", id)).join(", ")}${s.studiosAny.length > 3 ? ` + ${s.studiosAny.length - 3} more` : ""}`);
  if (ids(s.excludeStudios)?.length) active.push(`NOT from: ${s.excludeStudios.map((id) => entityName("studios", id)).join(", ")}`);
  if (s.date?.from || s.date?.to) {
    active.push(`scenes dated ${s.date.from || "the beginning"} → ${s.date.to || "today"}`);
  }
  if (s.duration?.min != null) active.push(`running at least ${fmtMinutes(s.duration.min)}`);
  if (s.duration?.max != null) active.push(`running at most ${fmtMinutes(s.duration.max)}`);
  if (s.createdAt?.withinDays) active.push(`added to the library within the last ${s.createdAt.withinDays} days (moves with the calendar)`);
  if (s.studioSceneCount && (s.studioSceneCount.min || s.studioSceneCount.max)) {
    const {min, max} = s.studioSceneCount;
    const c = min && max ? `with ${min}–${max} scenes` : max ? `with fewer than ${max} scenes` : `with ${min} or more scenes`;
    active.push(`dynamically: studios ${c} (membership updates itself)`);
  }
  if (s.performerSceneCount && (s.performerSceneCount.min || s.performerSceneCount.max)) {
    const {min, max} = s.performerSceneCount;
    const c = min && max ? `with ${min}–${max} scenes` : max ? `with fewer than ${max} scenes` : `with ${min} or more scenes`;
    active.push(`dynamically: performers ${c} (membership updates itself)`);
  }
  if (s.q) active.push(`matching the text search “${s.q}”`);
  if (!active.length) return ["No rules yet — this pool would be empty until at least one rule is on."];
  return [`${active.length} rule${active.length === 1 ? "" : "s"}, combined with AND${active.length > 1 ? joiner : ":"}`, ...active.map((a, i) => `${i + 1}. ${a}`)];
}

export function summaryBox(source) {
  return el("div", { class: "summary-box", "aria-label": "Plain-language rule summary" },
    summarizeLines(source).map((line, i) =>
      el("div", { class: i === 0 ? "rule-sentence head" : "rule-sentence", style: i === 0 ? "font-weight:600;margin-bottom:4px" : "" }, line)));
}

// ---------- simulated pool numbers (clearly labeled) ----------

export function simulatedPool(source) {
  const sig = JSON.stringify(source, Object.keys(source || {}).sort());
  const h = hashOf(sig);
  if (source?.type === "savedFilter") return 40 + (h % 160);
  const base = 30 + (h % 900);
  return base;
}

// Returns {poolCount, rotationSize, sample:[{id,title,duration,studio,date}], stale}
// Bounded: rotation is always ≤ 50; sample ≤ 10; all simulated.
export function simulatedPreview(source, seedCount = null) {
  const sig = JSON.stringify(source, Object.keys(source || {}).sort());
  const h = hashOf(sig);
  const emptyPool = isZeroRule(source) || sig.includes("999999") || sig.includes("424242");
  const pool = emptyPool ? 0 : (seedCount && h % 3 === 0 ? seedCount : 30 + (h % 900));
  const rotationSize = Math.min(50, pool);
  const sample = [];
  const titles = ["Late Shift", "Quiet Hours", "Double Feature", "Backlot", "After Curfew",
    "First Take", "Encore", "Matinee", "Nightcap", "Cold Open", "Director's Cut", "Intermission"];
  const studios = ["Meridian", "Northwind", "Halcyon", "Parallax", "Blue Door", "Cardinal"];
  for (let i = 0; i < Math.min(10, pool === 0 ? 0 : 10); i++) {
    const hh = hashOf(sig + ":" + i);
    sample.push({
      id: `sim-${(hh % 99991).toString(36)}`,
      title: titles[hh % titles.length],
      duration: 480 + (hh % 5400),
      studio: studios[(hh >> 3) % studios.length],
      date: `2026-0${1 + (hh % 9)}-${String(1 + (hh % 28)).padStart(2, "0")}`,
    });
  }
  return { poolCount: pool, rotationSize, sample, simulated: true };
}

function isZeroRule(source) {
  if (!source) return true;
  if (source.type === "filter") {
    return !ids(source.tags).length && !ids(source.excludeTags).length && !ids(source.tagsAny).length
      && !ids(source.performers).length && !ids(source.performersAny).length
      && !ids(source.studios).length && !ids(source.studiosAny).length
      && !source.date && !source.duration && !source.createdAt && !source.q;
  }
  return false;
}

// ---------- rule editor ----------

// sourceDraft is the editor's private working copy (already deep-cloned below).
export function ruleEditor(sourceDraft, onDraftChange) {
  const node = el("div", {});
  let changed = () => onDraftChange();
  sourceDraft = JSON.parse(JSON.stringify(sourceDraft || { type: "filter" }));

  function ensureFilterShape() {
    if (sourceDraft.type !== "filter") {
      // One-time, explicit conversion of a simple source into rule rows.
      const old = { ...sourceDraft };
      sourceDraft = Object.assign(sourceDraft, {
        type: "filter",
        tags: old.type === "tag" ? ids(old.ids).length ? ids(old.ids) : [old.id] : [],
        tagsAny: [],
        performers: old.type === "performer" ? [old.id] : [],
        performersAny: [],
        studios: old.type === "studio" ? [old.id] : [],
        studiosAny: [],
        excludeTags: [], excludePerformers: [], excludeStudios: [],
        q: undefined,
      });
    }
    for (const k of ["tags", "tagsAny", "excludeTags", "performers", "performersAny", "studios", "studiosAny", "excludePerformers", "excludeStudios"]) {
      if (!Array.isArray(sourceDraft[k])) sourceDraft[k] = [];
    }
  }

  function rerender() { clear(node); build(); changed(); }

  function logicToggle(getList, setList, otherGet, otherSet, labelAll, labelAny) {
    // ALL stores in one field, ANY in the other; toggling moves the ids.
    const wrap = el("span", { class: "logic", role: "group", "aria-label": "Match logic" });
    const mk = (mode) => {
      const hasAll = getList().length > 0;
      const hasAny = otherGet().length > 0;
      const pressed = hasAll && !hasAny ? mode === "all" : hasAny && !hasAll ? mode === "any" : false;
      return el("button", {
        "aria-pressed": String(pressed),
        onclick: (e) => {
          ensureFilterShape();
          const from = mode === "all" ? otherGet() : getList();
          const to = mode === "all" ? getList() : otherGet();
          to.push(...from);
          setList(sortedIds(new Set(to)));
          otherSet([]);
          for (const b of wrap.querySelectorAll("button")) b.setAttribute("aria-pressed", "false");
          e.currentTarget.setAttribute("aria-pressed", "true");
          changed();
          rerender();
        },
      }, mode === "all" ? labelAll : labelAny);
    };
    wrap.append(mk("all"), mk("any"));
    return wrap;
  }

  function pickerButton(kind, title, getIds, setIds, { exclude = false } = {}) {
    return el("button", {
      class: "btn small",
      onclick: () => entityPicker({
        title, entities: ENT[kind], selected: new Set(getIds()),
        onDone: (set) => { setIds(sortedIds(set)); changed(); rerender(); },
      }),
    }, `Choose (${getIds().length ? getIds().length.toLocaleString() : "none"})`);
  }

  function chipsLine(kind, list, onRemove, emptyText) {
    const line = el("div", { class: "entity-summary", "aria-label": "selected entries" });
    if (!list.length) line.append(el("span", { style: "color:var(--text-faint)" }, emptyText));
    for (const id of list.slice(0, 100)) {
      line.append(el("span", { class: "entity-chip" },
        entityName(kind, id),
        el("button", { "aria-label": `Remove ${entityName(kind, id)}`, onclick: () => { onRemove(id); changed(); rerender(); } }, "✕")));
    }
    if (list.length > 100) line.append(el("span", { class: "entity-chip" }, `+ ${list.length - 100} more`));
    return line;
  }

  function dynamicCountRow(facetKey, noun) {
    // "match by activity": fewer than N / N or more scenes (dynamic, server-resolved)
    const spec = sourceDraft[facetKey] || {};
    const maxIn = el("input", {
      type: "number", min: "1", style: "width:80px", placeholder: "N",
      value: spec.max ?? "", "aria-label": `${noun}: fewer than N scenes`,
      onchange: () => {
        const v = maxIn.value === "" ? undefined : parseInt(maxIn.value, 10);
        sourceDraft[facetKey] = {...(sourceDraft[facetKey] || {}), ...(v ? {max: v} : {max: undefined})};
        if (!sourceDraft[facetKey].min && !sourceDraft[facetKey].max) delete sourceDraft[facetKey];
        changed();
      },
    });
    const minIn = el("input", {
      type: "number", min: "0", style: "width:80px", placeholder: "N",
      value: spec.min ?? "", "aria-label": `${noun}: N or more scenes`,
      onchange: () => {
        const v = minIn.value === "" ? undefined : parseInt(minIn.value, 10);
        sourceDraft[facetKey] = {...(sourceDraft[facetKey] || {}), ...(v !== undefined ? {min: v} : {min: undefined})};
        if (!sourceDraft[facetKey].min && !sourceDraft[facetKey].max) delete sourceDraft[facetKey];
        changed();
      },
    });
    return el("div", {class: "rule-dates", style: "margin-top:8px"},
      el("span", {class: "pill sim"}, "or dynamic:"), el("span", {style:"color:var(--text-faint)"}, `${noun} with fewer than`), maxIn,
      el("span", {style:"color:var(--text-faint)"}, "or more than"), minIn, el("span", {style:"color:var(--text-faint)"}, "scenes"),
      el("span", {class: "pill sim", title: "Resolved by Stash on every query — no id list to maintain"}, "updates itself"));
  }

  function build() {
    ensureFilterShape();
    const s = sourceDraft;

    // -- saved search row (when applicable) --
    if (s.type === "savedFilter") {
      node.append(el("div", { class: "rule-row" },
        el("div", { class: "rule-head" }, el("span", { class: "rule-name" }, "Linked saved search"),
          el("span", { class: "pill sim" }, "used verbatim")),
        el("div", { class: "sub", style: "color:var(--text-dim)" },
          `Saved search #${s.id} — its stored criteria and text query are the membership. Editing the search in Stash changes what airs here; the rules below are not available for linked searches.`),
      ));
      return;
    }

    // -- tags row --
    node.append(el("div", { class: "rule-row" },
      el("div", { class: "rule-head" },
        el("span", { class: "rule-name" }, "Tags"),
        logicToggle(() => s.tags, (v) => { s.tags = v; }, () => s.tagsAny, (v) => { s.tagsAny = v; }, "ALL", "ANY"),
        pickerButton("tags", "Choose tags", () => [...s.tags, ...s.tagsAny], (v) => {
          if (s.tags.length) s.tags = v; else s.tagsAny = v;
        }),
        el("label", { class: "pill", style: "gap:6px", title: "Excluded tags (any-of)" },
          " exclude ", pickerButton("tags", "Choose excluded tags", () => s.excludeTags, (v) => { s.excludeTags = v; })),
      ),
      chipsLine("tags", [...s.tags, ...s.tagsAny], (id) => {
        s.tags = s.tags.filter((x) => x !== id);
        s.tagsAny = s.tagsAny.filter((x) => x !== id);
      }),
      s.excludeTags?.length ? chipsLine("tags", s.excludeTags, (id) => { s.excludeTags = s.excludeTags.filter((x) => x !== id); }, "") : null,
      el("div", { class: "hint", style: "color:var(--text-faint);font-size:12px;margin-top:4px" },
        "Sub-tags always count (hierarchy included). ALL = scene must carry every tag; ANY = at least one."),
    ));

    // -- performers row --
    node.append(el("div", { class: "rule-row" },
      el("div", { class: "rule-head" },
        el("span", { class: "rule-name" }, "Performers"),
        logicToggle(() => s.performers, (v) => { s.performers = v; }, () => s.performersAny, (v) => { s.performersAny = v; }, "ALL", "ANY"),
        pickerButton("performers", "Choose performers", () => [...s.performers, ...s.performersAny], (v) => {
          if (s.performers.length) s.performers = v; else s.performersAny = v;
        }),
        el("label", { class: "pill", style: "gap:6px" }, " exclude ",
          pickerButton("performers", "Choose excluded performers", () => s.excludePerformers, (v) => { s.excludePerformers = v; })),
      ),
      chipsLine("performers", [...s.performers, ...s.performersAny], (id) => {
        s.performers = s.performers.filter((x) => x !== id);
        s.performersAny = s.performersAny.filter((x) => x !== id);
      }),
      s.excludePerformers?.length ? chipsLine("performers", s.excludePerformers, (id) => { s.excludePerformers = s.excludePerformers.filter((x) => x !== id); }, "") : null,
      dynamicCountRow("performerSceneCount", "performers"),
    ));

    // -- studios row --
    node.append(el("div", { class: "rule-row" },
      el("div", { class: "rule-head" },
        el("span", { class: "rule-name" }, "Studios"),
        logicToggle(() => s.studios, (v) => { s.studios = v; }, () => s.studiosAny, (v) => { s.studiosAny = v; }, "ALL", "ANY"),
        pickerButton("studios", "Choose studios", () => [...s.studios, ...s.studiosAny], (v) => {
          if (s.studios.length) s.studios = v; else s.studiosAny = v;
        }),
        el("label", { class: "pill", style: "gap:6px" }, " exclude ",
          pickerButton("studios", "Choose excluded studios", () => s.excludeStudios, (v) => { s.excludeStudios = v; })),
      ),
      chipsLine("studios", [...s.studios, ...s.studiosAny], (id) => {
        s.studios = s.studios.filter((x) => x !== id);
        s.studiosAny = s.studiosAny.filter((x) => x !== id);
      }),
      s.excludeStudios?.length ? chipsLine("studios", s.excludeStudios, (id) => { s.excludeStudios = s.excludeStudios.filter((x) => x !== id); }, "") : null,
      dynamicCountRow("studioSceneCount", "studios"),
      el("div", { class: "hint", style: "color:var(--text-faint);font-size:12px;margin-top:4px" }, "Sub-studios count (hierarchy included)."),
    ));

    // -- metadata row --
    const dateFrom = el("input", { type: "date", value: s.date?.from || "", "aria-label": "Scene date from",
      onchange: () => { s.date = { from: dateFrom.value || "", to: dateTo.value || "" }; if (!dateFrom.value && !dateTo.value) s.date = undefined; changed(); rerender(); } });
    const dateTo = el("input", { type: "date", value: s.date?.to || "", "aria-label": "Scene date to",
      onchange: () => { s.date = { from: dateFrom.value || "", to: dateTo.value || "" }; if (!dateFrom.value && !dateTo.value) s.date = undefined; changed(); rerender(); } });
    const durMin = el("input", { type: "number", min: 0, step: "any", style: "width:90px",
      value: s.duration?.min != null ? (Number.isInteger(s.duration.min / 60) ? String(s.duration.min / 60) : String(Math.round(s.duration.min / 60 * 100) / 100)) : "",
      "aria-label": "Minimum duration in minutes",
      placeholder: "min",
      onchange: () => { setDuration(); } });
    const durMax = el("input", { type: "number", min: 0, step: "any", style: "width:90px",
      value: s.duration?.max != null ? (Number.isInteger(s.duration.max / 60) ? String(s.duration.max / 60) : String(Math.round(s.duration.max / 60 * 100) / 100)) : "",
      "aria-label": "Maximum duration in minutes",
      placeholder: "max",
      onchange: () => { setDuration(); } });
    function setDuration() {
      // Decimals allowed (stored to the nearest second); explicit 0 is a
      // real bound (presence-based, never erased); blank is absence (E8).
      const lo = durMin.value.trim() === "" ? undefined : Number(durMin.value);
      const hi = durMax.value.trim() === "" ? undefined : Number(durMax.value);
      if (lo === undefined && hi === undefined) s.duration = undefined;
      else {
        s.duration = {};
        if (lo !== undefined && Number.isFinite(lo)) s.duration.min = Math.round(lo * 60);
        if (hi !== undefined && Number.isFinite(hi)) s.duration.max = Math.round(hi * 60);
      }
      changed();
      rerender();
    }
    const created = el("input", { type: "number", min: 1, style: "width:80px", value: s.createdAt?.withinDays || "", "aria-label": "Added within days",
      placeholder: "days",
      onchange: () => { s.createdAt = created.value ? { withinDays: parseInt(created.value, 10) } : undefined; changed(); rerender(); } });
    const q = el("input", { type: "text", value: s.q || "", placeholder: "text search…", style: "flex:1", "aria-label": "Text search",
      oninput: () => { s.q = q.value.trim() || undefined; changed(); } }); // synchronous draft truth (E6)

    node.append(el("div", { class: "rule-row" },
      el("div", { class: "rule-head" }, el("span", { class: "rule-name" }, "Scene details")),
      el("div", { class: "rule-dates" },
        el("span", { class: "pill sim" }, "dated"), dateFrom, "→", dateTo,
        el("span", { class: "pill sim" }, "duration ≥"), durMin, el("span", { class: "pill sim" }, "≤"), durMax, el("span", { style: "color:var(--text-faint)" }, "min"),
      ),
      el("div", { class: "rule-dates", style: "margin-top:8px" },
        el("span", { class: "pill sim" }, "added within"), created, el("span", { style: "color:var(--text-faint)" }, "days"),
        el("span", { class: "pill sim" }, "text"), q,
      ),
    ));
  }

  build();
  return {
    node,
    source: () => sourceDraft,
    setOnChange(fn) { changed = fn; },
  };
}

export { ENT };
