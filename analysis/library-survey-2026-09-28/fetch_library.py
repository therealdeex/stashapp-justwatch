#!/usr/bin/env python3
"""Bulk-export the production Stash library into a local SQLite mirror.

Read-only against the server; everything downstream (slicing, channel
proposals) runs against library.db so the survey is reproducible without
hammering prod. Credentials come from a file, never argv.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DB = HERE / "library.db"

SCENE_PAGE = """
query Survey($filter: FindFilterType!) {
  findScenes(filter: $filter) {
    count
    scenes {
      id title code date rating100 o_counter organized created_at
      files { duration }
      studio { id name }
      tags { id }
      performers { id }
    }
  }
}
"""

PERFORMER_PAGE = """
query Survey($filter: FindFilterType!) {
  findPerformers(filter: $filter) {
    count
    performers {
      id name disambiguation gender birthdate death_date ethnicity country
      eye_color hair_color height_cm weight measurements fake_tits
      career_start career_end favorite scene_count
    }
  }
}
"""

TAG_PAGE = """
query Survey($filter: FindFilterType!) {
  findTags(filter: $filter) {
    count
    tags { id name scene_count performer_count parents { id } children { id } }
  }
}
"""

STUDIO_PAGE = """
query Survey($filter: FindFilterType!) {
  findStudios(filter: $filter) {
    count
    studios { id name parent_studio { id } }
  }
}
"""


def submit(url: str, api_key: str, query: str, variables: dict, retries: int = 4) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, data=body, headers={
                "Apikey": api_key, "Content-Type": "application/json"})
            with urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            if data.get("errors"):
                raise RuntimeError(f"graphql errors: {data['errors'][:2]}")
            return data["data"]
        except Exception as exc:  # noqa: BLE001 - retry any transport hiccup
            last = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{retries} after error: {exc}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"query failed after {retries} tries: {last}")


def paged(url: str, key: str, api_key: str, query: str, page_size: int = 1000):
    page, total = 1, None
    while total is None or (page - 1) * page_size < total:
        data = submit(url, api_key, query, {
            "filter": {"page": page, "per_page": page_size, "sort": "id"}})
        bucket = data[key]
        total = int(bucket["count"])
        items = bucket[{"findScenes": "scenes",
                        "findPerformers": "performers",
                        "findTags": "tags",
                        "findStudios": "studios"}[key]]
        print(f"  {key} page {page}: {len(items)} rows (total {total})", file=sys.stderr)
        yield from items
        if not items:
            break
        page += 1


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.8.40:9999").rstrip("/")
    if not url.endswith("/graphql"):
        url += "/graphql"
    key_file = Path(sys.argv[2] if len(sys.argv) > 2
                    else "~/.config/stash-justwatch/prod-api-key").expanduser()
    api_key = key_file.read_text(encoding="utf-8").strip()

    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}/graphql"
    print(f"survey: fetching from {origin}", file=sys.stderr)

    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE scenes (id INTEGER PRIMARY KEY, title TEXT, code TEXT, date TEXT,
      duration REAL, rating100 INTEGER, o_counter INTEGER, organized INTEGER,
      created_at TEXT, studio_id INTEGER, studio_name TEXT);
    CREATE TABLE scene_tags (scene_id INTEGER, tag_id INTEGER,
      PRIMARY KEY (scene_id, tag_id));
    CREATE TABLE scene_performers (scene_id INTEGER, performer_id INTEGER,
      PRIMARY KEY (scene_id, performer_id));
    CREATE TABLE performers (id INTEGER PRIMARY KEY, name TEXT, disambiguation TEXT,
      gender TEXT, birthdate TEXT, death_date TEXT, ethnicity TEXT, country TEXT,
      eye_color TEXT, hair_color TEXT, height_cm INTEGER, weight REAL,
      measurements TEXT, fake_tits TEXT, career_start TEXT, career_end TEXT,
      favorite INTEGER, scene_count INTEGER);
    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, scene_count INTEGER,
      performer_count INTEGER);
    CREATE TABLE tag_parents (tag_id INTEGER, parent_id INTEGER,
      PRIMARY KEY (tag_id, parent_id));
    CREATE TABLE studios (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER);
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)

    n = 0
    for s in paged(origin, "findScenes", api_key, SCENE_PAGE):
        files = s.get("files") or []
        duration = max((f.get("duration") or 0) for f in files) if files else None
        studio = s.get("studio")
        conn.execute("INSERT OR REPLACE INTO scenes VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            int(s["id"]), s.get("title"), s.get("code"), s.get("date"), duration,
            s.get("rating100"), s.get("o_counter"), s.get("organized"),
            s.get("created_at"), int(studio["id"]) if studio else None,
            studio["name"] if studio else None))
        conn.executemany("INSERT OR IGNORE INTO scene_tags VALUES (?,?)",
                         [(int(s["id"]), int(t["id"])) for t in s.get("tags") or []])
        conn.executemany("INSERT OR IGNORE INTO scene_performers VALUES (?,?)",
                         [(int(s["id"]), int(p["id"])) for p in s.get("performers") or []])
        n += 1
        if n % 2000 == 0:
            conn.commit()
            print(f"  scenes {n}", file=sys.stderr)
    conn.commit()

    for p in paged(origin, "findPerformers", api_key, PERFORMER_PAGE):
        conn.execute("INSERT OR REPLACE INTO performers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            int(p["id"]), p.get("name"), p.get("disambiguation"), p.get("gender"),
            p.get("birthdate"), p.get("death_date"), p.get("ethnicity"),
            p.get("country"), p.get("eye_color"), p.get("hair_color"),
            p.get("height_cm"), p.get("weight"), p.get("measurements"),
            p.get("fake_tits"), p.get("career_start"), p.get("career_end"),
            1 if p.get("favorite") else 0, p.get("scene_count")))
    conn.commit()

    for t in paged(origin, "findTags", api_key, TAG_PAGE):
        conn.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?,?)", (
            int(t["id"]), t.get("name"), t.get("scene_count"),
            t.get("performer_count")))
        conn.executemany("INSERT OR IGNORE INTO tag_parents VALUES (?,?)",
                         [(int(t["id"]), int(parent["id"])) for parent in t.get("parents") or []])
    conn.commit()

    for st in paged(origin, "findStudios", api_key, STUDIO_PAGE):
        parent = st.get("parent_studio")
        conn.execute("INSERT OR REPLACE INTO studios VALUES (?,?,?)", (
            int(st["id"]), st.get("name"), int(parent["id"]) if parent else None))
    conn.commit()

    conn.execute("INSERT INTO meta VALUES ('fetched_at', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    conn.commit()
    for table in ("scenes", "scene_tags", "scene_performers", "performers", "tags", "studios"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
