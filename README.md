# stash-justwatch

Companion Stash server plugin for the [StashAppAndroidTV](https://github.com/damontecres/StashAppAndroidTV)
**Just Watch** feature: author your own TV channels (numbers **1–99**) in the
Stash web UI, tune them on the TV.

Just Watch airs your library as simulated cable TV on a deterministic
wall-clock schedule. The TV app ships with 60 curated networks (101–160) and
auto-generated channels (201+, tags/studios/performers). This plugin adds the
**My Channels** band (1–99): channels you name, number, and program yourself —
edited comfortably from a desktop browser in the **Channel Studio** page this
plugin injects into Stash's web UI (`/plugins/stash-justwatch`).

## How it works

- **Catalog** (channels + tuning settings) lives in
  `<stash config dir>/stash-justwatch-data/catalog.json`.
- The **TV app** pulls `Directory` and `Lineup` through Stash's
  `runPluginOperation` GraphQL mutation (sync, API-key authenticated) and
  runs its broadcast schedule exactly as for built-in channels.
- The **Channel Studio** page reads health snapshots from
  `/plugin/stash-justwatch/assets/snapshots/directory.json` and writes edits
  via `runPluginTask` (Save Channel Edit / Refresh Data).
- **No plugin, no problem**: the TV feature works unchanged without this
  plugin; the plugin only adds the My Channels band and desktop editing.

## Channel sources

| Source | Airs |
| --- | --- |
| Saved filter | Any saved **scene** filter, verbatim (the powerful path) |
| Tag | Scenes with the tag (hierarchical — subtags included). A channel can air from a **set of tags**: a scene matching any of them airs |
| Performer | Scenes featuring the performer |
| Studio | Scenes from the studio (hierarchical — child studios included) |

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
`contractVersion: 1`, the operation map, feature flags (custom channels,
global settings, draft preview, health snapshots), the shared glyph set, and
limits. Clients whitelist operations and never pin plugin versions.

Sync ops (`runPluginOperation`): `Capabilities`, `Directory`, `FullDirectory`,
`Lineup`, `PreviewLineup`, `GetCatalog`, `ValidateCatalog`, `Schedule`,
`PreviewProgramming`, `ProgrammingDesk`.
Task ops (`runPluginTask`): `SaveCatalog`, `RefreshData`, `PrepareProgramming`
— writes run as tasks so snapshot regeneration never blocks a GraphQL
connection.

Determinism note: seeded shuffle relies on Stash's `random_<seed>` sort being
stable for a given seed — the same assumption the TV app makes for its own
channels.

## Development

```bash
python3 -m pytest tests/ -q      # 106 focused tests, stdlib only
node tools/test_autosave_harness.mjs   # autosave coordinator replay
```

House conventions follow `~/dev/stash-tag-curator` (raw plugin interface,
assets-mirror snapshots, save-result side-channel). The UX contract for the
Channel Studio page lives in `AGENTS.md`.
