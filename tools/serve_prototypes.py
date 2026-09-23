#!/usr/bin/env python3
"""Serve the channel-curation prototypes on the fixed dev port (8462).

No-cache headers so prototype edits show up on refresh. Also publishes the
port over tailscale (`tailscale serve --bg --https=8462 8462`) so the URL
works across the tailnet as https://dev-lab2.manx-teeth.ts.net:8462/.

Usage:  python3 tools/serve_prototypes.py            # foreground
        python3 tools/serve_prototypes.py --tailscale  # also (re)publish
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import socketserver
import subprocess
import sys
from pathlib import Path

PORT = 8462
ROOT = Path(__file__).resolve().parent.parent / "prototypes" / "channel-curation"


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def publish_tailscale() -> bool:
    try:
        subprocess.run(
            ["tailscale", "serve", "--bg", "--https=8462", str(PORT)],
            check=True, capture_output=True, text=True, timeout=20,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"tailscale publish failed: {exc}\n")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tailscale", action="store_true", help="also publish via tailscale serve")
    args = ap.parse_args()

    handler = functools.partial(Handler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"channel-curation prototypes: http://localhost:{PORT}/ (Ctrl-C to stop)")
        if args.tailscale and publish_tailscale():
            print(f"tailscale: https://dev-lab2.manx-teeth.ts.net:{PORT}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
