"""Catalog storage: the single source of truth for custom channels + settings.

The catalog lives in ``<stash Dir>/stash-justwatch-data/catalog.json`` -- the
plugin's own data directory, NOT the plugin install dir (updates replace that).
Every write is atomic (temp file + rename) because ops are short-lived
processes and Stash may run them concurrently.

``settings`` here mirror the TV app's per-server Just Watch preferences. When
the plugin is connected the TV app uses these; otherwise it falls back to its
own ServerPreferences.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from justwatch import contract

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

DATA_DIR_NAME = "stash-justwatch-data"
CATALOG_NAME = "catalog.json"

_ID_RE = re.compile(r"^ch_[0-9a-f]{8}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DIGITS_RE = re.compile(r"^[0-9]+$")

MIN_NAME_LEN = 1
MAX_NAME_LEN = 60
MAX_SEED = 2_147_483_647


class CatalogError(Exception):
    """Structural problem that prevents reading or writing a catalog."""


def new_channel_id() -> str:
    return f"ch_{secrets.token_hex(4)}"


def new_seed() -> int:
    return secrets.randbelow(MAX_SEED + 1)


def catalog_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / CATALOG_NAME


def load(data_dir: str | Path) -> dict:
    """Load the stored catalog, strictly.

    A missing file initializes a fresh empty catalog. A file that exists but is
    not a structurally valid catalog at THIS plugin's schema version raises
    ``CatalogError`` — never silently normalize it into an empty editable
    catalog, because the next save would make that loss permanent. The original
    bytes are left untouched for the operator.
    """
    path = catalog_path(data_dir)
    if not path.exists():
        return empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"catalog unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"catalog at {path} is not a JSON object")
    _require_storable_shape(raw, path)
    return normalize(raw)


def _require_storable_shape(raw: dict, path: Path) -> None:
    """Structural gate for stored catalogs: what must be true before any part
    of the file may be normalized, let alone rewritten."""

    def bad(message: str) -> CatalogError:
        return CatalogError(f"catalog at {path} is corrupt: {message}")

    version = raw.get("schemaVersion")
    if version != contract.SCHEMA_VERSION:
        raise bad(
            f"unsupported schemaVersion {version!r} (this plugin writes "
            f"{contract.SCHEMA_VERSION}); refusing to load or overwrite",
        )
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise bad(f"revision must be a non-negative integer, got {revision!r}")
    channels = raw.get("channels")
    if not isinstance(channels, list):
        raise bad(f"channels must be a list, got {type(channels).__name__}")
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise bad(f"channels[{i}] must be a JSON object, got {type(ch).__name__}")
        ch_id = ch.get("id")
        if not isinstance(ch_id, str) or not _ID_RE.match(ch_id):
            raise bad(f"channels[{i}].id {ch_id!r} is not a channel id (ch_XXXXXXXX)")
        number = ch.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or \
                not (contract.MIN_CHANNEL_NUMBER <= number <= contract.MAX_CHANNEL_NUMBER):
            raise bad(f"channels[{i}] ({ch_id}) has invalid number {number!r}")
        if not isinstance(ch.get("name"), str):
            raise bad(f"channels[{i}] ({ch_id}) has a non-string name")
        source = ch.get("source")
        if not isinstance(source, dict) or source.get("type") not in contract.SOURCE_TYPES \
                or not _DIGITS_RE.match(str(source.get("id", ""))):
            raise bad(f"channels[{i}] ({ch_id}) has an invalid source")
        if ch.get("sort") not in contract.SORTS:
            raise bad(f"channels[{i}] ({ch_id}) has unknown sort {ch.get('sort')!r}")
        if not _valid_seed(ch.get("seed")):
            raise bad(f"channels[{i}] ({ch_id}) is missing or has an invalid seed")
    settings = raw.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise bad("settings must be a JSON object")


def empty() -> dict:
    return {
        "schemaVersion": contract.SCHEMA_VERSION,
        "revision": 0,
        "settings": _deep_copy_settings(contract.DEFAULT_SETTINGS),
        "channels": [],
    }


def save(data_dir: str | Path, catalog: dict) -> None:
    path = catalog_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: concurrent writers must never share a temp file (a
    # shared name lets one process rename away another's half-written bytes).
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


@contextlib.contextmanager
def catalog_lock(data_dir: str | Path, timeout: float = 30.0):
    """Process-safe exclusive lock for the read-check-write save section.

    Stash's job queue serializes tasks, but sync operations and direct
    RunPluginOperation calls are not dispatched through it, so writers cannot
    assume they are the only process touching catalog.json. One lock file per
    data directory; advisory locks are held per open file handle, which makes
    them exclusive across processes (and across handles in one process).
    """
    lock_path = Path(data_dir) / ".catalog.lock"
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
                    raise CatalogError(
                        "another save is holding the catalog lock; try again",
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


def validate(raw: Any) -> tuple[list[dict], dict]:
    """Validate a catalog draft.

    Returns ``(errors, normalized)``. ``errors`` is a list of
    ``{path, code, message}``; when non-empty the draft must not be saved, but
    ``normalized`` is still returned (best-effort) so callers can echo a
    repaired shape. Settings problems are silently clamped (they are numeric
    ranges, not user errors); channel problems are reported.
    """
    errors: list[dict] = []

    def err(path: str, code: str, message: str) -> None:
        errors.append({"path": path, "code": code, "message": message})

    if not isinstance(raw, dict):
        return [{"path": "", "code": "not_an_object", "message": "catalog must be a JSON object"}], {}
    if raw.get("schemaVersion") != contract.SCHEMA_VERSION:
        err("schemaVersion", "unsupported_schema",
            f"expected schemaVersion {contract.SCHEMA_VERSION}")

    settings = _validate_settings(raw.get("settings"), err)
    channels, channel_ids, numbers = [], set(), {}
    raw_channels = raw.get("channels")
    if raw_channels is None:
        raw_channels = []
    if not isinstance(raw_channels, list):
        err("channels", "not_a_list", "channels must be a list")
        raw_channels = []

    for i, ch in enumerate(raw_channels):
        prefix = f"channels[{i}]"
        if not isinstance(ch, dict):
            err(prefix, "not_an_object", "channel must be a JSON object")
            continue
        ch_id = ch.get("id")
        if not isinstance(ch_id, str) or not _ID_RE.match(ch_id):
            err(f"{prefix}.id", "bad_id", "channel id must match ch_XXXXXXXX (lowercase hex)")
        elif ch_id in channel_ids:
            err(f"{prefix}.id", "duplicate_id", f"channel id {ch_id} appears more than once")
        else:
            channel_ids.add(ch_id)

        number = ch.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or \
                not (contract.MIN_CHANNEL_NUMBER <= number <= contract.MAX_CHANNEL_NUMBER):
            err(f"{prefix}.number", "bad_number",
                f"number must be an integer {contract.MIN_CHANNEL_NUMBER}-{contract.MAX_CHANNEL_NUMBER}")
        elif number in numbers:
            err(f"{prefix}.number", "duplicate_number",
                f"channel {number} is already taken by {numbers[number]}")
        else:
            numbers[number] = ch_id

        name = ch.get("name")
        if not isinstance(name, str) or not (MIN_NAME_LEN <= len(name.strip()) <= MAX_NAME_LEN):
            err(f"{prefix}.name", "bad_name", f"name must be 1-{MAX_NAME_LEN} characters")

        glyph = ch.get("glyph")
        if glyph not in contract.GLYPHS:
            err(f"{prefix}.glyph", "unknown_glyph", "glyph is not in the shared glyph set")

        color = ch.get("color")
        if not isinstance(color, str) or not _COLOR_RE.match(color):
            err(f"{prefix}.color", "bad_color", "color must be #RRGGBB")

        sort = ch.get("sort")
        if sort not in contract.SORTS:
            err(f"{prefix}.sort", "unknown_sort",
                f"sort must be one of: {', '.join(contract.SORTS)}")

        source = ch.get("source")
        if not isinstance(source, dict) or source.get("type") not in contract.SOURCE_TYPES:
            err(f"{prefix}.source", "bad_source",
                f"source.type must be one of: {', '.join(contract.SOURCE_TYPES)}")
        else:
            src_id = source.get("id")
            if not isinstance(src_id, (str, int)) or not _DIGITS_RE.match(str(src_id)):
                err(f"{prefix}.source.id", "bad_source_id", "source.id must be a numeric id")

        seed = ch.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed <= MAX_SEED):
            # Missing/invalid seeds are filled by normalize(), not an error.
            if seed is not None:
                err(f"{prefix}.seed", "bad_seed", f"seed must be an integer 0-{MAX_SEED}")

        if not isinstance(ch.get("enabled", True), bool):
            err(f"{prefix}.enabled", "bad_enabled", "enabled must be true or false")

    return errors, normalize(raw)


def normalize(raw: dict) -> dict:
    """Return the canonical on-disk shape: defaults filled, seeds generated,
    settings clamped, unknown top-level keys dropped.

    Tolerant by design: drafts (PreviewLineup payloads, editor working copies)
    may carry malformed fields; ``validate`` reports them as structured errors
    and normalization must never crash on them.
    """
    settings = _validate_settings(raw.get("settings"), lambda *a: None)
    channels = []
    for ch in raw.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        source = ch.get("source")
        source = source if isinstance(source, dict) else {}
        channels.append({
            "id": ch.get("id"),
            "number": ch.get("number"),
            "name": _as_text(ch.get("name")),
            "glyph": ch.get("glyph"),
            "color": _as_text(ch.get("color")).lower(),
            "source": {
                "type": source.get("type"),
                "id": str(source.get("id", "")),
            },
            "sourceLabel": _as_text(ch.get("sourceLabel")),
            "sort": ch.get("sort"),
            "seed": ch.get("seed") if _valid_seed(ch.get("seed")) else new_seed(),
            "enabled": bool(ch.get("enabled", True)),
            "programming": _programming_policy(ch.get("programming")),
        })
    channels.sort(key=lambda c: (c["number"] is None, c["number"] or 0))
    return {
        "schemaVersion": contract.SCHEMA_VERSION,
        "revision": raw.get("revision") if isinstance(raw.get("revision"), int) else 0,
        "settings": settings,
        "channels": channels,
    }


def _as_text(value: Any) -> str:
    """Stringly-typed fields survive any JSON garbage without crashing."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _valid_seed(seed: Any) -> bool:
    return not isinstance(seed, bool) and isinstance(seed, int) and 0 <= seed <= MAX_SEED


