"""
Priority route pairs for the scraping queue, per airline.

Routes are defined as unidirectional (origin, dest) tuples. The queue seeder adds both
directions automatically since award pricing is often asymmetric (e.g. SEA→JFK ≠ JFK→SEA).
The queue is per-airline (routes_queue PK is origin,dest,airline), so each airline only
scrapes routes it actually flies.

Tiers:
  HIGH — primary hub pairs, refreshed on the HIGH TTL
  MED  — secondary hub pairs, refreshed on the MED TTL

LOW routes are added dynamically via handle_user_search().

Live airlines are Alaska + JetBlue. Alaska runs daily — every route is MED; HIGH is
intentionally empty (the 3×/day HIGH refresh oversubscribed the shared single-IP worker).
JetBlue covers 13 pairs anchored on JFK/BOS/FLL/EWR. American (AAdvantage) was removed
(Akamai-walled). Delta is scraped from the points-pilot-jobs repo (nodriver browser
scrape), not here.
"""

from config.settings import PriorityTier
from config.metros import airports_for


def route_set(origin: str, dest: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for concrete_origin in airports_for(origin):
        for concrete_dest in airports_for(dest):
            if concrete_origin == concrete_dest:
                continue
            pair = (concrete_origin, concrete_dest)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def expand_route_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    expanded: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for origin, dest in pairs:
        for pair in route_set(origin, dest):
            if pair in seen:
                continue
            seen.add(pair)
            expanded.append(pair)
    return expanded

# ---------------------------------------------------------------------------
# Alaska Airlines Mileage Plan — anchored on SEA/PDX/ANC + Hawaii.
# Runs DAILY: every route is MED (24h TTL). HIGH is intentionally empty — the
# 3×/day HIGH refresh oversubscribed the shared single-IP worker, so the prior
# HIGH+MED set was folded into a single daily MED tier during the route expansion.
# ---------------------------------------------------------------------------
ALASKA_HIGH_ROUTES: list[tuple[str, str]] = []

ALASKA_MED_ROUTES: list[tuple[str, str]] = [
    # core hubs / transcons (was the prior HIGH+MED set, now all daily)
    *route_set("SEA", "NYC"),
    ("SEA", "BOS"),
    ("SEA", "ORD"),
    ("SEA", "LAX"),
    ("SEA", "SFO"),
    ("SEA", "DEN"),
    *route_set("PDX", "NYC"),
    ("PDX", "LAX"),
    ("ANC", "SEA"),
    ("SEA", "ATL"),
    ("SEA", "DFW"),
    ("SEA", "MIA"),
    ("SEA", "LAS"),
    ("PDX", "ORD"),
    ("PDX", "SFO"),
    ("PDX", "DEN"),
    *route_set("SFO", "NYC"),
    *route_set("LAX_METRO", "NYC"),
    ("LAX", "BOS"),
    ("SAN", "SEA"),
    ("SJC", "SEA"),
    ("BOI", "LAX"),
    ("BOI", "SEA"),
    ("GEG", "SEA"),
    # new: SEA transcons + focus
    ("SEA", "IAD"),
    ("SEA", "DCA"),
    ("SEA", "PHL"),
    ("SEA", "MCO"),
    ("SEA", "FLL"),
    ("SEA", "AUS"),
    ("SEA", "PHX"),
    ("SEA", "SLC"),
    ("SEA", "MSP"),
    ("SEA", "TPA"),
    ("SEA", "BNA"),
    ("SEA", "RDU"),
    ("SEA", "SNA"),
    ("SEA", "PSP"),
    ("SEA", "FAI"),
    # new: Hawaii (Alaska's strength)
    ("SEA", "HNL"),
    ("SEA", "OGG"),
    ("SEA", "KOA"),
    ("SEA", "LIH"),
    ("SFO", "HNL"),
    ("LAX", "HNL"),
    ("PDX", "HNL"),
    ("PDX", "OGG"),
    # new: PDX / west-coast spokes
    ("PDX", "BOS"),
    ("PDX", "PHX"),
    ("PDX", "SAN"),
    ("PDX", "SEA"),
    ("LAX", "PHX"),
    ("SAN", "SFO"),
    ("ANC", "PDX"),
]

# Delta is no longer scraped from this repo — it runs as a nodriver browser scrape in the
# points-pilot-jobs repo (GH Actions / Azure IP clears Akamai where this server's IP gets 444).
# American (AAdvantage) was removed entirely (Akamai-walled; the scraper + its routes are gone).

# ---------------------------------------------------------------------------
# Cron airlines (Delta / Southwest / Turkish / Etihad) — scraped from the
# points-pilot-jobs repo (nodriver / Azure IP), seeded HERE (canonical) into the
# shared scored queue. All MED, empty HIGH (cron wakes daily; adaptive cadence
# backs off stable routes). Each list ≈2× its prior static jobs/*_browser_scrape.py
# set (hub-spoke expansion — cron airlines accumulate no routes_queue demand yet).
# Both directions are auto-seeded by all_seeded_routes(), so list each pair ONCE.
# ---------------------------------------------------------------------------

# Delta SkyMiles — anchored on ATL/MSP/DTW/SLC hubs + JFK/LAX/SEA/BOS gateways.
# 26 → 50 directed pairs.
DELTA_MED_ROUTES: list[tuple[str, str]] = [
    # existing ATL megahub + transcons
    ("ATL", "LAX"),
    ("ATL", "MCO"),
    ("ATL", "LGA"),
    ("JFK", "LAX"),
    ("ATL", "SEA"),
    ("ATL", "DEN"),
    ("ATL", "FLL"),
    ("ATL", "BOS"),
    ("LAX", "SEA"),
    ("ATL", "DFW"),
    # MSP hub
    ("MSP", "JFK"),
    ("MSP", "SEA"),
    ("MSP", "HNL"),
    ("MSP", "LAX"),
    ("MSP", "ATL"),
    ("MSP", "DTW"),
    ("MSP", "MCO"),
    ("MSP", "LAS"),
    ("MSP", "DEN"),
    ("MSP", "BOS"),
    # DTW + SLC hubs
    ("DTW", "ATL"),
    ("DTW", "LAX"),
    ("DTW", "MCO"),
    ("DTW", "LGA"),
    ("SLC", "ATL"),
    ("SLC", "SEA"),
    # new: ATL spokes
    ("ATL", "JFK"),
    ("ATL", "MIA"),
    ("ATL", "PHX"),
    ("ATL", "AUS"),
    ("ATL", "TPA"),
    ("ATL", "RDU"),
    ("ATL", "DCA"),
    # new: DTW spokes
    ("DTW", "BOS"),
    ("DTW", "DEN"),
    ("DTW", "SEA"),
    ("DTW", "SFO"),
    ("DTW", "JFK"),
    # new: SLC spokes
    ("SLC", "LAX"),
    ("SLC", "JFK"),
    ("SLC", "DEN"),
    ("SLC", "MSP"),
    ("SLC", "BOS"),
    # new: JFK / BOS transcons + LAX/SEA spokes
    ("JFK", "SEA"),
    ("JFK", "SFO"),
    ("BOS", "LAX"),
    ("BOS", "SFO"),
    ("SEA", "DEN"),
    ("SEA", "LAS"),
    ("LAX", "DEN"),
]

# Southwest Rapid Rewards — focus cities DEN/MDW/BWI/LAS/PHX/DAL/HOU/OAK/SAN.
# 22 → 42 directed pairs.
SOUTHWEST_MED_ROUTES: list[tuple[str, str]] = [
    # existing focus-city pairs
    ("LAS", "LAX"),
    ("LAS", "OAK"),
    ("DAL", "HOU"),
    ("MDW", "LAS"),
    ("DEN", "PHX"),
    ("BWI", "MCO"),
    ("PHX", "LAS"),
    ("SAN", "LAS"),
    ("DAL", "MDW"),
    ("DEN", "LAS"),
    ("DEN", "LAX"),
    ("DEN", "MDW"),
    ("DEN", "BWI"),
    ("DEN", "OAK"),
    ("MDW", "MCO"),
    ("MDW", "BWI"),
    ("BWI", "FLL"),
    ("BWI", "BOS"),
    ("PHX", "LAX"),
    ("PHX", "SAN"),
    ("OAK", "SAN"),
    ("SEA", "LAX"),
    # new: DEN focus-city spokes
    ("DEN", "MCO"),
    ("DEN", "SAN"),
    ("DEN", "SEA"),
    ("DEN", "DAL"),
    ("DEN", "HOU"),
    # new: MDW spokes
    ("MDW", "PHX"),
    ("MDW", "FLL"),
    ("MDW", "SAN"),
    ("MDW", "HOU"),
    # new: BWI spokes
    ("BWI", "LAS"),
    ("BWI", "TPA"),
    ("BWI", "HOU"),
    # new: LAS / PHX / west-coast spokes
    ("LAS", "DAL"),
    ("LAS", "SEA"),
    ("LAS", "MCO"),
    ("PHX", "HOU"),
    ("OAK", "LAX"),
    ("SAN", "SMF"),
    ("HOU", "MCO"),
    ("SEA", "OAK"),
]

# Turkish Miles&Smiles — US gateways ↔ IST hub (cron-only; long-haul US↔IST).
# 10 → 20 directed pairs (more US gateways to IST).
TURKISH_MED_ROUTES: list[tuple[str, str]] = [
    ("JFK", "IST"),
    ("EWR", "IST"),
    ("IAD", "IST"),
    ("ORD", "IST"),
    ("BOS", "IST"),
    ("MIA", "IST"),
    ("SFO", "IST"),
    ("LAX", "IST"),
    ("IAH", "IST"),
    ("SEA", "IST"),
    # new: additional US gateways Turkish serves to IST
    ("ATL", "IST"),
    ("DFW", "IST"),
    ("DTW", "IST"),
    ("PHL", "IST"),
    ("DEN", "IST"),
    ("MCO", "IST"),
    ("DCA", "IST"),
    ("SLC", "IST"),
    ("PDX", "IST"),
    ("LAS", "IST"),
]

# Etihad Guest — US gateways ↔ AUH hub (cron-only; long-haul US↔AUH).
# 5 → 10 directed pairs (more US gateways to AUH).
ETIHAD_MED_ROUTES: list[tuple[str, str]] = [
    ("JFK", "AUH"),
    ("ORD", "AUH"),
    ("IAD", "AUH"),
    ("BOS", "AUH"),
    ("LAX", "AUH"),
    # new: additional US gateways Etihad serves to AUH
    ("SFO", "AUH"),
    ("EWR", "AUH"),
    ("IAH", "AUH"),
    ("ATL", "AUH"),
    ("MIA", "AUH"),
]

# ---------------------------------------------------------------------------
# JetBlue TrueBlue — anchored on JFK/BOS/FLL (smallest set)
# ---------------------------------------------------------------------------
JETBLUE_HIGH_ROUTES: list[tuple[str, str]] = [
    *route_set("NYC", "LAX_METRO"),
    ("JFK", "FLL"),
    ("BOS", "JFK"),
]

JETBLUE_MED_ROUTES: list[tuple[str, str]] = [
    ("BOS", "FLL"),
    ("JFK", "SFO"),
    # new: JFK / BOS / FLL / EWR network
    ("JFK", "MCO"),
    ("JFK", "LAS"),
    ("JFK", "SEA"),
    ("JFK", "RSW"),
    ("BOS", "MCO"),
    ("BOS", "LAX"),
    ("FLL", "EWR"),
    ("EWR", "MCO"),
]


# Per-airline registry: (scraper_slug, high_routes, med_routes).
# Live airlines only. Delta is scraped from the points-pilot-jobs repo (nodriver browser
# scrape), not here, so it's not seeded in this queue.
_AIRLINE_ROUTES: list[tuple[str, list[tuple[str, str]], list[tuple[str, str]]]] = [
    ("alaska", ALASKA_HIGH_ROUTES, ALASKA_MED_ROUTES),
    ("jetblue", JETBLUE_HIGH_ROUTES, JETBLUE_MED_ROUTES),
    # Cron airlines (scraped in points-pilot-jobs) — all MED, empty HIGH.
    ("delta", [], DELTA_MED_ROUTES),
    ("southwest", [], SOUTHWEST_MED_ROUTES),
    ("turkish", [], TURKISH_MED_ROUTES),
    ("etihad", [], ETIHAD_MED_ROUTES),
]


# Hub routes whose CASH (CPP) is always refreshed FIRST each cash-scraper cycle, ahead of
# demand-ranked routes (get_top_cash_routes). Guarantees a designated hub never falls out of
# cash coverage when its organic search demand is low. Both directions are covered
# automatically. Award/points coverage for these should also be seeded above (ALASKA_*/etc.).
CASH_PINNED_ROUTES: list[tuple[str, str]] = [
    ("SEA", "MSP"),
    *route_set("SEA", "NYC"),
    *route_set("PDX", "NYC"),
    *route_set("SFO", "NYC"),
    *route_set("LAX_METRO", "NYC"),
]


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def all_seeded_routes() -> list[tuple[str, str, str, str]]:
    """
    Flat list of (origin, dest, airline, tier) tuples — both directions, all airlines.
    Used by QueueManager.seed_from_config().
    """
    result: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    def add(origin: str, dest: str, airline: str, tier: str) -> None:
        key = (origin, dest, airline)
        if key in seen:
            return
        seen.add(key)
        result.append((origin, dest, airline, tier))

    for airline, highs, meds in _AIRLINE_ROUTES:
        for origin, dest in expand_route_pairs(highs):
            add(origin, dest, airline, PriorityTier.HIGH)
            add(dest, origin, airline, PriorityTier.HIGH)
        for origin, dest in expand_route_pairs(meds):
            add(origin, dest, airline, PriorityTier.MED)
            add(dest, origin, airline, PriorityTier.MED)
    return result
