"""Simulation metric + gate tests (review finding R8).

The simulator's measurements must be an INDEPENDENT acceptance oracle: these
tests feed hand-calculated fixtures to the metric helpers and ADVERSARIAL
(corrupted) input to the gate logic, proving the checks fail when the engine
misbehaves. A gate that cannot fail is not a gate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "simulate_continuing", REPO / "tools" / "simulate_continuing.py")
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

HOUR = sim.HOUR


def a(start, end, sid):
    return (start, end, sid)


# ---------------------------------------------------------------------------
# repeat_gaps: hand-calculated fixtures
# ---------------------------------------------------------------------------


def test_repeat_gaps_daily_airings_are_24h_not_garbage():
    """The review probe: four exactly-daily airings produced a 48h median and
    a 500,024h p90 because gaps and epoch timestamps shared one list."""
    airings = [a(i * 24 * HOUR, i * 24 * HOUR + HOUR, "one") for i in range(4)]
    gaps = sim.repeat_gaps(airings)
    assert gaps == {"medianHours": 24.0, "minHours": 24.0, "p90Hours": 24.0}


def test_repeat_gaps_irregular_and_multi_scene():
    airings = [
        a(0, 1, "x"), a(10 * HOUR, 11 * HOUR, "x"), a(40 * HOUR, 41 * HOUR, "x"),
        a(5 * HOUR, 6 * HOUR, "y"), a(29 * HOUR, 30 * HOUR, "y"),
    ]
    gaps = sim.repeat_gaps(airings)
    # x: 10h, 30h; y: 24h  -> median 24, min 10, p90 30
    assert gaps["minHours"] == 10.0
    assert gaps["medianHours"] == 24.0
    assert gaps["p90Hours"] == 30.0


def test_repeat_gaps_single_airing_has_no_gaps():
    assert sim.repeat_gaps([a(0, 1, "x")]) is None


# ---------------------------------------------------------------------------
# duplicate_airings: the independent consumption oracle
# ---------------------------------------------------------------------------


def test_duplicate_airings_detects_intra_pass_repeat():
    """Nine scenes per pass; scene a re-airs after a single slot (a real
    double-consumption) — flagged."""
    ids = {str(i) for i in range(9)}
    order = ["0", "1", "0"] + [str(i) for i in range(2, 9)]
    airings = [(30 * i * 60_000, 30 * (i + 1) * 60_000, sid) for i, sid in enumerate(order)]
    dupes = sim.duplicate_airings(airings, lambda _t: len(ids))
    assert ("0", airings[2][0]) in dupes


def test_duplicate_airings_clean_rotation_is_clean():
    ids = [str(i) for i in range(9)]
    airings = [(30 * i * 60_000, 30 * (i + 1) * 60_000, sid)
               for i, sid in enumerate(ids + ids)]
    assert sim.duplicate_airings(airings, lambda _t: 9) == []


def test_duplicate_airings_tolerates_pass_boundary_order_swings():
    """Consecutive passes in DIFFERENT orders are not duplicates: a rotated
    second pass keeps every scene's repeat at least two-thirds of a pass away
    (reversals that violate the engine's guard window are out of scope — the
    guard itself is what the clamp encodes)."""
    pass_one = [str(i) for i in range(100)]
    pass_two = pass_one[30:] + pass_one[:30]  # rotated: order substantially differs
    airings = [(30 * i * 60_000, 30 * (i + 1) * 60_000, sid)
               for i, sid in enumerate(pass_one + pass_two)]
    assert sim.duplicate_airings(airings, lambda _t: 100) == []


def test_duplicate_airings_ignores_released_reservations():
    """A released reservation (canceled future airing after a deletion)
    legitimately re-airs early — exempt when the release lies between."""
    ids = {str(i) for i in range(9)}
    order = ["0", "1", "0"] + [str(i) for i in range(2, 9)]
    airings = [(30 * i * 60_000, 30 * (i + 1) * 60_000, sid) for i, sid in enumerate(order)]
    assert sim.duplicate_airings(airings, lambda _t: len(ids)), \
        "without release context the re-air IS suspicious"
    assert sim.duplicate_airings(airings, lambda _t: len(ids),
                                 released=[("0", airings[1][0] + 1)]) == []


# ---------------------------------------------------------------------------
# viewing_repetition / same_window_recurrence: hand-calculated sanity
# ---------------------------------------------------------------------------


def _local_ms(date, hour):
    """Epoch ms for a wall-clock hour in the simulation's Toronto timezone."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    d = dt.date.fromisoformat(date)
    return int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=ZoneInfo("America/Toronto"))
               .timestamp() * 1000)


def test_viewing_repetition_identical_evenings_is_one():
    airings = []
    for d, date in enumerate(("2026-02-16", "2026-02-17", "2026-02-18")):
        for h in (18, 19):
            t = _local_ms(date, h)
            airings.append(a(t, t + HOUR, "same"))
    assert sim.viewing_repetition(airings) == 1.0


def test_viewing_repetition_disjoint_evenings_is_zero():
    airings = []
    for d, date in enumerate(("2026-02-16", "2026-02-17", "2026-02-18")):
        t = _local_ms(date, 18)
        airings.append(a(t, t + HOUR, f"v{d}"))
    assert sim.viewing_repetition(airings) == 0.0


def test_viewing_repetition_ignores_non_evening_hours():
    airings = []
    for d, date in enumerate(("2026-02-16", "2026-02-17")):
        for h in (2, 10):  # local night/morning: never counted
            t = _local_ms(date, h)
            airings.append(a(t, t + HOUR, "same"))
    assert sim.viewing_repetition(airings) == 1.0  # vacuous default: no evenings


def test_same_window_recurrence_flags_same_window_only():
    day = 24 * HOUR
    # x airs at 00:00 day0 and 01:00 day1 (same local window, <7d apart): hit.
    # y airs at 00:00 day0 and 12:00 day1 (different window): miss.
    airings = [
        a(0, HOUR, "x"), a(day + HOUR, day + 2 * HOUR, "x"),
        a(0, HOUR, "y"), a(day + 12 * HOUR, day + 13 * HOUR, "y"),
    ]
    assert sim.same_window_recurrence(airings) == 0.5
