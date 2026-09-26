"""Durable incremental-refresh journal for the editable library.

An Apply that changes MEMBERSHIP must eventually refresh that channel's
counts/rotation/publication — but never inline in a GraphQL connection, and
never by reindexing the whole tier. The protocol (audit C5 remediation):

1. The Apply task records pending work BEFORE the library document commits:
   :func:`library.apply_transaction` runs the :func:`commit_hook` inside the
   writer lock on the validated candidate, so the intent journal is durably
   on disk first. Every crash window is recoverable:

   * crash before the journal write — nothing committed, the retry re-runs;
   * crash between journal write and commit — the entry's signature does not
     match the (unchanged) current source, and the worker drops it as
     already-current (or requeues against the real current signature);
   * crash after commit — the scheduler's next run drains the journal.

2. :func:`process_pending` (the Apply task itself, PrepareProgramming, and
   the hourly scheduler) refreshes ONLY pending channels: claim/snapshot
   under no lock, compute outside the lock, then commit under the library
   writer lock — revalidating the channel, merging health into the CURRENT
   snapshot (not the batch's stale one), and acknowledging ONLY the entry
   generation that was processed. Work enqueued concurrently (another Apply,
   another worker) is never clobbered: the final journal rewrite preserves
   any entry whose generation is newer than what this run processed.

3. Publication guard: a worker may only publish for a channel whose full
   selection identity (source, sort, seed, policy, playable state, effective
   mode) still matches what it computed with. Superseded results are dropped
   and re-queued against the current identity.

Cosmetic edits (name/color/glyph/group/number) never enter this journal —
they change the directory, not content. Pause/archive/enabled DO enter: the
channel's publications must stop being served.
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
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("channelId"), str)]


def write_pending(data_dir: str | Path, entries: list[dict]) -> None:
    snapshots.write_json(pending_path(data_dir), entries)


def enqueue(data_dir: str | Path, channel_ids: list[str], revision: int,
            signatures: dict[str, str]) -> int:
    """Add (or refresh) pending entries for ``channel_ids``; newest wins.

    MUST run under the library writer lock (the Apply path does, via
    :func:`commit_hook`) so a concurrent worker's journal rewrite can never
    erase this work."""
    import time
    existing = {e["channelId"]: e for e in read_pending(data_dir)}
    stamp = time.time_ns()
    for i, cid in enumerate(channel_ids):
        prior = existing.get(cid)
        generation = (prior or {}).get("generation")
        try:
            generation = int(generation) + 1
        except (TypeError, ValueError):
            generation = 1
        existing[cid] = {
            "channelId": cid,
            "signature": signatures.get(cid),
            "revision": revision,
            "generation": generation,
            "enqueuedAt": library._now_iso(),
            "stamp": stamp + i,
        }
    out = sorted(existing.values(), key=lambda e: (e.get("stamp", 0), e["channelId"]))
    write_pending(data_dir, out)
    return len(out)


def commit_hook(data_dir: str | Path, before: dict[str, dict]):
    """Build the :func:`library.apply_transaction` ``pre_commit`` hook that
    journals membership-refresh intent for everything this transaction
    changes — durably ordered BEFORE the document commit, inside the writer
    lock (an exception aborts the whole transaction with nothing written)."""
    def hook(candidate: dict, original: dict) -> None:
        after = {c["id"]: c for c in candidate["channels"]}
        affected, signatures = affected_channels(before, after)
        if affected:
            enqueue(data_dir, affected, candidate["revision"] + 1, signatures)
    return hook


def affected_channels(before: dict[str, dict], after: dict[str, dict]) -> tuple[list[str], dict[str, str]]:
    """Ids whose CONTENT-affecting state changed between two channel maps.

    Creation counts (nothing → something); archiving/pausing/disabling counts
    (its publications must stop being served); programming-policy changes
    count; pure cosmetic changes do not.
    """
    affected: list[str] = []
    signatures: dict[str, str] = {}
    for cid, channel in after.items():
        before_channel = before.get(cid)
        before_sig = criteria.source_signature(before_channel["source"]) if before_channel else None
        after_sig = criteria.source_signature(channel["source"])
        # Programming is compared in its CANONICAL stored form: the
        # transaction normalizes the policy (defaults filled), which alone is
        # cosmetic — and the normalizer is engine-aware (a continuing
        # network's newShare survives a rename).
        before_policy = library.normalize_programming(before_channel.get("programming")) if before_channel else None
        after_policy = library.normalize_programming(channel.get("programming"))
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


def _health_is_current(data_dir, assets_dir, cid: str, signature: str) -> bool:
    """True when the stored health entry was computed for exactly this
    membership signature (a crashed/retried Apply that changed nothing does
    no redundant work, and stale entries stay retryable). Entries written
    before this check stayed the single authority: an "unavailable" status
    never matches, so failed work is always retried."""
    entry = (snapshots.read_snapshot(data_dir, assets_dir).get("channels") or {}).get(cid)
    return isinstance(entry, dict) and entry.get("sourceSignature") == signature \
        and entry.get("healthStatus") in ("ok", "offAir", "missingSource")


def requeue(data_dir: str | Path, channel: dict, revision: int) -> dict:
    """Durable ONE-channel recompute request (the UI's "Retry refresh", or an
    owner's "refresh this now"). ``force`` defeats the health-current
    short-circuit for exactly one pass, so even a channel whose health looks
    current is recomputed. Unlike :func:`enqueue` (which runs inside an Apply
    transaction's lock), this takes the library lock itself; a crash before
    the next worker pass loses nothing — the intent is already on disk."""
    import time
    cid = channel["id"]
    with library.library_lock(data_dir):
        existing = {e["channelId"]: e for e in read_pending(data_dir)}
        prior = existing.get(cid) or {}
        try:
            generation = int(prior.get("generation")) + 1
        except (TypeError, ValueError):
            generation = 1
        entry = {
            "channelId": cid,
            "signature": criteria.source_signature(channel["source"]),
            "revision": revision,
            "generation": generation,
            "enqueuedAt": library._now_iso(),
            "stamp": time.time_ns(),
            "force": True,
        }
        existing[cid] = entry
        write_pending(data_dir, sorted(
            existing.values(), key=lambda e: (e.get("stamp", 0), e["channelId"])))
    return entry


def process_pending(client: Any, data_dir: str | Path, assets_dir: Path,
                    max_channels: int | None = None) -> dict:
    """Refresh pending channels; acknowledge only the generations processed.

    Returns ``{processed: {id: outcome}, remaining: n}``. Per-channel failure
    isolation: one broken channel never blocks the rest, and its entry stays
    queued for the next run."""
    entries = read_pending(data_dir)
    if not entries:
        return {"processed": {}, "remaining": 0}
    try:
        view = library.load(data_dir)
    except library.LibraryError:
        return {"processed": {}, "remaining": len(entries)}
    current = {c["id"]: c for c in view["channels"]}

    batch = entries if max_channels is None else entries[:max_channels]
    outcomes: dict[str, str] = {}
    # cid -> generation whose work is DONE and current (acknowledged)
    acknowledged: dict[str, int] = {}
    # cid -> True when the run must keep/refresh an entry (failure/supersede)
    keep: set[str] = set()

    for entry in batch:
        cid = entry["channelId"]
        try:
            generation = int(entry.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        channel = current.get(cid)
        if channel is None:
            outcomes[cid] = "channel_removed"
            acknowledged[cid] = generation  # retired identities keep nothing pending
            continue
        current_sig = criteria.source_signature(channel["source"])
        if entry.get("signature") not in (None, current_sig):
            # Superseded: an older work order for a channel whose membership
            # changed again. Re-queue against the CURRENT signature.
            keep.add(cid)
            outcomes[cid] = "superseded_requeued"
            continue
        # A forced requeue (the UI's Retry) recomputes even when health is
        # current; everything else short-circuits on a current entry.
        if not entry.get("force") and _health_is_current(data_dir, assets_dir, cid, current_sig):
            outcomes[cid] = "current"
            acknowledged[cid] = generation
            continue
        if not channel["enabled"] or channel["archived"] or channel["paused"]:
            outcomes[cid] = "not_playable"
            acknowledged[cid] = generation
            continue
        legacy_shape = {
            "id": channel["id"], "number": channel["number"], "name": channel["name"],
            "source": channel["source"], "sourceLabel": channel.get("sourceLabel", ""),
            "sort": channel.get("sort", "shuffle"), "seed": channel["seed"],
            "enabled": True, "programming": channel.get("programming"),
        }
        # The CURRENT published snapshot is the stale-fallback basis, so a
        # failed check keeps the channel's last-known numbers (E2: passing {}
        # here silently dropped them).
        previous_snapshot = snapshots.read_snapshot(data_dir, assets_dir)
        health = snapshots._channel_health(client, legacy_shape, previous_snapshot)
        unavailable = health.get("healthStatus") == "unavailable"
        # Commit under the writer lock: revalidate the channel, merge into the
        # CURRENT snapshot (never the batch's stale one), acknowledge.
        try:
            with library.library_lock(data_dir):
                latest = library.load(data_dir)
                latest_channel = next(
                    (c for c in latest["channels"] if c["id"] == cid), None)
                if latest_channel is None:
                    outcomes[cid] = "channel_removed"
                    acknowledged[cid] = generation
                    continue
                if criteria.source_signature(latest_channel["source"]) != current_sig:
                    keep.add(cid)
                    outcomes[cid] = "superseded_requeued"
                    continue
                # Merge into the CURRENT published snapshot, preserving every
                # sibling channel's record (concurrent batches included).
                merged = snapshots.read_snapshot(data_dir, assets_dir)
                channels = dict(merged.get("channels") or {})
                channels[cid] = health
                snapshots.write_snapshot(data_dir, assets_dir, {
                    **merged, "revision": latest["revision"], "channels": channels,
                    "computedAt": library._now_iso(),
                })
        except library.LibraryError:
            keep.add(cid)
            outcomes[cid] = "library_unavailable"
            continue
        except Exception as exc:  # per-channel isolation; entry stays queued
            keep.add(cid)
            outcomes[cid] = f"failed: {exc}"
            continue
        if unavailable:
            # E2: a failed check is a RETRYABLE stage failure, not a result —
            # the stale-flagged entry (last-known counts) is published above,
            # the journal entry stays queued for the next pass, and the
            # generation is NOT acknowledged. The failure is never acked and
            # forgotten.
            keep.add(cid)
            outcomes[cid] = "unavailable_retryable"
            continue
        acknowledged[cid] = generation
        # Programming publication (explore/discovery) for the channel.
        if channel.get("programming") and \
                (channel["programming"] or {}).get("mode") in ("explore", "discovery"):
            try:
                result = _prepare_one(client, data_dir, channel)
                if outcomes.get(cid) in (None, "not_playable"):
                    outcomes[cid] = result
            except Exception as exc:
                keep.add(cid)
                if outcomes.get(cid) is None:
                    outcomes[cid] = f"programming failed: {exc}"
                continue
        elif outcomes.get(cid) is None:
            outcomes[cid] = "refreshed"

    remaining = _rewrite_pending(data_dir, entries, set(acknowledged) | keep,
                                 acknowledged, keep)
    return {"processed": outcomes, "remaining": remaining}


def _rewrite_pending(data_dir, entries: list[dict], batch_ids: set[str],
                     acknowledged: dict[str, int], keep: set[str]) -> int:
    """The single journal write that ends a run — under the library lock so
    it cannot race a concurrent Apply's enqueue. For channels this run
    touched: drop ONLY entries whose generation is the one we processed (a
    newer generation was enqueued after our success and survives); requeue
    everything else against the channel's CURRENT signature. Channels this
    run never touched keep whatever is in the file verbatim."""
    with library.library_lock(data_dir):
        current_entries = read_pending(data_dir)
        try:
            latest = library.load(data_dir)
            live = {c["id"]: c for c in latest["channels"]}
        except library.LibraryError:
            return len(current_entries)
        out: dict[str, dict] = {}
        for e in current_entries:
            cid = e["channelId"]
            if cid not in batch_ids:
                out[cid] = e  # untouched by this run: preserved verbatim
                continue
            try:
                generation = int(e.get("generation") or 0)
            except (TypeError, ValueError):
                generation = 0
            if acknowledged.get(cid) == generation and cid not in keep:
                continue  # our completed, still-current work
            channel = live.get(cid)
            if channel is None:
                continue  # channel gone: nothing to keep pending
            if cid in keep:
                out[cid] = {**e, "signature": criteria.source_signature(channel["source"]),
                            "revision": latest["revision"]}
        write_pending(data_dir, sorted(out.values(), key=lambda e: e["channelId"]))
        return len(out)


def _prepare_one(client: Any, data_dir: str | Path, channel: dict) -> str:
    """Prepare programming for exactly one channel with a full-selection
    stale-guard: the commit compares source, order, policy, playable state
    AND the effective mode of the CURRENT library channel (audit C6: an
    older worker must not publish after a sort/policy/pause edit), under the
    actual library writer lock."""
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
    prior_generation = prior.get("generation") if prior else None
    with library.library_lock(data_dir):
        latest = library.load(data_dir)
        latest_channel = next((c for c in latest["channels"] if c["id"] == channel["id"]), None)
        if latest_channel is None:
            return "channel_removed"
        outcome = _publication_stale(latest_channel, channel, prior)
        if outcome is not None:
            return outcome
        current_pub = programming.read(data_dir, channel["id"])
        if current_pub is not None \
                and current_pub.get("generation") != prior_generation:
            return "changed_during_build"
        programming.snapshots.write_json(
            programming.path(data_dir, channel["id"]), publication)
    return "ready"


def _publication_stale(latest_channel: dict, indexed_channel: dict, prior: dict | None) -> str | None:
    """Why a computed publication may NOT be published against the current
    library channel (None = publish). Covers source, sort/seed ordering,
    policy, and playable state; the effective-mode check lives in the
    scheduler-side guards."""
    from justwatch import programming
    if criteria.source_signature(latest_channel["source"]) \
            != criteria.source_signature(indexed_channel["source"]):
        return "changed_during_build"
    if latest_channel.get("sort") != indexed_channel.get("sort"):
        return "changed_during_build"
    if int(latest_channel.get("seed") or 0) != int(indexed_channel.get("seed") or 0):
        return "changed_during_build"
    if programming.policy(latest_channel.get("programming")) \
            != programming.policy(indexed_channel.get("programming")):
        return "changed_during_build"
    for flag, default in (("enabled", True), ("archived", False), ("paused", False)):
        if bool(latest_channel.get(flag, default)) != bool(indexed_channel.get(flag, default)):
            return "changed_during_build"
    return None
