# The new channel lineup

800 channels, numbered 100–899, using the original CSV's 26-column contract.
All channel names have been refreshed. Tag-channel branding is playful;
the include/exclude columns remain the precise definition of membership.

## Files

- **[New-taxonomy proposal](proposed_channels_new_taxonomy.csv)** — the full
  intended 800-channel lineup. Fourteen spotlight slots introduce the new
  canonical categories. These rows intentionally have blank tag IDs, scene
  counts and library percentages because the categories do not yet exist in
  production. This is a draft and must not be imported yet.
- **[Current-production version](proposed_channels_new_taxonomy_scene_validated.csv)**
  — 800 renamed channels with the original filters preserved and counts
  recomputed against production. This passes the existing importer and is
  usable with the current library. It keeps the existing programs in the
  fourteen pending slots until the taxonomy is deployed.
- **[Pending replacements](proposed_channels_new_taxonomy_pending.csv)** —
  identifies those fourteen slots, their proposed names and required tags.
- **[Validation record](proposed_channels_new_taxonomy_validation.json)** —
  timestamp, library total, channel-family totals and original-file checksum.

The source `proposed_channels_scene_validated.csv`, compiled `networks.json`,
custom channel catalog and production installation have not been modified.

## Naming samples

| Name | Filter concept |
|---|---|
| Knot Your Average Network | Bondage |
| HR Has Left the Chat | Office setting, used in combinations |
| Batteries Not Included | Sex toys, used in combinations |
| Counter Intelligence | Kitchen setting |
| Mick Blue Has the Remote | Performer spotlight |
| Poolside Terms & Slippery Conditions | Cheating premise, pool/water, oiled |
| The Fourth Wall Is on Lunch | Behind the scenes — pending |
| Snack to the Future | Food play — pending |
| Headset & Forget | VR — pending |
| Chest a Minute | Breast play — pending |
| Choose Your Own Remote | Interactive — pending |

Existing performer/studio identities are preserved in their channel names.
Channel names do not assert real relationships, sexual orientation or exact
ages. The current version's Married IRL channel is named **Ring Information
Desk**, with a rationale clarifying that a performer marriage tag does not
establish a relationship between co-stars. The future draft replaces that
spotlight with Behind the scenes. Cast filters refer to linked metadata and
cannot guarantee that every participant has been credited.

## Validation and rollout

All 800 current-production rows were recounted using the runtime's own
`build_source` → `build_scene_filter` projection, including hierarchical
includes and exclusions. Queries were read-only. The library had **20,178
scenes**; every channel matched at least **12**, with no empty channels.
Counts describe current assignments, not hypothetical results after retagging.
They can drift as the library changes.

The 14 newly retained categories were checked by name against production and
none existed. No substitute raw-tag IDs or invented counts were used. The
future slots trade narrower/redundant appearance and production spotlights
for distinct formats and clearer concepts; the pending CSV shows every
replacement. Existing combinations and performer/studio channels remain.

JAV exclusion `9320` remains on every row except the four sanctioned studio
channels **407, 461, 480 and 835**, including in the future draft. Existing
include/exclude policies have not been broadened. The family distribution
remains unchanged. Affinity-lift values are blank because they were not
recomputed; they must not be mistaken for fresh statistics.

To use the current-production version, explicitly compile that file with
`tools/import_channels.py`. Compiling writes `justwatch/networks.json`; it was
not done as part of generating this proposal. Review before replacing the
existing tier. Because the importer hashes **number and name**, these requested
renames will change network IDs and rotation seeds when compiled, even though
numbers and current filters remain the same.

For the intended future lineup, first deploy the reviewed tag rules and update
scene tags. Then resolve the fourteen canonical tag IDs, recount **all 800**
rows using the normal recount tool, and compile only after every row has valid
IDs and counts. Merely creating empty tags would not validate the lineup.
Keep the existing JAV exemptions when reapplying exclusion policy.
