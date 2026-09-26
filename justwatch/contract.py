"""The v1 client contract: constants every consumer (TV app, web editor) shares.

The contract is the API surface, not the plugin version: any build that answers
``capabilities`` with the values below is compatible, whatever its point
release (same policy as stash-reels).
"""

from __future__ import annotations

PLUGIN_ID = "stash-justwatch"
CONTRACT_VERSION = 1
SCHEMA_VERSION = 1

#: Channel numbers reserved for custom channels. The network tier (the
#: owner's curated list compiled into networks.json) numbers from 100 up;
#: 1-99 is the free "low band" that sits first in the flip order.
MIN_CHANNEL_NUMBER = 1
MAX_CHANNEL_NUMBER = 99

#: The network tier's number band (informational; the Directory payload
#: carries the authoritative channels).
MIN_NETWORK_NUMBER = 100

#: The active rotation policy. A channel airs a bounded, ordered window over
#: its source — up to ROTATION_SIZE *playable* scenes in the channel's own
#: order, scanning at most ROTATION_SCAN_LIMIT raw rows server-side. TV
#: playback, editor previews, health counts, and loop lengths all derive from
#: that same rotation, so every client describes the same program loop. The
#: rotation is deliberately NOT the whole library: scheduling never fetches an
#: unbounded source, and the editor labels the rotation as what's on air while
#: `sourceTotal` reports the library behind it.
ROTATION_SIZE = 50
ROTATION_SCAN_LIMIT = 1000

#: Lineup sorts and their server-side FindFilterType projection. ``shuffle``
#: uses the server's deterministic ``random_<seed>`` sort keyed on the
#: channel's stored seed.
SORTS: dict[str, dict[str, str]] = {
    "shuffle": {"sort": "", "direction": "DESC"},  # sort filled per channel seed
    "newest": {"sort": "date", "direction": "DESC"},
    "oldest": {"sort": "date", "direction": "ASC"},
    "top_rated": {"sort": "rating", "direction": "DESC"},
    "longest": {"sort": "duration", "direction": "DESC"},
    "shortest": {"sort": "duration", "direction": "ASC"},
}

SOURCE_TYPES = ("savedFilter", "tag", "performer", "studio")

LAUNCH_MODES = ("last", "random")

#: Default tuning for the TV app's auto-generated channels (201+). Custom
#: channels never inherit these; they are exactly what the owner authored.
#: NOTE: these shape the plugin's own computed directory (FullDirectory) and
#: the Channel Studio's rail only. The TV app tunes its generated channels from
#: its own per-server preferences and never reads these values.
DEFAULT_SETTINGS: dict = {
    "soloThreshold": 10,
    "groupThreshold": 5,
    "launchMode": "last",
    "includeTags": {"general": [], "studios": [], "performers": []},
    "excludeTags": {"general": [], "studios": [], "performers": []},
}

#: FontAwesome solid codepoints, proven renderable on the TV (they are the
#: brand glyphs of the 60 curated dial networks, minus the chevron/play
#: furniture). The web editor offers exactly this set so a glyph picked on the
#: web renders identically on TV.
GLYPHS: tuple[str, ...] = (
    "\ue131", "\uf004", "\uf005", "\uf007", "\uf008", "\uf015", "\uf030",
    "\uf03d", "\uf043", "\uf06b", "\uf06c", "\uf06d", "\uf06e", "\uf072",
    "\uf07a", "\uf084", "\uf091", "\uf0a1", "\uf0c4", "\uf0c5", "\uf0d6",
    "\uf0eb", "\uf0f1", "\uf111", "\uf11b", "\uf130", "\uf135", "\uf14e",
    "\uf182", "\uf186", "\uf19d", "\uf1b0", "\uf1b9", "\uf1da", "\uf1e0",
    "\uf1fc", "\uf236", "\uf256", "\uf2cc", "\uf2e7", "\uf3a5", "\uf44b",
    "\uf44e", "\uf4d8", "\uf4e3", "\uf508", "\uf52d", "\uf52e", "\uf54c",
    "\uf56d", "\uf5bb", "\uf5e4", "\uf6de", "\uf6fa", "\uf753", "\uf773",
    "\uf8d7", "\uf8d9",
)

