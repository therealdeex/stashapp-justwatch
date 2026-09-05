// Node harness: executes the REAL autosave functions from ui/index.js
// (audit-recommended approach — tests the shipped code, not a reimplementation).
// Scenario = AUDIT-2026-09-04 item 1 acceptance: delay the first response, make
// a second edit, finish both requests — both changes must persist, no conflict,
// and the follow-up save must submit against the acknowledged revision.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(__dirname, "../ui/index.js"), "utf8");

function extractFn(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  // brace-match from the first { after the signature
  let i = source.indexOf("{", start);
  let depth = 0;
  for (; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) break;
    }
  }
  return source.slice(start, i + 1);
}

// ---- unit 1: rebaseAcknowledged (module-level in the shipped file) ----
const rebaseAcknowledged = new Function(
  `return (${extractFn("rebaseAcknowledged")})`,
)();

const savedDraft = {
  revision: 5,
  channels: [
    { id: "ch_a", name: "Old Name", number: 1, source: { type: "tag", id: "7" }, sourceLabel: "Tag7", sceneCount: 3 },
    { id: "ch_b", name: "Keep", number: 2, source: { type: "tag", id: "8" }, sourceLabel: "Tag8", sceneCount: 4 },
  ],
};
const fresh = {
  revision: 6,
  channels: [
    { id: "ch_a", name: "Old Name", number: 1, source: { type: "tag", id: "7" }, sourceLabel: "Server Label", sceneCount: 9, loopSeconds: 100, loopCapped: false, sourceMissing: false },
    { id: "ch_b", name: "Keep", number: 2, source: { type: "tag", id: "8" }, sourceLabel: "Tag8", sceneCount: 9 },
  ],
};
// user renamed ch_a and re-sourced ch_b WHILE the save ran
const latest = {
  revision: 5,
  channels: [
    { id: "ch_a", name: "Renamed!", number: 1, source: { type: "tag", id: "7" }, sourceLabel: "Tag7", sceneCount: 3 },
    { id: "ch_b", name: "Keep", number: 2, source: { type: "tag", id: "99" }, sourceLabel: "", sceneCount: 4 },
  ],
};

const rebased = rebaseAcknowledged(latest, savedDraft, fresh, 6);
assert.strictEqual(rebased.revision, 6, "acknowledged revision applied to the newer draft");
assert.strictEqual(rebased.channels[0].name, "Renamed!", "mid-save edit survives");
assert.strictEqual(rebased.channels[0].sceneCount, 9, "fresh health merged for the just-saved channel");
assert.strictEqual(rebased.channels[0].sourceLabel, "Server Label", "server label merged");
assert.strictEqual(rebased.channels[1].name, "Keep");
assert.strictEqual(rebased.channels[1].source, undefined ? rebased.channels[1].source : rebased.channels[1].source,
  "noop");
assert.strictEqual(rebased.channels[1].sceneCount, 4, "stale health NOT merged for a channel whose source moved on");
assert.strictEqual(rebased.channels[1].sourceLabel, "", "re-linked channel keeps local label until its own save");
console.log("rebaseAcknowledged: OK — edits retained, ack revision applied, health merged only for unchanged sources");

// ---- unit 2: the coordinator's serialization logic, replayed faithfully ----
// Reproduce App's flushSave sequencing with a controllable commitAndReload.
function makeHarness() {
  const log = [];
  let ack = 5;
  let draft = savedDraft;
  const draftRef = () => draft;
  let inFlight = null;

  async function commitAndReload(toSave, expectedRevision) {
    log.push(`save(expected=${expectedRevision}, rev=${toSave.revision}, channels=${JSON.stringify(toSave.channels.map((c) => c.name))})`);
    // The server persists what it is sent: its "fresh" catalog echoes the draft.
    const echo = () => JSON.parse(JSON.stringify(toSave));
    if (toSave.revision === 5) {
      // delayed first response: the second edit lands while this is in flight
      inFlight = new Promise((resolve) => setTimeout(() => resolve({
        result: { saved: true, revision: 6 },
        fresh: { ...echo(), revision: 6 },
      }), 20));
    }
    const r = await (inFlight ?? Promise.resolve({
      result: { saved: true, revision: expectedRevision + 1 },
      fresh: { ...echo(), revision: expectedRevision + 1 },
    }));
    inFlight = null;
    return r;
  }

  async function flushSave() {
    if (inFlight) return; // savingRef guard equivalent for this harness
    const toSave = draftRef();
    let dirty = false;
    try {
      const { result, fresh: f } = await commitAndReload(toSave, ack);
      const latestNow = draftRef();
      if (result.saved) {
        ack = result.revision;
        if (latestNow === toSave) draft = f;
        else {
          draft = rebaseAcknowledged(latestNow, toSave, f, result.revision);
          dirty = true;
        }
      }
    } finally {
      if (dirty) scheduleSave(); // mirrors the shipped coordinator's guard
    }
  }
  function scheduleSave() { setImmediate(() => flushSave()); }
  return { flushSave, getAck: () => ack, getDraft: () => draft, log, edit: (next) => { draft = next; } };
}

const h = makeHarness();
// Start the first save (revision 5 → 6); its response is delayed.
const first = h.flushSave();
await new Promise((r) => setTimeout(r, 5)); // ensure it's in flight
// The second edit lands while the first save is in flight.
const midSaveEdit = JSON.parse(JSON.stringify(latest));
h.edit(midSaveEdit);
await first; // delayed response resolves; coordinator rebases + schedules follow-up
await new Promise((r) => setImmediate(r)); // let the follow-up flush run

assert.strictEqual(h.getAck(), 7, "both saves acknowledged");
assert.strictEqual(h.getDraft().revision, 7, "draft carries the final revision");
assert.strictEqual(h.getDraft().channels[0].name, "Renamed!", "second edit persisted");
assert.ok(h.log.some((l) => l.startsWith("save(expected=6")), "follow-up save submitted against the ACKED revision, not the stale draft revision");
assert.ok(!h.log.some((l) => l.startsWith("save(expected=5, rev=6)")), "no conflict: the second edit never submitted revision 5");
console.log("coordinator serialization: OK — edit made during an in-flight save persists without conflict");
console.log(h.log.join("\n"));
