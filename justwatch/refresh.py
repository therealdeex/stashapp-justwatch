"""Incremental refresh journal for the editable library.

An Apply that changes MEMBERSHIP must eventually refresh that channel's
counts/rotation/publication — but never inline in a GraphQL connection, and
never by reindexing the whole tier. The flow:

1. The Apply task commits the transaction, then records one pending entry per
   affected channel (id + membership signature + applied revision) in
   ``<data>/channel-library-pending.json`` — durably, in the same task, so a
   crash between commit and refresh is recovered by the next scheduler run.
2. :func:`process_pending` (called by PrepareProgramming and by the Apply
   task itself) refreshes ONLY the pending channels: targeted health
   computation merged into the existing snapshot, targeted programming
   publication, targeted continuing preparation (rollout-gated).
3. Publication guard: a worker may only publish results for an entry whose
   signature still equals the channel's CURRENT membership signature under
   the library lock. A superseded worker's results are dropped and its entry
   is re-queued against the new signature (the newer entry wins).

Cosmetic edits (name/color/glyph/group/number) and pause/archive never enter
this journal — they change the directory, not content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from justwatch import catalog, continuing, criteria, library, lineup, snapshots

PENDING_NAME = "channel-library-pending.json"


def pending_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / PENDING_NAME


def read_pending(data_dir: str | Path) -> list[dict]:
    try:
        raw = json.loads(pending_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("channelId"), str)]


def write_pending(data_dir: str | Path, entries: list[dict]) -> None:
    snapshots.write_json(pending_path(data_dir), entries)


def enqueue(data_dir: str | Path, channel_ids: list[str], revision: int,
            signatures: dict[str, str]) -> int:
    """Add (or refresh) pending entries for ``channel_ids``; newest wins."""
    existing = {e["channelId"]: e for e in read_pending(data_dir)}
    for cid in channel_ids:
        existing[cid] = {
            "channelId": cid,
            "signature": signatures.get(cid),
            "revision": revision,
            "enqueuedAt": library._now_iso(),
        }
    out = sorted(existing.values(), key=lambda e: (e["revision"], e["channelId"]))
    write_pending(data_dir, out)
    return len(out)


def affected_channels(before: dict[str, dict], after: dict[str, dict]) -> tuple[list[str], dict[str, str]]:
    """Ids whose MEMBERSHIP changed between two channel maps (id -> record).

    Creation counts (nothing → something); archiving/pausing counts (its
    publications must stop being served); pure cosmetic changes do not.
    """
    affected: list[str] = []
    signatures: dict[str, str] = {}
    from justwatch.programming import policy as policy_shape
    for cid, channel in after.items():
        before_channel = before.get(cid)
        before_sig = criteria.source_signature(before_channel["source"]) if before_channel else None
        after_sig = criteria.source_signature(channel["source"])
        # programming is compared in its CLAMPED form: the transaction
        # normalizes the policy (defaults filled), which alone is cosmetic.
        before_policy = policy_shape(before_channel.get("programming")) if before_channel else None
        after_policy = policy_shape(channel.get("programming"))
        pool_changed = (
            before_channel is None
            or before_sig != after_sig
            or before_channel.get("sort") != channel.get("sort")
            or int(before_channel.get("seed") or 0) != int(channel.get("seed") or 0)
            or before_policy != after_policy
            or bool(before_channel.get("archived")) != bool(channel.get("archived"))
            or bool(before_channel.get("paused")) != bool(channel.get("paused"))
            or bool(before_channel.get("enabled", True)) != bool(channel.get("enabled", True))
        )
        if pool_changed:
            affected.append(cid)
            signatures[cid] = after_sig
    return affected, signatures


def process_pending(client: Any, data_dir: str | Path, assets_dir: str | Path,
                    max_channels: int | None = None) -> dict:
    """Refresh pending channels; drop entries whose work is done and current.

    Returns ``{processed: {id: outcome}, remaining: n}``. Per-channel failure
    isolation: one broken channel never blocks the rest, and its entry stays
    queued for the next run.
    """
    entries = read_pending(data_dir)
    if not entries:
        return {"processed": {}, "remaining": 0}
    outcomes: dict[str, str] = {}
    remaining: list[dict] = []
    view = None
    try:
        view = library.load(data_dir)
    except library.LibraryError:
        return {"processed": {}, "remaining": len(entries)}
    current = {c["id"]: c for c in view["channels"]}
    current_revision = view["revision"]
    previous_snapshot = snapshots.read_snapshot(data_dir, assets_dir)

    batch = entries if max_channels is None else entries[:max_channels]
    deferred: list[dict] = []
    for entry in batch:
        cid = entry["channelId"]
        channel = current.get(cid)
        if channel is None:
            outcomes[cid] = "channel_removed"
            continue  # drop: retired identities keep nothing pending
        current_sig = criteria.source_signature(channel["source"])
        if entry.get("signature") not in (None, current_sig):
            # Superseded: an older revision's work order for a channel whose
            # membership changed again. Re-queue against the CURRENT signature.
            deferred.append({**entry, "signature": current_sig,
                             "revision": current_revision})
            outcomes[cid] = "superseded_requeued"
            continue
        if not channel["enabled"] or channel["archived"] or channel["paused"]:
            outcomes[cid] = "not_playable"
            continue
        legacy_shape = {
            "id": channel["id"], "number": channel["number"], "name": channel["name"],
            "source": channel["source"], "sourceLabel": channel.get("sourceLabel", ""),
            "sort": channel.get("sort", "shuffle"), "seed": channel["seed"],
            "enabled": True, "programming": channel.get("programming"),
        }
        health = snapshots._channel_health(client, legacy_shape, previous_snapshot)
        # Publication guard: revalidate under the lock that the channel's
        # membership signature is still what this worker computed for.
        try:
            with library.library_lock(data_dir):
                latest = library.load(data_dir)
                latest_channel = next((c for c in latest["channels"] if c["id"] == cid), None)
                if latest_channel is None:
                    outcomes[cid] = "channel_removed"
                    continue
                if criteria.source_signature(latest_channel["source"]) != current_sig:
                    deferred.append({**entry, "signature":
                                     criteria.source_signature(latest_channel["source"]),
                                     "revision": latest["revision"]})
                    outcomes[cid] = "superseded_requeued"
                    continue
                merged = dict(previous_snapshot) if isinstance(previous_snapshot, dict) else {}
                channels = dict(merged.get("channels") or {})
                channels[cid] = health
                snapshots.write_snapshot(data_dir, assets_dir, {
                    **merged, "revision": latest["revision"], "channels": channels,
                    "computedAt": library._now_iso(),
                })
        except library.LibraryError:
            deferred.append(entry)
            outcomes[cid] = "library_unavailable"
            continue
        except Exception as exc:  # per-channel isolation; entry stays queued
            deferred.append(entry)
            outcomes[cid] = f"failed: {exc}"
            continue
        # Programming publication (explore/discovery) for the channel.
        if channel.get("programming") and \
                (channel["programming"] or {}).get("mode") in ("explore", "discovery"):
            try:
                result = _prepare_one(client, data_dir, channel)
                if outcomes.get(cid) in (None, "not_playable"):
                    outcomes[cid] = result
            except Exception as exc:
                deferred.append(entry)
                if outcomes.get(cid) is None:
                    outcomes[cid] = f"programming failed: {exc}"
                continue
        elif outcomes.get(cid) is None:
            outcomes[cid] = "refreshed"
    # Keep: superseded/failed entries (re-queued against their newest
    # signature), entries beyond this batch, and failed attempts. Entries
    # whose work is done (refreshed / not playable / removed / current) drop.
    attempted = {e["channelId"] for e in batch}
    deferred_ids = {e["channelId"] for e in deferred}
    tail = [e for e in entries if e["channelId"] not in attempted]
    failed = [e for e in batch
              if e["channelId"] in attempted
              and e["channelId"] not in deferred_ids
              and str(outcomes.get(e["channelId"], "")).startswith("failed")]
    seen: set[str] = set()
    final: list[dict] = []
    for e in [*deferred, *tail, *failed]:
        if e["channelId"] in seen:
            continue
        seen.add(e["channelId"])
        final.append(e)
    write_pending(data_dir, final)
    return {"processed": outcomes, "remaining": len(final)}


def _prepare_one(client: Any, data_dir: str | Path, channel: dict) -> str:
    """Prepare programming for exactly one channel with a stale-publication
    guard: the commit guard compares the channel record that was indexed."""
    from justwatch import programming
    prior = programming.read(data_dir, channel["id"])
    signature = programming.digest([
        channel["source"], channel.get("sort", "shuffle"), int(channel.get("seed") or 0),
        programming.policy(channel.get("programming")),
    ])
    if prior and prior["configuration"] == signature:
        return "programming_current"
    now = programming.time.time() * 1000
    reusable = prior and prior.get("index") is not None \
        and prior.get("configuration") == signature \
        and now - prior.get("indexedAt", 0) < 6 * programming.HOUR
    entries = prior["index"] if reusable else programming.index_source(client, channel)
    publication = programming.build(channel, entries, prior)
    with library.library_lock(data_dir):
        latest = library.load(data_dir)
        latest_channel = next((c for c in latest["channels"] if c["id"] == channel["id"]), None)
        if latest_channel is None:
            return "channel_removed"
        if criteria.source_signature(latest_channel["source"]) \
                != criteria.source_signature(channel["source"]):
            return "changed_during_build"
        programming.snapshots.write_json(
            programming.path(data_dir, channel["id"]), publication)
    return "ready"
