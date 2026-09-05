"""Minimal Stash GraphQL client for the justwatch plugin (stdlib only).

Slimmed from the stash-tag-curator client: no retries (every justwatch call is
short and user-interactive), no progress hooks. Authentication follows the
same proven pattern: ``SessionCookie`` from ``server_connection`` on every
call, plus ``ApiKey`` from plugin settings when present, falling back to the
``api_key`` in Stash's ``config.yml`` (Stash v0.31.1 does not inject saved
plugin settings into the raw envelope).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_UNSPECIFIED_HOSTS = {"0.0.0.0", "::", ""}
DEFAULT_TIMEOUT = 60.0


class GraphQLClientError(Exception):
    pass


class GraphQLAuthError(GraphQLClientError):
    pass


class GraphQLError(GraphQLClientError):
    def __init__(self, message: str, errors: list | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


def build_endpoint(connection: dict) -> str:
    scheme = str(connection.get("Scheme") or "http")
    host = str(connection.get("Host") or "localhost")
    if host in _UNSPECIFIED_HOSTS:
        host = "localhost"
    try:
        port = int(connection.get("Port") or 9999)
    except (TypeError, ValueError):
        port = 9999
    return f"{scheme}://{host}:{port}/graphql"


def read_api_key_from_config(stash_dir: str, plugin_dir: str) -> str | None:
    """Fall back to the server's own ``config.yml`` for the API key."""
    try:
        import yaml
    except ImportError:  # PyYAML is the sole optional dep; cookie auth still works
        return None
    candidates = []
    if stash_dir:
        candidates.append(os.path.join(stash_dir, "config.yml"))
    if plugin_dir:
        # Stash installs plugins under <stash>/plugins/<name>/
        candidates.append(os.path.join(plugin_dir, os.pardir, os.pardir, "config.yml"))
    for path in candidates:
        try:
            with open(os.path.abspath(path), encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh)
            key = cfg.get("api_key") if isinstance(cfg, dict) else None
            if isinstance(key, str) and key.strip():
                return key.strip()
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return None


class StashClient:
    def __init__(
        self,
        server_connection: dict,
        settings: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.endpoint = build_endpoint(server_connection or {})
        self.timeout = timeout
        cookie = (server_connection or {}).get("SessionCookie") or {}
        # Stash marshals a Go http.Cookie, whose exported fields are Name and
        # Value (capitalized). Older envelopes used lowercase keys; accept both.
        self.session_cookie = str(
            cookie.get("Value") or cookie.get("value") or "",
        )
        self.cookie_name = str(cookie.get("Name") or cookie.get("name") or "session")
        api_key = ""
        for source in (settings or {}, (server_connection or {})):
            value = source.get("ApiKey") or source.get("stash_api_key")
            if isinstance(value, str) and value.strip():
                api_key = value.strip()
                break
        if not api_key:
            api_key = read_api_key_from_config(
                str((server_connection or {}).get("Dir") or ""),
                str((server_connection or {}).get("PluginDir") or ""),
            ) or ""
        self.api_key = api_key

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        elif self.session_cookie:
            headers["Cookie"] = f"{self.cookie_name}={self.session_cookie}"
        return headers

    def submit(self, query: str, variables: dict | None = None) -> Any:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=payload, headers=self._headers(), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise GraphQLAuthError(
                    f"Stash rejected authentication (HTTP {exc.code})"
                ) from exc
            raise GraphQLClientError(f"HTTP {exc.code} from {self.endpoint}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GraphQLClientError(f"cannot reach Stash at {self.endpoint}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GraphQLClientError(f"non-JSON response from Stash: {exc}") from exc
        if body.get("errors"):
            message = body["errors"][0].get("message", "GraphQL error")
            raise GraphQLError(message, body["errors"])
        return body.get("data")
