"""The channel-library editing operations (additive contract-v1 surface).

Capabilities advertise ``features.channelLibrary`` plus these operations; new
clients (the Channel Studio GUI, enhanced TVs) discover and call them there.
Playback shapes (Directory/Lineup/Schedule) are untouched — this module is
the EDITING and DIRECTORY-DISCOVERY surface:

* GetChannelLibrary      sync   bounded, paged/searched lightweight list
* GetChannelDirectory    sync   ordered groups + playback rows (+signatures)
* GetChannelDefinition   sync   one full editable record
* ValidateChannelChanges sync   typed errors + effect summary, no writes
* PreviewChannelPool     sync   draft-correlated count + bounded sample
* ApplyChannelChanges    TASK   touched-records transaction + refresh
* GetChannelApplyResult  sync   durable receipt lookup by requestId
* GetChannelHistory      sync   bounded revision archive

Every read here is bounded (no scene queries in the library surface except
PreviewChannelPool, which is capped at a count + one page). Apply is the ONLY
write path and never runs from a GraphQL connection.
"""

from __future__ import annotations

import json
import time
from typing import Any

from justwatch import channel_service, contract, criteria, library, lineup, refresh


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_json_arg(raw: Any, what: str) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    return raw


def _library(ctx) -> dict:
    if not library.exists(ctx.data_dir):
        raise ValueError(
            "no channel library on this deployment — run the migration first "
            "(tools/migrate_channel_library.py)")
    return library.load(ctx.data_dir)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


_LIGHT_FIELDS = ("id", "kind", "number", "name", "glyph", "color", "groupId",
                 "sort", "enabled", "archived", "paused")


def op_get_channel_library(ctx) -> dict:
    doc = _library(ctx)
    args = ctx.args
    query = str(args.get("query") or "").strip().lower()
    group = str(args.get("groupId") or "").strip()
    offset = _as_int(args.get("offset"), 0)
    limit = min(1000, max(1, _as_int(args.get("limit"), 200)))
    include_signatures = bool(args.get("includeSignatures"))
    channels = []
    for channel in doc["channels"]:
        if group and channel["groupId"] != group:
            continue
        if query and query not in channel["name"].lower() \
                and query not in str(channel["number"]):
            continue
        row = {k: channel.get(k) for k in _LIGHT_FIELDS}
        row["sourceType"] = (channel.get("source") or {}).get("type")
        row["sourceLabel"] = channel.get("sourceLabel", "")
        row["programmingMode"] = (channel.get("programming") or {}).get("mode", "fixed")
        row["membershipSignature"] = criteria.source_signature(channel.get("source") or {})
        if include_signatures:
            row["presentationSignature"] = channel_service.presentation_signature(channel)
        channels.append(row)
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "schemaVersion": library.STORAGE_VERSION,
        "libraryId": doc["libraryId"],
        "revision": doc["revision"],
        "migration": doc.get("migration"),
        "groups": doc["groups"],
        "total": len(channels),
        "offset": offset,
        "channels": channels[offset:offset + limit],
        "limits": {"maxChannels": library.MAX_LIBRARY_CHANNELS,
                   "bands": library.BANDS},
    }


def op_get_channel_directory(ctx) -> dict:
    """Complete lightweight playback directory for enhanced clients: ordered
    groups, exactly-once membership, signatures, and honest pool status.
    Includes ALL namespaces; archived/paused/disabled channels carry their
    state explicitly instead of disappearing ( TVs decide omission, but the
    default playback filter excludes them)."""
    doc = _library(ctx)
    schedule_status = _continuing_status(ctx)
    channels = []
    for channel in doc["channels"]:
        playable = channel["enabled"] and not channel["archived"] and not channel["paused"]
        row = {
            "id": channel["id"],
            "kind": channel["kind"],
            "number": channel["number"],
            "name": channel["name"],
            "glyph": channel.get("glyph"),
            "color": channel.get("color", "#455A64"),
            "groupId": channel["groupId"],
            "sort": channel.get("sort", "shuffle"),
            "seed": channel["seed"],
            "sourceType": (channel.get("source") or {}).get("type"),
            "programmingMode": (channel.get("programming") or {}).get("mode", "fixed"),
            "membershipSignature": criteria.source_signature(channel.get("source") or {}),
            "presentationSignature": channel_service.presentation_signature(channel),
            "playable": playable,
        }
        entry = schedule_status.get(channel["id"])
        if entry and entry.get("ready"):
            row["schedule"] = {k: entry[k] for k in (
                "version", "generatedAt", "preparedThrough", "coverageHours",
                "degraded", "expiring") if k in entry}
        channels.append(row)
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "libraryId": doc["libraryId"],
        "revision": doc["revision"],
        "groups": doc["groups"],
        "channels": channels,
        "emptyLibrary": not doc["channels"],
    }


