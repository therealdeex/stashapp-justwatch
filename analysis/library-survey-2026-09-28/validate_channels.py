#!/usr/bin/env python3
"""Live-validate every channel proposal against the production Stash.

Reads channel-proposals.csv, projects each row exactly as the runtime does
(importer build_source -> lineup.build_scene_filter), asks the live server
for the count, and reports drift vs the CSV's exact_scene_count. Read-only.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

COUNT_QUERY = """
query SurveyCount($filter: FindFilterType!, $scene_filter: SceneFilterType!) {
  findScenes(filter: $filter, scene_filter: $scene_filter) { count }
}
"""
PAGE = {"page": 1, "per_page": 0}


def load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_channels", REPO / "tools" / "import_channels.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_channels"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.8.40:9999")
    key_file = Path(sys.argv[2] if len(sys.argv) > 2
                    else "~/.config/stash-justwatch/prod-api-key").expanduser()
    api_key = key_file.read_text(encoding="utf-8").strip()

    from justwatch.stash_client import StashClient
    parts = urlsplit(url if "://" in url else "http://" + url)
    client = StashClient(
        {"Scheme": parts.scheme, "Host": parts.hostname, "Port": parts.port},
        settings={"stash_api_key": api_key})

    importer = load_importer()
    from justwatch import lineup

    rows = list(csv.DictReader(open(
        Path(__file__).resolve().parent / "channel-proposals.csv",
        encoding="utf-8-sig")))
    print(f"validating {len(rows)} channels against {parts.scheme}://"
          f"{parts.hostname}:{parts.port}", file=sys.stderr)

    drifted = []
    for i, row in enumerate(rows, 1):
        try:
            source = importer.build_source(row)
            scene_filter = lineup.build_scene_filter(source)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"BUILD FAIL {row['channel_number']} {row['channel_name']}: {exc}",
                  file=sys.stderr)
            drifted.append((row, -1))
            continue
        data = client.submit(COUNT_QUERY, {"filter": PAGE, "scene_filter": scene_filter})
        live = int((data.get("findScenes") or {}).get("count") or 0)
        stored = int(row["exact_scene_count"])
        if live != stored:
            drifted.append((row, live))
            print(f"DRIFT {row['channel_number']} {row['channel_name']}: "
                  f"csv={stored} live={live}", file=sys.stderr)
        if i % 40 == 0:
            print(f"  {i}/{len(rows)} checked", file=sys.stderr)

    print(f"\nvalidated {len(rows)} channels: {len(drifted)} drifted")
    if drifted:
        for row, live in drifted:
            print(f"  {row['channel_number']} {row['channel_name']}: "
                  f"csv={row['exact_scene_count']} live={live}")
        return 1
    print("ALL CHANNEL COUNTS MATCH THE LIVE SERVER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
