"""Shared test fixtures: a fake Stash client + temp data dirs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from justwatch.stash_client import GraphQLClientError


class FakeClient:
    """Records submits; answers from a canned response map.

    Responses are keyed by a marker substring of the query so tests stay
    readable; a missing key raises like a real transport failure.
    """

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def submit(self, query: str, variables: dict | None = None) -> dict:
        self.calls.append((query, variables or {}))
        for marker, payload in self.responses.items():
            if marker in query:
                return json.loads(json.dumps(payload))  # deep copy
        raise GraphQLClientError(f"no canned response for query: {query[:60]}")

    def last_variables(self) -> dict:
        return self.calls[-1][1]


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient(responses={
        # Keys are markers matched against the query text; the operation names
        # (JustWatchLineup / JustWatchSavedFilter / JustWatchEntityName) are
        # unique per document, unlike the field names they share.
        "JustWatchLineup": {
            "findScenes": {
                "count": 2,
                "scenes": [
                    {"id": "10", "title": "Alpha", "duration": None,
                     "date": "2022-01-02", "studio": {"name": "Blue Blur"},
                     "files": [{"duration": 1200.5}],
                     "paths": {"preview": "http://localhost:9998/scene/10/preview"}},
                    {"id": "11", "title": "Beta", "files": [],
                     "paths": {"preview": "/scene/11/preview.svg"}},
                    {"id": "12", "title": "Gamma", "files": [{"duration": 300.0}],
                     "paths": {"preview": "/scene/12/preview.svg"}},
                ],
            },
        },
        "JustWatchSavedFilter": {
            "findSavedFilter": {
                "id": "7", "name": "Long runs", "mode": "SCENES",
                "find_filter": {"q": None},
                "object_filter": {"rating100": {"value": 4, "modifier": "GREATER_THAN"}},
            },
        },
        "JustWatchEntityName": {
            "findTag": {"name": "Outdoor"},
            "findPerformer": None,
            "findStudio": None,
            "findSavedFilter": {"name": "Long runs", "mode": "SCENES"},
        },
    })


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "stash-justwatch-data"


@pytest.fixture
def assets_dir(tmp_path: Path) -> Path:
    return tmp_path / "assets"


@pytest.fixture
def envelope(data_dir: Path, assets_dir: Path) -> dict:
    return {
        "server_connection": {
            "Scheme": "http", "Host": "localhost", "Port": 9999,
            "Dir": str(data_dir.parent), "PluginDir": str(assets_dir.parent),
        },
        "settings": {},
        "args": {},
    }
