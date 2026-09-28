#!/usr/bin/env python3
"""Slice-and-dice the library mirror for themed-channel candidates.

Computes every cross-section offline against library.db; prints a survey
report. The final channel lineup is validated against the live server by
validate_channels.py (same projection the runtime serves).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "library.db"

JAV_TAG = 9320


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def age_on(born: date, when: date) -> int:
    return when.year - born.year - ((when.month, when.day) < (born.month, born.day))


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------- JAV set
    jav_scene_ids = {r["scene_id"] for r in conn.execute(
        "SELECT scene_id FROM scene_tags WHERE tag_id = ?", (JAV_TAG,))}
    jav_studios = {r[0] for r in conn.execute("""
        SELECT DISTINCT studio_id FROM scenes WHERE id IN (%s)
        AND studio_id IS NOT NULL""" % ",".join(map(str, jav_scene_ids)))}
    for r in conn.execute("SELECT id FROM scenes WHERE studio_id IN (%s)"
                          % ",".join(map(str, jav_studios))):
        jav_scene_ids.add(r[0])
    print(f"JAV universe: {len(jav_scene_ids)} scenes, {len(jav_studios)} studios")
    jav_ids = ",".join(map(str, jav_scene_ids)) or "0"

    def q(sql: str, params=()) -> list[sqlite3.Row]:
        return conn.execute(sql, params).fetchall()

    def tag_count(tag_id: int, exclude_jav: bool = True) -> int:
        sql = (f"SELECT COUNT(DISTINCT st.scene_id) FROM scene_tags st "
               f"WHERE st.tag_id = {int(tag_id)}")
        if exclude_jav:
            sql += f" AND st.scene_id NOT IN ({jav_ids})"
        return q(sql)[0][0]

    def scenes_with_tags(*tag_ids: int, exclude_jav: bool = True) -> set[int]:
        """Scenes carrying ALL of the given tags."""
        placeholders = ",".join("?" * len(tag_ids))
        sql = (f"SELECT scene_id FROM scene_tags WHERE tag_id IN ({placeholders}) "
               f"GROUP BY scene_id HAVING COUNT(DISTINCT tag_id) = {len(tag_ids)}")
        rows = q(sql, tag_ids)
        ids = {r[0] for r in rows}
        return ids - jav_scene_ids if exclude_jav else ids

    print("\n=== SINGLE-TAG CHANNEL CANDIDATES (non-JAV) ===")
    families = {
        "KINK": "KINK:%", "THEME": "THEME:%", "ACT": "ACT:%", "CAST": "CAST:%",
        "DEMO": "DEMO:%", "PROD": "PROD:%", "WARD": "WARD:%", "SET": "SET:%",
        "BODY": "BODY:%",
    }
    for family, pattern in families.items():
        rows = q("SELECT id, name, scene_count FROM tags WHERE name LIKE ? "
                 "ORDER BY scene_count DESC", (pattern,))
        out = [(r["name"], tag_count(r["id"])) for r in rows]
        out = [(n, c) for n, c in out if c >= 25]
        print(f"\n-- {family} ({len(out)} tags >= 25 non-JAV scenes)")
        print("   " + "; ".join(f"{n}={c}" for n, c in out))

    print("\n=== KEY CROSS-SECTIONS (non-JAV, both tags required) ===")
    tagid = {r["name"]: r["id"] for r in q("SELECT id, name FROM tags")}
    combos = [
        ("Lesbian+BDSM", ["CAST: 2F", "KINK: Bondage"]),
        ("Lesbian+femdom-ish", ["CAST: 2F", "KINK: Dom/Sub"]),
        ("Lesbian+toys", ["CAST: 2F", "ACT: Sex toys"]),
        ("Femdom (pegging)", ["KINK: Pegging"]),
        ("MILF+step-family", ["AGE: 30-39 (F)", "THEME: Step-family"]),
        ("MILF+cheating", ["THEME: Cheating", "AGE: 30-39 (F)"]),
        ("Interracial+MILF", ["DEMO: Interracial", "AGE: 30-39 (F)"]),
        ("Interracial+teens", ["DEMO: Interracial", "AGE: 18-22 (F)"]),
        ("Bondage+hardcore", ["KINK: Bondage", "ACT: Anal sex"]),
        ("Spanking-only-ish", ["KINK: Spanking/impact"]),
        ("Anal+petite", ["ACT: Anal sex", "BODY: Petite"]),
        ("Anal+MILF", ["ACT: Anal sex", "AGE: 30-39 (F)"]),
        ("Teen+small", ["AGE: 18-22 (F)", "BODY: Petite"]),
        ("Teens+oldermen", ["AGE: 18-22 (F)", "AGE: 40-49 (M)"]),
        ("POV+blowjob", ["PROD: POV", "ACT: Blowjob"]),
        ("Narrative+cheating", ["PROD: Narrative", "THEME: Cheating"]),
        ("Amateur+interracial", ["PROD: Amateur", "DEMO: Interracial"]),
        ("Romance+married", ["THEME: Romance", "THEME: Married IRL"]),
        ("Outdoor+sex", ["SET: Outdoor"]),
        ("Massage+romance", ["THEME: Massage", "THEME: Romance"]),
        ("Cuckold+interracial", ["THEME: Cuckolding", "DEMO: Interracial"]),
        ("Gangbang+DP", ["ACT: DP"]),
        ("Bukkake", ["ACT: Bukkake"]),
        ("Squirting", ["ACT: Squirting"]),
        ("Fisting", ["ACT: Fisting"]),
        ("Latex+lesbian", ["WARD: Latex/leather", "CAST: 2F"]),
        ("Stockings+MILF", ["WARD: Stockings", "AGE: 30-39 (F)"]),
        ("Foot fetish", ["KINK: Foot fetish"]),
        ("Vintage-ish pre-2010", None),  # handled separately below
    ]
    for name, tags in combos:
        if tags is None:
            continue
        ids = scenes_with_tags(*(tagid[t] for t in tags))
        print(f"  {name}: {len(ids)}")

    pre2010 = q("SELECT COUNT(*) c FROM scenes WHERE date < '2010-01-01' "
                f"AND id NOT IN ({jav_ids})")[0]["c"]
    print(f"  Vintage-ish pre-2010: {pre2010}")

    print("\n=== ETHNICITY CROSS-SECTIONS (performer-attributed, non-JAV scenes) ===")
    eth_norm = """
      CASE UPPER(COALESCE(NULLIF(ethnicity,''),''))
        WHEN 'CAUCASIAN' THEN 'Caucasian' WHEN 'LATIN' THEN 'Latin'
        WHEN 'BLACK' THEN 'Black' WHEN 'ASIAN' THEN 'Asian'
        WHEN 'MIXED' THEN 'Mixed' WHEN 'INDIAN' THEN 'Indian'
        WHEN 'MIDDLE EASTERN' THEN 'Middle Eastern'
        WHEN '' THEN '(blank)' ELSE UPPER(COALESCE(ethnicity,'')) END"""
    rows = q(f"""
      SELECT {eth_norm} eth, p.gender, COUNT(DISTINCT sp.scene_id) scenes,
             COUNT(DISTINCT p.id) performers
      FROM performers p JOIN scene_performers sp ON sp.performer_id = p.id
      WHERE sp.scene_id NOT IN ({jav_ids})
      GROUP BY eth, p.gender HAVING scenes >= 50
      ORDER BY scenes DESC""")
    for r in rows:
        print(f"  {r['eth']:15s} {r['gender'] or '-':18s} scenes={r['scenes']:5d} performers={r['performers']}")

    print("\n  top FEMALE performers per ethnicity (non-JAV scenes):")
    for eth in ("Latin", "Black", "Asian", "Indian", "Middle Eastern", "Mixed"):
        rows = q(f"""
          SELECT p.id, p.name, p.birthdate, COUNT(DISTINCT sp.scene_id) scenes
          FROM performers p JOIN scene_performers sp ON sp.performer_id = p.id
          WHERE {eth_norm} = ? AND p.gender = 'FEMALE'
            AND sp.scene_id NOT IN ({jav_ids})
          GROUP BY p.id ORDER BY scenes DESC LIMIT 8""", (eth,))
        pretty = "; ".join(f"{r['name']}({r['id']},{r['scenes']})" for r in rows)
        print(f"  {eth:15s} {pretty}")

    print("\n=== PERFORMER SPOTLIGHTS (non-JAV scene counts, >= 80) ===")
    rows = q(f"""
      SELECT p.id, p.name, p.gender, p.birthdate, {eth_norm} eth,
             COUNT(DISTINCT sp.scene_id) scenes
      FROM performers p JOIN scene_performers sp ON sp.performer_id = p.id
      WHERE sp.scene_id NOT IN ({jav_ids})
      GROUP BY p.id HAVING scenes >= 80 ORDER BY scenes DESC""")
    for r in rows:
        print(f"  {r['name']:22s} id={r['id']:<5d} {r['gender'] or '-':8s} "
              f"bd={r['birthdate'] or '-':10s} {r['eth'] or '-':15s} scenes={r['scenes']}")

    print("\n=== AGE-DIFFERENCE (real ages at scene date) ===")
    perf = {r["id"]: parse_date(r["birthdate"]) for r in
            q("SELECT id, birthdate FROM performers")}
    genders = {r["id"]: r["gender"] for r in q("SELECT id, gender FROM performers")}
    buckets = {
        "any_gap>=10": 0, "any_gap>=15": 0, "any_gap>=20": 0,
        "M_older_F_gap>=15": 0, "F_older_M_gap>=10": 0,
        "olderM45+_youngerF25-": 0, "F45+_M_under30": 0,
    }
    scenes_rows = q(f"""
      SELECT s.id, s.date, s.created_at FROM scenes s
      WHERE s.date IS NOT NULL AND s.id NOT IN ({jav_ids})
        AND EXISTS (SELECT 1 FROM scene_performers sp WHERE sp.scene_id = s.id)""")
    sp_rows = q("SELECT scene_id, performer_id FROM scene_performers")
    by_scene: dict[int, list[int]] = {}
    for r in sp_rows:
        by_scene.setdefault(r["scene_id"], []).append(r["performer_id"])
    for r in scenes_rows:
        when = parse_date(r["date"])
        if not when:
            continue
        ages = []
        for pid in by_scene.get(r["id"], ()):
            born = perf.get(pid)
            if born and (when - born).days > 3650:  # age >= ~10 sanity
                ages.append((age_on(born, when), genders.get(pid)))
        if len(ages) < 2:
            continue
        fems = [a for a, g in ages if g == "FEMALE"]
        males = [a for a, g in ages if g == "MALE"]
        allv = [a for a, _ in ages]
        gap = max(allv) - min(allv)
        if gap >= 10:
            buckets["any_gap>=10"] += 1
        if gap >= 15:
            buckets["any_gap>=15"] += 1
        if gap >= 20:
            buckets["any_gap>=20"] += 1
        if fems and males:
            if max(males) - min(fems) >= 15:
                buckets["M_older_F_gap>=15"] += 1
            if min(males) >= 45 and max(fems) <= 25:
                buckets["olderM45+_youngerF25-"] += 1
            if min(males) <= 29 and max(fems) - min(males) >= 10:
                buckets["F_older_M_gap>=10"] += 1
            if max(fems) >= 45 and min(males) <= 30:
                buckets["F45+_M_under30"] += 1
    for k, v in buckets.items():
        print(f"  {k}: {v}")

    print("\n=== JAV THEME SLICE (tags over the JAV universe) ===")
    rows = q(f"""
      SELECT t.name, COUNT(DISTINCT st.scene_id) c FROM scene_tags st
      JOIN tags t ON t.id = st.tag_id
      WHERE st.scene_id IN ({jav_ids}) AND t.name NOT LIKE 'CURATOR%%'
      GROUP BY t.id ORDER BY c DESC LIMIT 45""")
    for r in rows:
        print(f"  {r['name']:35s} {r['c']}")

    print("\n=== STUDIO SPOTLIGHT CANDIDATES (non-JAV, >= 120 scenes) ===")
    rows = q(f"""
      SELECT studio_id, studio_name, COUNT(*) c FROM scenes
      WHERE studio_id IS NOT NULL AND id NOT IN ({jav_ids})
      GROUP BY studio_id HAVING c >= 120 ORDER BY c DESC""")
    for r in rows:
        print(f"  {r['studio_name']:35s} id={r['studio_id']:<5d} {r['c']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
