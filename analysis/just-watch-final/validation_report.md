# Just Watch v4 Final — validation report

Catalog `just-watch-v4-final` · 513 channels · generated 2026-09-09T00:00:00Z · validated 2026-09-09

| Status | Check | Detail |
| --- | --- | --- |
| PASS | 0. extraction scene count | 20299 scenes (20299 expected) |
| PASS | 1. 513 unique channel definitions | 513 unique of 513 |
| PASS | 2. no duplicate numbers | 513 numbers |
| PASS | 3. no duplicate stable keys | 513 keys |
| PASS | 4. no duplicate network ids | 513 ids |
| PASS | 5. no empty channel | min count 12 |
| PASS | 6. every channel reproduces its count exactly (ordinary, aggregate, and metadata filters alike) | 513/513 exact |
| PASS | 7. aggregate ANY channels reproduce the reference unions | One-Scene Performers: 2,013 listed vs 2,013 expected, identical=True; Rare Performers: 3,569 listed vs 3,569 expected, identical=True; Prolific Performers: 69 listed vs 69 expected, identical=True; Fringe Studios: 794 maximal studios; union identical=True (2035 scenes) |
| PASS | 8. metadata filters reproduce reference memberships | 2010s Vault (2010-01-01..2019-12-31): 10789 vs 10789; 2020s Vault (2020-01-01..2026-12-31): 7831 vs 7831; Feature Length (60+ Min) (>= 3600s): 1566 vs 1566; New Arrivals (Last 180 Days) (anchor 2026-03-12): 4620 vs 4620 |
| PASS | 9. JAV excluded everywhere except the 5 sanctioned channels | 5 exempt, covering ['1599', '1661', '2733', '819', '8926'] |
| PASS | 10. post-JAV library coverage ~100% | 100.00% of 19,771 scenes |
| PASS | 15-20. family composition (158/100/142/15/15/16 + 8 specials) | performer_pair=16, performer_spotlight=158, performer_tag=15, studio_spotlight=100, studio_tag=15, tag_include_exclude=4, tag_pair=40, tag_spotlight=142, tag_triple=15, specials=8 |
| PASS | 11. tag pairs: exactly 40, <=2 same-namespace total | 40 pairs, 2 same-namespace |
| PASS | 12. triples exactly the 15 locked concepts | 15 triples, match=True |
| PASS | 13. include/exclude exactly the 4 locked concepts | [('gonzo', '1m1f'), ('narrative', 'gonzo'), ('roleplay', 'narrative'), ('spanking/impact', 'gonzo')] |
| PASS | 14. duos exactly the 16 locked pairs | 16 duos, match=True |
| PASS | 21. final total 513; sections 209/115/189 | total=513, sections={'general': 209, 'performers': 189, 'studios': 115} |
| PASS | 22. CSV header/row widths all match | 8 files, all rows exact |
| PASS | 23. JSON, CSV, authoring CSV, and preview all agree | ids align=True (513 stable keys -> preview ids) |
| PASS | 24. importer compiles the CSV; strict loader accepts | compiled + strict-loaded 513 networks |
| PASS | 25/26. plugin tests green (incl. stable-key, ANY matrix, metadata, epoch, golden) | 172 passed in 5.78s |
| PASS | R1. golden: upgraded importer leaves the production compile byte-identical | production CSV -> live networks.json |
| PASS | R2. finalizer determinism (fixed --timestamp rerun) | 13 artifacts, 0 differ on rerun |
| PASS | R3. operational: largest ANY filter measured | Rare Performers scene_filter = 28,414 bytes (3,569 ids); dev Stash v0.31.1: accepted, count=0 (328 ms); prod timing skipped (no local production credentials — scheduler env points at the 10-scene dev instance) |

## Statistics

- Scene depth: min **12** · p10 **58** · median **184** · max **12351**
- Channels below 25 scenes: **12**; below 50: **27**
- Post-JAV library coverage: **100.00%** (19,771 scenes)
- Current networks preserved by stable id: **292** (number, name, network id, and seed all byte-identical)
- Current networks retired: **503** (221 slots repurposed for new channels, 282 numbers left unused)
- New networks: **221**
- Sections: General 209 · Studios 115 · Performers 189 = 513

## The final 40 tag pairs