def _continuing_status(ctx) -> dict[str, dict]:
    from justwatch import continuing
    try:
        return continuing.read_status(ctx.data_dir).get("channels", {})
    except Exception:
        return {}


def op_get_channel_definition(ctx) -> dict:
    channel_id = str(ctx.args.get("channelId") or "").strip()
    doc = _library(ctx)
    channel = next((c for c in doc["channels"] if c["id"] == channel_id), None)
    if channel is None:
        raise LookupError(f"no channel {channel_id!r} in the library")
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        "revision": doc["revision"],
        "channel": channel,
        "summary": criteria.summarize(channel.get("source") or {}),
    }


# ---------------------------------------------------------------------------
# Validation + effects (no writes)
# ---------------------------------------------------------------------------


def op_validate_channel_changes(ctx) -> dict:
    doc = _library(ctx)
    ops = _parse_json_arg(ctx.args.get("ops"), "ops")
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    errors = library._check_ops(doc, ops)
    effects = _effect_summary(doc, ops)
    return {
        "valid": not errors,
        "errors": errors,
        "effects": effects,
        "revision": doc["revision"],
    }


def _effect_summary(doc: dict, ops: list[dict]) -> list[dict]:
    """Per-op effect classification: which channels change membership (and
    therefore need re-indexing/publication) vs metadata only."""
    by_id = {c["id"]: c for c in doc["channels"]}
    effects = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "channel.put":
            channel = op.get("channel") or {}
            stored = by_id.get(channel.get("id"))
            if stored is None:
                continue
            membership_changed = (
                criteria.source_signature(channel.get("source") or {})
                != criteria.source_signature(stored.get("source") or {})
                or channel.get("sort") != stored.get("sort")
                or int(channel.get("seed") or 0) != int(stored.get("seed") or 0)
                or (channel.get("programming") or {}) != (stored.get("programming") or {})
                or bool(channel.get("archived")) != bool(stored.get("archived"))
                or bool(channel.get("paused")) != bool(stored.get("paused"))
                or bool(channel.get("enabled", True)) != bool(stored.get("enabled", True))
            )
            effects.append({
                "op": "channel.put", "channelId": stored["id"],
                "kind": "membership" if membership_changed else "metadata",
                "reindex": membership_changed,
            })
        elif kind == "channel.create":
            effects.append({"op": kind, "channelId": op.get("tempId"),
                            "kind": "creation", "reindex": True})
        elif kind in ("channels.move", "channels.patch", "channel.swap"):
            effects.append({"op": kind, "kind": "metadata", "reindex": False,
                            "count": len(op.get("channelIds") or []) if "channelIds" in op
                            else (2 if kind == "channel.swap" else None)})
        elif kind == "group.put":
            effects.append({"op": kind, "kind": "metadata", "reindex": False})
        elif kind == "group.delete":
            effects.append({"op": kind, "kind": "metadata", "reindex": False})
    return effects


# ---------------------------------------------------------------------------
# Preview (bounded, draft-correlated)
# ---------------------------------------------------------------------------


def op_preview_channel_pool(ctx) -> dict:
    """Count + bounded sample for a DRAFT source. Read-only: no publication,
    no mutation, at most a count and one page of scenes. The response echoes
    the draft's membership signature so the caller can discard stale replies."""
    source = _parse_json_arg(ctx.args.get("source"), "source")
    if not isinstance(source, dict):
        raise ValueError("source must be a JSON object")
    for e in criteria.validate(source):
        if e["code"] in ("empty_rules", "conflicting_rows", "orphan_exclusion",
                         "unknown_fields", "unknown_source_type"):
            raise ValueError(e["message"])
    signature = criteria.source_signature(source)
    sort = str(ctx.args.get("sort") or "shuffle")
    seed = _as_int(ctx.args.get("seed"), 0)
    sample_limit = min(12, max(1, _as_int(ctx.args.get("sampleLimit"), 10)))
    object_filter = None
    text_query = None
    if source.get("type") == "savedFilter":
        try:
            object_filter, text_query = lineup.resolve_saved_criteria(
                ctx.client, str(source.get("id", "")))
        except LookupError as exc:
            return {"signature": signature, "status": "missing_source",
                    "message": str(exc), "poolCount": None,
                    "rotationSize": 0, "sample": []}
    scene_filter = criteria.build_scene_filter(source, object_filter)
    find_filter = lineup.build_find_filter(sort, seed, 1, 50, text_query)
    data = ctx.client.submit(lineup.FIND_SCENES, {
        "filter": find_filter, "scene_filter": scene_filter, "include_paths": False,
    })
    node = (data or {}).get("findScenes") or {}
    pool_count = int(node.get("count") or 0)
    sample = []
    for scene in node.get("scenes") or []:
        item = lineup._scene_item(scene, include_paths=False)
        if item is not None:
            sample.append(item)
        if len(sample) >= sample_limit:
            break
    epoch = lineup.effective_epoch(source)
    return {
        "signature": signature,
        "status": "ok",
        "poolCount": pool_count,
        "rotationSize": min(contract.ROTATION_SIZE, pool_count),
        "rotationComplete": pool_count <= contract.ROTATION_SCAN_LIMIT,
        "sample": sample,
        "epoch": epoch,
        "resolvedAt": int(time.time() * 1000),
        "note": "poolCount is the source behind the channel; rotationSize is the "
                "bounded on-air loop (max 50). The first sample item is not “now”.",
    }


