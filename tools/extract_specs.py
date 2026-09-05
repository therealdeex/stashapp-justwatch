#!/usr/bin/env python3
"""Extract the curated Just Watch channel specs from the Android app source.

The single source of truth for the curated catalog is the TV app repo:

    app/src/main/java/com/github/damontecres/stashapp/features/justwatch/JustWatchChannelSpecs.kt

This script parses the Kotlin list literals (simple `Spec(n, "name", listOf(...))`
constructors) and writes ``justwatch/specs.json`` next to this file's package.
Re-run it whenever the app's curated catalog changes:

    python3 tools/extract_specs.py ~/dev/StashAppAndroidTV
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_APP_REPO = "/home/shahram/dev/StashAppAndroidTV"
SPECS_KT = (
    "app/src/main/java/com/github/damontecres/stashapp/"
    "features/justwatch/JustWatchChannelSpecs.kt"
)


def _parse_strings(block: str) -> list[str]:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', block)


def _parse_calls(text: str, constructor: str) -> list[dict]:
    """Parse `Constructor(n, "name", ..., listOf(...))` occurrences, handling
    multi-line calls by balancing parentheses from each match start."""
    out: list[dict] = []
    for match in re.finditer(re.escape(constructor) + r"\s*\(", text):
        start = match.end() - 1
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = text[start + 1 : end]

        # First integer = number.
        num_match = re.search(r"\b(\d+)\s*,", body)
        if not num_match:
            continue
        number = int(num_match.group(1))

        # Top-level string literals (description precedes the alias list where
        # present); the LAST top-level listOf(...) holds the aliases.
        list_start = body.rfind("listOf(")
        head = body[:list_start] if list_start >= 0 else body
        head_strings = _parse_strings(head)
        if len(head_strings) < 1:
            continue
        name = head_strings[0]
        description = head_strings[1] if len(head_strings) > 1 else ""
        aliases = _parse_strings(body[list_start:]) if list_start >= 0 else []

        out.append(
            {
                "number": number,
                "name": name,
                **({"description": description} if description or "GroupSpec" in constructor else {}),
                "aliases": aliases,
            },
        )
    return out


def main() -> int:
    app_repo = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP_REPO)
    source = (app_repo / SPECS_KT).read_text(encoding="utf-8")

    specs = {
        "tagChannels": _parse_calls(source, "TagChannelSpec"),
        "performerGroups": _parse_calls(source, "PerformerGroupSpec"),
        "studioGroups": _parse_calls(source, "StudioGroupSpec"),
        # Spillover channel display names (spec number 0 entries).
        "performerSpilloverName": next(
            (
                s["name"]
                for s in _parse_calls(source, "PerformerGroupSpec")
                if s["number"] == 0
            ),
            "Rookie Row",
        ),
        "studioSpilloverName": next(
            (
                s["name"]
                for s in _parse_calls(source, "StudioGroupSpec")
                if s["number"] == 0
            ),
            "Two-Reel Theater",
        ),
    }

    dest = Path(__file__).resolve().parent.parent / "justwatch" / "specs.json"
    dest.write_text(json.dumps(specs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {k: len(v) for k, v in specs.items() if isinstance(v, list)}
    print(f"wrote {dest} {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
