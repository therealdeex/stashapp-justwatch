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
import time
import traceback
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from justwatch import catalog, contract, continuing, lineup, networks, snapshots, programming  # noqa: E402
from justwatch import channel_ops, channel_service, library, refresh  # noqa: E402
from justwatch.stash_client import (  # noqa: E402
    GraphQLAuthError,
    GraphQLClientError,
    StashClient,
)

PLUGIN_VERSION = "0.8.0"

SYNC_MODES = frozenset({
    "capabilities", "directory", "full_directory", "lineup", "preview_lineup",
    "get_catalog", "validate_catalog", "schedule", "programming_desk", "preview_programming",
    "programming_status",
    "get_channel_library", "get_channel_directory", "get_channel_definition",
    "validate_channel_changes", "preview_channel_pool", "get_channel_apply_result",
    "get_channel_history",
})
TASK_MODES = frozenset({
    "save_catalog", "refresh_data", "prepare_programming", "apply_channel_changes",
})
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
    """The legacy editor's read view (custom channels only). After migration
    this is a read-only compatibility projection of the library's ``ch_``
    namespace — editing goes through the channel-library operations."""
    if channel_service.library_active(ctx.data_dir):
        view = channel_service.load_view(ctx.data_dir)
        snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
        health = snapshot.get("channels", {}) if snapshot.get("revision") == view["revision"] else {}
        channels = []
        for channel in view["channels"]:
            if channel["kind"] != "ch":
                continue
            enriched = channel_service.as_legacy_catalog_channel(channel)
            enriched["groupId"] = channel["groupId"]
            enriched["archived"] = channel.get("archived", False)
            enriched["paused"] = channel.get("paused", False)
            try:
                published = programming.read(ctx.data_dir, channel["id"])
            except (OSError, ValueError):
                published = None
            enriched["programmingVersion"] = published.get("version") if published else None
            entry = health.get(channel["id"], {})
            enriched["sceneCount"] = entry.get("sceneCount")
            enriched["loopSeconds"] = entry.get("loopSeconds")
            enriched["loopCapped"] = entry.get("loopCapped", False)
            enriched["sourceMissing"] = entry.get("sourceMissing", False)
            enriched["healthStatus"] = entry.get("healthStatus", "ok")
            channels.append(enriched)
        return {
            "pluginId": contract.PLUGIN_ID,
            "contractVersion": contract.CONTRACT_VERSION,
            "schemaVersion": contract.SCHEMA_VERSION,
            "revision": view["revision"],
            "libraryBacked": True,
            "settings": catalog.load(ctx.data_dir).get("settings", {}),
            "channels": channels,
            "note": "This deployment is library-backed: apply edits through "
                    "ApplyChannelChanges. SaveCatalog maps onto it for customs only.",
        }
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

    After the library migration the payload is projected from the ONE
    authoritative library through the resolved channel service (same shapes:
    legacy ``channels`` plus the ``networks`` block with legal legacy section
    names — an owner group never leaks into ``section``). Pre-migration
    deployments read the legacy stores exactly as before.
    """
    schedule_status = continuing.read_status(ctx.data_dir).get("channels", {})
    if channel_service.library_active(ctx.data_dir):
        view = channel_service.load_view(ctx.data_dir)
        snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
        health = snapshot.get("channels", {}) if snapshot.get("revision") == view["revision"] else {}
        channels = []
        for channel in view["channels"]:
            if channel["kind"] != "ch" or not channel.get("enabled", True) \
                    or channel.get("archived") or channel.get("paused"):
                continue
            row = {
                "id": channel["id"],
                "number": channel["number"],
                "name": channel["name"],
                "glyph": channel.get("glyph"),
                "color": channel.get("color", "#455A64"),
                "sort": channel.get("sort", "shuffle"),
                "seed": channel["seed"],
                "sourceType": (channel.get("source") or {}).get("type"),
                "programmingMode": (channel.get("programming") or {}).get("mode", "fixed"),
                "sceneCount": health.get(channel["id"], {}).get("sceneCount"),
            }
            entry = schedule_status.get(channel["id"])
            if entry and entry.get("ready"):
                row["programming"] = {
                    key: entry[key]
                    for key in ("version", "generatedAt", "preparedThrough", "coverageHours", "degraded", "expiring")
                    if key in entry
                }
            channels.append(row)
        result = {
            "pluginId": contract.PLUGIN_ID,
            "contractVersion": contract.CONTRACT_VERSION,
            "revision": view["revision"],
            "channels": channels,
            "networks": _library_networks_block(ctx, view, schedule_status),
        }
        if result["networks"] is None:
            del result["networks"]
        return result

    current = catalog.load(ctx.data_dir)
    snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
    health = snapshot.get("channels", {}) if snapshot.get("revision") == current.get("revision") else {}
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
        for network in continuing.network_rows(ctx.data_dir):
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


def _library_networks_block(ctx, view: dict, schedule_status: dict) -> dict | None:
    """The legacy ``networks`` block projected from the library: legal legacy
    section names via the group fallback, historical seed counts, resolved
    continuing modes. Absent when the library has no ``net_`` channels — an
    intentionally empty tier must NOT resurrect the compiled artifact."""
    rollout = continuing.try_load_rollout(ctx.data_dir)
    resolved = {}
    if rollout["enabled"]:
        for channel in view["channels"]:
            if channel["kind"] == "net":
                resolved[channel["id"]] = continuing.resolved_mode(
                    channel_service.as_legacy_network_channel(channel), rollout)
    rows = [channel_service.as_legacy_network_channel(c)
            for c in view["channels"] if c["kind"] == "net"]
    if not rows:
        # An intentionally empty owner tier is AUTHORITATIVE: the block stays
        # present but empty so an old client never resurrects its generated
        # channels (an ABSENT block is the pre-networks "keep your fallback"
        # signal, which a library deployment must never emit).
        return {"revision": f"lib-{view['revision']}", "channels": []}
    channels = []
    for row in rows:
        if row["id"] in resolved:
            row["programmingMode"] = resolved[row["id"]]
        entry = schedule_status.get(row["id"])
        if entry and entry.get("ready"):
            row["schedule"] = {
                key: entry[key]
                for key in ("version", "generatedAt", "preparedThrough", "coverageHours", "degraded", "expiring")
                if key in entry
            }
        channels.append(row)
    revision = networks.revision(channels)
    return {"revision": revision, "channels": channels}


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
    if channel_service.library_active(ctx.data_dir):
        channel = channel_service.get_channel(ctx.data_dir, channel_id)
        if channel is None or not channel.get("enabled", True) \
                or channel.get("archived") or channel.get("paused"):
            raise LookupError(f"no enabled channel {channel_id!r}")
        result = _lineup_for_channel(ctx, channel, per_page, include_paths=False)
        revision = channel_service.directory_revision(ctx.data_dir)
        result.update({
            "channelId": channel_id,
            "revision": revision,
            "sort": channel.get("sort"),
            # Rotation identity: ordering hash + the revision that published
            # it. Clients compare this to drop stale cached lineups.
            "rotationVersion": f"r{revision}-{result['rotationVersion']}",
        })
        return result
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

    On a library-backed deployment the draft maps onto ONE Apply transaction
    touching only the ``ch_`` namespace: networks, groups and other channels'
    drafts are never published by a legacy save, and a source shape the old
    editor cannot represent is reported per-channel instead of truncated.
    """
    request_id = str(ctx.args.get("requestId") or "").strip()

    def mirror(payload: dict) -> None:
        snapshots.write_save_result(
            ctx.data_dir, ctx.assets_dir, {**payload, "requestId": request_id},
        )

    try:
        if channel_service.library_active(ctx.data_dir):
            result = _save_catalog_via_library(ctx)
        else:
            result = _save_catalog_inner(ctx)
    except catalog.CatalogError as exc:
        # A corrupt stored catalog must never be overwritten by a save.
        result = {
            "saved": False,
            "error": "catalog_corrupt",
            "message": str(exc),
        }
    except library.LibraryError as exc:
        result = {"saved": False, "error": "library_corrupt", "message": str(exc)}
    except Exception as exc:  # every handled failure publishes its result
        _log(f"save failed: {exc}")
        result = {"saved": False, "error": "internal_error", "message": str(exc)}
    mirror(result)
    return result


