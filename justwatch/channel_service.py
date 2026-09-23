"""One resolved channel view for every consumer.

After the library migration the plugin has ONE authoritative store
(``library.load``); before it, the legacy stores (``catalog`` + ``networks``)
are the truth. Every runtime consumer — Directory, FullDirectory, Lineup,
Schedule, programming, continuing, snapshots, the editor — asks THIS module,
never the stores directly, so what the GUI edits is what airs.

The resolved shape is the LIBRARY record (both namespaces carry the same
fields); :func:`as_legacy_catalog_channel` / :func:`as_legacy_network_channel`
project it onto the exact legacy dict shapes when a consumer (or a legacy
payload contract) needs them. ``groupId`` rides along additively; legacy
consumers ignore it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from justwatch import catalog, library, networks


class ChannelServiceError(Exception):
    pass


def library_active(data_dir: str | Path) -> bool:
    """True when the authoritative library document exists. Installing new
    plugin code never activates it — only the migration creates the file."""
    return library.exists(data_dir)


def load_view(data_dir: str | Path) -> dict:
    """The resolved view: ``{libraryId, revision, groups, channels, source}``.

    ``source`` is ``"library"`` or ``"legacy"``. Legacy views synthesize the
    same record shape so callers need no branching; group membership falls
    back to each network's compiled ``section`` and customs go to the first
    group ("My Channels").
    """
    if library_active(data_dir):
        doc = library.load(data_dir)
        return {
            "libraryId": doc["libraryId"],
            "revision": doc["revision"],
            "groups": [dict(g) for g in doc["groups"]],
            "channels": [dict(c) for c in doc["channels"]],
            "source": "library",
            "migration": doc.get("migration"),
        }
    return _legacy_view(data_dir)


def _legacy_view(data_dir: str | Path) -> dict:
    cat = catalog.load(data_dir)
    channels: list[dict] = []
    for row in cat.get("channels", []):
        channels.append(_from_legacy_catalog_row(row))
    nets = networks.load()
    for row in nets.get("channels", []):
        channels.append(_from_legacy_network_row(row))
    channels.sort(key=lambda c: c["number"])
    return {
        "libraryId": f"legacy-{cat.get('revision', 0)}",
        "revision": cat.get("revision", 0),
        "groups": _legacy_groups(),
        "channels": channels,
        "source": "legacy",
        "migration": None,
    }


def _legacy_groups() -> list[dict]:
    return [
        {"id": "grp_my", "name": "My Channels", "position": 1, "legacySection": None},
        {"id": "grp_general", "name": "General", "position": 2, "legacySection": "general"},
        {"id": "grp_studios", "name": "Studios", "position": 3, "legacySection": "studios"},
        {"id": "grp_performers", "name": "Performers", "position": 4, "legacySection": "performers"},
    ]


def _base_group_id(legacy_section: str | None) -> str:
    return {
        "general": "grp_general",
        "studios": "grp_studios",
        "performers": "grp_performers",
    }.get(legacy_section or "", "grp_general")


def _from_legacy_catalog_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": "ch",
        "number": row["number"],
        "name": row["name"],
        "glyph": row.get("glyph"),
        "color": row.get("color", "#455A64"),
        "groupId": "grp_my",
        "sort": row.get("sort", "shuffle"),
        "seed": row["seed"],
        "enabled": row.get("enabled", True),
        "archived": False,
        "paused": False,
        "source": row.get("source") or {},
        "sourceLabel": row.get("sourceLabel", ""),
        "programming": row.get("programming"),
        "provenance": {"origin": "custom"},
    }


def _from_legacy_network_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": "net",
        "number": row["number"],
        "name": row["name"],
        "glyph": row.get("glyph"),
        "color": row.get("color", "#455A64"),
        "groupId": _base_group_id(row.get("section")),
        "sort": row.get("sort", "shuffle"),
        "seed": row["seed"],
        "enabled": True,
        "archived": False,
        "paused": False,
        "source": row.get("source") or {},
        "sourceLabel": row.get("sourceLabel", ""),
        "programming": row.get("programming"),
        "provenance": {
            "origin": "network",
            "stableKey": row.get("stableKey"),
            "legacySection": row.get("section"),
            "seedCount": row.get("count"),
        },
    }


# ---------------------------------------------------------------------------
# Consumer helpers
# ---------------------------------------------------------------------------


def playable_channels(data_dir: str | Path) -> list[dict]:
    """Enabled, unarchived, unpaused channels in dial order (number order)."""
    view = load_view(data_dir)
    return [c for c in view["channels"]
            if c["enabled"] and not c["archived"] and not c["paused"]]


def get_channel(data_dir: str | Path, channel_id: str) -> dict | None:
    view = load_view(data_dir)
    for channel in view["channels"]:
        if channel["id"] == channel_id:
            return channel
    return None


def custom_channels_legacy(data_dir: str | Path) -> list[dict]:
    """The ``ch_`` namespace in the exact legacy catalog-row shape, for
    consumers (snapshots, programming) that predate the library."""
    view = load_view(data_dir)
    return [as_legacy_catalog_channel(c) for c in view["channels"] if c["kind"] == "ch"]


def network_channels_legacy(data_dir: str | Path) -> list[dict]:
    """The ``net_`` namespace in the exact legacy networks-row shape."""
    view = load_view(data_dir)
    return [as_legacy_network_channel(c) for c in view["channels"] if c["kind"] == "net"]


def as_legacy_catalog_channel(channel: dict) -> dict:
    from justwatch.programming import policy
    return {
        "id": channel["id"],
        "number": channel["number"],
        "name": channel["name"],
        "glyph": channel.get("glyph"),
        "color": channel.get("color", "#455A64"),
        "source": channel.get("source") or {},
        "sourceLabel": channel.get("sourceLabel", ""),
        "sort": channel.get("sort", "shuffle"),
        "seed": channel["seed"],
        "enabled": bool(channel.get("enabled", True)),
        "programming": policy(channel.get("programming")),
    }


def as_legacy_network_channel(channel: dict) -> dict:
    """Legacy networks-row projection. ``section`` is the group's
    ``legacySection`` fallback — always one of the three legal legacy names
    (groups without a legacy mapping land in "general"); ``count`` is the
    HISTORICAL seed count when known (legacy payloads keep their documented
    numeric semantics; freshness is the pool-status surface's job)."""
    legacy = channel.get("provenance", {}).get("legacySection") or "general"
    if legacy not in networks.SECTIONS:
        legacy = "general"
    seed_count = channel.get("provenance", {}).get("seedCount")
    row = {
        "id": channel["id"],
        "number": channel["number"],
        "name": channel["name"],
        "glyph": channel.get("glyph"),
        "color": channel.get("color", "#455A64"),
        "section": legacy,
        "family": channel.get("provenance", {}).get("family", "channel"),
        "count": seed_count if isinstance(seed_count, int) else 0,
        "sort": channel.get("sort", "shuffle"),
        "seed": channel["seed"],
        "programmingMode": (channel.get("programming") or {}).get("mode", "fixed"),
        "sourceLabel": channel.get("sourceLabel", ""),
        "source": channel.get("source") or {},
    }
    provenance = channel.get("provenance") or {}
    if isinstance(provenance.get("rationale"), str):
        row["rationale"] = provenance["rationale"]
    return row


def groups_ordered(data_dir: str | Path) -> list[dict]:
    return load_view(data_dir)["groups"]


def group_of(data_dir: str | Path, channel_id: str) -> dict | None:
    view = load_view(data_dir)
    channel = next((c for c in view["channels"] if c["id"] == channel_id), None)
    if channel is None:
        return None
    return next((g for g in view["groups"] if g["id"] == channel["groupId"]), None)


def directory_revision(data_dir: str | Path) -> int:
    return load_view(data_dir)["revision"]


def membership_signature(channel: dict) -> str:
    """What requires invalidation: the source's membership identity."""
    from justwatch import criteria
    return criteria.source_signature(channel.get("source") or {})


def presentation_signature(channel: dict) -> str:
    """Cosmetic identity (name/number/brand/group) — changes here must NOT
    reset rotations or schedules. Enhanced clients compare this to skip
    needless playback resets after directory refreshes."""
    import hashlib
    import json
    basis = [channel.get("name"), channel.get("number"), channel.get("color"),
             channel.get("groupId"), bool(channel.get("archived")),
             bool(channel.get("paused"))]
    return hashlib.sha1(json.dumps(basis, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
