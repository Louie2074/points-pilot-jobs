"""Shared metro-airport expansion for route seeding."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Metro:
    key: str
    display: str
    airports: tuple[str, ...]


METROS: dict[str, Metro] = {
    "NYC": Metro("NYC", "New York", ("JFK", "EWR", "LGA")),
    "LAX_METRO": Metro("LAX_METRO", "Los Angeles", ("LAX", "BUR", "SNA", "ONT", "LGB")),
    "BAY": Metro("BAY", "Bay Area", ("SFO", "OAK", "SJC")),
    "WAS": Metro("WAS", "Washington, DC", ("DCA", "IAD", "BWI")),
    "CHI": Metro("CHI", "Chicago", ("ORD", "MDW")),
    "HOU_METRO": Metro("HOU_METRO", "Houston", ("IAH", "HOU")),
    "DAL_METRO": Metro("DAL_METRO", "Dallas", ("DFW", "DAL")),
    "SOUTH_FLORIDA": Metro("SOUTH_FLORIDA", "South Florida", ("MIA", "FLL", "PBI")),
    "LON": Metro("LON", "London", ("LHR", "LGW")),
    "PAR": Metro("PAR", "Paris", ("CDG", "ORY")),
    "TYO": Metro("TYO", "Tokyo", ("HND", "NRT")),
}


def airports_for(token: str) -> tuple[str, ...]:
    code = token.strip().upper()
    metro = METROS.get(code)
    return metro.airports if metro else (code,)
