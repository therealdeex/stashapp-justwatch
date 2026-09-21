# Continuing-programming pilot manifest

Prepared 2026-09-21 per `docs/CONTINUING-PROGRAMMING-PLAN.md` phase 7 step 6.
Seven production network ids chosen by STRUCTURE (source size × family ×
section) so the pilot exercises every engine regime — not by preference. The
owner may swap any row; keep the spread of sizes.

| # | Network | Number | Family | CSV count | Why it is in the pilot |
| - | --- | --- | --- | --- | --- |
| 1 | `net_04685144` Mick Blue Has the Remote | 100 | performer_spotlight | 573 | Large single-performer source; multi-pass cycling within days. |
| 2 | `net_894ff073` Real Wife Stories: The Plot Factory | 376 | studio_spotlight | 756 | Large studio source; different metadata shape than #1. |
| 3 | `net_1fae3d5f` Clap Back TV | 500 | tag_spotlight | 3,932 | The tier's largest source: full pass ≈ 100+ days; proves coverage far beyond 50 and bounds indexing cost. |
| 4 | `net_bef19c03` The Fourth Wall Is on Lunch | 490 | tag_spotlight | 227 | Mid-size tag intersection; the typical network shape. |
| 5 | `net_78e77f1d` Alex Coal: Cancel Your Plans | 185 | performer_spotlight | 90 | Small-mid source; cooldown/window preferences start relaxing. |
| 6 | `net_def79fa0` HR Has Left the Chat | 556 | tag_pair | 51 | Thin source (~17h pass): repeat-cooldown relaxation, honest thin-library behavior. |
| 7 | `net_9869e3f9` New Sensations: You Are Here-ish | 842 | studio_tag | 5 | Sparse edge: near-daily full pass; must never stall or invent filler. |

Notes:

- The current production CSV has no `created_within_days` / `duration_min`
  rows, so the dynamic-recency path is NOT represented in the pilot; it is
  covered by unit tests and the 30-day simulation (Recent Window fixture).
- Counts are the CSV-validated values; live library drift is expected.

## Activation (explicit, per-deployment — never automatic)

On the target Stash host, write the rollout file into the plugin's DATA
directory (`<stash Dir>/stash-justwatch-data/continuing-networks.json`):

```json
{
  "enabled": true,
  "networkIds": [
    "net_04685144",
    "net_894ff073",
    "net_1fae3d5f",
    "net_bef19c03",
    "net_78e77f1d",
    "net_def79fa0",
    "net_9869e3f9"
  ]
}
```

Then trigger one preparation and verify the durable status (a queued task id
is NOT success):

```sh
python3 tools/prepare_programming.py --url http://<stash>:<port> \
    --api-key-file <keyfile> --verify
```

`ProgrammingStatus` (sync op) reports per-channel coverage; every pilot
channel must show `mode: continuing`, `ready: true`, `coverageHours ≥ 24`
before the TV build relies on it.

## Rollback

1. Set `"enabled": false` in the rollout file (or delete it). Directory,
   Schedule, and Lineup immediately describe the tier as fixed again; no
   catalog or networks.json change is involved.
2. Publications under `<data>/programming/net_*.json` (schema 2) are inert
   once the rollout is off — leave them for forensics, or archive them:
   `mkdir -p ~/continuing-backup && mv <data>/programming/net_*.json ~/continuing-backup/`.
   NEVER delete while active (deletion mid-flight is the only way to lose
   consumption history; if that happens the channel re-bootstraps safely —
   it just starts a fresh pass).
3. Upgraded TVs fall back to the fixed rotation automatically (the client
   gates on Directory's resolved mode); an old TV never noticed the pilot.

Rollback timing note: cutover back to fixed happens at the next Directory
load on each TV; a TV mid-airing on a continuing schedule finishes that
airing and then rejoins the fixed loop — a possible one-airing discontinuity
is expected and visible.
