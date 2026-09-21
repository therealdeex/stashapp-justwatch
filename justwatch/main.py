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

from justwatch import catalog, contract, continuing, lineup, networks, snapshots, programming  # noqa: E402
from justwatch.stash_client import (  # noqa: E402
    GraphQLAuthError,
    GraphQLClientError,
    StashClient,
)

PLUGIN_VERSION = "0.7.0"

SYNC_MODES = frozenset({
    "capabilities", "directory", "full_directory", "lineup", "preview_lineup",
    "get_catalog", "validate_catalog", "schedule", "programming_desk", "preview_programming",
    "programming_status",
})
TASK_MODES = frozenset({"save_catalog", "refresh_data", "prepare_programming"})
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
        try:
            published = programming.read(ctx.data_dir, channel["id"])
        except (OSError, ValueError):
            published = None
        enriched["programmingVersion"] = published.get("version") if published else None
        health_entry = health.get(channel.get("id") or "", {})
        enriched["sceneCount"] = health_entry.get("sceneCount")
        enriched["loopSeconds"] = health_entry.get("loopSeconds")
        enriched["loopCapped"] = health_entry.get("loopCapped", False)
        enriched["sourceMissing"] = health_entry.get("sourceMissing", False)
        enriched["healthStatus"] = health_entry.get("healthStatus", "ok")
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
    """The TV app's view: enabled channels only, no catalog internals.

    The ``networks`` block carries the owner's curated tier (channels 100+)
    which replaces the TV's client-generated sections; it is absent entirely
    when this deployment has no networks file, so older clients keep their
    fallback path. Continuing-activated networks overlay their resolved mode
    and a lightweight schedule status (from the status manifest — reading
    every schedule JSON per directory request would be unbounded).
    """
    current = catalog.load(ctx.data_dir)
    snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
    health = snapshot.get("channels", {}) if snapshot.get("revision") == current.get("revision") else {}
    schedule_status = continuing.read_status(ctx.data_dir).get("channels", {})
    channels = []
    for channel in current["channels"]:
        if not channel.get("enabled", True):
            continue
        mode = programming.policy(channel.get("programming"))["mode"]
        row = {
            "id": channel["id"],
            "number": channel["number"],
            "name": channel["name"],
            "glyph": channel["glyph"],
            "color": channel["color"],
            "sort": channel["sort"],
            "seed": channel["seed"],
            "sourceType": (channel.get("source") or {}).get("type"),
            "programmingMode": mode,
            "sceneCount": health.get(channel.get("id") or "", {}).get("sceneCount"),
        }
        entry = schedule_status.get(channel["id"])
        if entry and entry.get("ready"):
            row["programming"] = {
                key: entry[key]
                for key in ("version", "generatedAt", "preparedThrough", "coverageHours", "degraded", "expiring")
                if key in entry
            }
        channels.append(row)
    rollout = continuing.try_load_rollout(ctx.data_dir)
    resolved = {}
    if rollout["enabled"]:
        for network in networks.load()["channels"]:
            resolved[network["id"]] = continuing.resolved_mode(network, rollout)
    result = {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "revision": current.get("revision", 0),
        "channels": channels,
        "networks": networks.directory_payload(
            resolved_modes=resolved or None,
            schedule_status=schedule_status or None,
        ),
    }
    if result["networks"] is None:
        del result["networks"]
    return result


def _find_channel(current: dict, channel_id: str) -> dict | None:
    for channel in current.get("channels", []):
        if channel.get("id") == channel_id:
            return channel
    return None


def _op_lineup(ctx: TaskContext) -> dict:
    channel_id = str(ctx.args.get("channelId") or "").strip()
    per_page = min(100, max(1, _as_int(ctx.args.get("perPage"), contract.ROTATION_SIZE)))
    if not channel_id:
        raise ValueError("channelId is required")
    network = networks.get(channel_id)
    if network is not None:
        return _network_lineup(ctx, network, per_page)
    current = catalog.load(ctx.data_dir)
    channel = _find_channel(current, channel_id)
    if channel is None or not channel.get("enabled", True):
        raise LookupError(f"no enabled channel {channel_id!r}")
    result = _lineup_for_channel(ctx, channel, per_page, include_paths=False)
    revision = current.get("revision", 0)
    result.update({
        "channelId": channel_id,
        "revision": revision,
        "sort": channel.get("sort"),
        # Rotation identity: ordering hash + the revision that published it.
        # Clients compare this to drop stale cached lineups.
        "rotationVersion": f"r{revision}-{result['rotationVersion']}",
    })
    return result


