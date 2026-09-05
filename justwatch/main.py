#!/usr/bin/env python3
"""Raw task entrypoint for the stash-justwatch plugin.

Stash spawns this process for every operation, sending the raw-plugin envelope
on stdin and reading a single JSON object from stdout. ALL diagnostics go to
stderr so stdout stays parseable.

Raw contract (stash-tag-curator precedent):

* stdout: exactly ONE JSON object (``{"output": ...}`` or ``{"error": ...}``)
* stderr: every diagnostic line / traceback
* progress frames: ``\\x01p\\02<float>\\n`` on stderr
* exit 0 on success (including handled failures), 1 on hard errors

Modes are addressed via ``args["mode"]`` (CamelCase manifest tokens are
normalised to snake_case). Sync ops (``runPluginOperation``) answer
capabilities/directory/lineup/previewLineup/getCatalog/validateCatalog;
the editor's writes (saveCatalog/refreshData) run as tasks so snapshot
regeneration never blocks a GraphQL connection.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from justwatch import autodir, catalog, contract, lineup, snapshots  # noqa: E402
from justwatch.stash_client import (  # noqa: E402
    GraphQLAuthError,
    GraphQLClientError,
    StashClient,
)

PLUGIN_VERSION = "0.1.0"

SYNC_MODES = frozenset({
    "capabilities", "directory", "full_directory", "lineup", "preview_lineup",
    "get_catalog", "validate_catalog",
})
TASK_MODES = frozenset({"save_catalog", "refresh_data"})
ALL_MODES = SYNC_MODES | TASK_MODES

_CAMEL_RE_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _log(message: str) -> None:
    sys.stderr.write(f"justwatch: {message}\n")
    sys.stderr.flush()


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_mode(value: str) -> str:
    s = _CAMEL_RE_1.sub(r"\1_\2", value)
    s = _CAMEL_RE_2.sub(r"\1_\2", s)
    return s.lower().replace("-", "_")


class TaskContext:
    """Resolved runtime context for one invocation."""

    def __init__(self, server_connection: dict, settings: dict, args: dict) -> None:
        self.settings = settings
        self.args = args
        stash_dir = str(server_connection.get("Dir") or "").strip()
        plugin_dir = str(server_connection.get("PluginDir") or "").strip() or _PLUGIN_ROOT
        self.data_dir = (
            Path(stash_dir) / catalog.DATA_DIR_NAME if stash_dir
            else Path(plugin_dir) / catalog.DATA_DIR_NAME
        )
        self.assets_dir = Path(plugin_dir) / "assets"
        self.client = StashClient(server_connection, settings)


# ---------------------------------------------------------------------------
# Op handlers
# ---------------------------------------------------------------------------


def _op_capabilities(ctx: TaskContext) -> dict:
    return contract.capabilities(PLUGIN_VERSION)


def _op_get_catalog(ctx: TaskContext) -> dict:
    current = catalog.load(ctx.data_dir)
    snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
    health = snapshot.get("channels", {}) if snapshot.get("revision") == current.get("revision") else {}
    channels = []
    for channel in current.get("channels", []):
        enriched = dict(channel)
        health_entry = health.get(channel.get("id") or "", {})
        enriched["sceneCount"] = health_entry.get("sceneCount")
        enriched["loopSeconds"] = health_entry.get("loopSeconds")
        enriched["loopCapped"] = health_entry.get("loopCapped", False)
        enriched["sourceMissing"] = health_entry.get("sourceMissing", False)
        channels.append(enriched)
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        # Round-trippable: the editor sends this shape back to SaveCatalog.
        "schemaVersion": contract.SCHEMA_VERSION,
        "revision": current.get("revision", 0),
        "settings": current.get("settings", {}),
        "channels": channels,
    }


def _op_directory(ctx: TaskContext) -> dict:
    """The TV app's view: enabled channels only, no catalog internals."""
    current = catalog.load(ctx.data_dir)
    snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
    health = snapshot.get("channels", {}) if snapshot.get("revision") == current.get("revision") else {}
    channels = []
    for channel in current.get("channels", []):
        if not channel.get("enabled", True):
            continue
        channels.append({
            "id": channel["id"],
            "number": channel["number"],
            "name": channel["name"],
            "glyph": channel["glyph"],
            "color": channel["color"],
            "sort": channel["sort"],
            "seed": channel["seed"],
            "sourceType": (channel.get("source") or {}).get("type"),
            "sceneCount": health.get(channel.get("id") or "", {}).get("sceneCount"),
        })
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "revision": current.get("revision", 0),
        "channels": channels,
    }


def _find_channel(current: dict, channel_id: str) -> dict | None:
    for channel in current.get("channels", []):
        if channel.get("id") == channel_id:
            return channel
    return None


