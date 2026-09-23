# Channel curation prototypes — five interaction options

Five genuinely different, fully interactive workflows over the SAME library:
the real 513-channel v4-final proposal (compiled from
`analysis/just-watch-final/networks.preview.json` by
`tools/build_prototype_fixtures.py`) plus synthetic custom channels and
fixture states (empty pool, deleted entity, paused/archived, 3,569-performer
rule row, long names). **Every count, preview, schedule and image is
simulated.** No production calls, no media, no build step, no dependencies.

## Run

```bash
python3 tools/serve_prototypes.py          # from the repo root
# add --tailscale to (re)publish https://dev-lab2.manx-teeth.ts.net:8462/
```

Then open <http://localhost:8462/> — the landing page links all five variants,
describes the shared review script, and documents the fixture channels.
The port is fixed (8462). If the machine rebooted, run the same command;
`tailscale serve --https=8462 off` unpublishes the tailnet URL.

| URL | Variant |
| --- | --- |
| `/studio/` | A — Channel Studio: searchable dial + focused side-by-side editor |
| `/organizer/` | B — Group Organizer: group-first cards, drag/bulk moves, group manager |
| `/library/` | C — Channel Library: dense filterable/sortable table, bulk ops, CSV export |
| `/guide/` | D — Visual Guide: EPG browsing with contextual editing in place |
| `/curator/` | E — Guided Curator: progressive steps, review-then-Apply |

## What's shared vs. different

Shared (by design — these are the production semantics being prototyped):
mock API with expectedRevision + requestId receipts (idempotent retries),
per-channel drafts that never autosave, the rule model + plain-language
summary, simulated pool-vs-rotation preview, failure scenarios, and the
Apply state machine (`clean -> dirty -> validating -> applying -> applied`,
with conflict/transport/validation branches that keep the draft).

Different (the actual choice): navigation and organization model —
dial+editor (A), groups-first (B), table+bulk (C), guide-in-place (D),
stepper+review (E).

## Layout of this directory

```
shared/fixture-data.js   generated — do not edit (tools/build_prototype_fixtures.py)
shared/mock-api.js       server stand-in: library, transactions, receipts, scenarios
shared/editor-sections.js  channel editor + Apply/Discard state machine + top bar
shared/rules.js          rule model, visual rule rows, plain-language summary
shared/bulk.js           bulk move (with group creation), groups manager
shared/ui.js             DOM helpers, virtual list, dialogs, entity picker
studio/ organizer/ library/ guide/ curator/   one html+js pair each
screenshots/             current captures (supplement, never a substitute)
```

Drafts live in `sessionStorage` per variant (survive reload in the tab, die
with the tab); Reset in any variant's top bar restores the same starting
fixture and clears that variant's drafts.
