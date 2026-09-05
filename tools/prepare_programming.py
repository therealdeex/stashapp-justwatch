#!/usr/bin/env python3
"""Queue the hourly programming task. Credentials are read from a file, never argv."""
import argparse
import json
import os
from pathlib import Path
import urllib.request
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("STASH_URL"))
    parser.add_argument("--api-key-file", default=os.getenv("STASH_API_KEY_FILE"))
    args = parser.parse_args()
    if not args.url or urlparse(args.url).scheme not in ("http", "https") or not args.api_key_file:
        parser.error("set STASH_URL and STASH_API_KEY_FILE, or supply --url and --api-key-file")
    payload = {"query": 'mutation { runPluginTask(plugin_id: "stash-justwatch", task_name: "Prepare Programming", args_map: {mode: "PrepareProgramming"}) }'}
    request = urllib.request.Request(args.url.rstrip("/") + "/graphql", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "ApiKey": Path(args.api_key_file).read_text().strip()})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("errors"):
        raise SystemExit("Programming task could not be queued; check Stash plugin availability.")
    print("Programming task queued:", result["data"]["runPluginTask"])


if __name__ == "__main__":
    main()
