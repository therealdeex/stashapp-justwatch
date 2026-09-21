#!/usr/bin/env python3
"""Queue the hourly programming task. Credentials are read from a file, never argv.

By default this only QUEUES the task — a job id is not success. ``--verify``
generates a run id, passes it into the task, waits for THAT job to finish, and
then reads the plugin's durable status manifest (ProgrammingStatus) until the
entry carrying OUR run id appears. An unrelated older successful run can never
satisfy verification, and per-channel outcomes are reported from what actually
executed, not from the queue acknowledgement.
"""
import argparse
import json
import os
import sys
import time
import uuid
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


#: Outcomes that mean the channel prepared successfully this run.
SUCCESS = ("ready", "recovered_from_outage", "indexed_empty")
#: Named operational states that are neither success nor failure.
DEFERRED = ("deferred_index_budget", "backoff", "changed_during_build",
            "deactivated_during_build")


def outcome_class(outcome):
    if outcome in SUCCESS:
        return "success"
    if outcome in DEFERRED:
        return "deferred"
    return "failure"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("STASH_URL"))
    parser.add_argument("--api-key-file", default=os.getenv("STASH_API_KEY_FILE"))
    parser.add_argument("--verify", action="store_true",
                        help="wait for OUR run and report durable per-channel outcomes")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="--verify gives up after this many seconds (default 300)")
    args = parser.parse_args()
    if not args.url or urlparse(args.url).scheme not in ("http", "https") or not args.api_key_file:
        parser.error("set STASH_URL and STASH_API_KEY_FILE, or supply --url and --api-key-file")
    api_key = Path(args.api_key_file).read_text().strip()

    run_id = f"cli-{uuid.uuid4().hex[:12]}"
    queued = query(args.url, api_key,
                   'mutation($args: Map!) { runPluginTask(plugin_id: "stash-justwatch", '
                   'task_name: "Prepare Programming", args_map: $args) }',
                   {"args": {"mode": "PrepareProgramming", "runId": run_id}})
    job_id = queued["runPluginTask"]
    print("Programming task queued:", job_id, "runId:", run_id)
    if not args.verify:
        return

    # Correlate by job ID, never by description: a description poll can match
    # an unrelated queued/running job (or miss ours entirely).
    deadline = time.monotonic() + args.timeout
    while True:
        jobs = query(args.url, api_key,
                     "query { jobQueue { id status description } }").get("jobQueue") or []
        ours = next((j for j in jobs if str(j.get("id")) == str(job_id)), None)
        if ours is None or (ours.get("status") or "").upper() in ("FINISHED", "FAILED", "CANCELLED", "CANCELED"):
            break
        if time.monotonic() >= deadline:
            raise SystemExit("task still running after --timeout; check ProgrammingStatus later")
        time.sleep(2)

    # The run is only verified when the durable manifest carries OUR run id
    # with a finishedAt — absence of matching jobs proves nothing.
    status = None
    while time.monotonic() < deadline:
        status = query(args.url, api_key, 'mutation { runPluginOperation(plugin_id: "stash-justwatch", '
                       'args: {mode: "ProgrammingStatus"}) }').get("runPluginOperation") or {}
        last_run = status.get("lastRun") or {}
        if last_run.get("runId") == run_id and last_run.get("finishedAt"):
            break
        time.sleep(2)
    else:
        raise SystemExit(f"no status entry for run {run_id}; the task may have failed before "
                         "publishing status — check Stash logs")
    last_run = status["lastRun"]

    print(json.dumps({
        "runId": run_id,
        "jobStatus": (ours or {}).get("status"),
        "startedAt": last_run.get("startedAt"),
        "finishedAt": last_run.get("finishedAt"),
        "customChannels": last_run.get("customChannels"),
        "networkChannels": last_run.get("networkChannels"),
        "channels": {cid: {k: v for k, v in entry.items() if k in
                           ("mode", "published", "ready", "empty", "coverageHours",
                            "degraded", "expiring", "encore", "version")}
                     for cid, entry in (status.get("channels") or {}).items()},
    }, indent=2))

    outcomes = {**(last_run.get("customChannels") or {}), **(last_run.get("networkChannels") or {})}
    failures = {k: v for k, v in outcomes.items() if outcome_class(v) == "failure"}
    deferred = {k: v for k, v in outcomes.items() if outcome_class(v) == "deferred"}
    if deferred:
        print(f"deferred (will retry on a later run): {json.dumps(deferred)}", file=sys.stderr)
    # Readiness is separate from run success: a channel can be prepared yet
    # carry no airable coverage (empty source, expired publication).
    not_ready = sorted(
        cid for cid, entry in (status.get("channels") or {}).items()
        if entry.get("mode") in ("explore", "discovery", "continuing") and not entry.get("ready")
    )
    if not_ready:
        print(f"NOT ready to air (no live coverage): {json.dumps(not_ready)}", file=sys.stderr)
    if failures:
        raise SystemExit(f"preparation completed WITH failures: {json.dumps(failures)}")
    print("verified: run %s completed; %d channel(s) prepared, %d deferred, %d not ready"
          % (run_id, len(outcomes) - len(deferred) - len(failures), len(deferred), len(not_ready)))


if __name__ == "__main__":
    main()
