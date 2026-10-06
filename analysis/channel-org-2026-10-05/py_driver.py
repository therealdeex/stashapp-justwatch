#!/usr/bin/env python3
"""One channel-ops command against a temp data dir, for the browser harness.

usage: py_driver.py <command> <data_dir> [request.json]

The request JSON may arrive as a file path (argv 3 — the normal path from the
harness, since pipe stdin deadlocks under some sandboxes) or on stdin.

The browser mock transport shells out here for every PreviewChannelArrangement,
ApplyChannelChanges/GetChannelApplyResult, ValidateChannelChanges,
GetChannelLibrary/GetChannelDefinition — so the flows run against the REAL
planner/validator/transaction code (justwatch.organization + justwatch.library
+ justwatch.channel_ops), not a JS re-implementation. Idioms (Ctx/FakeClient)
are copied from tests/test_arrangement_ui_bridge.py. State lives entirely in
the data dir (document + receipts + history), so each process sees exactly
what a real server would.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from justwatch import channel_ops, contract, library  # noqa: E402


class FakeClient:
    """Bridged from tests/test_arrangement_ui_bridge.py: answers findScenes
    (health/rotation) with a small fixed library; records queries."""

    def __init__(self, count: int = 7):
        self.count = count
        self.queries: list = []

    def submit(self, query, variables):
        self.queries.append(variables)
        rows = [
            {"id": str(i), "title": f"T{i}", "date": "2026-01-01",
             "studio": {"name": "S"}, "files": [{"duration": 600.0}]}
            for i in range(min(self.count, 50))]
        return {"findScenes": {"count": self.count, "scenes": rows}}


class Ctx:
    def __init__(self, data_dir: Path, client, args: dict):
        self.data_dir = data_dir
        self.assets_dir = data_dir / "assets"
        self.client = client
        self.args = args


def handle(cmd: str, data_dir: Path, args: dict):
    client = FakeClient()

    if cmd == "seed":
        library.save(data_dir, args)
        return {"revision": args["revision"], "channels": len(args["channels"])}

    ctx = Ctx(data_dir, client, args)
    if cmd == "capabilities":
        caps = contract.capabilities("browser-harness")
        if not args.get("arrangement", True):
            caps["features"].pop("arrangement", None)  # old-backend simulation
        return caps
    if cmd == "get_library":
        return channel_ops.op_get_channel_library(ctx)
    if cmd == "get_definition":
        return channel_ops.op_get_channel_definition(ctx)
    if cmd == "validate":
        return channel_ops.op_validate_channel_changes(ctx)
    if cmd == "preview":
        return channel_ops.op_preview_channel_arrangement(ctx)
    if cmd == "pool":
        return channel_ops.op_preview_channel_pool(ctx)
    if cmd == "apply":
        return channel_ops.op_apply_channel_changes(ctx)
    if cmd == "receipt":
        return channel_ops.op_get_channel_apply_result(ctx)
    if cmd == "history":
        return channel_ops.op_get_channel_history(ctx)
    if cmd == "refresh_status":
        return channel_ops.op_get_channel_refresh_status(ctx)
    if cmd == "rename":
        # An EXTERNAL writer (another tab): commit a pure rename of one
        # channel as a full-record channel.put, so the editor's three-way
        # rebase sees a server-side name change.
        doc = library.load(data_dir)
        channel = next((c for c in doc["channels"] if c["id"] == args["channelId"]), None)
        if channel is None:
            raise LookupError(f"no channel {args['channelId']!r}")
        put = dict(channel)
        put["name"] = args["name"]
        return channel_ops.op_apply_channel_changes(Ctx(
            data_dir, client, {
                "requestId": args.get("requestId") or "harness-rename-1",
                "expectedRevision": str(doc["revision"]),
                "ops": json.dumps([{"op": "channel.put", "channel": put}]),
                "actor": "harness-external",
            }))
    raise ValueError(f"unknown command {cmd!r}")


def main() -> int:
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: py_driver.py <command> <data_dir> [request.json]"}))
        return 2
    cmd, data_dir = sys.argv[1], Path(sys.argv[2])
    try:
        if len(sys.argv) > 3:
            raw = Path(sys.argv[3]).read_text(encoding="utf-8") or "{}"
        else:
            raw = sys.stdin.read() or "{}"
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError("request must be a JSON object")
        if cmd != "seed" and not library.exists(data_dir):
            raise ValueError(f"no seeded library at {data_dir}")
        result = handle(cmd, data_dir, args)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:  # plugin errors travel as ok:false, not crashes
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
