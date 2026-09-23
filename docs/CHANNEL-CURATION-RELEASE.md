# Channel curation — release runbook (production)

Production deployment is NOT part of this implementation; this is the
executable path for when the owner approves it. Validated as far as dev; the
unvalidated-here steps are marked ⚠.

## Prerequisites

* Owner approval of the frontend (recorded in
  `docs/CHANNEL-CURATION-DESIGN-DECISION.md`) and of production rollout.
* Production Stash `192.168.8.40` (systemd `stash.service`), plugin install
  at `/home/shahram/dev/stash-justwatch` (plain copy), plugins path
  `/mnt/stash-virtiofs/stashapp/plugins`.
* A working production API key (prod stick shared_prefs path is the known
  source). Never print keys.
* TV repo AGENTS.md device policy respected (production `.169` install-only;
  never drive its UI).

## Release steps

1. **Ship plugin code** (old contract-compatible; activates nothing):
   ```bash
   rsync -az --delete --exclude '.git' ./ \
       shahram@192.168.8.40:/home/shahram/dev/stash-justwatch/
   curl -s -H "Apikey: $KEY" \
       -X POST -d '{"query":"mutation{reloadPlugins}"}' http://192.168.8.40:9999/…
   ```
   ⚠ (`reloadPlugins` must be issued against the production GraphQL endpoint
   exactly as the established deploy flow does.)
2. **Backup + migrate** (run ON the prod host, or remotely against a mounted
   path — the tool refuses to guess):
   ```bash
   python3 tools/migrate_channel_library.py \
       --data-dir /mnt/stash-virtiofs/stashapp/stash-justwatch-data --dry-run
   # read the report; expect the production catalog's actual revision/channel
   # count in the manifest — drift must be explained before proceeding
   python3 tools/migrate_channel_library.py \
       --data-dir /mnt/stash-virtiofs/stashapp/stash-justwatch-data --apply
   ```
3. **Reload the plugin again**; verify read-only:
   `GetChannelLibrary` (revision 1, 513 networks + N customs),
   `Directory` (legacy shape intact), `ProgrammingStatus` unchanged.
4. **TV APK** (debug-signed, same keystore): `adb install -r` the
   armeabi-v7a APK onto `.169` per that repo's policy (install only).
   The dynamic-group guide activates only against the migrated server; old
   APK keeps the legacy Directory shapes — safe in either order, plugin
   first is conventional.
5. **Continuing activation is NOT part of this release**: rollout entries for
   retired ids are recorded in the migration marker and skipped; enabling
   continuing for any of the 513 remains the operator's separate rollout-file
   decision (kill switch semantics unchanged).

## Rollback

```bash
python3 tools/migrate_channel_library.py \
    --data-dir /mnt/stash-virtiofs/stashapp/stash-justwatch-data \
    --restore /mnt/stash-virtiofs/stashapp/migration-backup-<stamp>
# then reloadPlugins; old APK keeps working either way
```

Restores the whole pre-migration snapshot (definitions, rollout file,
publications, snapshots) and removes the library. It is deliberately coarse:
it is disaster recovery, not an edit-undo (that is definition history /
Discard).

## Post-release verification

* Browser: edit a channel → Apply → receipt committed →
  `GetChannelApplyResult` by requestId → Directory/TV reflect it after
  refresh.
* TV: groups appear by owner order; pad tunes globally; favorites that
  survive migration still tune; a favorite whose channel was retired shows
  unavailable rather than tuning an unrelated channel.
* Scheduler: next hourly PrepareProgramming drains `channel-library-pending.json`
  (or reports it empty); no channel flips to continuing that the rollout file
  did not name.
