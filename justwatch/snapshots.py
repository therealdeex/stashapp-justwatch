"""Health snapshots + result side-channels.

Two file families, both written atomically (unique temp files, same-filesystem
rename) and mirrored into the plugin's ``assets/`` dir so the Channel Studio
page can read them over ``/plugin/stash-justwatch/assets/...`` (session-cookie
auth) without any GraphQL round-trip:

* ``snapshots/directory.json`` -- per-channel health: rotation count, loop
  length, source existence. Written on every save and by the Refresh Data
  task. Publication is revision-aware: an older computation never replaces a
  newer snapshot.
* ``snapshots/save_result.json`` -- the results of recent Save Channel Edit
  tasks, stored as a list keyed by the editor's ``requestId`` (newest first,
  retention-capped). Stash relays a job's status but NOT the plugin's stdout
  JSON, so the editor matches its own save by requestId; per-request entries
  stop a second tab's save from clobbering the first tab's result before it
  is read.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from justwatch import contract, lineup

_SNAPSHOT_STATUSES = ("ok", "offAir", "missingSource", "unavailable")
_SAVE_RESULT_KEEP = 20
_SAVE_RESULT_MAX_AGE = timedelta(hours=24)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def write_snapshot(data_dir: Path, assets_dir: Path, payload: Any) -> None:
    """Dual-write: authoritative data-dir copy + transient assets mirror.

    Revision-aware: a payload older than what is already published (a slow
    task finishing after a newer save) is dropped rather than regressing the
    snapshot. Mirrors are best-effort; the data-dir copy is too on write
    failure (health is never worth failing a save over).
    """
    body = json.dumps(payload, ensure_ascii=False)
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            path = base / "directory.json"
            if _is_newer_snapshot(path, payload):
                continue
            write_json(path, payload)
        except OSError:
            pass  # mirrors are best-effort


def _is_newer_snapshot(path: Path, payload: dict) -> bool:
    try:
        if not path.exists():
            return False
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(existing, dict):
        return False
    existing_rev = existing.get("revision")
    new_rev = payload.get("revision")
    return isinstance(existing_rev, int) and isinstance(new_rev, int) and existing_rev > new_rev


def read_snapshot(data_dir: Path, assets_dir: Path) -> dict:
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            path = base / "directory.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


# ---------------------------------------------------------------------------
# Save results: a small per-request store, not a single overwritten file
# ---------------------------------------------------------------------------


def read_save_results(data_dir: Path) -> list[dict]:
    path = Path(data_dir) / "snapshots" / "save_result.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    results = body.get("results") if isinstance(body, dict) else None
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def write_save_result(data_dir: Path, assets_dir: Path, payload: dict) -> None:
    """Upsert one save's result by ``requestId`` with retention cleanup."""
    payload = dict(payload)
    payload["writtenAt"] = _now_iso()
    request_id = payload.get("requestId")
    results = [
        r for r in read_save_results(Path(data_dir))
        if r.get("requestId") != request_id
    ]
    results.insert(0, payload)
    results = results[:_SAVE_RESULT_KEEP]
    cutoff = datetime.now(timezone.utc) - _SAVE_RESULT_MAX_AGE
    fresh = []
    for entry in results:
        try:
            written = datetime.fromisoformat(str(entry.get("writtenAt")))
        except (TypeError, ValueError):
            written = datetime.now(timezone.utc)
        if written >= cutoff:
            fresh.append(entry)
    body = json.dumps({"results": fresh}, ensure_ascii=False)
    for base in (Path(data_dir) / "snapshots", Path(assets_dir) / "snapshots"):
        try:
            path = base / "save_result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
            try:
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        except OSError:
            pass  # mirrors are best-effort


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _stale_entry(previous: dict, channel_id: str, status: str) -> dict:
    """The last known numbers for a channel, flagged as temporarily unavailable."""
    entry: dict[str, Any] = {"healthStatus": status}
    stale = (previous or {}).get("channels", {}).get(channel_id)
    if isinstance(stale, dict):
        for key in ("sceneCount", "loopSeconds", "loopCapped", "sourceMissing",
                    "sourceTotal", "rotationVersion"):
            if key in stale:
                entry[key] = stale[key]
    return entry