# ---------------------------------------------------------------------------
# Apply (task) + receipts + history
# ---------------------------------------------------------------------------


def op_apply_channel_changes(ctx) -> dict:
    """THE write path. Validates + commits the touched-records transaction,
    records durable pending refresh work for membership changes, then runs
    the incremental refresh inline (same task). The correlated receipt is
    stored in the library document BEFORE this task returns, so a lost
    response resolves via GetChannelApplyResult, never by re-applying."""
    request_id = str(ctx.args.get("requestId") or "").strip()
    if not request_id:
        raise ValueError("requestId is required (client-generated, unique per submit)")
    expected = _as_int(ctx.args.get("expectedRevision"), -1)
    ops = _parse_json_arg(ctx.args.get("ops"), "ops")
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")

    before = _channel_map(ctx.data_dir)
    receipt = library.apply_transaction(
        ctx.data_dir, expected_revision=expected, request_id=request_id, ops=ops,
        actor=str(ctx.args.get("actor") or "channel-studio"),
    )
    if receipt.get("status") != "committed":
        return receipt

    # Membership changes -> durable pending refresh entries (survives a crash
    # between this commit and the refresh; the scheduler also drains it).
    after = _channel_map(ctx.data_dir)
    affected, signatures = refresh.affected_channels(before, after)
    if affected:
        refresh.enqueue(ctx.data_dir, affected, receipt["revision"], signatures)

    # Inline incremental refresh for THIS apply (bounded: affected only).
    processed = {}
    try:
        result = refresh.process_pending(ctx.client, ctx.data_dir, ctx.assets_dir)
        processed = result["processed"]
    except Exception as exc:  # the commit is durable; refresh retries on the timer
        processed = {"error": str(exc)}
    receipt = dict(receipt)
    receipt["refresh"] = processed
    receipt["note"] = (
        "Committed. Background programming refresh is pending where membership changed."
        if affected else "Committed. Metadata-only change: no reindex, no playback reset.")
    return receipt


def _channel_map(data_dir) -> dict[str, dict]:
    try:
        doc = library.load(data_dir)
    except library.LibraryError:
        return {}
    return {c["id"]: c for c in doc["channels"]}


def op_get_channel_apply_result(ctx) -> dict:
    request_id = str(ctx.args.get("requestId") or "").strip()
    if not request_id:
        raise ValueError("requestId is required")
    receipt = library.get_receipt(ctx.data_dir, request_id)
    if receipt is None:
        return {"requestId": request_id, "status": "unknown",
                "message": "no receipt for this requestId (expired, or the "
                           "transaction never reached the server; resubmitting "
                           "with the SAME requestId is safe)"}
    return receipt


def op_get_channel_history(ctx) -> dict:
    if not library.exists(ctx.data_dir):
        raise ValueError("no channel library on this deployment")
    offset = _as_int(ctx.args.get("offset"), 0)
    limit = min(50, max(1, _as_int(ctx.args.get("limit"), 20)))
    result = library.read_history(ctx.data_dir, offset=offset, limit=limit)
    include_defs = bool(ctx.args.get("includeDefinitions"))
    if not include_defs:
        for entry in result["entries"]:
            entry.pop("channels", None)
            entry.pop("groups", None)
    return {
        "pluginId": contract.PLUGIN_ID,
        "contractVersion": contract.CONTRACT_VERSION,
        **result,
        "note": "restore is a NEW Apply of a past definition — never a rewind "
                "of the actually-aired ledger",
    }
