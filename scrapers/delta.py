"""
Delta Air Lines SkyMiles award availability scraper.

Runs on the BrowserScraper (nodriver/Chrome) transport: Delta's GraphQL endpoint is
Akamai-blocked to plain httpx (HTTP 444), but an in-page fetch() inside a warmed delta.com
Chrome session clears Akamai from a GitHub Actions (Azure) datacenter IP (proven 2026-06-07).
Run by `delta_browser_scrape.py` on a daily GitHub Actions cron in this (points-pilot-jobs) repo
(`delta-browser-scrape.yml`). Canonical home for the Delta browser scraper.

Uses Delta's public GraphQL offer endpoint (``offer-api-prd.delta.com``) — no login, the
``authorization: GUEST`` header is the anonymous token the dotcom search uses. The request
is a single GraphQL POST whose ``variables.offerSearchCriteria`` encodes the trip; the
response nests, under ``data.gqlSearchOffers.gqlOffersSets[]``, a list of ``trips`` (the
itinerary/legs) paired with ``offers`` (one per branded fare = one cabin).

The browser transport (pacing, warm session, challenge/429/444 → ScraperBlockedError) is
inherited from BrowserScraper; this class only builds the request and maps the response to
FlightRecords. The field mapping was validated against a real captured response
(scripts/fixtures/delta_C_datacenter_BOS-SEA_2026-06-19.json → 65 records).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.airport_tz import AIRPORT_TZ
from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord
from scrapers.browser import BrowserScraper

logger = logging.getLogger(__name__)

_API_URL = "https://offer-api-prd.delta.com/prd/rm-offer-gql"

# Delta brand IDs (offer.additionalOfferProperties.dominantSegmentBrandId and
# fareInformation.brandByFlightLegs[].brandId) → canonical cabin class. We match on substrings
# of the upper-cased brand id.
#
# Delta's live offer API does NOT use verbose names like "DELTA_ONE"; it uses short prefixed
# brand codes (captured JFK-CDG 2026-06-25): CD1=Delta One, CDPS=Delta Premium Select,
# CFIRST=domestic First, CMAIN/BMAIN=Main/Basic economy, CDCP=Comfort+. Partner-operated SkyTeam
# legs carry the partner's codes (e.g. Air France AFST=business/SkyTeam, AFPE=premium economy).
# We must match these real codes or the premium cabins resolve to None and get dropped — which is
# exactly why business never landed (CD1 matched no rule → Delta One silently lost).
#
# Order matters: the table is scanned top-down and the FIRST substring hit wins, so list the most
# specific / most premium tokens first. In particular DELTA-ONE/business and the premium-economy
# tokens must precede the bare "FIRST" token, because a Delta One itinerary that connects through a
# domestic-First leg exposes a "CFIRST" leg brand whose "FIRST" substring would otherwise hijack
# the cabin (the old bug). Within a single id the first matching token decides, so e.g. "CDCP"
# (Comfort+) is caught by the economy "DCP" token before anything else.
_CABIN_RULES: tuple[tuple[str, str], ...] = (
    # --- Delta One / business (most premium first) ---
    ("DELTA_ONE", "business"),
    ("DELTAONE", "business"),
    ("DELTA ONE", "business"),
    ("CD1", "business"),  # live Delta One brand code (e.g. "CD1") — transatlantic/-pacific business
    ("AFST", "business"),  # Air France SkyTeam-partner business leg (SkyTeam "ST")
    ("BUSINESS", "business"),
    # --- Premium economy (must precede the bare "FIRST"/"MAIN" tokens) ---
    ("CDPS", "premium_economy"),  # live Delta Premium Select brand code
    ("PREMIUM_SELECT", "premium_economy"),  # Delta Premium Select (long-haul PE)
    ("PREMIUM SELECT", "premium_economy"),
    ("AFPE", "premium_economy"),  # Air France partner premium-economy leg
    ("PREMIUM", "premium_economy"),
    # --- Comfort+ is extra-legroom ECONOMY, NOT a separate cabin (must precede "FIRST"/"MAIN") ---
    ("COMFORT", "economy"),  # Delta Comfort+ — extra-legroom ECONOMY, not a separate cabin
    ("DCP", "economy"),  # Comfort+ branded-fare code (e.g. CDCP), seen live — economy
    # --- First (domestic First) — AFTER the premium tokens so a "CFIRST" leg can't steal Delta One.
    ("FIRST", "first"),  # Domestic First / First Class (live code "CFIRST")
    # --- Economy ---
    ("BASIC", "economy"),  # Basic Economy
    ("MAIN", "economy"),  # Main Cabin (live codes "CMAIN"/"BMAIN")
    ("ECONOMY", "economy"),
)


def _brand_to_cabin(brand_id: object) -> str | None:
    """Map a Delta brand id to a canonical cabin class, or None if unrecognised."""
    if not isinstance(brand_id, str) or not brand_id:
        return None
    key = brand_id.upper()
    for token, cabin in _CABIN_RULES:
        if token in key:
            return cabin
    return None


# Cabin precedence for the per-leg fallback (used ONLY when the offer-level dominant brand is
# unknown). Delta One (business) ranks ABOVE domestic First on purpose: a Delta One long-haul that
# connects through a domestic-First hub exposes legs of two cabins (["CFIRST", "CD1"]) and is sold
# as the Delta One product, so business must win over the incidental domestic-First connector.
# (Pure domestic First itineraries have every leg "first", so this never down-ranks a real First.)
_CABIN_RANK: dict[str, int] = {
    "business": 4,
    "first": 3,
    "premium_economy": 2,
    "economy": 1,
}


def _most_premium_cabin(cabins: list[str]) -> str | None:
    """Return the highest-precedence cabin among the given canonical cabins (per _CABIN_RANK),
    or None if empty. Used only when the offer's dominant brand is unknown."""
    ranked = [c for c in cabins if c in _CABIN_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda c: _CABIN_RANK[c])


