// Mock API: the server-side stand-in every prototype talks to. Mirrors the
// planned backend semantics (touched-record transactions, expectedRevision,
// durable requestId receipts, idempotent retries) with SIMULATED latency and
// injectable failure scenarios. No network, no production calls.

import { FIXTURES } from "./fixture-data.js";
import { clone, deepEqual, hashOf } from "./ui.js";

export const SCENARIOS = {
  normal: "Normal",
  slowApply: "Slow Apply (~2.5 s)",
  conflict: "Revision conflict on next Apply",
  lostResponse: "Lost response, then idempotent retry",
  validationError: "Validation error on next Apply",
};

const APPLY_LATENCY_MS = 420;
const PREVIEW_LATENCY_MS = 380;

export function createMockApi() {
  // ----- library state (authoritative "server" copy) -----
  const networkChannels = FIXTURES.channels.map((c) => ({
    id: c.id, kind: "net", number: c.number, name: c.name, glyph: c.glyph,
    color: c.color, groupId: groupForLegacySection(c.section), sort: c.sort,
    seed: c.seed, enabled: true, archived: false, paused: false,
    source: clone(c.source), sourceLabel: c.sourceLabel,
    programming: { mode: c.programmingMode },
    seedCount: c.seedCount, // historical extraction count — NOT a live number
    provenance: { origin: "v4-final-proposal", stableKey: `${c.number}|${c.name}` },
  }));
  const customChannels = FIXTURES.customChannels.map((c) => ({
    id: c.id, kind: "ch", number: c.number, name: c.name, glyph: "",
    color: c.color, groupId: c.groupId, sort: c.sort, seed: c.seed,
    enabled: c.enabled !== false && !c.paused, archived: !!c.archived,
    paused: !!c.paused,
    source: clone(c.source), sourceLabel: c.sourceLabel,
    programming: clone(c.programming) || { mode: "fixed" },
    seedCount: c.seedCount ?? 0,
    provenance: { origin: "custom", note: c.note },
  }));

  const library = {
    schemaVersion: 1,
    libraryId: "lib-proto-7f3a",
    revision: 21,
    groups: clone(FIXTURES.groups),
    channels: [...customChannels, ...networkChannels].sort((a, b) => a.number - b.number),
  };

  const receipts = new Map(); // requestId -> receipt (bounded, newest kept)
  const receiptsOrder = [];
  let scenario = "normal";
  let lostResponseReceipt = null;
  const listeners = new Set();

  function groupForLegacySection(section) {
    return { general: "grp_general", studios: "grp_studios", performers: "grp_performers" }[section] || "grp_general";
  }

  function notify() { for (const fn of listeners) fn(); }

  function nextFreeNumber(bandMin, bandMax) {
    const taken = new Set(library.channels.filter((c) => !c.archived || true).map((c) => c.number));
    for (let n = bandMin; n <= bandMax; n++) if (!taken.has(n)) return n;
    return null;
  }

  // ----- validation (structure + references relevant to the touched edit) -----
  function validateOps(ops) {
    const errors = [];
    const groupIds = new Set(library.groups.map((g) => g.id));
    const newGroups = [];
    for (const op of ops) if (op.op === "group.put") newGroups.push(op.group);
    for (const g of newGroups) groupIds.add(g.id);

    ops.forEach((op, i) => {
      const p = (msg) => errors.push({ path: `ops[${i}]`, code: "invalid", message: msg });
      switch (op.op) {
        case "channel.put": {
          const ch = op.channel;
          if (!ch || typeof ch !== "object") { p("channel.put needs a channel"); break; }
          if (!ch.name || !String(ch.name).trim()) errors.push({ path: `ops[${i}].channel.name`, code: "bad_name", message: "Name can't be empty." });
          else if (String(ch.name).trim().length > 60) errors.push({ path: `ops[${i}].channel.name`, code: "bad_name", message: "Name must be 60 characters or fewer." });
          if (!groupIds.has(ch.groupId)) errors.push({ path: `ops[${i}].channel.groupId`, code: "unknown_group", message: "Choose a group that exists." });
          const num = ch.number;
          if (!Number.isInteger(num) || num < 1 || num > 899) errors.push({ path: `ops[${i}].channel.number`, code: "bad_number", message: "Number must be 1–899." });
          else {
            const clash = library.channels.find((c) => c.number === num && c.id !== ch.id);
            if (clash) errors.push({ path: `ops[${i}].channel.number`, code: "duplicate_number", message: `Channel ${num} is taken by “${clash.name}”. Use Swap.` });
          }
          if (!ch.source || !ch.source.type) p("channel needs a source");
          if (ch.source && ch.source.type === "filter") {
            const s = ch.source;
            const any = (v) => Array.isArray(v) && v.length;
            if (!any(s.tags) && !any(s.tagsAny) && !any(s.excludeTags) && !any(s.performers)
              && !any(s.performersAny) && !any(s.studios) && !any(s.studiosAny)
              && !s.date && !s.duration && !s.createdAt && !s.q) {
              errors.push({ path: `ops[${i}].channel.source`, code: "empty_rules", message: "Add at least one rule (an intentionally empty pool needs a rule that matches nothing, not no rules)." });
            }
          }
          if (ch.id === "ch_f0a1b2c3") {
            // fixture: source references a deleted entity; editing metadata is
            // allowed but a CHANGED source must reference something valid.
            if (op._sourceTouched) {
              errors.push({ path: `ops[${i}].channel.source`, code: "missing_source", message: "The referenced performer no longer exists in Stash. Re-link this channel's source first." });
            }
          }
          break;
        }
        case "group.put": {
          const g = op.group;
          if (!g?.name || !String(g.name).trim()) errors.push({ path: `ops[${i}].group.name`, code: "bad_name", message: "Group name can't be empty." });
          else {
            const norm = String(g.name).trim().toLowerCase();
            const clash = [...library.groups, ...newGroups].find((x) => x.id !== g.id && x.name.trim().toLowerCase() === norm);
            if (clash) errors.push({ path: `ops[${i}].group.name`, code: "duplicate_group", message: `A group named “${clash.name}” already exists.` });
          }
          break;
        }
        case "group.delete": {
          const members = library.channels.filter((c) => c.groupId === op.id).length;
          if (library.groups.length + newGroups.length - 1 <= 0) p("Cannot remove the last group.");
          if (members > 0 && !op.moveTo) errors.push({ path: `ops[${i}].moveTo`, code: "destination_required", message: `Choose a destination for ${members} channel(s).` });
          if (!groupIds.has(op.moveTo || "")) p("group.delete moveTo must exist");
          break;
        }
        case "channels.move": {
          if (!groupIds.has(op.groupId)) p("channels.move target group must exist");
          break;
        }
        case "channels.patch": {
          if (!Array.isArray(op.channelIds) || !op.channelIds.length) p("channels.patch needs channelIds");
          else if (typeof op.patch !== "object" || op.patch === null) p("channels.patch needs a patch object");
          break;
        }
        default:
          p(`unknown op ${op.op}`);
      }
    });
    return errors;
  }

  function applyOps(ops) {
    for (const op of ops) {
      switch (op.op) {
        case "channel.put": {
          const idx = library.channels.findIndex((c) => c.id === op.channel.id);
          if (idx >= 0) {
            const stored = library.channels[idx];
            const next = { ...clone(op.channel) };
            next.id = stored.id; next.seed = stored.seed; next.kind = stored.kind; // identity immutable
            next.provenance = stored.provenance;
            library.channels[idx] = next;
          } else {
            library.channels.push(clone(op.channel));
            library.channels.sort((a, b) => a.number - b.number);
          }
          break;
        }
        case "group.put": {
          const idx = library.groups.findIndex((g) => g.id === op.group.id);
          if (idx >= 0) library.groups[idx] = { ...library.groups[idx], ...clone(op.group) };
          else library.groups.push(clone(op.group));
          library.groups.sort((a, b) => a.position - b.position);
          break;
        }
        case "group.delete": {
          for (const c of library.channels) if (c.groupId === op.id) c.groupId = op.moveTo;
          library.groups = library.groups.filter((g) => g.id !== op.id);
          break;
        }
        case "channels.move": {
          for (const c of library.channels) if (op.channelIds.includes(c.id)) c.groupId = op.groupId;
          break;
        }
        case "channels.patch": {
          // bulk metadata patch (pause/resume/archive/enable)
          for (const c of library.channels) {
            if (op.channelIds.includes(c.id)) Object.assign(c, clone(op.patch));
          }
          break;
        }
        default: break;
      }
    }
  }

  function recordReceipt(receipt) {
    receipts.set(receipt.requestId, receipt);
    receiptsOrder.push(receipt.requestId);
    if (receiptsOrder.length > 200) receipts.delete(receiptsOrder.shift());
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  return {
    // ----- reads -----
    getLibrary() {
      return { revision: library.revision, groups: clone(library.groups), channels: clone(library.channels), libraryId: library.libraryId };
    },
    getDefinition(channelId) {
      const ch = library.channels.find((c) => c.id === channelId);
      return ch ? clone(ch) : null;
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    nextFreeNumber,
    groups: () => clone(library.groups),

    // ----- simulated preview: signature-correlated, bounded, read-only -----
    async preview(draft) {
      await sleep(PREVIEW_LATENCY_MS);
      const signature = "sig-" + hashOf(JSON.stringify(draft.source)).toString(36);
      const seedCount = draft.seedCount ?? null;
      const source = draft.source || {};
      const isZero = JSON.stringify(source).includes("999999") || JSON.stringify(source).includes("424242");
      const pool = isZero ? 0 : (seedCount && hashOf(JSON.stringify(draft.source)) % 3 === 0
        ? seedCount : 30 + (hashOf(JSON.stringify(draft.source)) % 900));
      return {
        signature,
        poolCount: pool,
        rotationSize: Math.min(50, pool),
        rotationComplete: pool <= 50,
        sample: Array.from({ length: Math.min(10, pool) }, (_, i) => ({
          id: `sim-${(hashOf(signature + i) % 99991).toString(36)}`,
          title: ["Late Shift", "Quiet Hours", "Double Feature", "Backlot", "After Curfew",
            "First Take", "Encore", "Matinee", "Nightcap", "Cold Open"][i],
          duration: 480 + (hashOf(signature + i) % 5400),
          studio: ["Meridian", "Northwind", "Halcyon", "Parallax", "Blue Door", "Cardinal"][i % 6],
          date: `2026-0${1 + (i % 9)}-${String(3 + (i % 25)).padStart(2, "0")}`,
        })),
        // Continuing channels would also carry their published-schedule status
        // here; in the prototype only the two seeded continuing channels do.
        schedule: draft.programming?.mode === "continuing" ? {
          status: "ready", coverageHours: 96 + (hashOf(draft.id) % 48),
          note: "SIMULATED published schedule status",
        } : null,
        simulated: true,
      };
    },

    // ----- the one write path -----
    // apply(requestId, expectedRevision, ops) -> receipt
    //   { requestId, status:"committed", revision, appliedAt, results:[{op,status}] }
    //   { requestId, status:"rejected", error:"revision_conflict", currentRevision }
    //   { requestId, status:"rejected", error:"validation_failed", errors }
    async apply(requestId, expectedRevision, ops) {
      if (receipts.has(requestId)) return clone(receipts.get(requestId)); // idempotent replay
      await sleep(scenario === "slowApply" ? 2500 : APPLY_LATENCY_MS);

      if (scenario === "conflict") {
        // Another editor lands an unrelated commit first: this Apply must come
        // back revision_conflict with the current revision.
        scenario = "normal";
        await this.externalEdit();
      }

      if (scenario === "lostResponse") {
        // First attempt with a fresh id: commit server-side, then "lose" the
        // response. The retry (same requestId) finds the receipt — the demo
        // for why the client must retry with the SAME id, not a new request.
        if (!lostResponseReceipt) {
          await this.applyInner(requestId, expectedRevision, ops); // throws on conflict
          lostResponseReceipt = receipts.get(requestId);
          throw new TypeError("Failed to fetch: network connection lost after submit");
        }
      }

      const receipt = this.applyInner(requestId, expectedRevision, ops);
      return receipt;
    },

    applyInner(requestId, expectedRevision, ops) {
      if (scenario === "validationError") {
        scenario = "normal";
        const receipt = {
          requestId, status: "rejected", error: "validation_failed",
          errors: [{ path: "ops[0].channel.name", code: "bad_name", message: "(Scenario) Name collides with an existing channel." }],
          revision: library.revision,
        };
        recordReceipt(receipt);
        return receipt;
      }
      if (expectedRevision !== library.revision) {
        const receipt = {
          requestId, status: "rejected", error: "revision_conflict",
          message: `Draft expects revision ${expectedRevision}, server is at ${library.revision}.`,
          currentRevision: library.revision,
        };
        recordReceipt(receipt);
        return receipt;
      }
      const errors = validateOps(ops);
      if (errors.length) {
        const receipt = { requestId, status: "rejected", error: "validation_failed", errors, revision: library.revision };
        recordReceipt(receipt);
        return receipt;
      }
      applyOps(ops);
      library.revision += 1;
      const receipt = {
        requestId, status: "committed", revision: library.revision,
        appliedAt: new Date().toISOString(),
        touched: ops.length,
        note: "Committed. Background programming refresh is pending where membership changed.",
      };
      recordReceipt(receipt);
      notify();
      return receipt;
    },

    getReceipt(requestId) {
      return receipts.has(requestId) ? clone(receipts.get(requestId)) : null;
    },

    // ----- scenario controls + reset -----
    setScenario(name) {
      scenario = name;
      if (name !== "lostResponse") lostResponseReceipt = null;
    },
    get scenario() { return scenario; },

    // Simulates another editor committing an unrelated change (bumps revision).
    async externalEdit() {
      await sleep(200);
      const other = library.channels.find((c) => c.id === "ch_b5c6d7e8");
      if (other) other.sourceLabel = `Tuned elsewhere at ${new Date().toLocaleTimeString()}`;
      library.revision += 1;
      notify();
      return library.revision;
    },

    reset() {
      // Rebuild from the same starting fixture: simplest correct reset.
      const fresh = createMockApi();
      library.channels = fresh.getLibrary().channels.map((c) => ({ ...c }));
      library.groups = fresh.groups();
      library.revision = fresh.getLibrary().revision;
      receipts.clear(); receiptsOrder.length = 0;
      lostResponseReceipt = null;
      scenario = "normal";
      notify();
    },
  };
}

// ----- per-variant draft store (session-scoped, versioned; never autosaves) -----
const DRAFT_NS = "jw-proto-drafts-v1:";

function announceDraftChange() {
  window.dispatchEvent(new CustomEvent("jw-drafts-changed"));
}

export function draftStore(variant) {
  const key = DRAFT_NS + variant;
  function readAll() {
    try { return JSON.parse(sessionStorage.getItem(key) || "{}"); } catch { return {}; }
  }
  function writeAll(all) {
    try { sessionStorage.setItem(key, JSON.stringify(all)); } catch { /* full storage: drafts stay in memory */ }
  }
  return {
    get(channelId) { return readAll()[channelId] || null; },
    put(channelId, draft) { const all = readAll(); all[channelId] = draft; writeAll(all); announceDraftChange(); },
    drop(channelId) { const all = readAll(); delete all[channelId]; writeAll(all); announceDraftChange(); },
    ids() { return Object.keys(readAll()); },
    clear() { try { sessionStorage.removeItem(key); } catch { /* noop */ } announceDraftChange(); },
  };
}
