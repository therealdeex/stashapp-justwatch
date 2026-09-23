"""The authoritative, owner-editable channel library.

Stored at ``<stash Dir>/stash-justwatch-data/channel-library.json`` — the
plugin's DATA directory, never the install dir. It holds both channel
namespaces (``ch_`` customs 1-99, ``net_`` networks 100-899) plus the channel
groups, and — after ``tools/migrate_channel_library.py`` runs — it is the ONE
store every consumer reads. The compiled networks artifact and the legacy
catalog become seed/provenance inputs: re-imports and redeploys never
overwrite an initialized library.

Load policy mirrors the catalog: strict. A file that exists but is not a
structurally valid library at THIS storage version raises ``LibraryError`` —
never silently normalized, never reset to empty, because the next save would
make that loss permanent. A MISSING file is legal (the deployment is
pre-migration; callers check :func:`exists` and use the legacy stores).

Transactions: one writer path (:func:`apply_transaction`) carrying
``expectedRevision`` + a client ``requestId`` + a payload ``digest``. The
commit (document + receipt) is one atomic rename under an exclusive
process-safe lock, so a crash after commit can never leave the retry to
apply twice: the same requestId replays its stored receipt. Receipts are
bounded (newest kept). Revision history snapshots are auxiliary JSONL under
``<data>/channel-library-history/`` written AFTER the primary commit; the
library document itself is the recovery authority.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from justwatch import catalog, contract, criteria

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

LIBRARY_NAME = "channel-library.json"
HISTORY_DIR_NAME = "channel-library-history"
STORAGE_VERSION = 1

RECEIPTS_KEPT = 200
HISTORY_KEPT = 60

CH_ID_RE = re.compile(r"^ch_[0-9a-f]{8}$")
NET_ID_RE = re.compile(r"^net_[0-9a-f]{8}$")
GRP_ID_RE = re.compile(r"^grp_[0-9a-z]{1,24}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

MAX_SEED = 2_147_483_647
MAX_NAME_LEN = 60
MAX_LIBRARY_CHANNELS = 899

#: Channel numbers per namespace (compatibility bands, not group identities).
BANDS = {"ch": (1, 99), "net": (100, 899)}

#: Programming-preference keys the GUI may set per channel (the rest of the
#: policy is engine-owned and passes through untouched).
EDITABLE_PROGRAMMING_KEYS = ("mode", "spacingMinutes", "repeatCooldownHours",
                             "spotlight", "newShare")

PATCHABLE_FLAGS = ("enabled", "paused", "archived")


class LibraryError(Exception):
    """Structural problem that prevents reading or writing the library."""


def library_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / LIBRARY_NAME


def history_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / HISTORY_DIR_NAME


def exists(data_dir: str | Path) -> bool:
    """True when an authoritative library document is present.

    This — not an emptiness check — is what separates a pre-migration
    deployment (absent file, legacy stores serve) from an intentionally
    empty or fully paused library (present file; the guide is empty because
    the owner made it so, never because the plugin resurrected tiers).
    """
    return library_path(data_dir).exists()


def new_channel_id(kind: str) -> str:
    prefix = "net" if kind == "net" else "ch"
    return f"{prefix}_{secrets.token_hex(4)}"


def new_group_id() -> str:
    return f"grp_{secrets.token_hex(6)}"


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Loading (strict)
# ---------------------------------------------------------------------------


def load(data_dir: str | Path) -> dict:
    """Load the stored library, strictly. Missing file raises LibraryError.

    Every consumer that reads the library must have checked :func:`exists`
    first (or intentionally wants this hard failure — a missing file during
    a committed transaction IS an integrity problem).
    """
    path = library_path(data_dir)
    if not path.exists():
        raise LibraryError(f"no channel library at {path} (deployment is pre-migration)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LibraryError(f"channel library unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LibraryError("channel library is not a JSON object")
    _require_storable_shape(raw, path)
    return raw


def _require_storable_shape(raw: dict, path: Path) -> None:
    def bad(message: str) -> LibraryError:
        return LibraryError(f"channel library at {path} is corrupt: {message}")

    version = raw.get("schemaVersion")
    if version != STORAGE_VERSION:
        raise bad(
            f"unsupported schemaVersion {version!r} (this plugin writes "
            f"{STORAGE_VERSION}); refusing to load or overwrite",
        )
    library_id = raw.get("libraryId")
    if not isinstance(library_id, str) or not library_id.strip():
        raise bad("libraryId must be a non-empty string")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise bad(f"revision must be a non-negative integer, got {revision!r}")
    unknown_top = sorted(set(raw) - {
        "schemaVersion", "libraryId", "revision", "migration", "groups",
        "channels", "settings", "recentRequests",
    })
    if unknown_top:
        raise bad(f"unknown top-level fields: {', '.join(unknown_top)}")
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise bad("groups must be a non-empty list")
    seen_grp: dict[str, str] = {}
    for i, group in enumerate(groups):
        _require_group(group, i, bad)
        if group["id"] in seen_grp:
            raise bad(f"duplicate group id {group['id']}")
        seen_grp[group["id"]] = group["name"]
        norm = group["name"].strip().casefold()
        if any(other is not group and other["name"].strip().casefold() == norm
               for other in groups):
            raise bad(f"duplicate group name {group['name']!r}")
    channels = raw.get("channels")
    if not isinstance(channels, list):
        raise bad("channels must be a list")
    if len(channels) > MAX_LIBRARY_CHANNELS:
        raise bad(f"channel count {len(channels)} exceeds the {MAX_LIBRARY_CHANNELS}-number space")
    seen_ids: set[str] = set()
    seen_numbers: dict[int, str] = {}
    for i, channel in enumerate(channels):
        _require_channel(channel, i, bad)
        if channel["id"] in seen_ids:
            raise bad(f"duplicate channel id {channel['id']}")
        seen_ids.add(channel["id"])
        holder = seen_numbers.get(channel["number"])
        if holder is not None:
            raise bad(f"channel {channel['number']} is held by both {holder} and {channel['id']}")
        seen_numbers[channel["number"]] = channel["id"]
        if channel["groupId"] not in seen_grp:
            raise bad(f"channel {channel['id']} references unknown group {channel['groupId']!r}")
    requests = raw.get("recentRequests")
    if not isinstance(requests, list):
        raise bad("recentRequests must be a list")
    for i, receipt in enumerate(requests):
        if not isinstance(receipt, dict) or not isinstance(receipt.get("requestId"), str):
            raise bad(f"recentRequests[{i}] is not a receipt")
    migration = raw.get("migration")
    if migration is not None and not isinstance(migration, dict):
        raise bad("migration must be a JSON object or null")
    settings = raw.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise bad("settings must be a JSON object")


def _require_group(group: Any, i: int, bad) -> None:
    if not isinstance(group, dict):
        raise bad(f"groups[{i}] must be a JSON object")
    unknown = sorted(set(group) - {"id", "name", "position", "legacySection"})
    if unknown:
        raise bad(f"groups[{i}] has unknown fields: {', '.join(unknown)}")
    if not isinstance(group.get("id"), str) or not GRP_ID_RE.match(group["id"]):
        raise bad(f"groups[{i}].id {group.get('id')!r} is not a group id (grp_…)")
    name = group.get("name")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME_LEN):
        raise bad(f"groups[{i}] ({group.get('id')}) name must be 1-{MAX_NAME_LEN} characters")
    position = group.get("position")
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise bad(f"groups[{i}] ({group.get('id')}) position must be a positive integer")
    legacy = group.get("legacySection")
    if legacy is not None and legacy not in catalog_sections():
        raise bad(f"groups[{i}] legacySection {legacy!r} is not a legacy section")


def catalog_sections() -> tuple[str, ...]:
    from justwatch.networks import SECTIONS
    return SECTIONS


def _require_channel(channel: Any, i: int, bad) -> None:
    if not isinstance(channel, dict):
        raise bad(f"channels[{i}] must be a JSON object")
    unknown = sorted(set(channel) - {
        "id", "kind", "number", "name", "glyph", "color", "groupId", "sort",
        "seed", "enabled", "archived", "paused", "source", "sourceLabel",
        "programming", "provenance",
    })
    if unknown:
        raise bad(f"channels[{i}] has unknown fields: {', '.join(unknown)}")
    ch_id = channel.get("id")
    kind = channel.get("kind")
    if kind == "ch":
        if not isinstance(ch_id, str) or not CH_ID_RE.match(ch_id):
            raise bad(f"channels[{i}].id {ch_id!r} is not a custom channel id")
    elif kind == "net":
        if not isinstance(ch_id, str) or not NET_ID_RE.match(ch_id):
            raise bad(f"channels[{i}].id {ch_id!r} is not a network channel id")
    else:
        raise bad(f"channels[{i}].kind must be 'ch' or 'net', got {kind!r}")
    number = channel.get("number")
    lo, hi = BANDS[kind]
    if isinstance(number, bool) or not isinstance(number, int) or not (lo <= number <= hi):
        raise bad(f"channels[{i}] ({ch_id}) number {number!r} outside the {kind} band {lo}-{hi}")
    name = channel.get("name")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME_LEN):
        raise bad(f"channels[{i}] ({ch_id}) name must be 1-{MAX_NAME_LEN} characters")
    if channel.get("glyph") is not None and channel["glyph"] not in contract.GLYPHS:
        raise bad(f"channels[{i}] ({ch_id}) glyph is not in the shared glyph set")
    color = channel.get("color")
    if not isinstance(color, str) or not COLOR_RE.match(color):
        raise bad(f"channels[{i}] ({ch_id}) color must be #RRGGBB")
    if channel.get("sort") not in contract.SORTS:
        raise bad(f"channels[{i}] ({ch_id}) unknown sort {channel.get('sort')!r}")
    seed = channel.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed <= MAX_SEED):
        raise bad(f"channels[{i}] ({ch_id}) invalid seed")
    for flag in ("enabled", "archived", "paused"):
        if not isinstance(channel.get(flag), bool):
            raise bad(f"channels[{i}] ({ch_id}) {flag} must be a real boolean")
    if not isinstance(channel.get("groupId"), str) or not channel["groupId"]:
        raise bad(f"channels[{i}] ({ch_id}) groupId is required (exactly one group)")
    source = channel.get("source")
    if not isinstance(source, dict):
        raise bad(f"channels[{i}] ({ch_id}) source must be a JSON object")
    if criteria.validate(source):
        raise bad(f"channels[{i}] ({ch_id}) source is invalid: {criteria.validate(source)[0]['message']}")
    if not isinstance(channel.get("sourceLabel", ""), str):
        raise bad(f"channels[{i}] ({ch_id}) sourceLabel must be a string")
    programming = channel.get("programming")
    if programming is not None and not isinstance(programming, dict):
        raise bad(f"channels[{i}] ({ch_id}) programming must be a JSON object")
    provenance = channel.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise bad(f"channels[{i}] ({ch_id}) provenance must be a JSON object")


# ---------------------------------------------------------------------------
# Draft validation (transaction-level, tolerant input -> typed errors)
# ---------------------------------------------------------------------------


def validate_document(raw: Any) -> tuple[list[dict], dict]:
    """Validate a full-library draft (migration/import path).

    Returns ``(errors, normalized)``; ``normalized`` is best-effort for echo.
    """
    errors: list[dict] = []

    def err(path: str, code: str, message: str) -> None:
        errors.append({"path": path, "code": code, "message": message})

    if not isinstance(raw, dict):
        return [{"path": "", "code": "not_an_object", "message": "library must be a JSON object"}], {}
    if raw.get("schemaVersion") != STORAGE_VERSION:
        err("schemaVersion", "unsupported_schema",
            f"expected schemaVersion {STORAGE_VERSION}")
    groups = raw.get("groups") or []
    channels = raw.get("channels") or []
    group_ids = set()
    def quiet_bad(message: str) -> Exception:
        return ValueError(str(message).split("is corrupt: ")[-1])

    for i, group in enumerate(groups):
        try:
            _require_group(group, i, quiet_bad)
        except ValueError as exc:
            err(f"groups[{i}]", "bad_group", str(exc))
            continue
        group_ids.add(group["id"])
    for i, channel in enumerate(channels):
        try:
            _require_channel(channel, i, quiet_bad)
        except ValueError as exc:
            err(f"channels[{i}]", "bad_channel", str(exc))
            continue
        if channel.get("groupId") not in group_ids:
            err(f"channels[{i}].groupId", "unknown_group",
                f"references group {channel.get('groupId')!r}")
    return errors, raw


# ---------------------------------------------------------------------------
# Saving + locking
# ---------------------------------------------------------------------------


def save(data_dir: str | Path, library: dict) -> None:
    path = library_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_storable_shape(library, path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(
            json.dumps(library, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


@contextlib.contextmanager
def library_lock(data_dir: str | Path, timeout: float = 60.0):
    """Process-safe exclusive lock for the read-check-write commit section."""
    lock_path = Path(data_dir) / ".library.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LibraryError(
                        "another writer is holding the library lock; try again",
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

_OK_OPS = ("channel.put", "channel.create", "channel.swap", "group.put",
           "group.delete", "channels.move", "channels.patch")


def apply_transaction(
    data_dir: str | Path,
    *,
    expected_revision: int,
    request_id: str,
    ops: list[dict],
    actor: str = "editor",
) -> dict:
    """Commit a touched-records transaction atomically; return the receipt.

    Idempotent: a repeat of the same ``requestId`` replays its stored receipt
    without re-applying — unless the payload digest differs, which is
    rejected (a reused id with different content is a client bug). The
    receipt is part of the document, so the commit and its acknowledgement
    become visible together.

    Identity: existing channels never change id/seed/kind (a draft that
    carries a different seed is rejected); created channels get server-assigned
    ids/seeds recorded in the receipt's ``idMap`` keyed by the client's tempId.
    """
    if not isinstance(request_id, str) or not request_id.strip():
        raise LibraryError("requestId is required")
    digest = _digest({"expected": expected_revision, "ops": ops})
    with library_lock(data_dir):
        library = load(data_dir)
        for receipt in library.get("recentRequests", []):
            if receipt.get("requestId") != request_id:
                continue
            if receipt.get("digest") != digest:
                return {
                    "requestId": request_id, "status": "rejected",
                    "error": "request_replayed_with_different_content",
                    "message": "this requestId was already used with a different payload",
                    "revision": library["revision"],
                }
            return dict(receipt)
        errors = _check_ops(library, ops)
        if errors:
            return _finish(library, data_dir, request_id, digest, {
                "requestId": request_id, "status": "rejected",
                "error": "validation_failed", "errors": errors,
                "revision": library["revision"],
            })
        if expected_revision != library["revision"]:
            return _finish(library, data_dir, request_id, digest, {
                "requestId": request_id, "status": "rejected",
                "error": "revision_conflict",
                "message": f"transaction expects revision {expected_revision}, "
                           f"server has {library['revision']}",
                "currentRevision": library["revision"],
            })
        try:
            id_map = _apply_ops(library, ops)
        except _OpError as exc:
            return _finish(library, data_dir, request_id, digest, {
                "requestId": request_id, "status": "rejected",
                "error": exc.code, "message": str(exc),
                "revision": library["revision"],
            })
        library["revision"] += 1
        receipt = {
            "requestId": request_id, "status": "committed",
            "revision": library["revision"], "digest": digest,
            "appliedAt": _now_iso(), "actor": actor,
            "touched": len(ops),
            **({"idMap": id_map} if id_map else {}),
        }
        return _finish(library, data_dir, request_id, digest, receipt)


class _OpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _finish(library: dict, data_dir: str | Path, request_id: str, digest: str,
            receipt: dict) -> dict:
    """Persist the receipt WITH the document (one atomic save), archive
    history, and hand a copy back."""
    receipts = [dict(receipt), *(
        r for r in library.get("recentRequests", []) if r.get("requestId") != request_id
    )][:RECEIPTS_KEPT]
    library["recentRequests"] = receipts
    save(data_dir, library)
    _append_history(data_dir, library)
    return dict(receipt)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_ops(library: dict, ops: list[dict]) -> list[dict]:
    """Static validation of the ops against the CURRENT document (identity,
    references, band and uniqueness constraints). Number-uniqueness for puts
    is re-checked under the lock inside _apply_ops too — this pass exists so
    drafts get precise field errors before any revision check."""
    errors: list[dict] = []

    def err(op_i: int, path: str, code: str, message: str) -> None:
        errors.append({"path": f"ops[{op_i}].{path}", "code": code, "message": message})

    by_id = {c["id"]: c for c in library["channels"]}
    groups = {g["id"]: g for g in library["groups"]}
    new_groups: dict[str, str] = {}
    swap_pairs = {
        frozenset((op.get("a"), op.get("b")))
        for op in ops if isinstance(op, dict) and op.get("op") == "channel.swap"
    }
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in _OK_OPS:
            err(i, "op", "unknown_op", f"op must be one of: {', '.join(_OK_OPS)}")
            continue
        kind = op["op"]
        if kind == "channel.put":
            channel = op.get("channel")
            if not isinstance(channel, dict):
                err(i, "channel", "not_an_object", "channel.put needs a channel object")
                continue
            existing = by_id.get(channel.get("id"))
            if existing is not None:
                if channel.get("seed") is not None and channel["seed"] != existing["seed"]:
                    err(i, "channel.seed", "identity_change",
                        f"seed is immutable for {channel['id']}")
                if channel.get("kind") is not None and channel["kind"] != existing["kind"]:
                    err(i, "channel.kind", "identity_change",
                        f"kind is immutable for {channel['id']}")
            _check_channel_draft(i, channel, by_id, groups, new_groups, err,
                                 swap_pairs=swap_pairs, creating=False)
        elif kind == "channel.create":
            channel = op.get("channel") or {}
            temp_id = op.get("tempId")
            if not isinstance(temp_id, str) or not temp_id.strip():
                err(i, "tempId", "bad_temp_id", "channel.create needs a tempId")
            if channel.get("id") not in (None, ""):
                err(i, "channel.id", "identity_change",
                    "created channels must not carry an id (the server assigns it)")
            _check_channel_draft(i, channel, by_id, groups, new_groups, err,
                                 swap_pairs=swap_pairs, creating=True)
        elif kind == "channel.swap":
            a, b = by_id.get(op.get("a")), by_id.get(op.get("b"))
            if a is None or b is None:
                err(i, "a/b", "unknown_channel", "swap needs two existing channel ids")
            elif a["id"] == b["id"]:
                err(i, "a/b", "bad_swap", "cannot swap a channel with itself")
            elif a["kind"] != b["kind"]:
                err(i, "a/b", "bad_swap", "swaps stay within one number band")
        elif kind == "group.put":
            group = op.get("group")
            if not isinstance(group, dict):
                err(i, "group", "not_an_object", "group.put needs a group object")
                continue
            gid = group.get("id")
            if gid is not None and gid not in groups and not GRP_ID_RE.match(str(gid)):
                err(i, "group.id", "bad_group_id", "group ids look like grp_…")
            name = group.get("name")
            if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME_LEN):
                err(i, "group.name", "bad_name", f"group name must be 1-{MAX_NAME_LEN} characters")
            else:
                norm = name.strip().casefold()
                clash = any(
                    existing["name"].strip().casefold() == norm
                    for existing_id, existing in groups.items()
                    if existing_id != gid
                ) or any(
                    pending.strip().casefold() == norm
                    for pending_id, pending in new_groups.items() if pending_id != gid
                )
                if clash:
                    err(i, "group.name", "duplicate_group", f"a group named {name.strip()!r} exists")
            position = group.get("position")
            if isinstance(position, bool) or not isinstance(position, int) or position < 1:
                err(i, "group.position", "bad_position", "position must be a positive integer")
            new_groups[gid or "__new__"] = name if isinstance(name, str) else ""
        elif kind == "group.delete":
            gid = op.get("id")
            if gid not in groups:
                err(i, "id", "unknown_group", f"no group {gid!r}")
            elif len(groups) == 1:
                err(i, "id", "last_group", "cannot remove the last group")
            else:
                members = sum(1 for c in library["channels"] if c["groupId"] == gid)
                if members and op.get("moveTo") not in groups and op.get("moveTo") is None:
                    err(i, "moveTo", "destination_required",
                        f"choose a destination for {members} channel(s)")
        elif kind == "channels.move":
            if op.get("groupId") not in groups:
                err(i, "groupId", "unknown_group", "target group must exist")
            ids_ = op.get("channelIds")
            if not isinstance(ids_, list) or not ids_:
                err(i, "channelIds", "bad_channel_ids", "needs a non-empty channel id list")
            else:
                missing = [cid for cid in ids_ if cid not in by_id]
                if missing:
                    err(i, "channelIds", "unknown_channel",
                        f"no such channel(s): {', '.join(missing[:5])}"
                        + (f" +{len(missing) - 5}" if len(missing) > 5 else ""))
    for i, op in enumerate(ops):
        if isinstance(op, dict) and op.get("op") == "channels.patch":
            missing = [cid for cid in op.get("channelIds", []) if cid not in by_id]
            if missing:
                err(i, "channelIds", "unknown_channel",
                    f"no such channel(s): {', '.join(missing[:5])}"
                    + (f" +{len(missing) - 5}" if len(missing) > 5 else ""))
        elif kind == "channels.patch":
            ids_ = op.get("channelIds")
            patch = op.get("patch")
            if not isinstance(ids_, list) or not ids_:
                err(i, "channelIds", "bad_channel_ids", "needs a non-empty channel id list")
            if not isinstance(patch, dict) or not patch \
                    or any(k not in PATCHABLE_FLAGS for k in patch):
                err(i, "patch", "bad_patch",
                    f"patch may only set: {', '.join(PATCHABLE_FLAGS)}")
            elif any(not isinstance(v, bool) for v in patch.values()):
                err(i, "patch", "bad_patch", "patch values must be booleans")
    return errors


def _check_channel_draft(i, channel, by_id, groups, new_groups, err, *,
                         creating: bool, swap_pairs=frozenset()) -> None:
    if creating:
        channel = dict(channel)
        channel.setdefault("kind", "net")
    kind = channel.get("kind")
    if kind not in BANDS:
        err(i, "channel.kind", "bad_kind", "kind must be 'ch' or 'net'")
        return
    name = channel.get("name")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME_LEN):
        err(i, "channel.name", "bad_name", f"name must be 1-{MAX_NAME_LEN} characters")
    gid = channel.get("groupId")
    if gid not in groups and gid not in new_groups:
        err(i, "channel.groupId", "unknown_group", "choose a group that exists")
    number = channel.get("number")
    lo, hi = BANDS[kind]
    if isinstance(number, bool) or not isinstance(number, int) or not (lo <= number <= hi):
        err(i, "channel.number", "bad_number", f"number must be {lo}-{hi}")
    else:
        holder = next((c for c in by_id.values() if c["number"] == number), None)
        swapped = op_swap_targets(i, by_id) if False else None
        if holder is not None and holder["id"] != channel.get("id") \
                and frozenset((channel.get("id"), holder["id"])) not in swap_pairs:
            err(i, "channel.number", "duplicate_number",
                f"channel {number} is taken by {holder['name']}; use Swap")
    color = channel.get("color")
    if color is not None and (not isinstance(color, str) or not COLOR_RE.match(color)):
        err(i, "channel.color", "bad_color", "color must be #RRGGBB")
    if channel.get("glyph") is not None and channel["glyph"] not in contract.GLYPHS:
        err(i, "channel.glyph", "unknown_glyph", "glyph is not in the shared glyph set")
    if channel.get("sort") is not None and channel["sort"] not in contract.SORTS:
        err(i, "channel.sort", "unknown_sort", f"sort must be one of: {', '.join(contract.SORTS)}")
    source = channel.get("source")
    if not isinstance(source, dict):
        err(i, "channel.source", "bad_source", "source must be a JSON object")
    else:
        for e in criteria.validate(source):
            err(i, f"channel.{e['path']}", e["code"], e["message"])
    for flag in ("enabled", "archived", "paused"):
        v = channel.get(flag)
        if v is not None and not isinstance(v, bool):
            err(i, f"channel.{flag}", "bad_flag", f"{flag} must be a boolean")
    programming = channel.get("programming")
    if programming is not None:
        if not isinstance(programming, dict):
            err(i, "channel.programming", "bad_programming", "programming must be a JSON object")
        else:
            from justwatch.programming import policy as policy_shape
            mode = programming.get("mode")
            if mode is not None and mode not in ("fixed", "explore", "discovery", "continuing"):
                err(i, "channel.programming.mode", "bad_mode",
                    "mode must be fixed/explore/discovery/continuing")
            policy_shape(programming)  # tolerant clamp; shape errors surface via mode check


def _apply_ops(library: dict, ops: list[dict]) -> dict:
    """Mutate ``library`` in place. Assumes _check_ops passed. Raises _OpError
    on races impossible to see statically (number taken mid-transaction)."""
    by_id = {c["id"]: c for c in library["channels"]}
    groups = {g["id"]: g for g in library["groups"]}
    id_map: dict[str, str] = {}

    # group.put first so channel puts can reference brand-new groups
    for op in ops:
        if op["op"] == "group.put":
            group = dict(op["group"])
            gid = group.get("id")
            if gid is None:
                gid = new_group_id()
                while gid in groups:
                    gid = new_group_id()
                group["id"] = gid
            existing = groups.get(gid)
            if existing is not None:
                group.setdefault("legacySection", existing.get("legacySection"))
            group.setdefault("legacySection", None)
            position = group.get("position")
            if not isinstance(position, int) or isinstance(position, bool):
                position = (max((g["position"] for g in library["groups"]), default=0)) + 1
                group["position"] = position
            groups[gid] = group
    for op in ops:
        kind = op["op"]
        if kind == "channel.put":
            channel = dict(op["channel"])
            stored = by_id[channel["id"]]
            # identity + provenance are server-owned on update
            channel["id"] = stored["id"]
            channel["seed"] = stored["seed"]
            channel["kind"] = stored["kind"]
            channel["provenance"] = stored.get("provenance")
            _finalize_channel(channel, stored)
            by_id[channel["id"]] = channel
        elif kind == "channel.create":
            channel = dict(op["channel"])
            kind_ns = channel.get("kind") or "net"
            new_id = new_channel_id(kind_ns)
            while new_id in by_id:
                new_id = new_channel_id(kind_ns)
            channel["id"] = new_id
            channel["seed"] = secrets.randbelow(MAX_SEED + 1)
            channel["kind"] = kind_ns
            channel.setdefault("provenance", {"origin": "created"})
            channel.setdefault("enabled", True)
            channel.setdefault("archived", False)
            channel.setdefault("paused", False)
            channel.setdefault("color", "#455A64")
            channel.setdefault("sort", "shuffle")
            _finalize_channel(channel, None)
            by_id[new_id] = channel
            id_map[str(op.get("tempId"))] = new_id
        elif kind == "channel.swap":
            a, b = by_id[op["a"]], by_id[op["b"]]
            a["number"], b["number"] = b["number"], a["number"]
        elif kind == "channels.move":
            for cid in op["channelIds"]:
                if cid in by_id:
                    by_id[cid]["groupId"] = op["groupId"]
        elif kind == "channels.patch":
            for cid in op["channelIds"]:
                if cid in by_id:
                    by_id[cid].update(op["patch"])
        elif kind == "group.delete":
            gid = op["id"]
            dest = op.get("moveTo")
            if dest is not None:
                for c in by_id.values():
                    if c["groupId"] == gid:
                        c["groupId"] = dest
            del groups[gid]
    # renumber positions densely after possible deletes
    for pos, gid in enumerate(sorted(groups, key=lambda g: (groups[g]["position"], g)), start=1):
        groups[gid]["position"] = pos
    library["groups"] = sorted(groups.values(), key=lambda g: g["position"])
    for channel in by_id.values():
        if channel["groupId"] not in groups:
            raise _OpError("unknown_group",
                           f"channel {channel['id']} lost its group {channel['groupId']!r}")
    numbers: dict[int, str] = {}
    for channel in by_id.values():
        lo, hi = BANDS[channel["kind"]]
        if not (lo <= channel["number"] <= hi):
            raise _OpError("bad_number", f"{channel['id']} number {channel['number']} outside its band")
        holder = numbers.get(channel["number"])
        if holder is not None:
            raise _OpError("duplicate_number",
                           f"channel {channel['number']} now held by both {holder} and {channel['id']}")
        numbers[channel["number"]] = channel["id"]
    library["channels"] = sorted(by_id.values(), key=lambda c: c["number"])
    return id_map


def _finalize_channel(channel: dict, stored: dict | None) -> None:
    """Fill the canonical stored form of a channel (source canonicalization,
    defaults), preserving anything the transaction did not touch."""
    channel["source"] = criteria.normalize(channel.get("source") or {})
    channel["sourceLabel"] = str(channel.get("sourceLabel") or "")
    channel.setdefault("glyph", None)
    channel.setdefault("color", "#455A64")
    channel.setdefault("sort", "shuffle")
    channel.setdefault("enabled", True)
    channel.setdefault("archived", False)
    channel.setdefault("paused", False)
    channel.setdefault("groupId", "")  # _require_channel would have failed earlier
    programming = channel.get("programming")
    if isinstance(programming, dict):
        from justwatch.programming import policy as policy_shape
        channel["programming"] = policy_shape(programming)
    else:
        channel["programming"] = None


def next_free_number(library: dict, kind: str) -> int | None:
    lo, hi = BANDS[kind]
    taken = {c["number"] for c in library["channels"]}
    for n in range(lo, hi + 1):
        if n not in taken:
            return n
    return None


# ---------------------------------------------------------------------------
# Receipts (read path) + history
# ---------------------------------------------------------------------------


def get_receipt(data_dir: str | Path, request_id: str) -> dict | None:
    """One stored receipt by requestId (the Apply-result lookup)."""
    library = load(data_dir)
    for receipt in library.get("recentRequests", []):
        if receipt.get("requestId") == request_id:
            return dict(receipt)
    return None


def _append_history(data_dir: str | Path, library: dict) -> None:
    """Auxiliary revision archive. Written AFTER the primary save; failure is
    logged and swallowed — the library document is the recovery authority and
    the next revision re-appends."""
    try:
        hdir = history_dir(data_dir)
        hdir.mkdir(parents=True, exist_ok=True)
        entry = {
            "revision": library["revision"],
            "savedAt": _now_iso(),
            "snapshot": {"groups": library["groups"], "channels": library["channels"]},
        }
        tmp = hdir / f".rev-{library['revision']}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        target = hdir / f"rev-{library['revision']:08d}.json"
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        revisions = sorted(hdir.glob("rev-*.json"))
        for stale in revisions[:-HISTORY_KEPT]:
            with contextlib.suppress(OSError):
                stale.unlink()
    except OSError:
        pass


def read_history(data_dir: str | Path, offset: int = 0, limit: int = 20) -> dict:
    """Bounded, newest-first revision summaries (definitions included for the
    requested page). Restoration is NOT a rewind: it submits a new Apply."""
    hdir = history_dir(data_dir)
    entries: list[dict] = []
    if hdir.exists():
        revisions = sorted(hdir.glob("rev-*.json"), reverse=True)
        for path in revisions[offset:offset + limit]:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            snapshot = raw.get("snapshot") or {}
            entries.append({
                "revision": raw.get("revision"),
                "savedAt": raw.get("savedAt"),
                "channelCount": len(snapshot.get("channels") or []),
                "groups": snapshot.get("groups") or [],
                "channels": snapshot.get("channels") or [],
            })
    return {"entries": entries, "offset": offset, "limit": limit}