def _parse_iso(s: object, iata: str) -> datetime | None:
    """Parse Delta's *LocalTs (local airport time) as the wall-clock time at airport `iata`,
    returning a timezone-AWARE datetime. Returns None on failure or unknown airport.

    Delta reports naive local airport times. We attach the airport's IANA timezone so the
    stored value is a correct UTC instant — otherwise a naive value lands in the TIMESTAMPTZ
    column as UTC, shifting every departure by the airport's offset (e.g. 4h at ATL).
    """
    if not isinstance(s, str) or not s:
        return None
    tz = AIRPORT_TZ.get(iata.upper())
    if tz is None:
        logger.warning("[DL] no timezone for %s — dropping time %r", iata, s)
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        return None


def _dhm_to_minutes(d: object) -> int | None:
    """Convert a Delta {dayCnt,hourCnt,minuteCnt} duration object to total minutes."""
    if not isinstance(d, dict):
        return None
    days = d.get("dayCnt") or 0
    hours = d.get("hourCnt") or 0
    mins = d.get("minuteCnt") or 0
    try:
        total = int(days) * 1440 + int(hours) * 60 + int(mins)
    except (TypeError, ValueError):
        return None
    return total if total > 0 else None


def _legs(trip: dict) -> list[dict]:
    """Flatten a trip's flightSegment[].flightLeg[] into a single ordered list of legs."""
    legs: list[dict] = []
    for seg in trip.get("flightSegment") or []:
        if isinstance(seg, dict):
            for leg in seg.get("flightLeg") or []:
                if isinstance(leg, dict):
                    legs.append(leg)
    return legs


