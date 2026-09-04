"""Health snapshots + result side-channels.

Two file families, both written atomically and mirrored into the plugin's
``assets/`` dir so the Channel Studio page can read them over
``/plugin/stash-justwatch/assets/...`` (session-cookie auth) without any
GraphQL round-trip:

* ``snapshots/directory.json`` -- per-channel health: scene count, loop
  length, source existence. Written on every save and by the Refresh Data
  task.
* ``save_result.json`` -- the result of a Save Channel Edit task. Stash relays
  a job's status but NOT the plugin's stdout JSON, so the editor matches on
  the ``requestId`` it generated to tell its own save apart from a stale one.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from justwatch import lineup

SNAPSHOT_LIMIT_SCENES = 20_000  # above this, loop length is reported as null


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def write_snapshot(data_dir: Path, assets_dir: Path, payload: Any) -> None:
    """Dual-write: authoritative data-dir copy + transient assets mirror."""
    body = json.dumps(payload, ensure_ascii=False)
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            path = base / "directory.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # mirrors are best-effort


def read_snapshot(data_dir: Path, assets_dir: Path) -> dict:
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            path = base / "directory.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def write_save_result(data_dir: Path, assets_dir: Path, payload: dict) -> None:
    payload = dict(payload)
    payload["writtenAt"] = _now_iso()
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            write_json(base / "save_result.json", payload)
        except OSError:
            pass  # mirrors are best-effort


def build_directory_snapshot(client: Any, catalog: dict) -> dict:
    """Recompute health for every enabled channel (one aggregate query each).

    Scene count + loop length come from a single ``per_page: -1`` lineup fetch
    (bounded: very large sources report a null loop length rather than
    scanning unbounded rows). Source existence is re-checked so the editor can
    offer a relink flow when a saved filter or entity was deleted in Stash.
    """
    channels: dict[str, Any] = {}
    for channel in catalog.get("channels", []):
        if not channel.get("enabled", True):
            continue
        channel_id = channel.get("id") or ""
        entry: dict[str, Any] = {
            "sceneCount": None,
            "loopSeconds": None,
            "loopCapped": False,
            "sourceMissing": False,
        }
        object_filter = None
        source = channel.get("source") or {}
        if source.get("type") == "savedFilter":
            object_filter = lineup.fetch_saved_object_filter(
                client, str(source.get("id", ""))
            )
            if object_filter is None:
                entry["sourceMissing"] = True
                channels[channel_id] = entry
                continue
        try:
            full = lineup.fetch_lineup(
                client,
                source=source,
                sort=channel.get("sort") or "shuffle",
                seed=int(channel.get("seed") or 0),
                page=1,
                per_page=1,
                include_paths=False,
                object_filter=object_filter,
            )
            entry["sceneCount"] = full["total"]
            if full["total"] and full["total"] <= SNAPSHOT_LIMIT_SCENES:
                everything = lineup.fetch_lineup(
                    client,
                    source=source,
                    sort=channel.get("sort") or "shuffle",
                    seed=int(channel.get("seed") or 0),
                    page=1,
                    per_page=-1,
                    include_paths=False,
                    object_filter=object_filter,
                )
                entry["loopSeconds"] = round(
                    sum(i["duration"] for i in everything["items"]), 1,
                )
            elif full["total"] > SNAPSHOT_LIMIT_SCENES:
                entry["loopCapped"] = True
        except lineup.LookupError:
            entry["sourceMissing"] = True
        except Exception:
            # counts are health extras; a failure must not fail the save
            pass
        channels[channel_id] = entry
    return {
        "computedAt": _now_iso(),
        "revision": catalog.get("revision", 0),
        "channels": channels,
    }
