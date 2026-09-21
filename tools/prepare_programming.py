#!/usr/bin/env python3
"""Queue the hourly programming task. Credentials are read from a file, never argv.

By default this only QUEUES the task — a job id is not success. ``--verify``
waits for the queued job to finish and then reads the plugin's durable status
manifest (ProgrammingStatus) so the run's per-channel outcomes are reported
from what actually executed, not from the queue acknowledgement.
"""
import argparse
import json
import os
import time
from pathlib import Path
import urllib.request
from urllib.parse import urlparse


def post(url, api_key, payload, timeout=30):
    request = urllib.request.Request(url.rstrip("/") + "/graphql", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "ApiKey": api_key})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def query(url, api_key, document, variables=None):
    result = post(url, api_key, {"query": document, "variables": variables or {}})
    if result.get("errors"):
        raise SystemExit(f"GraphQL error: {result['errors']}")
    return result["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("STASH_URL"))
    parser.add_argument("--api-key-file", default=os.getenv("STASH_API_KEY_FILE"))
    parser.add_argument("--verify", action="store_true",
                        help="wait for the job and report durable per-channel outcomes")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="--verify gives up after this many seconds (default 300)")
    args = parser.parse_args()
    if not args.url or urlparse(args.url).scheme not in ("http", "https") or not args.api_key_file:
        parser.error("set STASH_URL and STASH_API_KEY_FILE, or supply --url and --api-key-file")
    api_key = Path(args.api_key_file).read_text().strip()

    queued = query(args.url, api_key, 'mutation { runPluginTask(plugin_id: "stash-justwatch", '
                       'task_name: "Prepare Programming", args_map: {mode: "PrepareProgramming"}) }')
    print("Programming task queued:", queued["runPluginTask"])
    if not args.verify:
        return

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        jobs = query(args.url, api_key,
                     "query { jobQueue { id status description } }").get("jobQueue") or []
        pending = [j for j in jobs if "Prepare Programming" in (j.get("description") or "")
                   and j.get("status") in ("READY", "RUNNING", "STOPPING", "QUEUED", "WAITING")]
        if not pending:
            break
        time.sleep(2)
    else:
        raise SystemExit("task still running after --timeout; check ProgrammingStatus later")

    status = query(args.url, api_key, 'query { runPluginOperation(plugin_id: "stash-justwatch", '
                    'args_map: {mode: "ProgrammingStatus"}) { output } }')
    output = (status.get("runPluginOperation") or {}).get("output") or {}
    last_run = output.get("lastRun") or {}
    print(json.dumps({
        "statusGeneratedAt": output.get("generatedAt"),
        "customChannels": last_run.get("customChannels"),
        "networkChannels": last_run.get("networkChannels"),
        "channels": {cid: {k: v for k, v in entry.items() if k in
                           ("mode", "coverageHours", "degraded", "expiring", "version")}
                     for cid, entry in (output.get("channels") or {}).items()},
    }, indent=2))
    failures = {k: v for group in ("customChannels", "networkChannels")
                for k, v in (last_run.get(group) or {}).items() if v != "ready"}
    if failures:
        raise SystemExit(f"preparation completed WITH failures: {json.dumps(failures)}")
    print("verified: preparation task completed and every channel reported ready")


if __name__ == "__main__":
    main()