def _op_lineup(ctx: TaskContext) -> dict:
    channel_id = str(ctx.args.get("channelId") or "").strip()
    page = max(1, _as_int(ctx.args.get("page"), 1))
    per_page = min(100, max(1, _as_int(ctx.args.get("perPage"), 50)))
    if not channel_id:
        raise ValueError("channelId is required")
    current = catalog.load(ctx.data_dir)
    channel = _find_channel(current, channel_id)
    if channel is None or not channel.get("enabled", True):
        raise LookupError(f"no enabled channel {channel_id!r}")
    result = _lineup_for_channel(ctx, channel, page, per_page, include_paths=False)
    result.update({
        "channelId": channel_id,
        "revision": current.get("revision", 0),
        "sort": channel.get("sort"),
    })
    return result


def _lineup_for_channel(
    ctx: TaskContext, channel: dict, page: int, per_page: int, *, include_paths: bool,
) -> dict:
    source = channel.get("source") or {}
    object_filter = None
    if source.get("type") == "savedFilter":
        object_filter = lineup.fetch_saved_object_filter(ctx.client, str(source.get("id", "")))
        if object_filter is None:
            raise LookupError("channel source (saved filter) is missing")
    result = lineup.fetch_lineup(
        ctx.client,
        source=source,
        sort=channel.get("sort") or "shuffle",
        seed=int(channel.get("seed") or 0),
        page=page,
        per_page=per_page,
        include_paths=include_paths,
        object_filter=object_filter,
    )
    result.update({"page": page, "perPage": per_page})
    return result


def _op_preview_lineup(ctx: TaskContext) -> dict:
    """Lineup for an UNSAVED draft channel (the editor's On Air strip)."""
    raw = ctx.args.get("channel")
    try:
        draft = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError(f"channel draft is not valid JSON: {exc}") from exc
    if not isinstance(draft, dict):
        raise ValueError("channel draft must be a JSON object")
    normalized = catalog.normalize({"channels": [draft]})["channels"]
    if not normalized or not (normalized[0].get("source") or {}).get("type"):
        raise ValueError("channel draft needs a valid source")
    channel = normalized[0]
    page = max(1, _as_int(ctx.args.get("page"), 1))
    per_page = min(100, max(1, _as_int(ctx.args.get("perPage"), 12)))
    return _lineup_for_channel(ctx, channel, page, per_page, include_paths=True)


def _parse_catalog_arg(ctx: TaskContext) -> dict:
    raw = ctx.args.get("catalog")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"catalog draft is not valid JSON: {exc}") from exc
    if isinstance(raw, dict):
        return raw
    raise ValueError("catalog draft is required")


def _op_validate_catalog(ctx: TaskContext) -> dict:
    """Validate a draft (and resolve its sources) without writing anything."""
    draft = _parse_catalog_arg(ctx)
    errors, normalized = catalog.validate(draft)
    for i, channel in enumerate(normalized.get("channels", [])):
        label, exists = lineup.resolve_source(ctx.client, channel.get("source") or {})
        if not exists:
            errors.append({
                "path": f"channels[{i}].source",
                "code": "missing_source",
                "message": f"source not found in Stash (looking for {label or 'it'})",
            })
    return {"valid": not errors, "errors": errors}


def _op_save_catalog(ctx: TaskContext) -> dict:
    """Validate + persist a draft, then refresh the health snapshot.

    Optimistic concurrency: ``expectedRevision`` must match the stored
    revision. The result is mirrored to ``save_result.json`` because Stash's
    job object does not relay plugin stdout to the editor.
    """
    request_id = str(ctx.args.get("requestId") or "").strip()
    raw_draft = ctx.args.get("catalog")

    def mirror(payload: dict) -> None:
        snapshots.write_save_result(
            ctx.data_dir, ctx.assets_dir, {**payload, "requestId": request_id},
        )

    if raw_draft is None or (isinstance(raw_draft, str) and not raw_draft.strip()):
        result = {"saved": False, "message": "no draft to save (Tasks-page runs are a no-op)"}
        mirror(result)
        return result

    draft = _parse_catalog_arg(ctx)
    errors, normalized = catalog.validate(draft)
    if errors:
        result = {"saved": False, "errors": errors, "error": "validation_failed"}
        mirror(result)
        return result

    current = catalog.load(ctx.data_dir)
    expected = _as_int(ctx.args.get("expectedRevision"), -1)
    stored_revision = current.get("revision", 0)
    if expected != stored_revision:
        result = {
            "saved": False,
            "error": "revision_conflict",
            "message": f"draft expects revision {expected}, server has {stored_revision}",
            "currentRevision": stored_revision,
        }
        mirror(result)
        return result

    # Resolve source labels + existence; a save must reference real sources.
    for i, channel in enumerate(normalized.get("channels", [])):
        source = channel.get("source") or {}
        try:
            if (source.get("type") or "") == "savedFilter":
                object_filter = lineup.fetch_saved_object_filter(
                    ctx.client, str(source.get("id", "")),
                )
                if object_filter is None:
                    errors.append({
                        "path": f"channels[{i}].source",
                        "code": "missing_source",
                        "message": "saved filter not found (or not a scene filter)",
                    })
                    continue
            label, exists = lineup.resolve_source(ctx.client, source)
        except GraphQLClientError as exc:
            # A transport blip must NOT mark sources missing; fail the save.
            result = {"saved": False, "error": "source_check_failed", "message": str(exc)}
            mirror(result)
            return result
        if not exists:
            errors.append({
                "path": f"channels[{i}].source",
                "code": "missing_source",
                "message": "source not found in Stash",
            })
        channel["sourceLabel"] = label
    if errors:
        result = {"saved": False, "errors": errors, "error": "validation_failed"}
        mirror(result)
        return result

    normalized["revision"] = stored_revision + 1
    catalog.save(ctx.data_dir, normalized)
    _log(f"saved catalog revision {normalized['revision']} "
         f"({len(normalized['channels'])} channels)")

    try:
        snapshot = snapshots.build_directory_snapshot(ctx.client, normalized)
        snapshots.write_snapshot(ctx.data_dir, ctx.assets_dir, snapshot)
    except Exception as exc:  # save already succeeded; health is best-effort
        _log(f"snapshot regeneration skipped: {exc}")

    result = {"saved": True, "revision": normalized["revision"]}
    mirror(result)
    return result