def _network_lineup(ctx: TaskContext, network: dict, per_page: int) -> dict:
    """A network channel's rotation: the same bounded-rotation machinery as
    custom channels (one rotation policy everywhere), keyed by the network's
    own content revision instead of the catalog's."""
    result = _lineup_for_channel(ctx, network, per_page, include_paths=False)
    revision = networks.revision([network])
    result.update({
        "channelId": network["id"],
        "revision": revision,
        "sort": network.get("sort"),
        "rotationVersion": f"n{revision}-{result['rotationVersion']}",
    })
    return result


def _lineup_for_channel(
    ctx: TaskContext, channel: dict, max_items: int, *, include_paths: bool,
) -> dict:
    """The channel's active rotation (bounded, ordered, playable). ``max_items``
    caps the entries returned; the rotation itself is always assembled whole so
    every client sees the same loop."""
    source = channel.get("source") or {}
    object_filter = None
    text_query = None
    if source.get("type") == "savedFilter":
        try:
            object_filter, text_query = lineup.resolve_saved_criteria(
                ctx.client, str(source.get("id", "")),
            )
        except LookupError as exc:
            raise LookupError(f"channel source (saved filter) is missing: {exc}") from exc
    rotation = lineup.fetch_rotation(
        ctx.client,
        source=source,
        sort=channel.get("sort") or "shuffle",
        seed=int(channel.get("seed") or 0),
        include_paths=include_paths,
        object_filter=object_filter,
        text_query=text_query,
    )
    # `total` quotes the whole rotation even when the caller takes a prefix
    # (the editor's preview asks for a thumb-strip slice of it).
    rotation["total"] = len(rotation["items"])
    rotation["items"] = rotation["items"][:max_items]
    rotation["perPage"] = max_items
    return rotation


