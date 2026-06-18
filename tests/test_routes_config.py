"""Tests for the vendored per-airline route config (mirror of scraper/tests/test_routes_config.py).

jobs has no live scheduler of its own — it cron-scrapes the four browser airlines (Delta,
Southwest, Turkish, Etihad). The route registry is vendored verbatim from the canonical
scraper repo so both read one source of truth; the cron entrypoints draw their per-airline
due batch from the seeded queue.
"""

from config.routes import (
    _AIRLINE_ROUTES,
    all_seeded_routes,
)
from config.settings import PriorityTier

CRON_AIRLINES = {"delta", "southwest", "turkish", "etihad"}


def test_all_seeded_routes_are_four_tuples():
    routes = all_seeded_routes()
    assert routes, "expected seeded routes"
    assert all(len(r) == 4 for r in routes)  # (origin, dest, airline, tier)


def test_cron_airlines_seeded_and_grown():
    # The four cron airlines are registered, all MED (empty HIGH), grown ~2x.
    slugs = {a for a, _h, _m in _AIRLINE_ROUTES}
    assert CRON_AIRLINES <= slugs

    counts = {a: len(h) + len(m) for a, h, m in _AIRLINE_ROUTES}
    # grown ~2x — delta 26→50, southwest 22→42, turkish 10→20, etihad 5→10.
    assert counts["delta"] >= 50
    assert counts["southwest"] >= 42
    assert counts["turkish"] >= 20
    assert counts["etihad"] >= 10

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


def test_no_reverse_or_exact_duplicate_pairs():
    # The seeder adds both directions, so a config list must not contain a pair AND its
    # reverse (that would double-seed the same route).
    for _airline, highs, meds in _AIRLINE_ROUTES:
        for direction_list in (highs, meds):
            keys = [frozenset(p) for p in direction_list]
            assert len(keys) == len(set(keys)), f"reverse/exact dup in {direction_list}"
            assert all(len(k) == 2 for k in keys), "a route cannot have origin == dest"