def _validate_settings(raw: Any, err) -> dict:
    """Clamp numeric thresholds (silent), validate enum + id lists."""
    raw = raw if isinstance(raw, dict) else {}

    def as_int(key: str, default: int, minimum: int) -> int:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            # tolerate numeric strings (args_map carries strings only)
            try:
                v = int(str(v).strip())
            except (TypeError, ValueError):
                return default
        return max(minimum, v)

    solo = as_int("soloThreshold", contract.DEFAULT_SETTINGS["soloThreshold"], 3)
    group = as_int("groupThreshold", contract.DEFAULT_SETTINGS["groupThreshold"], 2)
    group = min(group, solo - 1)  # the TV app clamps group below solo

    launch = raw.get("launchMode")
    if launch not in contract.LAUNCH_MODES:
        launch = contract.DEFAULT_SETTINGS["launchMode"]

    sections = ("general", "studios", "performers")
    include = raw.get("includeTags") if isinstance(raw.get("includeTags"), dict) else {}
    exclude = raw.get("excludeTags") if isinstance(raw.get("excludeTags"), dict) else {}

    def tag_lists(source: dict) -> dict:
        out = {}
        for section in sections:
            ids = source.get(section)
            if ids is None:
                ids = []
            if isinstance(ids, str):
                ids = [s.strip() for s in ids.split(",") if s.strip()]
            if not isinstance(ids, list):
                ids = []
            out[section] = [str(i) for i in ids if _DIGITS_RE.match(str(i))]
        return out

    return {
        "soloThreshold": solo,
        "groupThreshold": group,
        "launchMode": launch,
        "includeTags": tag_lists(include),
        "excludeTags": tag_lists(exclude),
    }


def _deep_copy_settings(settings: dict) -> dict:
    return json.loads(json.dumps(settings))


def _programming_policy(value):
    from justwatch.programming import policy
    return policy(value)