def _op_preview_lineup(ctx: TaskContext) -> dict:
    """Rotation preview for an UNSAVED draft channel (the editor's On Air strip)."""
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
    per_page = min(100, max(1, _as_int(ctx.args.get("perPage"), 12)))
    return _lineup_for_channel(ctx, channel, per_page, include_paths=True)


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
    revision, re-checked inside the catalog lock so concurrent processes
    cannot both pass the check. Every outcome — success, handled rejection,
    or internal failure — is mirrored per ``requestId`` to the save_result
    store, so the editor can always correlate an outcome with its own save.
    """
    request_id = str(ctx.args.get("requestId") or "").strip()

    def mirror(payload: dict) -> None:
        snapshots.write_save_result(
            ctx.data_dir, ctx.assets_dir, {**payload, "requestId": request_id},
        )

    try:
        result = _save_catalog_inner(ctx)
    except catalog.CatalogError as exc:
        # A corrupt stored catalog must never be overwritten by a save.
        result = {
            "saved": False,
            "error": "catalog_corrupt",
            "message": str(exc),
        }
    except Exception as exc:  # every handled failure publishes its result
        _log(f"save failed: {exc}")
        result = {"saved": False, "error": "internal_error", "message": str(exc)}
    mirror(result)
    return result


def _save_catalog_inner(ctx: TaskContext) -> dict:
    if ctx.args.get("catalog") is None or (
        isinstance(ctx.args.get("catalog"), str) and not ctx.args["catalog"].strip()
    ):
        return {"saved": False, "message": "no draft to save (Tasks-page runs are a no-op)"}

    draft = _parse_catalog_arg(ctx)
    errors, normalized = catalog.validate(draft)
    if errors:
        return {"saved": False, "errors": errors, "error": "validation_failed"}

    # Resolve source labels + existence; a save must reference real sources.
    # Pure Stash queries against the draft — safe outside the lock.
    for i, channel in enumerate(normalized.get("channels", [])):
        source = channel.get("source") or {}
        try:
            if (source.get("type") or "") == "savedFilter":
                try:
                    lineup.resolve_saved_criteria(ctx.client, str(source.get("id", "")))
                except LookupError:
                    errors.append({
                        "path": f"channels[{i}].source",
                        "code": "missing_source",
                        "message": "saved filter not found (or not a scene filter)",
                    })
                    continue
            label, exists = lineup.resolve_source(ctx.client, source)
        except GraphQLClientError as exc:
            # A transport blip must NOT mark sources missing; fail the save.
            return {"saved": False, "error": "source_check_failed", "message": str(exc)}
        if not exists:
            errors.append({
                "path": f"channels[{i}].source",
                "code": "missing_source",
                "message": "source not found in Stash",
            })
        channel["sourceLabel"] = label
    if errors:
        return {"saved": False, "errors": errors, "error": "validation_failed"}

    with catalog.catalog_lock(ctx.data_dir):
        current = catalog.load(ctx.data_dir)
        expected = _as_int(ctx.args.get("expectedRevision"), -1)
        stored_revision = current.get("revision", 0)
        if expected != stored_revision:
            return {
                "saved": False,
                "error": "revision_conflict",
                "message": f"draft expects revision {expected}, server has {stored_revision}",
                "currentRevision": stored_revision,
            }
        _preserve_existing_seeds(current, normalized)
        normalized["revision"] = stored_revision + 1
        catalog.save(ctx.data_dir, normalized)

    _log(f"saved catalog revision {normalized['revision']} "
         f"({len(normalized['channels'])} channels)")

    # Health regenerates outside the lock; a stale computation is dropped by
    # the revision-aware snapshot publication.
    try:
        previous = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
        snapshot = snapshots.build_directory_snapshot(ctx.client, normalized, previous)
        snapshots.write_snapshot(ctx.data_dir, ctx.assets_dir, snapshot)
    except Exception as exc:  # save already succeeded; health is best-effort
        _log(f"snapshot regeneration skipped: {exc}")

    try:
        result = programming.prepare(ctx.client, ctx.data_dir, only_changed=True)
        continuing.write_status(ctx.data_dir, {
            "startedAt": int(__import__("time").time() * 1000),
            "customChannels": result["channels"],
            "networkChannels": {},
        })
    except Exception as exc:  # save already succeeded; programming is best-effort
        _log(f"programming will retry on the next preparation task: {exc}")
    return {"saved": True, "revision": normalized["revision"]}


def _preserve_existing_seeds(stored: dict, draft: dict) -> None:
    """Channel seeds are identity: an existing channel keeps the seed it was
    first saved with, whatever the draft carries (changed, missing, or fresh).
    Only genuinely new channels get their draft's seed."""
    stored_seeds = {
        ch.get("id"): ch.get("seed")
        for ch in stored.get("channels", [])
        if isinstance(ch, dict) and ch.get("id")
    }
    for channel in draft.get("channels", []):
        prior = stored_seeds.get(channel.get("id"))
        if prior is not None:
            channel["seed"] = prior


def _op_refresh_data(ctx: TaskContext) -> dict:
    current = catalog.load(ctx.data_dir)
    previous = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
    snapshot = snapshots.build_directory_snapshot(ctx.client, current, previous)
    snapshots.write_snapshot(ctx.data_dir, ctx.assets_dir, snapshot)
    return {
        "mode": "refresh_data",
        "revision": current.get("revision", 0),
        "channels": len(snapshot.get("channels", {})),
        "computedAt": snapshot.get("computedAt"),
    }


def _op_full_directory(ctx: TaskContext) -> dict:
    """The complete lineup for the Channel Studio: custom channels PLUS the
    owner's curated networks, which replaced the General/Studios/Performers
    tiering the TV app used to generate client-side. Pure file reads — no
    Stash queries; network health is the CSV-validated count. Continuing
    networks carry their lightweight schedule status so the editor can say
    "advancing, Xh ahead" instead of loop language."""
    current = catalog.load(ctx.data_dir)
    grouped = networks.sections()
    schedule_status = continuing.read_status(ctx.data_dir).get("channels", {})
    rollout = continuing.try_load_rollout(ctx.data_dir)
    resolved = {}
    if rollout["enabled"]:
        for network in networks.load()["channels"]:
            resolved[network["id"]] = continuing.resolved_mode(network, rollout)

    def rows(section: str) -> list[dict]:
        out = []
        for ch in grouped[section]:
            row = {
                "id": ch["id"],
                "number": ch["number"],
                "name": ch["name"],
                "glyph": ch["glyph"],
                "color": ch["color"],
                "kind": ch["family"],
                "count": ch["count"],
                "offAir": ch["count"] <= 0,
                "origin": "network",
                "sourceLabel": ch["sourceLabel"],
            }
            mode = resolved.get(ch["id"])
            if mode:
                row["programmingMode"] = mode
            entry = schedule_status.get(ch["id"])
            if entry and entry.get("ready"):
                row["schedule"] = {
                    key: entry[key]
                    for key in ("coverageHours", "degraded", "expiring", "generatedAt")
                    if key in entry
                }
            out.append(row)
        return out

    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "revision": current.get("revision", 0),
        "custom": _op_directory(ctx)["channels"],
        "general": {"tagChannels": rows("general")},
        "studios": {"channels": rows("studios")},
        "performers": {"channels": rows("performers")},
        "networksNote": "Channels 100+ are the owner's curated networks (networks.json).",
    }