Same-namespace pairs are capped at 2 across the whole band; cross-namespace combinations at 2 each.

| # | Channel | Pair | Namespaces | Scenes |
| --- | --- | --- | --- | --- |
| 354 | Breast play + Living room | Breast play + Living room | ACT+SET | 265 |
| 355 | Married IRL + 1M2F | Married IRL + 1M2F | CAST+THEME | 548 |
| 356 | Group + Orgy | Orgy + Group | CAST+THEME | 213 |
| 357 | 1M2F + Facesitting | Facesitting + 1M2F | ACT+CAST | 481 |
| 358 | Vaginal sex + Casual | Vaginal sex + Casual | ACT+WARD | 1109 |
| 359 | Fingering + Married IRL | Married IRL + Fingering | ACT+THEME | 803 |
| 360 | Fingering + Spanking/impact | Fingering + Spanking/impact | ACT+KINK | 1628 |
| 361 | Kissing + 1M2F | 1M2F + Kissing | ACT+CAST | 672 |
| 362 | Kissing + Medium breasts | Kissing + Medium breasts | ACT+BODY | 616 |
| 363 | Cumshot - internal + Taboo | Cumshot - internal + Taboo | ACT+THEME | 109 |
| 364 | Cumshot - internal + POV | Cumshot - internal + POV | ACT+PROD | 247 |
| 365 | Small ass + Outdoor | Small ass + Outdoor | BODY+SET | 311 |
| 366 | Medium ass + Thong | Medium ass + Thong | BODY+WARD | 505 |
| 367 | Athletic + Humiliation | Athletic + Humiliation | BODY+KINK | 884 |
| 368 | Muscular + Amateur | Muscular + Amateur | BODY+PROD | 329 |
| 369 | Average body + Outdoor | Average body + Outdoor | BODY+SET | 383 |
| 370 | Cheating + Thong | Cheating + Thong | THEME+WARD | 322 |
| 371 | Roleplay + Dom/Sub | Roleplay + Dom/Sub | KINK+THEME | 1050 |
| 372 | Roleplay + Narrative | Roleplay + Narrative | PROD+THEME | 1301 |
| 373 | Bedroom + Breast play | Breast play + Bedroom | ACT+SET | 353 |
| 374 | Bedroom + Cheating | Cheating + Bedroom | SET+THEME | 359 |
| 375 | Lingerie + Stockings | Lingerie + Stockings | WARD | 755 |
| 380 | Heels + Average body | Average body + Heels | BODY+WARD | 587 |
| 398 | Heels + Cheating | Cheating + Heels | THEME+WARD | 487 |
| 405 | Dress + Bedroom | Bedroom + Dress | SET+WARD | 386 |
| 411 | Dress + Living room | Living room + Dress | SET+WARD | 316 |
| 413 | Casual + Breast play | Breast play + Casual | ACT+WARD | 576 |
| 416 | Casual + Dress | Dress + Casual | WARD | 711 |
| 426 | Spanking/impact + Athletic | Athletic + Spanking/impact | BODY+KINK | 1712 |
| 433 | Spanking/impact + Heels | Heels + Spanking/impact | KINK+WARD | 778 |
| 436 | Dom/Sub + Group | Group + Dom/Sub | CAST+KINK | 297 |
| 444 | Dom/Sub + Sex toys | Sex toys + Dom/Sub | ACT+KINK | 718 |
| 445 | Foot fetish + Bathroom | Bathroom + Foot fetish | KINK+SET | 109 |
| 446 | Foot fetish + Barefoot | Barefoot + Foot fetish | KINK+WARD | 110 |
| 447 | POV + Step-family | Step-family + POV | PROD+THEME | 149 |
| 448 | Amateur + Thong | Thong + Amateur | PROD+WARD | 227 |
| 608 | Slim Pickings, Big Ratings: The Plot Happens Department | Slim + Narrative | BODY+PROD | 877 |
| 638 | Lip Service: The Good Lighting Department Department | Kissing + Glamour | ACT+PROD | 412 |
| 643 | Couch Potato Confidential: A Do-It-Yourself Special | Living room + Amateur | PROD+SET | 304 |
| 644 | Method Acting Gone Rogue Goes The Mute Button | Roleplay + Gags/restraints | KINK+THEME | 257 |

**24/24 checks passed.**
