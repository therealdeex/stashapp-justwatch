# stash-justwatch

Companion Stash server plugin for the [StashAppAndroidTV](https://github.com/damontecres/StashAppAndroidTV)
**Just Watch** feature: author your own TV channels (numbers **1–99**) in the
Stash web UI, tune them on the TV.

Just Watch airs your library as simulated cable TV on a deterministic
wall-clock schedule. This plugin owns two tiers of the dial:

- **My Channels** (1–99): channels you name, number, and program yourself —
  edited comfortably from a desktop browser in the **Channel Studio** page
  this plugin injects into Stash's web UI (`/plugins/stash-justwatch`).
- **Networks** (100+): the owner's curated channel list, compiled from the
  authoring CSV (`data/proposed_channels_scene_validated.csv`) into
  `justwatch/networks.json` by `tools/import_channels.py`. The networks
  REPLACE the TV app's client-generated General/Studios/Performers sections —
  the dial becomes 1–99 custom channels plus 100–899 owner-curated networks,
  covering performer/studio/tag spotlights and their intersections
  (ALL-of tag pairs and triples, tag exclusions, performer pairs). Network
  ids are database ids of the Stash the CSV was validated against; against a
  different library the tier simply airs empty.

## How it works

- **Catalog** (channels + tuning settings) lives in
  `<stash config dir>/stash-justwatch-data/catalog.json`. The network tier is
  compiled into the plugin (`networks.json`) and is read-only — it never
  enters the catalog and needs no snapshots (its CSV-validated counts ARE the
  health data).
- The **TV app** pulls `Directory` (custom channels + the `networks` block)
  and `Lineup` through Stash's `runPluginOperation` GraphQL mutation (sync,
  API-key authenticated) and runs its broadcast schedule exactly as for
  built-in channels. A plugin (or app) without the networks block falls back
  to the legacy client-side generation — deploy order doesn't matter.
- The **Channel Studio** page reads health snapshots from
  `/plugin/stash-justwatch/assets/snapshots/directory.json` and writes edits
  via `runPluginTask` (Save Channel Edit / Refresh Data).
- **No plugin, no problem**: the TV feature works unchanged without this
  plugin; the plugin only adds the My Channels band, the network tier, and
  desktop editing.

## Channel sources

| Source | Airs |
| --- | --- |
| Saved filter | Any saved **scene** filter, verbatim (the powerful path) |
| Tag | Scenes with the tag (hierarchical — subtags included). A channel can air from a **set of tags**: a scene matching any of them airs |
| Performer | Scenes featuring the performer |
| Studio | Scenes from the studio (hierarchical — child studios included) |
| Filter (networks only) | The composite behind the curated tier: ALL-of tags / performers / studios plus any-of tag exclusions, projected to one `SceneFilterType` (`INCLUDES_ALL`, `excludes`, `depth -1`) |

Play order: seeded shuffle (default, deterministic — the same channel airs the
same rotation), newest, oldest, top rated, longest, shortest.

## Published programming

Instead of airing a raw rotation, a channel can publish a concrete 72-hour
schedule: **fixed** (the plain rotation), **explore**, or **discovery**
ordering, optional spacing between repeat performers/studios, a repeat
cooldown, and a weekly studio or performer spotlight block. The **Prepare
Programming** task builds each channel's publication (it pages the source
index in bounded chunks and rebuilds only changed channels); the TV app's
`Schedule` op serves the airings at wall-clock times, and the Channel Studio
previews draft policies without publishing.

Run it hourly with the bundled systemd user units:

```bash
mkdir -p ~/.config/stash-justwatch ~/.config/systemd/user
printf 'STASH_URL=http://localhost:9999\nSTASH_API_KEY_FILE=%s/.config/stash-justwatch/api_key\n' "$HOME" \
  > ~/.config/stash-justwatch/scheduler.env   # then put the API key in that api_key file
cp tools/systemd/stash-justwatch-programming.service \
   tools/systemd/stash-justwatch-programming.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stash-justwatch-programming.timer
```

## Install

Symlink or copy this repo into Stash's plugins directory and reload plugins:

```bash
ln -s ~/dev/stash-justwatch /opt/stash-dev/plugins/stash-justwatch
# Stash UI -> Settings -> Plugins -> reload
```

No Python dependencies beyond stdlib (PyYAML is optional, used only to fall
back to the server `config.yml` for the API key).

## Contract (v1)

`capabilities` is the handshake: `pluginId: stash-justwatch`,
`contractVersion: 1`, the operation map, feature flags (custom channels, the
network tier, global settings, draft preview, health snapshots), the shared
glyph set, and limits. Clients whitelist operations and never pin plugin
versions.

Sync ops (`runPluginOperation`): `Capabilities`, `Directory` (custom channels
+ the `networks` block), `FullDirectory`, `Lineup` (also serves `net_`
channels), `PreviewLineup`, `GetCatalog`, `ValidateCatalog`, `Schedule`,
`PreviewProgramming`, `ProgrammingDesk`.
Task ops (`runPluginTask`): `SaveCatalog`, `RefreshData`, `PrepareProgramming`
— writes run as tasks so snapshot regeneration never blocks a GraphQL
connection.

Determinism note: seeded shuffle relies on Stash's `random_<seed>` sort being
stable for a given seed — the same assumption the TV app makes for its own
channels.

## The channel CSV (networks)

`data/proposed_channels_scene_validated.csv` is the authoring artifact: one
row per network (number, name, family, validated scene count, include/exclude
criteria, rationale). Re-import after editing it:

```bash
python3 tools/import_channels.py        # data/... -> justwatch/networks.json
python3 -m pytest tests/ -q             # then test + reload plugins in Stash
```

The import is deterministic (ids and seeds hash the row identity), so
re-importing an unchanged CSV is a no-op and rotations never reshuffle.
Section assignment: `performer_*` families -> Performers, `studio_*` ->
Studios, `tag_*` families -> General; brand glyph/color per section.

`exact_scene_count` is the tier's health data — the importer trusts it
verbatim — so recompute it whenever criteria or library change, and sweep it
periodically, with `tools/recount_channels.py` (run against the Stash the
CSV's ids belong to; credentials from `--url`/`--api-key-file` or the
`STASH_URL`/`STASH_API_KEY_FILE` env vars):

```bash
python3 tools/recount_channels.py --check                  # validation sweep
python3 tools/recount_channels.py \
  --exclude-tag 9320=JAV --write \
  --except-row 407 --except-row 461 --except-row 480 --except-row 835   # JAV policy
python3 tools/import_channels.py                           # then re-import
```

`--exclude-tag ID=NAME` is the "this tag airs nowhere in the network tier"
knob (idempotent, appends to every row's any-of exclusions); `--except-row N`
exempts a channel from the policy (the four all-JAV studios are the standing
exception). Without `--write` the run reports drift only. Counts only mean
anything against the library the CSV was validated against — the ids are
Stash database ids.

## Development

```bash
python3 -m pytest tests/ -q      # 129 focused tests, stdlib only
node tools/test_autosave_harness.mjs   # autosave coordinator replay
```

House conventions follow `~/dev/stash-tag-curator` (raw plugin interface,
assets-mirror snapshots, save-result side-channel). The UX contract for the
Channel Studio page lives in `AGENTS.md`.