#: Every operation the plugin answers, and the client-visible token for each.
#: Consumers advertise/whitelist these names in ``capabilities.operations``.
OPERATIONS: dict[str, str] = {
    "schedule": "Schedule",
    "previewProgramming": "PreviewProgramming",
    "programmingDesk": "ProgrammingDesk",
    "programmingStatus": "ProgrammingStatus",
    "prepareProgramming": "PrepareProgramming",
    "capabilities": "Capabilities",
    "directory": "Directory",
    "fullDirectory": "FullDirectory",
    "lineup": "Lineup",
    "previewLineup": "PreviewLineup",
    "getCatalog": "GetCatalog",
    "validateCatalog": "ValidateCatalog",
    "saveCatalog": "SaveCatalog",
    "refreshData": "RefreshData",
    # --- channel library (additive v1: editing + dynamic groups) ---
    "getChannelLibrary": "GetChannelLibrary",
    "getChannelDirectory": "GetChannelDirectory",
    "getChannelDefinition": "GetChannelDefinition",
    "validateChannelChanges": "ValidateChannelChanges",
    "previewChannelPool": "PreviewChannelPool",
    "applyChannelChanges": "ApplyChannelChanges",
    "getChannelApplyResult": "GetChannelApplyResult",
    "getChannelHistory": "GetChannelHistory",
    "getChannelRefreshStatus": "GetChannelRefreshStatus",
    "requeueChannelRefresh": "RequeueChannelRefresh",
}

#: Operations a sync ``runPluginOperation`` may address (fast, bounded). The
#: async write ops (``saveCatalog``/``refreshData``/``applyChannelChanges``)
#: are task-only so slow work never blocks a GraphQL connection.
SYNC_OPERATIONS = ("capabilities", "directory", "lineup", "previewLineup",
                   "getCatalog", "validateCatalog", "schedule", "fullDirectory",
                   "programmingDesk", "previewProgramming", "programmingStatus",
                   "getChannelLibrary", "getChannelDirectory", "getChannelDefinition",
                   "validateChannelChanges", "previewChannelPool",
                   "getChannelApplyResult", "getChannelHistory",
                   "getChannelRefreshStatus", "requeueChannelRefresh")


def capabilities(plugin_version: str) -> dict:
    """Build the ``Capabilities`` handshake payload."""
    return {
        "pluginId": PLUGIN_ID,
        "pluginVersion": plugin_version,
        "contractVersion": CONTRACT_VERSION,
        "operations": dict(OPERATIONS),
        "features": {
            "publishedSchedule": {"version": 1, "pageSize": 50, "horizonHours": 72},
            # Continuing network programming (rollout-gated server side): full
            # eligible library instead of the fixed 50-item rotation, rolling
            # horizon with a 24h whole-airing protected window, ~15% of
            # flexible slots prioritizing new arrivals. Additive v1: Lineup
            # keeps serving the fixed rotation for every non-continuing (or
            # legacy-client) consumer.
            "programming": {
                "version": 2,
                "networks": True,
                "horizonHours": 168,
                "protectedHours": 24,
                "freshnessSharePercent": 15,
                "statusOperation": "ProgrammingStatus",
            },
            "customChannels": True,
            # The owner's curated network tier (Directory.networks): replaces
            # the TV app's client-generated General/Studios/Performers.
            "networks": {"version": 1, "minNumber": MIN_NETWORK_NUMBER},
            # The owner-editable channel library + explicit Apply (additive
            # v1): present only on deployments whose migration created the
            # authoritative library document. Old clients never read these.
            "channelLibrary": {
                "version": 1,
                "directoryOperation": "GetChannelDirectory",
                "libraryOperation": "GetChannelLibrary",
                "definitionOperation": "GetChannelDefinition",
                "historyOperation": "GetChannelHistory",
            },
            "channelGroups": {
                "version": 1,
                "singleMembership": True,
                "legacySectionFallback": True,
            },
            "explicitApply": {
                "version": 1,
                "applyOperation": "ApplyChannelChanges",
                "validateOperation": "ValidateChannelChanges",
                "resultOperation": "GetChannelApplyResult",
            },
            # Post-Apply refresh truth (additive v1): the durable pending-work
            # journal + published health for "is my channel still refreshing /
            # did the last check fail", plus a bounded requeue for the retry.
            "refreshStatus": {
                "version": 1,
                "operation": "GetChannelRefreshStatus",
                "requeueOperation": "RequeueChannelRefresh",
            },
            "poolPreview": {"version": 1, "operation": "PreviewChannelPool"},
            # The plugin exposes tuning settings, but they govern its own
            # computed directory, not the TV app's generated channels.
            "globalSettings": True,
            "draftPreview": True,
            "healthSnapshots": True,
            "rotation": {"size": ROTATION_SIZE, "scanLimit": ROTATION_SCAN_LIMIT},
            "channelNumbers": [MIN_CHANNEL_NUMBER, MAX_CHANNEL_NUMBER],
            "sorts": list(SORTS),
            "glyphs": list(GLYPHS),
        },
        "limits": {
            "maxChannels": MAX_CHANNEL_NUMBER,
            "lineupPerPage": 50,
            "lineupPerPageMax": 100,
            # The editable library's number space (My Channels 1-99 + networks
            # 100-899). Legacy clients keep reading maxChannels, which
            # continues to describe the custom-channel limit only.
            "libraryChannels": 899,
        },
    }