def _op_schedule(ctx):
    channel_id = str(ctx.args.get("channelId") or "")
    if channel_id.startswith("net_"):
        network = networks.get(channel_id)
        if network is None:
            raise LookupError("channel is unavailable")
        mode = continuing.resolved_mode(network, continuing.try_load_rollout(ctx.data_dir))
        if mode != continuing.MODE:
            return {"status": "fixed", "programs": []}
        return programming.schedule(ctx.data_dir, channel_id,
            _as_int(ctx.args.get("at"), int(__import__("time").time() * 1000)),
            _as_int(ctx.args.get("limit"), 50))
    channel = _find_channel(catalog.load(ctx.data_dir), channel_id)
    if channel is None or not channel["enabled"]:
        raise LookupError("channel is unavailable")
    if programming.policy(channel.get("programming"))["mode"] == "fixed":
        return {"status": "fixed", "programs": []}
    return programming.schedule(ctx.data_dir, channel_id,
        _as_int(ctx.args.get("at"), int(__import__("time").time() * 1000)),
        _as_int(ctx.args.get("limit"), 50))


def _op_preview_programming(ctx):
    raw = ctx.args.get("channel")
    draft = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(draft, dict):
        raise ValueError("channel draft is required")
    normalized = catalog.normalize({"channels": [draft]})["channels"][0]
    return programming.preview(ctx.data_dir, normalized)


def _op_programming_desk(ctx):
    result = programming.desk(ctx.data_dir, catalog.load(ctx.data_dir)["channels"])
    # Network diagnostics are bounded and carry NO pairwise overlap (all-pairs
    # over the tier is offline work, never a synchronous computation).
    result["networks"] = continuing.desk(
        ctx.data_dir,
        offset=_as_int(ctx.args.get("networkOffset"), 0),
        limit=_as_int(ctx.args.get("networkLimit"), 50),
    )
    result["status"] = {
        "generatedAt": continuing.read_status(ctx.data_dir).get("generatedAt"),
        "lastRun": continuing.read_status(ctx.data_dir).get("lastRun"),
    }
    return result


def _op_programming_status(ctx):
    """The lightweight scheduling surface: last run + per-channel publication
    status. This is what distinguishes a queued task from a generation that
    actually succeeded — ops tooling polls it instead of trusting a job id."""
    return continuing.read_status(ctx.data_dir) or {"schema": 1, "channels": {}, "lastRun": None}


def _op_prepare_programming(ctx):
    result = programming.prepare(ctx.client, ctx.data_dir, ctx.args.get("channelId"))
    network_result = {"channels": {}}
    try:
        network_result = continuing.prepare(ctx.client, ctx.data_dir, ctx.args.get("channelId"))
    except ValueError as exc:  # rollout file broken: loud, per-run, but customs still report
        network_result = {"channels": {"rollout": str(exc)}}
    run = {
        "startedAt": int(__import__("time").time() * 1000),
        "customChannels": result["channels"],
        "networkChannels": network_result["channels"],
    }
    continuing.write_status(ctx.data_dir, run)
    errors = {key: value for key, value in result["channels"].items() if value != "ready"}
    errors.update({key: value for key, value in network_result["channels"].items() if value != "ready"})
    if errors:
        raise GraphQLClientError("Some programming could not be prepared: " + json.dumps(errors))
    return {"channels": {**result["channels"], **network_result["channels"]},
            "networks": network_result["channels"]}


_HANDLERS = {
    "schedule": _op_schedule,
    "preview_programming": _op_preview_programming,
    "programming_desk": _op_programming_desk,
    "programming_status": _op_programming_status,
    "prepare_programming": _op_prepare_programming,
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
