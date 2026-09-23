# Channel curation — migration and rollback runbook (dev + production)

Audience: the operator (Shahram). Production steps are documented for release;
they were NOT executed as part of this implementation. Dev (`.40`-adjacent,
`:9998`) rehearsal and validation were.

## What the migration does

`tools/migrate_channel_library.py` creates the ONE authoritative,
owner-editable library at `<stash Dir>/stash-justwatch-data/channel-library.json`:

* all **513 networks of the v4-final proposal** (compiled through the real
  importer from `data/proposed_channels_final.csv` and verified against
  `analysis/just-watch-final/networks.preview.json` — identities, seeds,
  sources, exclusions, and the proposal's **five sanctioned JAV exceptions**
  (225, 252, 407, 461, 480) preserved byte-for-byte);
* every **existing custom channel unchanged** (ids, seeds, sources, policies,
  enabled state), placed in the "My Channels" group;
* the initial groups: My Channels, General (209), Studios (115),
  Performers (189);
* a migration marker (`seedCatalog: just-watch-v4-final`) plus digests.

It is **explicit** — never run by a read, page visit, or plugin start. It is
**idempotent** — a completed migration is a no-op on re-run and never resets
owner edits made after it. The old `catalog.json`/`networks.json` remain as
backed-up inputs; a deployment that has never run the migration keeps serving
from them exactly as before. Installing plugin code alone migrates nothing
and activates nothing (continuing rollout activation is still exclusively the
operator's `continuing-networks.json`).

## Pre-flight (any environment)

1. Confirm the target Stash is reachable and the plugin loads:
   `Capabilities` over GraphQL (or the Channel Studio page opening cleanly).
2. Note the current data dir: dev `…/stash-justwatch-data/` on dev-lab2
   (`:9998`); production `/mnt/stash-virtiofs/stashapp/stash-justwatch-data/`
   on `192.168.8.40`.
3. **Read the actual state, never assume it**: current catalog revision,
   custom channels, rollout file contents (`continuing-networks.json`),
   publication directories.

## Migrate (dry run first)

```bash
# DEV example
python3 tools/migrate_channel_library.py \
    --data-dir /opt/stash-dev/stash-justwatch-data --dry-run
# then, after reading the report:
python3 tools/migrate_channel_library.py \
    --data-dir /opt/stash-dev/stash-justwatch-data --apply
```

The tool: verifies the seed (513 networks; sections 209/115/189), reconciles
against the recorded migration map (292 kept / 221 replaced / 282 retired),
reports **drift** (any deployment record the map/seed do not explain —
e.g. a network renamed on a "keep" slot) and EXITS NON-ZERO on drift so an
unrecognized deployment is never silently overwritten. `--apply` writes an
immutable backup first (catalog, compiled networks, rollout file, publications
under `migration-backup-<stamp>/` next to the data dir) and commits the
library atomically under the lock.

Exit codes: `0` clean; `2` drift detected (dry run report on stderr).

## After migration

1. Reload the plugin (Settings → Plugins → reload, or the `reloadPlugins`
   mutation). `Capabilities` now advertises `features.channelLibrary`.
2. Open the Channel Studio: the library view shows 513 networks + your
   customs, grouped My Channels / General / Studios / Performers.
3. Sanity reads (no writes): `Directory` — customs + networks blocks unchanged
   in shape; `GetChannelLibrary` — revision 1, the full list; `ProgrammingStatus`
   — untouched channels stay ready.

## Rollback / restore

```bash
python3 tools/migrate_channel_library.py \
    --data-dir <data-dir> --restore <data-dir-parent>/migration-backup-<stamp>
```

Restore is an **operator recovery tool, not an edit-undo**: it reinstates the
complete pre-migration snapshot (definitions, rollout file, publications,
snapshots) and removes the library document. Never mix restored definitions
with newer publications: restore the snapshot as a whole. Restore of a
single bad edit is what definition history (GetChannelHistory → restore as a
new Apply) or Discard is for.

## Known migration-time facts (dev, 2026-09 rehearsal)

* The two dev rollout ids: `net_461ab3e3` survives into the 513; `net_8091d3ea`
  and `net_04685144` — check the tool's `retiredRolloutIds` line at run time;
  retired rollout entries are recorded and skipped explicitly (activation
  never transfers by number).
* The tiny dev library yields many empty pools against real Stash — that is
  honest: seed counts are historical (September 8 extraction), not health.
