"""The network tier: the owner's curated channel list (channels 100+).

Compiled from the authoring CSV by ``tools/import_channels.py`` into
``networks.json`` beside this module (the file deploys with the plugin
source, like everything else under the install dir). The networks REPLACE
the TV app's client-generated
General/Studios/Performers sections: the dial becomes 1-99 custom channels
plus 100+ networks, with no server-side tiering anywhere.

Load policy mirrors the catalog's: the file is a build artifact, so a
malformed one is a hard error (``NetworksError``) — never an empty dial and
never a silent partial lineup. A MISSING file is legal (empty tier) so an
older deployment keeps working.

Network channels are read-only: they never enter catalog.json, are not
editable through SaveCatalog, and keep the exact identity the CSV authored
(stable ids/seeds from the importer). Health comes free — each row carries
its CSV-validated ``count``, so no snapshots are computed for this tier.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from justwatch import contract

PATH = Path(__file__).resolve().parent / "networks.json"

SECTIONS = ("performers", "studios", "general")

DIGITS_RE = re.compile(r"^[0-9]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NetworksError(Exception):
    """The compiled networks file is malformed (a build artifact bug)."""


def load(path: Path | None = None) -> dict:
    """Load networks.json, strictly. Missing file -> empty tier.

    ``path`` overrides the deployed location (validation and tests compile a
    preview artifact and load THAT — never the live file by accident).
    """
    target = PATH if path is None else path
    if not target.exists():
        return {"revision": "", "channels": []}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NetworksError(f"networks unreadable at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise NetworksError("networks.json is not a JSON object")
    channels = raw.get("channels")
    if not isinstance(channels, list):
        raise NetworksError("networks.json channels must be a list")
    seen: set[int] = set()
    ids: set[str] = set()
    for i, channel in enumerate(channels):
        _require_channel(channel, i)
        if channel["number"] in seen:
            raise NetworksError(f"networks[{i}] ({channel['id']}): duplicate number")
        seen.add(channel["number"])
        if channel["id"] in ids:
            raise NetworksError(f"duplicate network id {channel['id']}")
        ids.add(channel["id"])
    return {"revision": revision(channels), "channels": channels}


def revision(channels: list) -> str:
    """Content identity of the tier (independent of file metadata)."""
    canonical = json.dumps(channels, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _require_channel(channel: Any, i: int) -> None:
    def bad(message: str) -> NetworksError:
        return NetworksError(f"networks[{i}] is malformed: {message}")

    if not isinstance(channel, dict):
        raise bad(f"must be a JSON object, got {type(channel).__name__}")
    if not str(channel.get("id", "")).startswith("net_"):
        raise bad(f"id {channel.get('id')!r} is not a network id (net_XXXXXXXX)")
    number = channel.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 100:
        raise bad(f"invalid number {number!r} (the network band starts at 100)")
    if not isinstance(channel.get("name"), str) or not channel["name"].strip():
        raise bad("missing name")
    if channel.get("glyph") not in contract.GLYPHS:
        raise bad("glyph is not in the shared glyph set")
    if channel.get("section") not in SECTIONS:
        raise bad(f"unknown section {channel.get('section')!r}")
    if channel.get("sort") not in contract.SORTS:
        raise bad(f"unknown sort {channel.get('sort')!r}")
    seed = channel.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed <= 2_147_483_647):
        raise bad(f"invalid seed {seed!r}")
    if isinstance(channel.get("count"), bool) or not isinstance(channel.get("count"), int):
        raise bad("count must be an integer")
    source = channel.get("source")
    if not isinstance(source, dict) or source.get("type") != "filter":
        raise bad("source must be a filter source")
    if not any(_ids(source.get(kind)) for kind in (
            "tags", "performers", "performersAny", "studios", "studiosAny")) \
            and not _metadata_criteria(source, bad):
        raise bad("source has no include criteria")
    if not isinstance(channel.get("programmingMode"), str):
        raise bad("missing programmingMode")


def _metadata_criteria(source: dict, bad) -> bool:
    """True when the source carries at least one metadata criterion (scene
    date range / minimum duration / created-at recency); malformed criteria
    raise (strict loader: a broken build artifact is never half-accepted)."""
    date = source.get("date")
    if date is not None:
        ok = (isinstance(date, dict)
              and all(isinstance(date.get(k), str) and DATE_RE.match(date[k])
                      for k in ("from", "to")))
        if not ok:
            raise bad("malformed date criterion (expected {from, to} YYYY-MM-DD)")
        return True
    duration = source.get("duration")
    if duration is not None:
        value = duration.get("min") if isinstance(duration, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise bad("malformed duration criterion (expected {min: seconds})")
        return True
    created = source.get("createdAt")
    if created is not None:
        value = created.get("withinDays") if isinstance(created, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise bad("malformed createdAt criterion (expected {withinDays: days})")
        return True
    return False


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if DIGITS_RE.match(str(x))]


def directory_payload() -> dict | None:
    """The ``networks`` block for the Directory response, or None when the
    deployment has no networks file (clients keep their fallback path)."""
    document = load()
    if not document["channels"]:
        return None
    return document


def get(channel_id: str) -> dict | None:
    """One network channel by id (the Lineup path for net_ channels)."""
    if not channel_id.startswith("net_"):
        return None
    for channel in load()["channels"]:
        if channel["id"] == channel_id:
            return channel
    return None


def sections() -> dict[str, list[dict]]:
    """Networks grouped for the guide/editor: general / studios / performers."""
    out: dict[str, list[dict]] = {name: [] for name in SECTIONS}
    for channel in load()["channels"]:
        out[channel["section"]].append(channel)
    return out
