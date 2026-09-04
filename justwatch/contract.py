"""The v1 client contract: constants every consumer (TV app, web editor) shares.

The contract is the API surface, not the plugin version: any build that answers
``capabilities`` with the values below is compatible, whatever its point
release (same policy as stash-reels).
"""

from __future__ import annotations

PLUGIN_ID = "stash-justwatch"
CONTRACT_VERSION = 1
SCHEMA_VERSION = 1

#: Channel numbers reserved for custom channels. The TV dial uses 101-160 for
#: curated networks and 201+ for auto-generated channels; 1-99 is the free
#: "low band" that sits first in the flip order.
MIN_CHANNEL_NUMBER = 1
MAX_CHANNEL_NUMBER = 99

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
    "capabilities": "Capabilities",
    "directory": "Directory",
    "lineup": "Lineup",
    "previewLineup": "PreviewLineup",
    "getCatalog": "GetCatalog",
    "validateCatalog": "ValidateCatalog",
    "saveCatalog": "SaveCatalog",
    "refreshData": "RefreshData",
}

#: Operations a sync ``runPluginOperation`` may address (fast, bounded). The
#: async write ops (``saveCatalog``/``refreshData``) are task-only so a slow
#: snapshot regeneration never blocks a GraphQL connection.
SYNC_OPERATIONS = ("capabilities", "directory", "lineup", "previewLineup",
                   "getCatalog", "validateCatalog")


def capabilities(plugin_version: str) -> dict:
    """Build the ``Capabilities`` handshake payload."""
    return {
        "pluginId": PLUGIN_ID,
        "pluginVersion": plugin_version,
        "contractVersion": CONTRACT_VERSION,
        "operations": dict(OPERATIONS),
        "features": {
            "customChannels": True,
            "globalSettings": True,
            "draftPreview": True,
            "healthSnapshots": True,
            "channelNumbers": [MIN_CHANNEL_NUMBER, MAX_CHANNEL_NUMBER],
            "sorts": list(SORTS),
            "glyphs": list(GLYPHS),
        },
        "limits": {
            "maxChannels": MAX_CHANNEL_NUMBER,
            "lineupPerPage": 50,
            "lineupPerPageMax": 100,
        },
    }