def _channel_health(client: Any, channel: dict, previous: dict) -> dict:
    """One channel's health, fully failure-isolated.

    Distinguishes a missing source (relink flow), a genuinely empty rotation
    (off air), and a temporarily unavailable check (stale numbers +
    ``healthStatus: unavailable``) — a transport blip or one broken channel
    must never fail the snapshot or its siblings. Every entry records the
    membership ``sourceSignature`` it was computed for, so the refresh
    journal can tell "current" from "retryable" without a recompute.
    """
    channel_id = channel.get("id") or ""
    source = channel.get("source") or {}
    from justwatch import criteria
    signature = criteria.source_signature(source)
    try:
        object_filter = None
        text_query = None
        if source.get("type") == "savedFilter":
            object_filter, text_query = lineup.resolve_saved_criteria(
                client, str(source.get("id", "")),
            )
        rotation = lineup.fetch_rotation(
            client,
            source=source,
            sort=channel.get("sort") or "shuffle",
            seed=int(channel.get("seed") or 0),
            include_paths=False,
            object_filter=object_filter,
            text_query=text_query,
        )
    except LookupError:
        return {
            "healthStatus": "missingSource",
            "sourceSignature": signature,
            "sourceMissing": True,
            "sceneCount": None,
            "loopSeconds": None,
            "loopCapped": False,
            "sourceTotal": None,
            "rotationVersion": None,
        }
    except Exception:
        return _stale_entry(previous, channel_id, "unavailable")

    items = rotation["items"]
    entry: dict[str, Any] = {
        "healthStatus": "ok",
        "sourceSignature": signature,
        "sceneCount": len(items),
        "loopSeconds": rotation["loopSeconds"],
        "loopCapped": not rotation["rotationComplete"],
        "sourceMissing": False,
        "sourceTotal": rotation["sourceTotal"],
        "rotationVersion": rotation["rotationVersion"],
    }
    if not items:
        # Empty rotation: WHERE the emptiness comes from decides the status.
        # Rule sources (filter/criteria) query Stash directly — a successful
        # zero-result query means the rules are valid and simply match
        # nothing, which is OFF AIR, never a "missing source" (audit E7:
        # resolve_source only understands linked shapes and would answer
        # false for a criteria pool). Linked sources (saved search / tag /
        # performer / studio) may genuinely have lost their target entity,
        # so the existence lookup still runs for them — and a FAILED lookup
        # is "unavailable" (stale, retryable), never "missing".
        if source.get("type") in ("filter", "criteria"):
            entry["healthStatus"] = "offAir"
            entry["sourceMissing"] = False
            return entry
        try:
            _, exists = lineup.resolve_source(client, source)
        except Exception:
            return _stale_entry(previous, channel_id, "unavailable")
        entry["healthStatus"] = "offAir" if exists else "missingSource"
        entry["sourceMissing"] = not exists
    return entry


def build_directory_snapshot(client: Any, catalog: dict, previous: dict | None = None) -> dict:
    """Recompute health for every enabled channel, one rotation each.

    Count and loop length come from the channel's active rotation — the same
    bounded, ordered loop the TV plays — so a 500-scene library with a 50-scene
    rotation reports the rotation, not the library. Each channel is computed
    inside its own failure boundary; ``previous`` keeps stale numbers alive
    (flagged unavailable) when a check fails outright.
    """
    previous = previous if isinstance(previous, dict) else {}
    channels: dict[str, Any] = {}
    for channel in catalog.get("channels", []):
        if not channel.get("enabled", True):
            continue
        entry = _channel_health(client, channel, previous)
        assert entry["healthStatus"] in _SNAPSHOT_STATUSES
        channels[channel.get("id") or ""] = entry
    return {
        "computedAt": _now_iso(),
        "revision": catalog.get("revision", 0),
        "rotation": {
            "size": contract.ROTATION_SIZE,
            "scanLimit": contract.ROTATION_SCAN_LIMIT,
        },
        "channels": channels,
    }
