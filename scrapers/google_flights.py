"""Google Flights cash-fare browser scraper (CPP cash anchor for AS/DL/B6).

Google Flights returns every carrier's CASH fare for a route in one search. We DOM-scrape the
flight rows' aria-labels (no flight number is present), parse them into GoogleFares, and let the
cash_matcher join them to award flights by departure time. Selector matches aria-label CONTENT
(`Select flight`) not the tag — the tag varies by Chrome version.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from scrapers.browser import BrowserScraper

logger = logging.getLogger(__name__)

# Google Flights seat-class codes (the field-9 varint in the tfs protobuf).
_SEAT = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


def _build_tfs(origin: str, dest: str, travel_date: date, seat: int) -> str:
    """Base64url Google Flights ``tfs`` protobuf for a one-way, single-adult search in a given
    seat class (1=economy, 2=premium economy, 3=business, 4=first). Deterministic — the only byte
    that changes across cabins is the field-9 seat varint. Verified byte-for-byte against tfs
    strings captured from live Google Flights (see test_build_tfs_premium_economy_golden).

    Used for premium economy, whose cabin Google's natural-language query cannot select (it
    returns zero rows); economy/business/first reach Google via the NL-text path in search_url."""

    def _ld(tag: int, payload: bytes) -> bytes:  # one length-delimited protobuf field
        return bytes([tag, len(payload)]) + payload

    date_field = _ld(0x12, travel_date.isoformat().encode())  # field 2: date YYYY-MM-DD
    from_field = _ld(0x6A, _ld(0x12, origin.encode()))  # field 13: {field 2: origin}
    to_field = _ld(0x72, _ld(0x12, dest.encode()))  # field 14: {field 2: dest}
    msg = _ld(0x1A, date_field + from_field + to_field)  # field 3: repeated flight-data
    msg += _ld(0x42, b"\x01")  # field 8: passengers (1 adult)
    msg += bytes([0x48, seat])  # field 9: seat class (varint)
    msg += b"\x98\x01\x02"  # field 19: trip type = one-way
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


_PRICE_RE = re.compile(r"([\d,]+)\s+US dollars")
_STOPS_RE = re.compile(r"(Nonstop|(\d+) stops?) flight with ([A-Za-z][A-Za-z .&'-]*?)\.")
_DEP_RE = re.compile(r"Leaves .*? at (\d{1,2}):(\d{2})\s*([AP]M)")


@dataclass(frozen=True)
class GoogleFare:
    """One cash fare row parsed from a Google Flights aria-label."""

    carrier: str  # display name, e.g. "JetBlue" (mapped to IATA later)
    dep_hhmm: str  # local departure, 24h "HH:MM"
    price: float  # cheapest cash price shown, USD
    nonstop: bool


def _to_24h(hour: str, minute: str, ampm: str) -> str:
    h = int(hour) % 12
    if ampm == "PM":
        h += 12
    return f"{h:02d}:{minute}"


def parse_google_fares(aria_labels: list[str]) -> list[GoogleFare]:
    """Parse flight-row aria-labels into GoogleFares. Skips bare price chips / non-flight rows.
    Connecting rows flow through as ``nonstop=False`` (the O&D anchor needs cheapest-any-routing;
    the per-flight nonstop matcher filters them out itself)."""
    out: list[GoogleFare] = []
    for label in aria_labels:
        stops = _STOPS_RE.search(label)
        dep = _DEP_RE.search(label)
        price_m = _PRICE_RE.search(label)
        if not (stops and dep and price_m):
            continue  # bare price chip / non-flight element
        nonstop = stops.group(1) == "Nonstop"
        carrier = stops.group(3).strip()
        dep_hhmm = _to_24h(dep.group(1), dep.group(2), dep.group(3))
        price = float(price_m.group(1).replace(",", ""))
        # Keep connecting rows (nonstop=False) — the O&D anchor pass needs cheapest-any-routing;
        # the per-flight nonstop matcher still filters them out via `if not fare.nonstop`.
        out.append(GoogleFare(carrier=carrier, dep_hhmm=dep_hhmm, price=price, nonstop=nonstop))
    return out


_EXTRACT_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('[aria-label*="Select flight"]').forEach(el => {
    const l = el.getAttribute('aria-label') || '';
    if (/US dollars/.test(l) && /flight with/.test(l)) out.push(l);
  });
  return JSON.stringify(out);
})()
"""


class GoogleFlightsScraper(BrowserScraper):
    """Navigate Google Flights and return parsed economy GoogleFares for one route/date."""

    source = "google_flights"
    warm_url = None  # navigate directly to the search URL
    # ~50s ceiling. Economy breaks on the first non-empty poll (fast); premium cabins keep
    # polling until the row count stabilizes (PE renders progressively), so they need the headroom.
    nav_settle_attempts = 25
    nav_settle_s = 2.0

    @staticmethod
    def search_url(origin: str, dest: str, travel_date: date, cabin: str = "economy") -> str:
        # premium_economy: Google's NL query cannot select it (returns 0 rows), so navigate by the
        # exact tfs protobuf with the seat field. economy/business/first use the working NL-text
        # path (economy = Google's default, no prefix).
        if cabin == "premium_economy":
            tfs = _build_tfs(origin, dest, travel_date, _SEAT["premium_economy"])
            return f"https://www.google.com/travel/flights?tfs={tfs}&curr=USD&hl=en&gl=US"
        d = travel_date.isoformat()
        prefix = {
            "economy": "",
            "business": "Business%20class%20",
            "first": "First%20class%20",
        }.get(cabin, "")
        return (
            f"https://www.google.com/travel/flights?q={prefix}Flights%20from%20{origin}%20to%20{dest}"
            f"%20on%20{d}%20one%20way&curr=USD&hl=en&gl=US"
        )

    async def fetch_raw(self, origin, dest, travel_date):  # not used (cash path)
        raise NotImplementedError("GoogleFlightsScraper uses scrape_fares(), not fetch_raw()")

    def normalize(self, raw, origin, dest, travel_date):  # not used (cash path)
        raise NotImplementedError("GoogleFlightsScraper uses scrape_fares(), not normalize()")

    async def fetch_fares(
        self, origin: str, dest: str, travel_date: date, cabin: str = "economy"
    ) -> list[GoogleFare]:
        tab = await self._ensure_browser()
        await tab.get(self.search_url(origin, dest, travel_date, cabin))
        labels: list[str] = []
        prev = -1
        for _ in range(self.nav_settle_attempts):
            await tab.sleep(self.nav_settle_s)
            raw = await tab.evaluate(_EXTRACT_JS, await_promise=False)
            labels = json.loads(raw) if isinstance(raw, str) else []
            if labels:
                # Economy renders fast — the first non-empty poll is complete. Premium cabins
                # (business / premium economy / first) render progressively (PE can show a single
                # row mid-render), so wait until the row count stops growing before trusting it.
                if cabin == "economy" or len(labels) == prev:
                    break
                prev = len(labels)
        return parse_google_fares(labels)

    def scrape_fares(
        self, origin: str, dest: str, travel_date: date, cabin: str = "economy"
    ) -> list[GoogleFare]:
        """Sync entry: drive fetch_fares on this instance's loop."""
        loop = self._ensure_loop()
        return loop.run_until_complete(self.fetch_fares(origin, dest, travel_date, cabin))
