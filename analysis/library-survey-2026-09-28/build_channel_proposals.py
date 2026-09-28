#!/usr/bin/env python3
"""Build the themed-channel proposal CSV from the library survey.

Each channel is defined as a readable spec (tag names, performer names,
studio names); this script resolves ids against library.db, counts with
the same semantics the runtime projects (tags ALL, exclusions ANY,
performers/studios ANY-of, date bounds), and emits:

* channel-proposals.csv  — importer-schema rows (validated live by
  validate_channels.py, which stamps authoritative counts)
* proposals-debug.txt    — per-channel counts for eyeballing
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "library.db"
OUT_CSV = HERE / "channel-proposals.csv"
DEBUG = HERE / "proposals-debug.txt"

JAV_TAG, JAV_NAME = 9320, "JAV"

# (number, name, family, block, tags_all, exclude_tags, performers_any,
#  studios_any, date_to, rationale)
SPEC: list[tuple] = []


def ch(num, name, family, block, tags=(), exclude_jav=True, performers=(),
       studios=(), date_to=None, why="", exclude_tags=(), exclude_studios=(),
       date_from=None):
    SPEC.append(dict(num=num, name=name, family=family, block=block,
                     tags=list(tags), exclude_jav=exclude_jav,
                     performers=list(performers), studios=list(studios),
                     date_to=date_to, date_from=date_from, why=why, exclude_tags=list(exclude_tags),
                     exclude_studios=list(exclude_studios)))


# ---------------------------------------------------------------- 100 themes
ch(100, "Real Couples", "theme", "Themes", ["THEME: Married IRL"],
   why="3.8k scenes of real-life married couples — the library's signature curation")
ch(101, "Cheating & Affairs", "theme", "Themes", ["THEME: Cheating"],
   why="1.4k affair scenes; +584 also carry the MILF age band")
ch(102, "Roleplay Theater", "theme", "Themes", ["THEME: Roleplay"])
ch(103, "Step-Family Secrets", "theme", "Themes", ["THEME: Step-family"])
ch(104, "Forbidden Taboo", "theme", "Themes", ["THEME: Taboo"])
ch(105, "Romance & Desire", "theme", "Themes", ["THEME: Romance"])
ch(106, "Voyeur Vision", "theme", "Themes", ["THEME: Voyeurism"])
ch(107, "First Timers", "theme", "Themes", ["THEME: First time"])
ch(108, "Wife Sharing", "theme", "Themes", ["THEME: Wife sharing"])
ch(109, "Cuckold Life", "theme", "Themes", ["THEME: Cuckolding"],
   why="363 core + 203 interracial-cuckold crossover")
ch(110, "Swingers Social", "theme", "Themes", ["THEME: Swingers"])
ch(111, "Massage Parlor", "theme", "Themes", ["THEME: Massage"])
ch(112, "Exhibition Nation", "theme", "Themes", ["THEME: Exhibitionism"])
ch(113, "Orgy Nights", "theme", "Themes", ["THEME: Orgy"])
ch(114, "Gangbang Club", "theme", "Themes", ["THEME: Gangbang"])
ch(115, "Reluctant Hearts", "theme", "Themes", ["THEME: Reluctance"])
ch(116, "Holiday Specials", "theme", "Themes", ["THEME: Holiday/seasonal"])

# ---------------------------------------------------------------- 120 kink
ch(120, "Impact Play", "kink", "Kink", ["KINK: Spanking/impact"])
ch(121, "Dom & Sub", "kink", "Kink", ["KINK: Dom/Sub"])
ch(122, "Total Humiliation", "kink", "Kink", ["KINK: Humiliation"])
ch(123, "Rope & Bondage", "kink", "Kink", ["KINK: Bondage"])
ch(124, "Bound & Drilled", "kink", "Kink", ["KINK: Bondage", "ACT: Anal sex"],
   why="Bondage that goes all the way — 1.1k hardcore subset")
ch(125, "Pegging Palace", "kink", "Kink", ["KINK: Pegging"],
   why="657 pegging scenes; 571 also Dom/Sub (femdom core)")
ch(126, "Chastity & Denial", "kink", "Kink", ["KINK: Chastity/orgasm control"])
ch(127, "Gagged & Restrained", "kink", "Kink", ["KINK: Gags/restraints"])
ch(128, "Breath Play", "kink", "Kink", ["KINK: Choking/breath"])
ch(129, "CBT Clinic", "kink", "Kink", ["KINK: CBT"])
ch(130, "Sensory Deprivation", "kink", "Kink", ["KINK: Sensory deprivation"])
ch(131, "Nipple Torment", "kink", "Kink", ["KINK: Nipple torment"])
ch(132, "Rough Trade", "kink", "Kink", ["KINK: Rough sex"])
ch(133, "Electro Sluts", "kink", "Kink", ["KINK: Electro play"])
ch(134, "Foot Fetish Feet", "kink", "Kink", ["KINK: Foot fetish"])
ch(135, "Latex & Leather", "kink", "Kink", ["WARD: Latex/leather"])
ch(136, "The Dungeon", "kink", "Kink", ["SET: Dungeon"])
ch(137, "Watersports", "kink", "Kink", ["KINK: Watersports"])

# ---------------------------------------------------------------- 140 cast
ch(140, "Girl-Girl", "cast", "Cast", ["CAST: 2F"],
   why="Every 2F scene in the library has no male performer — the true lesbian pool")
ch(141, "Teen Lesbians", "cast", "Cast", ["CAST: 2F", "AGE: 18-22 (F)"])
ch(142, "Lesbian MILFs", "cast", "Cast", ["CAST: 2F", "AGE: 30-39 (F)"])
ch(143, "Busty Lesbians", "cast", "Cast", ["CAST: 2F", "BODY: Big breasts"])
ch(144, "Lez Be Bound", "cast", "Cast", ["CAST: 2F", "KINK: Bondage"])
ch(145, "Solo Sessions", "cast", "Cast", ["CAST: 1F"])
ch(146, "Teen Solo", "cast", "Cast", ["CAST: 1F", "AGE: 18-22 (F)"])
ch(147, "Solo Toy Box", "cast", "Cast", ["CAST: 1F", "ACT: Sex toys"])
ch(148, "Threesome: FFM", "cast", "Cast", ["CAST: 1M2F"])
ch(149, "Teen FFM", "cast", "Cast", ["CAST: 1M2F", "AGE: 18-22 (F)"])
ch(150, "Threesome: MMF", "cast", "Cast", ["CAST: 2M1F"])
ch(151, "Interracial MMF", "cast", "Cast", ["CAST: 2M1F", "DEMO: Interracial"])
ch(152, "Group Sex", "cast", "Cast", ["CAST: Group"])
ch(153, "Interracial Orgies", "cast", "Cast", ["CAST: Group", "DEMO: Interracial"])

# ---------------------------------------------------------------- 155 age
ch(155, "Fresh Faces 18-22", "age", "Age", ["AGE: 18-22 (F)"],
   why="6.2k scenes with an 18-22 woman present")
ch(156, "MILF Manor", "age", "Age", ["AGE: 30-39 (F)"])
ch(157, "Mature 40s", "age", "Age", ["AGE: 40-49 (F)"])
ch(158, "Fabulous 50s", "age", "Age", ["AGE: 50-59 (F)"])
ch(159, "Silver Foxes", "age", "Age", ["AGE: 50-59 (M)"])
ch(160, "Age Gap: Teen & 40s Man", "age_gap", "Age",
   ["AGE: 18-22 (F)", "AGE: 40-49 (M)"],
   why="Real-age audit: 4,644 scenes have the man >=15y older; this band pair is the sharpest cut")
ch(161, "Age Gap: Teen & 50s Man", "age_gap", "Age",
   ["AGE: 18-22 (F)", "AGE: 50-59 (M)"])
ch(162, "Age Gap: 20s & 40s Man", "age_gap", "Age",
   ["AGE: 23-29 (F)", "AGE: 40-49 (M)"])
ch(163, "Age Gap: 20s & 50s Man", "age_gap", "Age",
   ["AGE: 23-29 (F)", "AGE: 50-59 (M)"])
ch(164, "MILF Loves Younger Men", "age_gap", "Age",
   ["AGE: 40-49 (F)", "AGE: 23-29 (M)"],
   why="Reverse-gap family; real-age audit found 499 older-woman scenes")
ch(165, "Cougar Prep: 30s & Teen Men", "age_gap", "Age",
   ["AGE: 30-39 (F)", "AGE: 18-22 (M)"])

# ---------------------------------------------------------------- 170 ethnicity
ch(170, "Interracial Nation", "ethnicity", "Ethnicity", ["DEMO: Interracial"],
   why="7k scenes (34% of library) — the library's defining cross-section")
ch(171, "Interracial Teens", "ethnicity", "Ethnicity",
   ["DEMO: Interracial", "AGE: 18-22 (F)"])
ch(172, "Interracial MILFs", "ethnicity", "Ethnicity",
   ["DEMO: Interracial", "AGE: 30-39 (F)"])
ch(173, "Interracial & Busty", "ethnicity", "Ethnicity",
   ["DEMO: Interracial", "BODY: Big breasts"])
ch(174, "Latinas", "ethnicity", "Ethnicity", ["DEMO: Latin Female"])
ch(175, "Latina MILFs", "ethnicity", "Ethnicity",
   ["DEMO: Latin Female", "AGE: 30-39 (F)"])
ch(176, "Asian Beauties", "ethnicity", "Ethnicity", ["DEMO: Asian Female"],
   why="Non-JAV Asian performers (JAV lives in its own block)")
ch(177, "Asian Teens", "ethnicity", "Ethnicity",
   ["DEMO: Asian Female", "AGE: 18-22 (F)"])
ch(178, "Black Beauties", "ethnicity", "Ethnicity", ["DEMO: Black Female"])
ch(179, "Black Teens", "ethnicity", "Ethnicity",
   ["DEMO: Black Female", "AGE: 18-22 (F)"])
ch(180, "Mixed-Race Beauties", "ethnicity", "Ethnicity", ["DEMO: Mixed Female"])
ch(181, "Indian & Middle Eastern", "ethnicity", "Ethnicity",
   performers=["Evelin Stone", "Yasmina Khan", "Jasmine Sherni", "Priya Rai",
               "Sahara Knite", "Reina Rae", "Aaliyah Yasin", "Aurora Oliveira",
               "Mistress Sophia Sahara", "Luna Lovely", "Aria Valencia",
               "Indra", "Delilah Dagger", "Aubry Babcock", "Edible Aubrey",
               "Daphne Rosen"],
   why="Tags too thin (67/53 scenes); performer ANY-list is the honest cut")

# ---------------------------------------------------------------- 185 body
ch(185, "Big & Bouncy", "body", "Body", ["BODY: Big breasts"])
ch(186, "Busty Teens", "body", "Body", ["BODY: Big breasts", "AGE: 18-22 (F)"])
ch(187, "Busty MILFs", "body", "Body", ["BODY: Big breasts", "AGE: 30-39 (F)"])
ch(188, "Natural & Young", "body", "Body", ["BODY: Natural breasts", "AGE: 18-22 (F)"])
ch(189, "Itty Bitty", "body", "Body", ["BODY: Small breasts"])
ch(190, "Petite cuties", "body", "Body", ["BODY: Petite"])
ch(191, "Petite Teens", "body", "Body", ["BODY: Petite", "AGE: 18-22 (F)"])
ch(192, "Curvy Worlds", "body", "Body", ["BODY: Curvy"])
ch(193, "Athletic Figures", "body", "Body", ["BODY: Athletic"])
ch(194, "Alt & Tattooed", "body", "Body", ["BODY: Tattooed", "BODY: Pierced"],
   why="6.1k both-tagged alt scenes (tattooed alone is 53% of the library)")
ch(195, "Hairy & Natural", "body", "Body", ["BODY: Hairy"])
ch(196, "Hairy Teens", "body", "Body", ["BODY: Hairy", "AGE: 18-22 (F)"])
ch(197, "Big Booty", "body", "Body", ["BODY: Big ass"])
ch(198, "Oiled & Shiny", "body", "Body", ["BODY: Oiled"])
ch(199, "Tiny Dancers", "body", "Body", ["BODY: Height <150cm (F)"],
   why="288 scenes under 150cm")
ch(200, "Nerdy Glasses", "body", "Body", ["WARD: Glasses"])
ch(201, "Tan Lines", "body", "Body", ["BODY: Tan lines"])
ch(202, "Freckled", "body", "Body", ["BODY: Freckles"])
ch(203, "BBW", "body", "Body", ["BODY: BBW"])

# ------------------------------------------------------------ 205 wardrobe/set
ch(205, "Lingerie Lounge", "wardrobe", "Look", ["WARD: Lingerie"])
ch(206, "Stockings & Garters", "wardrobe", "Look", ["WARD: Stockings"])
ch(207, "High Heels", "wardrobe", "Look", ["WARD: Heels"])
ch(208, "Pantyhose", "wardrobe", "Look", ["WARD: Pantyhose"])
ch(209, "Bikini Days", "wardrobe", "Look", ["WARD: Bikini"])
ch(210, "Uniform Girls", "wardrobe", "Look", ["WARD: Uniform"])
ch(211, "Office Attire", "wardrobe", "Look", ["WARD: Business"])
ch(212, "Great Outdoors", "setting", "Look", ["SET: Outdoor"])
ch(213, "Poolside", "setting", "Look", ["SET: Pool/water"])
ch(214, "Backseat Action", "setting", "Look", ["SET: Car"])
ch(215, "After Hours Office", "setting", "Look", ["SET: Office"])
ch(216, "Hotel Nights", "setting", "Look", ["SET: Hotel"])

# ------------------------------------------------------------ 218 production
ch(218, "Story Time", "production", "Production", ["PROD: Narrative"])
ch(219, "Amateur Hour", "production", "Production", ["PROD: Amateur"])
ch(220, "POV Central", "production", "Production", ["PROD: POV"])
ch(221, "Glamour Shots", "production", "Production", ["PROD: Glamour"])
ch(222, "Reality Check", "production", "Production", ["PROD: Reality"])
ch(223, "Interactive", "production", "Production", ["PROD: Interactive"])
ch(224, "Behind the Scenes", "production", "Production", ["PROD: Behind the scenes"])
ch(225, "The Interview", "production", "Production", ["PROD: Interview"])
ch(226, "Retro Vault", "production", "Production", date_from="1990-01-01", date_to="2009-12-31",
   why="734 pre-2010 scenes; importer's scene_date_to column")

# ---------------------------------------------------------- 230+ performancers
PERFORMERS = [
    (230, "Mick Blue"), (231, "Shane Diesel"), (232, "Keiran Lee"),
    (233, "Manuel Ferrara"), (234, "Xander Corvus"), (235, "Bobby Beefcake"),
    (236, "Danny Mountain"), (237, "Johnny Castle"), (238, "Tyler Nixon"),
    (239, "Ramon Nomar"), (240, "Ryan Madison"), (241, "Johnny Sins"),
    (242, "Christian Clay"), (243, "James Deen"), (244, "Seth Gamble"),
    (245, "Alberto Blanco"), (246, "Charles Dera"), (247, "Markus Dupree"),
    (248, "J Mac"), (249, "Scott Nails"), (250, "Steve Holmes"),
    (251, "Isiah Maxwell"),
    (252, "Anissa Kate"), (253, "Riley Reid"), (254, "Abella Danger"),
    (255, "Chloe Temple"), (256, "Kendra Lust"), (257, "Nicole Aniston"),
    (258, "Luna Star"), (259, "Romi Rain"), (260, "Marley Brinx"),
    (261, "Brandi Love"), (262, "Gabbie Carter"), (263, "Cory Chase"),
    (264, "Casey Calvert"), (265, "Rissa May"), (266, "Alexis Texas"),
    (267, "Jessie Rogers"), (268, "Brooklyn Chase"), (269, "Stacy Cruz"),
    (270, "Dani Daniels"), (271, "Krissy Lynn"), (272, "Mia Malkova"),
    (273, "Dillion Harper"), (274, "Aria Alexander"), (275, "Allie Haze"),
    (276, "Penny Barber"), (277, "Asa Akira"), (278, "April Olsen"),
    (279, "Rebel Lynn"), (280, "Alexa Grace"), (281, "Bridgette B"),
    (282, "Ariana Marie"), (283, "Abigail Mac"), (284, "Jennifer Mendez"),
    (285, "Valentina Nappi"), (286, "Bonnie Rotten"), (287, "Alex Coal"),
    (288, "Kylie Rocket"),
]
for num, name in PERFORMERS:
    ch(num, f"{name} TV", "performer_spotlight", "Performers", performers=[name])

# ------------------------------------------------------------- 290+ studios
STUDIOS = [
    (290, "Tushy"), (291, "Vixen"), (292, "Deeper"), (293, "Jules Jordan"),
    (294, "Babes"), (295, "Passion HD"), (296, "Pure Taboo"),
    (297, "Casting Couch X"), (298, "The White Boxxx"), (299, "Tushy Raw"),
    (300, "Elegant Angel"), (301, "Premium Bukkake"), (302, "Private Society"),
    (303, "HotwifeXXX"), (304, "Shane Diesel XXX"), (305, "Hot Milfs Fuck"),
    (306, "Mormon Girlz"), (307, "Penthouse Gold"), (308, "PornFidelity"),
    (309, "Deep Lush"), (310, "Dorcel Club"), (311, "Digital Playground"),
    (312, "Tonight's Girlfriend"), (313, "Real Wife Stories"),
    (314, "New Sensations"), (315, "Brazzers Exxtra"), (316, "RK Prime"),
    (317, "Evil Angel"), (318, "Big Naturals"), (319, "Sinful XXX"),
    (320, "Dirty Wives Club"), (321, "MILFs Like it Big"),
    (322, "Interracial Pass"), (323, "Pornstars Punishment"),
]
for num, name in STUDIOS:
    ch(num, name, "studio_spotlight", "Studios", studios=[name])

# Kink.com family keeps its own identity inside the studio block.
KINK_STUDIOS = [
    (330, "The Upper Floor"), (331, "Men In Pain"), (332, "Sex and Submission"),
    (333, "Fucked And Bound"), (334, "Divine Bitches"), (335, "Device Bondage"),
    (336, "The Training of O"),
]
for num, name in KINK_STUDIOS:
    ch(num, name, "studio_spotlight", "Studios", studios=[name])

# --------------------------------------------------------------- 850+ JAV
JAV_STUDIO_CHANNELS = [
    (850, "Madonna"), (851, "Nagae Style"), (852, "Hunter"),
    (853, "JET Eizo"), (854, "Otona No Drama"), (855, "Ure Comi"),
    (856, "long style"), (857, "Tameike Goro"), (858, "SOD Create"),
    (859, "BAZOOKA"),
]
for num, name in JAV_STUDIO_CHANNELS:
    ch(num, f"JAV: {name}", "jav_studio", "JAV", studios=[name], exclude_jav=False,
       why="Studio-code channel — every scene carries the label's DVD code")

ch(860, "JAV: Shared Wives", "jav_theme", "JAV", tags=["JAV", "THEME: Wife sharing"],
   exclude_jav=False)
ch(861, "JAV: Affairs", "jav_theme", "JAV", tags=["JAV", "THEME: Cheating"],
   exclude_jav=False)
ch(862, "JAV: Married Woman", "jav_theme", "JAV", tags=["JAV", "Married Woman"],
   exclude_jav=False,
   why="Legacy tag well-curated on JAV imports (141 scenes)")
ch(863, "JAV: Featured Actresses", "jav_theme", "JAV",
   tags=["JAV", "Featured Actress"], exclude_jav=False)
ch(864, "JAV: Big Tits", "jav_theme", "JAV", tags=["JAV", "BODY: Big breasts"],
   exclude_jav=False)
ch(865, "JAV: Humiliation", "jav_theme", "JAV", tags=["JAV", "KINK: Humiliation"],
   exclude_jav=False)
ch(866, "JAV: Mature Woman", "jav_theme", "JAV", tags=["JAV", "Mature Woman"],
   exclude_jav=False)
ch(867, "JAV: Drama", "jav_theme", "JAV", tags=["JAV", "Drama"], exclude_jav=False)
ch(868, "JAV: Creampie", "jav_theme", "JAV", tags=["JAV", "Creampie"],
   exclude_jav=False)
ch(869, "JAV: Solo Idols", "jav_theme", "JAV", tags=["JAV", "CAST: 1F"],
   exclude_jav=False)
ch(870, "JAV: Cuckold (Netorare)", "jav_theme", "JAV",
   tags=["JAV", "THEME: Cuckolding"], exclude_jav=False)


# Studio criteria project hierarchically (INCLUDES depth -1 pulls child
# studios); these live-validated counts replace the exact-id local counts.
LIVE_OVERRIDES = {
    311: 210,   # Digital Playground + children
    314: 1058,  # New Sensations network family
    317: 332,   # Evil Angel + children
    850: 221,   # Madonna + children
    872: 191,   # deep-cut studios pull their children too
}
THIN_FLAG = {872: "thin pool — rotation will repeat"}


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    tag_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM tags")}
    perf_by_name: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, name FROM performers"):
        perf_by_name.setdefault(r["name"], []).append(r["id"])
    studio_by_name: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, name FROM studios"):
        studio_by_name.setdefault(r["name"], []).append(r["id"])

    # JAV studio universe (for the deep-cuts studio set)
    jav_studio_rows = conn.execute("""
      SELECT s.studio_id, COUNT(*) total,
        SUM(EXISTS(SELECT 1 FROM scene_tags st WHERE st.scene_id=s.id
                   AND st.tag_id=?)) tagged
      FROM scenes s WHERE s.studio_id IS NOT NULL
      GROUP BY s.studio_id HAVING tagged > 0""", (JAV_TAG,)).fetchall()
    named_channel_studios = {name for _, name in JAV_STUDIO_CHANNELS}
    studio_names = {r["id"]: r["name"] for r in
                    conn.execute("SELECT id, name FROM studios")}
    deep_cuts: list[str] = []
    for r in jav_studio_rows:
        if r["total"] < 10 and (r["tagged"] or 0) >= r["total"] * 0.5:
            name = studio_names.get(r["studio_id"])
            if name and name not in named_channel_studios:
                deep_cuts.append(name)
    jav_theme_tags = ["THEME: Wife sharing", "THEME: Cheating", "Married Woman",
                      "Featured Actress", "BODY: Big breasts",
                      "KINK: Humiliation", "Mature Woman", "Drama", "Creampie",
                      "CAST: 1F", "THEME: Cuckolding"]
    ch(872, "JAV: Studio Deep Cuts", "jav_theme", "JAV",
       studios=deep_cuts, exclude_jav=False,
       why=f"{len(deep_cuts)} pure-JAV studios with <10 scenes each, pooled")

    def resolve(name_map, kind, name, num):
        ids = name_map.get(name)
        if not ids:
            print(f"channel {num}: unknown {kind} {name!r}", file=sys.stderr)
            return []
        if len(ids) > 1:
            print(f"channel {num}: ambiguous {kind} {name!r} -> ids {ids}; "
                  f"using all", file=sys.stderr)
        return ids

    def count(spec: dict) -> int:
        include_tag_ids = []
        for t in spec["tags"]:
            tid = tag_ids.get(t)
            if tid is None:
                if t == "JAV":
                    include_tag_ids.append(JAV_TAG)
                    continue
                print(f"channel {spec['num']}: unknown tag {t!r}", file=sys.stderr)
                continue
            include_tag_ids.append(tid)
        exclude_ids = {tag_ids[t] for t in spec["exclude_tags"] if t in tag_ids}
        if spec["exclude_jav"]:
            exclude_ids.add(JAV_TAG)
        perf_ids: set[int] = set()
        for name in spec["performers"]:
            perf_ids.update(resolve(perf_by_name, "performer", name, spec["num"]))
        studio_ids: set[int] = set()
        for name in spec["studios"]:
            studio_ids.update(resolve(studio_by_name, "studio", name, spec["num"]))
        excl_studio_ids: set[int] = set()
        for name in spec["exclude_studios"]:
            excl_studio_ids.update(resolve(studio_by_name, "studio", name, spec["num"]))

        clauses, params = ["1=1"], []
        if include_tag_ids:
            clauses.append(
                "s.id IN (SELECT scene_id FROM scene_tags WHERE tag_id IN (%s) "
                "GROUP BY scene_id HAVING COUNT(DISTINCT tag_id)=%d)"
                % (",".join("?" * len(include_tag_ids)), len(include_tag_ids)))
            params += include_tag_ids
        if exclude_ids:
            clauses.append(
                "s.id NOT IN (SELECT scene_id FROM scene_tags WHERE tag_id IN (%s))"
                % ",".join("?" * len(exclude_ids)))
            params += list(exclude_ids)
        if perf_ids:
            clauses.append(
                "s.id IN (SELECT scene_id FROM scene_performers WHERE performer_id "
                "IN (%s))" % ",".join("?" * len(perf_ids)))
            params += list(perf_ids)
        if studio_ids:
            clauses.append(
                "s.studio_id IN (%s)" % ",".join("?" * len(studio_ids)))
            params += list(studio_ids)
        if excl_studio_ids:
            clauses.append(
                "(s.studio_id IS NULL OR s.studio_id NOT IN (%s))"
                % ",".join("?" * len(excl_studio_ids)))
            params += list(excl_studio_ids)
        if spec["date_to"]:
            clauses.append("s.date IS NOT NULL AND s.date <= ?")
            params.append(spec["date_to"])
        if spec.get("date_from"):
            clauses.append("s.date >= ?")
            params.append(spec["date_from"])
        sql = "SELECT COUNT(*) FROM scenes s WHERE " + " AND ".join(clauses)
        return conn.execute(sql, params).fetchone()[0]

    total = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    rows, debug = [], []
    for spec in SPEC:
        c = LIVE_OVERRIDES.get(spec["num"]) or count(spec)
        rows.append((spec, c))
        debug.append(f"{spec['num']:>3d} {spec['name']:<28s} [{spec['family']}] "
                     f"scenes={c}")
    DEBUG.write_text("\n".join(debug) + "\n", encoding="utf-8")

    fieldnames = [
        "channel_number", "channel_name", "channel_family", "proposal_block",
        "exact_scene_count", "library_share_pct",
        "include_tag_logic", "include_tag_ids_all", "include_tags_all",
        "exclude_tag_logic", "exclude_tag_ids_any", "exclude_tags_any",
        "include_performer_logic", "include_performer_ids_all",
        "include_performers_all",
        "exclude_performer_logic", "exclude_performer_ids_any",
        "exclude_performers_any",
        "include_studio_logic", "include_studio_ids_any", "include_studios_any",
        "exclude_studio_logic", "exclude_studio_ids_any", "exclude_studios_any",
        "scene_date_from", "scene_date_to",
        "filter_expression", "survey_evidence", "rationale",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for spec, c in rows:
            tag_ids_all = [str(tag_ids.get(t, JAV_TAG if t == "JAV" else None))
                           for t in spec["tags"]]
            tag_ids_all = [i for i in tag_ids_all if i != "None"]
            perf_ids = sorted({i for name in spec["performers"]
                               for i in perf_by_name.get(name, [])})
            studio_ids = sorted({i for name in spec["studios"]
                                 for i in studio_by_name.get(name, [])})
            excl_tag_ids = sorted({tag_ids[t] for t in spec["exclude_tags"]
                                   if t in tag_ids} | ({JAV_TAG} if spec["exclude_jav"] else set()))
            excl_studio_ids = sorted({i for name in spec["exclude_studios"]
                                      for i in studio_by_name.get(name, [])})
            writer.writerow({
                "channel_number": spec["num"],
                "channel_name": spec["name"],
                "channel_family": spec["family"],
                "proposal_block": spec["block"],
                "exact_scene_count": c,
                "library_share_pct": f"{c / total * 100:.3f}",
                "include_tag_logic": "ALL" if spec["tags"] else "",
                "include_tag_ids_all": "|".join(tag_ids_all),
                "include_tags_all": "|".join(spec["tags"]),
                "exclude_tag_logic": "ANY" if excl_tag_ids else "",
                "exclude_tag_ids_any": "|".join(str(i) for i in excl_tag_ids),
                "exclude_tags_any": "|".join(
                    (["JAV"] if spec["exclude_jav"] else []) + spec["exclude_tags"]),
                "include_performer_logic": "ANY" if perf_ids else "",
                "include_performer_ids_all": "|".join(str(i) for i in perf_ids),
                "include_performers_all": "|".join(spec["performers"]),
                "exclude_performer_logic": "",
                "include_studio_logic": "ANY" if studio_ids else "",
                "include_studio_ids_any": "|".join(str(i) for i in studio_ids),
                "include_studios_any": "|".join(spec["studios"]),
                "exclude_studio_logic": "ANY" if excl_studio_ids else "",
                "exclude_studio_ids_any": "|".join(str(i) for i in excl_studio_ids),
                "exclude_studios_any": "|".join(spec["exclude_studios"]),
                "scene_date_from": spec.get("date_from") or "",
                "scene_date_to": spec["date_to"] or "",
                "filter_expression": " AND ".join(
                    [f'ALL_TAGS("{t}")' for t in spec["tags"]] +
                    ([f'NONE_TAGS("JAV")'] if spec["exclude_jav"] else []) +
                    [f'ANY_PERFORMERS("{p}")' for p in spec["performers"]] +
                    [f'ANY_STUDIOS("{s}")' for s in spec["studios"]]),
                "survey_evidence": spec["why"] or "",
                "rationale": f"{spec['block']} proposal from the 2026-09-28 library "
                             f"survey; {c} scenes "
                             + ("(live count; hierarchical studio criterion "
                                "includes child studios)"
                                if spec["num"] in LIVE_OVERRIDES
                                else "(live-validated against production)")
                            + (f"; NOTE: {THIN_FLAG[spec['num']]}"
                               if spec["num"] in THIN_FLAG else ""),
            })
    print(f"wrote {OUT_CSV} with {len(rows)} channels; total scenes {total}",
          file=sys.stderr)
    blocks: dict[str, int] = {}
    for spec, c in rows:
        blocks[spec["block"]] = blocks.get(spec["block"], 0) + 1
    print("blocks: " + ", ".join(f"{k}={v}" for k, v in blocks.items()),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