def _op_refresh_data(ctx: TaskContext) -> dict:
    current = catalog.load(ctx.data_dir)
    snapshot = snapshots.build_directory_snapshot(ctx.client, current)
    snapshots.write_snapshot(ctx.data_dir, ctx.assets_dir, snapshot)
    return {
        "mode": "refresh_data",
        "revision": current.get("revision", 0),
        "channels": len(snapshot.get("channels", {})),
        "computedAt": snapshot.get("computedAt"),
    }


def _op_full_directory(ctx: TaskContext) -> dict:
    """The complete lineup for the Channel Studio: custom channels PLUS the
    auto-generated General (tags) / Studios / Performers sections, computed
    with the same tiering the TV app uses. The curated 101–160 dial lives in
    the app itself and is represented here only by a note.
    """
    current = catalog.load(ctx.data_dir)
    sections = autodir.build_full_directory(ctx.client.submit, current.get("settings") or {})
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "revision": current.get("revision", 0),
        "custom": _op_directory(ctx)["channels"],
        **sections,
        "curatedDialNote": "Channels 101–160 are the built-in Just Watch networks curated in the TV app.",
    }


_HANDLERS = {
    "capabilities": _op_capabilities,
    "get_catalog": _op_get_catalog,
    "directory": _op_directory,
    "full_directory": _op_full_directory,
    "lineup": _op_lineup,
    "preview_lineup": _op_preview_lineup,
    "validate_catalog": _op_validate_catalog,
    "save_catalog": _op_save_catalog,
    "refresh_data": _op_refresh_data,
}


def _dispatch(envelope: dict, *, client: Any = None) -> dict:
    raw_args = envelope.get("args") or {}
    if not isinstance(raw_args, dict):
        raise ValueError("args envelope must be a mapping")
    args = dict(raw_args)
    server_connection = envelope.get("server_connection") or {}
    if not isinstance(server_connection, dict):
        raise ValueError("server_connection must be a mapping")
    settings = envelope.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}

    raw_mode = str(args.get("mode") or args.get("task") or "").strip()
    if not raw_mode:
        raise ValueError("no 'mode' (or 'task') key in args")
    mode = _normalize_mode(raw_mode)
    if mode not in ALL_MODES:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of: {', '.join(sorted(ALL_MODES))}"
        )
    _log(f"mode={mode}")

    ctx = TaskContext(server_connection, settings, args)
    if client is not None:  # test injection point
        ctx.client = client
    return _HANDLERS[mode](ctx)


def _emit_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.read()
        envelope = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"justwatch: invalid stdin JSON: {exc}\n")
        _emit_json({"error": f"invalid stdin JSON: {exc}"})
        return 1
    if not isinstance(envelope, dict):
        _emit_json({"error": "stdin must be a JSON object"})
        return 1
    try:
        result = _dispatch(envelope)
        _emit_json({"output": result})
        return 0
    except GraphQLAuthError as exc:
        sys.stderr.write(f"justwatch: auth error: {exc}\n")
        _emit_json({"error": f"authentication failed: {exc}"})
        return 1
    except (GraphQLClientError, LookupError, ValueError) as exc:
        sys.stderr.write(f"justwatch: error: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        _emit_json({"error": str(exc)})
        return 1
    except Exception as exc:
        sys.stderr.write(f"justwatch: error: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        _emit_json({"error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