def _save_catalog_via_library(ctx: TaskContext) -> dict:
    """Legacy SaveCatalog draft -> one touched-records Apply (customs only)."""
    if ctx.args.get("catalog") is None or (
        isinstance(ctx.args.get("catalog"), str) and not ctx.args["catalog"].strip()
    ):
        return {"saved": False, "message": "no draft to save (Tasks-page runs are a no-op)"}
    draft = _parse_catalog_arg(ctx)
    doc = library.load(ctx.data_dir)
    stored_custom = {c["id"]: c for c in doc["channels"] if c["kind"] == "ch"}
    ops = []
    unsupported = []
    seen_ids = set()
    for i, raw_channel in enumerate(draft.get("channels") or []):
        if not isinstance(raw_channel, dict):
            continue
        ch_id = raw_channel.get("id")
        if not isinstance(ch_id, str) or ch_id not in stored_custom:
            unsupported.append({
                "path": f"channels[{i}]", "code": "unknown_channel",
                "message": f"{ch_id!r} is not a custom channel in the library",
            })
            continue
        seen_ids.add(ch_id)
        stored_source = stored_custom[ch_id].get("source") or {}
        source_type = (raw_channel.get("source") or {}).get("type")
        if source_type not in ("savedFilter", "tag", "performer", "studio") \
                or (stored_source.get("type") in ("filter", "criteria")
                    and raw_channel.get("source") != stored_source):
            # A legacy client cannot author or round-trip composite rules:
            # its normalizer would truncate them. Refuse the edit loudly; the
            # stored rules survive untouched.
            unsupported.append({
                "path": f"channels[{i}].source", "code": "unsupported_edit",
                "message": "this client cannot represent this channel's rules; "
                           "edit it in the Channel Studio instead",
            })
            continue
        stored = stored_custom[ch_id]
        normalized = catalog.normalize({"channels": [raw_channel]})["channels"]
        if not normalized:
            continue
        row = normalized[0]
        ops.append({"op": "channel.put", "channel": {
            "id": stored["id"], "kind": stored["kind"], "seed": stored["seed"],
            "number": row.get("number") if row.get("number") is not None else stored["number"],
            "name": row.get("name") or stored["name"],
            "glyph": row.get("glyph") if row.get("glyph") in contract.GLYPHS else stored.get("glyph"),
            "color": row.get("color") or stored.get("color"),
            "groupId": stored["groupId"],
            "sort": row.get("sort") if row.get("sort") in contract.SORTS else stored.get("sort"),
            "enabled": bool(row.get("enabled", True)),
            "archived": stored.get("archived", False),
            "paused": stored.get("paused", False),
            "source": row.get("source") or stored["source"],
            "sourceLabel": row.get("sourceLabel") or stored.get("sourceLabel", ""),
            "programming": row.get("programming") or stored.get("programming"),
        }})
    if unsupported:
        return {"saved": False, "error": "unsupported_edit",
                "errors": unsupported,
                "message": "nothing was written; the library keeps channels this "
                           "client cannot represent intact"}
    if not ops:
        return {"saved": False, "message": "no custom channels in the draft"}
    expected = _as_int(ctx.args.get("expectedRevision"), -1)
    errors = library._check_ops(doc, ops)
    if errors:
        return {"saved": False, "error": "validation_failed", "errors": errors}
    rid = str(ctx.args.get("requestId") or "").strip() or f"save-{int(time.time() * 1000)}"
    receipt = library.apply_transaction(
        ctx.data_dir, expected_revision=expected, request_id=rid,
        ops=ops, actor="legacy-save-catalog",
    )
    if receipt.get("status") != "committed":
        payload = {"saved": False, "error": receipt.get("error"), "message": receipt.get("message", "")}
        if receipt.get("errors"):
            payload["errors"] = receipt["errors"]
        if receipt.get("currentRevision") is not None:
            payload["currentRevision"] = receipt["currentRevision"]
        return payload
    affected, signatures = [], {}
    before = {c["id"]: c for c in doc["channels"]}
    after = {c["id"]: c for c in library.load(ctx.data_dir)["channels"]}
    affected, signatures = refresh.affected_channels(before, after)
    if affected:
        refresh.enqueue(ctx.data_dir, affected, receipt["revision"], signatures)
        try:
            refresh.process_pending(ctx.client, ctx.data_dir, ctx.assets_dir)
        except Exception as exc:
            _log(f"refresh skipped (scheduler will retry): {exc}")
    return {"saved": True, "revision": receipt["revision"]}


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
            "runId": f"save-{normalized['revision']}",
            "startedAt": int(__import__("time").time() * 1000),
            "finishedAt": int(__import__("time").time() * 1000),
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
    if channel_service.library_active(ctx.data_dir):
        view = channel_service.load_view(ctx.data_dir)
        legacy_channels = [channel_service.as_legacy_catalog_channel(c)
                           for c in view["channels"]
                           if c["enabled"] and not c["archived"] and not c["paused"]]
        previous = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
        snapshot = snapshots.build_directory_snapshot(
            ctx.client, {"revision": view["revision"], "channels": legacy_channels}, previous)
        snapshots.write_snapshot(ctx.data_dir, ctx.assets_dir, snapshot)
        return {
            "mode": "refresh_data",
            "revision": view["revision"],
            "channels": len(snapshot.get("channels", {})),
            "computedAt": snapshot.get("computedAt"),
        }
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
    owner's networks. Pure file reads — no Stash queries. Library-backed
    deployments project the resolved view: the three legacy buckets come from
    each group's ``legacySection`` fallback, and channels in owner-defined
    groups (no legacy mapping) surface in the General bucket so nothing
    disappears for an old client."""
    if channel_service.library_active(ctx.data_dir):
        view = channel_service.load_view(ctx.data_dir)
        groups = {g["id"]: g for g in view["groups"]}
        snapshot = snapshots.read_snapshot(ctx.data_dir, ctx.assets_dir)
        health = snapshot.get("channels", {}) if snapshot.get("revision") == view["revision"] else {}
        schedule_status = continuing.read_status(ctx.data_dir).get("channels", {})
        buckets: dict[str, list[dict]] = {"general": [], "studios": [], "performers": []}

        def bucket_of(group_id: str) -> str:
            legacy = (groups.get(group_id) or {}).get("legacySection")
            return legacy if legacy in buckets else "general"

        custom_rows = []
        for channel in view["channels"]:
            if channel["kind"] == "ch":
                if not channel.get("enabled", True) or channel.get("archived") or channel.get("paused"):
                    continue
                custom_rows.append({
                    "id": channel["id"], "number": channel["number"],
                    "name": channel["name"], "glyph": channel.get("glyph"),
                    "color": channel.get("color", "#455A64"),
                    "sort": channel.get("sort", "shuffle"), "seed": channel["seed"],
                    "sourceType": (channel.get("source") or {}).get("type"),
                    "programmingMode": (channel.get("programming") or {}).get("mode", "fixed"),
                    "groupId": channel["groupId"],
                    "sceneCount": health.get(channel["id"], {}).get("sceneCount"),
                })
                continue
            row = {
                "id": channel["id"], "number": channel["number"],
                "name": channel["name"], "glyph": channel.get("glyph"),
                "color": channel.get("color", "#455A64"),
                "kind": (channel.get("provenance") or {}).get("family", "channel"),
                "count": (channel.get("provenance") or {}).get("seedCount") or 0,
                "offAir": False,
                "origin": "network",
                "sourceLabel": channel.get("sourceLabel", ""),
                "groupId": channel["groupId"],
                "groupName": (groups.get(channel["groupId"]) or {}).get("name"),
                "poolFreshness": "historical",
            }
            if channel["id"] in health:
                count = health[channel["id"]].get("sceneCount")
                if count is not None:
                    row["count"] = count
                    row["offAir"] = count <= 0
                    row["poolFreshness"] = "current"
            if (channel.get("programming") or {}).get("mode") == "continuing":
                row["programmingMode"] = continuing.resolved_mode(
                    channel_service.as_legacy_network_channel(channel),
                    continuing.try_load_rollout(ctx.data_dir))
            entry = schedule_status.get(channel["id"])
            if entry and entry.get("ready"):
                row["schedule"] = {
                    key: entry[key]
                    for key in ("coverageHours", "degraded", "expiring", "generatedAt")
                    if key in entry
                }
            buckets[bucket_of(channel["groupId"])].append(row)
        for bucket in buckets.values():
            bucket.sort(key=lambda r: r["number"])
        return {
            "pluginId": contract.PLUGIN_ID,
            "contractVersion": contract.CONTRACT_VERSION,
            "revision": view["revision"],
            "libraryId": view["libraryId"],
            "groups": view["groups"],
            "custom": custom_rows,
            "general": {"tagChannels": buckets["general"]},
            "studios": {"channels": buckets["studios"]},
            "performers": {"channels": buckets["performers"]},
            "networksNote": "Channels 100+ come from the owner's editable library.",
        }
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
    rollout = continuing.try_load_rollout(ctx.data_dir)
    if channel_service.library_active(ctx.data_dir):
        channel = channel_service.get_channel(ctx.data_dir, channel_id)
        if channel is None or not channel.get("enabled", True) \
                or channel.get("archived") or channel.get("paused"):
            raise LookupError("channel is unavailable")
        if channel["kind"] == "net":
            # The rollout gate + authored pin decide; the GUI never bypasses.
            mode = continuing.resolved_mode(
                channel_service.as_legacy_network_channel(channel), rollout)
        else:
            mode = programming.policy(channel.get("programming"))["mode"]
        if mode == "fixed":
            return {"status": "fixed", "programs": []}
        return programming.schedule(ctx.data_dir, channel_id,
            _as_int(ctx.args.get("at"), int(__import__("time").time() * 1000)),
            _as_int(ctx.args.get("limit"), 50))
    if channel_id.startswith("net_"):
        network = networks.get(channel_id)
        if network is None:
            raise LookupError("channel is unavailable")
        mode = continuing.resolved_mode(network, rollout)
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
    """The lightweight scheduling surface: last run (correlated by runId) +
    per-channel publication status with coverage/expiry derived at READ time.
    This is what distinguishes a queued task from a generation that actually
    succeeded — ops tooling polls it instead of trusting a job id, and a
    scheduler that stopped publishing cannot keep claiming fresh coverage."""
    return continuing.read_status(ctx.data_dir)


def _outcome_class(key: str, value) -> str:
    """One explicit outcome vocabulary for BOTH channel groups. Custom-channel
    outcomes are plain strings ("ready" or an error message); network outcomes
    classify through continuing's table. (Fixes the review's S3: the old
    predicate compared a boolean to the string "failure", so custom errors and
    rollout errors never failed the task.)"""
    if key.startswith("net_"):
        return continuing.outcome_class(value)
    if value == "ready":
        return "ready"
    if value in ("changed_during_build", "deferred_index_budget", "backoff",
                 "programming_current"):
        return "deferred"
    return "failure"


def _op_prepare_programming(ctx):
    # The run id correlates the queued task with the durable status entry (R7):
    # ops tooling matches on THIS id — an unrelated older successful run can
    # never be mistaken for this one. startedAt precedes the work; finishedAt
    # and the per-channel outcomes are recorded even when channels fail.
    run_id = str(ctx.args.get("runId") or "").strip() or f"task-{int(time.time() * 1000)}"
    started_at = int(time.time() * 1000)

    # Library deployments first drain the durable pending-refresh journal
    # (Apply commits may have crashed before their inline refresh ran).
    pending_result = None
    if channel_service.library_active(ctx.data_dir):
        try:
            pending_result = refresh.process_pending(
                ctx.client, ctx.data_dir, ctx.assets_dir)
        except Exception as exc:
            pending_result = {"error": str(exc)}

    result = programming.prepare(ctx.client, ctx.data_dir, ctx.args.get("channelId"))
    try:
        network_result = continuing.prepare(ctx.client, ctx.data_dir, ctx.args.get("channelId"))
    except ValueError as exc:  # rollout file broken: loud, per-run, but customs still report
        network_result = {"channels": {"rollout": str(exc)}}
    run = {
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": int(time.time() * 1000),
        "customChannels": result["channels"],
        "networkChannels": network_result["channels"],
    }
    continuing.write_status(ctx.data_dir, run)
    failures = {key: value for group in ("customChannels", "networkChannels")
                for key, value in run[group].items()
                if _outcome_class(key, value) == "failure"}
    deferred = {key: value for key, value in network_result["channels"].items()
                if continuing.outcome_class(value) == "deferred"}
    if failures:
        raise GraphQLClientError("Some programming could not be prepared: " + json.dumps(failures))
    response = {"channels": {**result["channels"], **network_result["channels"]},
                "networks": network_result["channels"],
                "runId": run_id,
                "deferred": deferred}
    if pending_result is not None:
        response["libraryRefresh"] = pending_result
    return response


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
    # channel library (editing surface)
    "get_channel_library": channel_ops.op_get_channel_library,
    "get_channel_directory": channel_ops.op_get_channel_directory,
    "get_channel_definition": channel_ops.op_get_channel_definition,
    "validate_channel_changes": channel_ops.op_validate_channel_changes,
    "preview_channel_pool": channel_ops.op_preview_channel_pool,
    "apply_channel_changes": channel_ops.op_apply_channel_changes,
    "get_channel_apply_result": channel_ops.op_get_channel_apply_result,
    "get_channel_history": channel_ops.op_get_channel_history,
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
