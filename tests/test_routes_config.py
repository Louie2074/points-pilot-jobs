"""Tests for the vendored per-airline route config (mirror of scraper/tests/test_routes_config.py).

jobs has no live scheduler of its own — it cron-scrapes the four browser airlines (Delta,
Southwest, Turkish, Etihad). The route registry is vendored verbatim from the canonical
scraper repo so both read one source of truth; the cron entrypoints draw their per-airline
due batch from the seeded queue.
"""

from config.routes import (
    _AIRLINE_ROUTES,
    all_seeded_routes,
    route_set,
)
from config.settings import PriorityTier

CRON_AIRLINES = {"delta", "southwest", "turkish", "etihad"}
EXPECTED_PAIR_COUNTS = {
    "alaska": 97,
    "jetblue": 38,
    "delta": 65,
    "southwest": 58,
    "turkish": 25,
    "etihad": 13,
}


def test_all_seeded_routes_are_four_tuples():
    routes = all_seeded_routes()
    assert routes, "expected seeded routes"
    assert all(len(r) == 4 for r in routes)  # (origin, dest, airline, tier)


def test_route_set_expands_nyc():
    assert route_set("SEA", "NYC") == [("SEA", "JFK"), ("SEA", "EWR"), ("SEA", "LGA")]


def test_seeded_routes_include_nyc_concrete_pairs():
    seeded = all_seeded_routes()
    keys = {(o, d, a) for o, d, a, _ in seeded}
    assert ("SEA", "JFK", "alaska") in keys
    assert ("SEA", "EWR", "alaska") in keys
    assert ("SEA", "LGA", "alaska") in keys
    assert all(o != d for o, d, *_ in seeded)


def test_cron_airlines_seeded_and_grown():
    # The four cron airlines are registered, all MED (empty HIGH), grown ~2x.
    slugs = {a for a, _h, _m in _AIRLINE_ROUTES}
    assert CRON_AIRLINES <= slugs

    counts = {a: len(h) + len(m) for a, h, m in _AIRLINE_ROUTES}
    assert counts == EXPECTED_PAIR_COUNTS

    # Cron airlines seed only at MED (empty HIGH).
    for a, highs, _meds in _AIRLINE_ROUTES:
        if a in CRON_AIRLINES:
            assert highs == []

    # All cron seed rows are MED, both directions present.
    seeded = all_seeded_routes()
    cron = [r for r in seeded if r[2] in CRON_AIRLINES]
    assert cron
    assert all(tier == PriorityTier.MED for *_x, tier in cron)
    keys = {(o, d, a) for o, d, a, _ in seeded}
    assert ("ATL", "LAX", "delta") in keys and ("LAX", "ATL", "delta") in keys
    assert ("JFK", "IST", "turkish") in keys and ("IST", "JFK", "turkish") in keys
    assert ("JFK", "AUH", "etihad") in keys and ("AUH", "JFK", "etihad") in keys
    assert ("LAS", "LAX", "southwest") in keys and ("LAX", "LAS", "southwest") in keys


def test_no_exact_duplicate_pairs():
    # The seeder dedupes directed rows, but config lists should not repeat the exact tuple.
    for _airline, highs, meds in _AIRLINE_ROUTES:
        for direction_list in (highs, meds):
            keys = list(direction_list)
            assert len(keys) == len(set(keys)), f"reverse/exact dup in {direction_list}"
            assert all(
                origin != dest for origin, dest in keys
            ), "a route cannot have origin == dest"