class DeltaScraper(BrowserScraper):
    """
    Scraper for Delta Air Lines SkyMiles award availability.

    No authentication required — Delta's dotcom search calls this GraphQL endpoint with an
    anonymous ``authorization: GUEST`` token. The browser transport (warm session, pacing,
    in-page fetch, block handling) is inherited from BrowserScraper; this class only builds
    the request and parses the GraphQL response.

    Usage:
        scraper = DeltaScraper()
        records = scraper.scrape("BOS", "SEA", date(2026, 7, 2))
    """

    airline_code = "DL"
    program_name = "SkyMiles"
    source = "delta"

    # Browser transport: warm delta.com once per run, then in-page fetch the GraphQL endpoint.
    warm_url = "https://www.delta.com/"
    headless = False  # headful under xvfb on Fly — Akamai challenges headless Chrome

    # Deliberately light cadence: refresh every 6h, small window, conservative pacing.
    min_delay_s = 12.0
    block_threshold = 4
    refresh_interval_min = 360  # 6 hours
    scrape_days_ahead = 21
    # 90d horizon via dense/sparse: 14 every-day near dates + sparse-to-30 + coarse-to-90
    # → 27 dates/route (< the prior 30 every-day dates), so strictly under Delta's Akamai ceiling.
    dense_days = 14
    sparse_step = 4
    max_routes_per_run = 12

    async def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """Build the GraphQL request and run it as an in-page fetch() in the warmed Chrome
        session. Returns the parsed JSON dict ({} on a benign/empty/blocked-soft body).
        ScraperBlockedError is raised by _page_fetch after repeated WAF blocks."""
        body = _build_request_body(origin, dest, travel_date)
        # `accept` + `content-type` are injected by BrowserScraper._page_fetch; only the
        # Delta-specific headers go here.
        headers = {
            "airline": "DL",
            "applicationid": "DC",
            "authorization": "GUEST",
            "channelid": "DCOM",
            "transactionid": f"{uuid.uuid4()}_{int(time.time() * 1000)}",
            "x-app-route": "search",
            "x-app-type": "dcom-shop",
        }
        return await self._page_fetch(_API_URL, body, headers)

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list[FlightRecord]:
        """
        Map Delta's GraphQL response → list[FlightRecord].

        Each offer set pairs ``trips`` (itinerary/legs) with ``offers`` (one per branded
        fare = one cabin). We emit one FlightRecord per priced, available branded fare.
        """
        if not raw:
            return []

        # Null-safe at every level: Delta returns an explicit null (e.g. {"data": null} or
        # {"data": {"gqlSearchOffers": null}}) for no-offer routes/dates and soft errors. A present
        # key whose value is null makes dict.get return that null (not the default), so a naive
        # .get("k", {}).get(...) chain would do None.get(...) and raise AttributeError, silently
        # dropping that route. `(x.get("k") or {})` handles both a missing key AND an explicit null.
        offer_sets = (
            ((raw.get("data") or {}).get("gqlSearchOffers") or {}).get("gqlOffersSets") or []
        )
        if not offer_sets:
            return []

        now = datetime.now(timezone.utc)
        ttl_h = TTL_HOURS[PriorityTier.MED]
        expires_at = now + timedelta(hours=ttl_h)
        records: list[FlightRecord] = []

        for offer_set in offer_sets:
            if not isinstance(offer_set, dict):
                continue
            try:
                records.extend(
                    self._records_for_offer_set(
                        offer_set, origin, dest, travel_date, now, expires_at
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad set must not sink the run
                logger.warning("[DL] Error processing offer set: %s", exc, exc_info=True)
                continue

        return records

    def _records_for_offer_set(
        self,
        offer_set: dict,
        origin: str,
        dest: str,
        travel_date: date,
        now: datetime,
        expires_at: datetime,
    ) -> list[FlightRecord]:
        trips = offer_set.get("trips") or []
        trip = trips[0] if trips and isinstance(trips[0], dict) else None
        if trip is None:
            return []

        legs = _legs(trip)

        # --- Itinerary-level fields (shared across this set's fares) ---
        stops = trip.get("stopCnt")
        if not isinstance(stops, int):
            stops = max(0, len(legs) - 1)

        dep_time = _parse_iso(trip.get("scheduledDepartureLocalTs"), origin)
        arr_time = _parse_iso(trip.get("scheduledArrivalLocalTs"), dest)
        duration_mins = _dhm_to_minutes(trip.get("totalTripTime"))
        if duration_mins is None and dep_time and arr_time:
            delta = int((arr_time - dep_time).total_seconds() / 60)
            duration_mins = delta if delta > 0 else None

        # Flight number: prefer the flightSegment-level marketing carrier (carrierNum is the
        # marketing flight number); fall back to the leg carriers only if the segments yield
        # NO number (e.g. a response captured before the segment-carrier query enrichment).
        # "DL 123" single, "DL 123+DL 456" multi-segment.
        def _flight_nums(sources: list[dict]) -> list[str]:
            nums: list[str] = []
            for src in sources:
                if not isinstance(src, dict):
                    continue
                carrier = src.get("marketingCarrier") or src.get("operatingCarrier") or {}
                code = carrier.get("carrierCode") if isinstance(carrier, dict) else None
                num = carrier.get("carrierNum") if isinstance(carrier, dict) else None
                if code and num:
                    nums.append(f"{code} {num}")
            return nums

        segments = [s for s in (trip.get("flightSegment") or []) if isinstance(s, dict)]
        flight_nums = _flight_nums(segments) or _flight_nums(legs)
        raw_fn = "+".join(flight_nums) if flight_nums else "UNKNOWN"

        # Aircraft from the first leg.
        aircraft_str: str | None = None
        if legs:
            ac = legs[0].get("aircraft")
            if isinstance(ac, dict):
                code = ac.get("fleetTypeCode") or ac.get("subFleetTypeCode")
                if isinstance(code, str) and code:
                    aircraft_str = code[:10]

        # Layovers from each leg's `layover` block (present on all but the last leg).
        layover_iatas: list[str] = []
        layover_total = 0
        layover_has = False
        for leg in legs:
            lay = leg.get("layover")
            if isinstance(lay, dict):
                ap = lay.get("destinationAirportCode")
                if isinstance(ap, str) and len(ap) == 3:
                    layover_iatas.append(ap.upper())
                mins = _dhm_to_minutes(lay.get("layoverDuration"))
                if mins:
                    layover_total += mins
                    layover_has = True
        layover_airports_str = ",".join(layover_iatas) if layover_iatas else None
        layover_dur_mins = layover_total if layover_has else None

        next_day_arr = bool(arr_time and arr_time.date() > travel_date)

        # --- One FlightRecord per priced, available branded fare ---
        records: list[FlightRecord] = []
        for offer in offer_set.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            props = offer.get("additionalOfferProperties") or {}
            fare_type = props.get("fareType") if isinstance(props, dict) else None
            dominant_brand = (
                props.get("dominantSegmentBrandId") if isinstance(props, dict) else None
            )

            for fare_info in _iter_fare_information(offer):
                rec = self._build_record(
                    fare_info=fare_info,
                    dominant_brand=dominant_brand,
                    fare_type=fare_type,
                    origin=origin,
                    dest=dest,
                    travel_date=travel_date,
                    stops=stops,
                    dep_time=dep_time,
                    arr_time=arr_time,
                    duration_mins=duration_mins,
                    aircraft_str=aircraft_str,
                    raw_fn=raw_fn,
                    layover_airports_str=layover_airports_str,
                    layover_dur_mins=layover_dur_mins,
                    next_day_arr=next_day_arr,
                    now=now,
                    expires_at=expires_at,
                )
                if rec is not None:
                    records.append(rec)
        return records

    def _build_record(
        self,
        *,
        fare_info: dict,
        dominant_brand: object,
        fare_type: object,
        origin: str,
        dest: str,
        travel_date: date,
        stops: int,
        dep_time: datetime | None,
        arr_time: datetime | None,
        duration_mins: int | None,
        aircraft_str: str | None,
        raw_fn: str,
        layover_airports_str: str | None,
        layover_dur_mins: int | None,
        next_day_arr: bool,
        now: datetime,
        expires_at: datetime,
    ) -> FlightRecord | None:
        brands = fare_info.get("brandByFlightLegs") or []
        brand_ids = [b.get("brandId") for b in brands if isinstance(b, dict) and b.get("brandId")]

        # Cabin: trust the offer-level dominant brand first — it names the offer's actual cabin
        # (e.g. "CD1" Delta One) and is correct even on connecting itineraries. Only when the
        # dominant brand is missing/unknown do we fall back to the per-leg brands, and then we
        # pick the MOST PREMIUM leg cabin rather than the first leg. A Delta One itinerary that
        # connects through a domestic-First hub lists legs ["CFIRST", "CD1"]; taking the first leg
        # would mislabel Delta One as "first", so we keep the most premium across all legs.
        cabin = _brand_to_cabin(dominant_brand)
        if cabin is None:
            leg_cabins = [c for c in (_brand_to_cabin(bid) for bid in brand_ids) if c]
            cabin = _most_premium_cabin(leg_cabins)
        if cabin is None:
            logger.debug(
                "[DL] Unrecognised brand(s) %r / dominant %r — skipping",
                brand_ids,
                dominant_brand,
            )
            return None

        # Live Delta returns farePrice as a LIST of priced options; older synthesized fixtures
        # used a bare dict. Handle both (take the first priced option).
        fare_price_raw = fare_info.get("farePrice")
        if isinstance(fare_price_raw, list):
            fare_price_raw = fare_price_raw[0] if fare_price_raw else {}
        fare_price = (fare_price_raw or {}).get("totalFarePrice") or {}
        miles = (fare_price.get("milesEquivalentPrice") or {}).get("mileCnt")
        if not isinstance(miles, (int, float)) or miles <= 0:
            return None

        cash = (fare_price.get("currencyEquivalentPrice") or {}).get("roundedCurrencyAmt")
        seats = fare_info.get("availableSeatCnt")

        # cosCode = booking class (fare bucket), e.g. "X", "J".
        fare_class: str | None = None
        for b in brands:
            if isinstance(b, dict) and isinstance(b.get("cosCode"), str) and b["cosCode"]:
                fare_class = b["cosCode"][:10]
                break

        mixed_cabin = len({_brand_to_cabin(b) for b in brand_ids if _brand_to_cabin(b)}) > 1
        is_saver = isinstance(fare_type, str) and "AWARD" in fare_type.upper()

        try:
            return FlightRecord(
                origin=origin.upper(),
                destination=dest.upper(),
                date=travel_date,
                airline=self.airline_code,
                program=self.program_name,
                source=self.source,
                points_cost=int(miles),
                cash_cost=float(cash) if isinstance(cash, (int, float)) else 0.0,
                cabin_class=cabin,
                stops=stops,
                available_seats=int(seats) if isinstance(seats, int) else -1,
                scraped_at_utc=now,
                expires_at_utc=expires_at,
                raw_flight_number=raw_fn,
                partner_airline=None,
                departure_time_local=dep_time,
                arrival_time_local=arr_time,
                duration_minutes=duration_mins,
                aircraft_type=aircraft_str,
                is_saver=is_saver,
                fare_class=fare_class,
                layover_airports=layover_airports_str,
                layover_duration_minutes=layover_dur_mins,
                next_day_arrival=next_day_arr,
                mixed_cabin=mixed_cabin,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[DL] Skipping invalid record: %s", exc)
            return None


def _iter_fare_information(offer: dict):
    """Yield each fareInformation dict nested under an offer's offerItems→retailItems."""
    for offer_item in offer.get("offerItems") or []:
        if not isinstance(offer_item, dict):
            continue
        for retail_item in offer_item.get("retailItems") or []:
            if not isinstance(retail_item, dict):
                continue
            meta = retail_item.get("retailItemMetaData") or {}
            for fare_info in meta.get("fareInformation") or []:
                if isinstance(fare_info, dict):
                    yield fare_info


def _build_request_body(origin: str, dest: str, travel_date: date) -> dict:
    """Build the GraphQL request body for a one-way MILES search (one ADT passenger)."""
    return {
        "variables": {
            "offerSearchCriteria": {
                "productGroups": [{"productCategoryCode": "FLIGHTS"}],
                "offersCriteria": {
                    "resultsPageNum": 1,
                    "resultsPerRequestNum": 20,
                    "preferences": {
                        "refundableOnly": False,
                        "showGlobalRegionalUpgradeCertificate": True,
                        "nonStopOnly": False,
                        "excludeBrandTypes": [],
                    },
                    "pricingCriteria": {"priceableIn": ["MILES"]},
                    "flightRequestCriteria": {
                        "currentTripIndexId": "0",
                        "sortableOptionId": None,
                        "selectedOfferId": "",
                        "searchOriginDestination": [
                            {
                                "departureLocalTs": f"{travel_date.strftime('%Y-%m-%d')}T00:00:00",
                                "destinations": [{"airportCode": dest.upper()}],
                                "origins": [{"airportCode": origin.upper()}],
                            }
                        ],
                        "sortByBrandId": "MAIN",
                        "additionalCriteriaMap": {"rollOutTag": "GBB"},
                    },
                },
                "customers": [{"passengerTypeCode": "ADT", "passengerId": "1"}],
            }
        },
        "query": _GRAPHQL_QUERY,
    }


# The GraphQL query, kept verbatim — it defines the exact selection set normalize() parses.
_GRAPHQL_QUERY = (
    "query ($offerSearchCriteria: OfferSearchCriteriaInput!) { "
    "gqlSearchOffers(offerSearchCriteria: $offerSearchCriteria) { "
    "offerResponseId gqlOffersSets { "
    "trips { tripId scheduledDepartureLocalTs scheduledArrivalLocalTs "
    "originAirportCode destinationAirportCode stopCnt "
    "totalTripTime { dayCnt hourCnt minuteCnt } "
    "flightSegment { "
    "marketingCarrier { carrierCode carrierNum } "
    "operatingCarrier { carrierCode carrierNum } "
    "flightLeg { legId "
    "marketingCarrier { carrierCode carrierNum } "
    "operatingCarrier { carrierCode carrierNum } "
    "aircraft { fleetTypeCode subFleetTypeCode } "
    "duration { dayCnt hourCnt minuteCnt } "
    "layover { destinationAirportCode layoverDuration { dayCnt hourCnt minuteCnt } } } } } "
    "offers { offerId "
    "additionalOfferProperties { fareType dominantSegmentBrandId } "
    "offerItems { retailItems { retailItemMetaData { fareInformation { "
    "brandByFlightLegs { brandId cosCode } availableSeatCnt "
    "farePrice { totalFarePrice { milesEquivalentPrice { mileCnt } "
    "currencyEquivalentPrice { roundedCurrencyAmt } } } } } } } } } } }"
)
