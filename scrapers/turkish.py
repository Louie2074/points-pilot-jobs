"""Turkish Airlines Miles&Smiles award availability scraper.

Runs on the BrowserScraper (nodriver/Chrome) transport: turkishairlines.com is blocked to plain
httpx at the TLS/HTTP-2 fingerprint level AND fronted by PerimeterX, but an in-page fetch() inside
a warmed turkishairlines.com Chrome session clears both from a GitHub Actions (Azure) datacenter
IP (proven 2026-06-15). Canonical home for the Turkish browser scraper; run on a daily GH Actions
cron in this (points-pilot-jobs) repo, like Delta/Southwest.

Endpoint: the public dotcom award-availability API ``/api/v1/availability`` — no login. The award
search is keyed by ``moduleType: "AWARD"``; the "session" headers (X-conversationId / X-clientId /
X-requestId) are client-generated UUIDs the API accepts as-is (no server-side session mint needed),
so each call is self-contained. The response nests, under
``data.originDestinationInformationList[0].originDestinationOptionList[]``, one option per priced
itinerary; ``option.startingPrice`` carries the miles (``currencyCode: "MILE"``) and
``option.segmentList[]`` the legs. We run one call per cabin (Economy, Business) and emit one
FlightRecord per priced option.

The browser transport (warm session, pacing, challenge/block → ScraperBlockedError) is inherited
from BrowserScraper; this class only builds the request and maps the response to FlightRecords.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.airport_tz import AIRPORT_TZ
from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord
from scrapers.browser import BrowserScraper

logger = logging.getLogger(__name__)

_API_URL = "https://www.turkishairlines.com/api/v1/availability"

# Cabins to search (one availability call each). Turkish award sells Economy + Business.
# Maps the API's selectedCabinClass token → our canonical cabin_class.
_CABINS: tuple[tuple[str, str], ...] = (
    ("ECONOMY", "economy"),
    ("BUSINESS", "business"),
)


def _parse_tk_dt(s: object, iata: str) -> datetime | None:
    """Parse Turkish's "DD-MM-YYYY HH:MM" local airport time as a tz-aware datetime at `iata`.

    Like Delta, Turkish reports naive local airport times; we attach the airport's IANA zone so
    the stored instant is correct. Returns None on failure or unmapped airport (foreign
    destinations are often unmapped — the time is simply dropped, not fatal)."""
    if not isinstance(s, str) or not s:
        return None
    tz = AIRPORT_TZ.get(iata.upper())
    if tz is None:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=ZoneInfo(tz))
    except (ValueError, TypeError):
        return None


def _flight_number(seg: dict) -> str | None:
    """"TK 12" from a segment's flightCode {airlineCode, flightNumber}."""
    fc = seg.get("flightCode")
    if not isinstance(fc, dict):
        return None
    code = fc.get("airlineCode")
    num = fc.get("flightNumber")
    if not code or num is None:
        return None
    try:
        return f"{code} {int(num)}"
    except (TypeError, ValueError):
        return f"{code} {num}"


class TurkishScraper(BrowserScraper):
    """Scraper for Turkish Airlines Miles&Smiles award availability (no login)."""

    airline_code = "TK"
    program_name = "Miles&Smiles"
    source = "turkish"

    # Browser transport: warm the award-booking page once per run (seeds PerimeterX + cookies),
    # then in-page fetch the availability API. Headful under xvfb — PX scores headless harshly.
    # NB: warm on the booking page, NOT the homepage — the homepage's persistent connections hang
    # nodriver's navigation (proven 2026-06-16); the booking page loads to readyState=complete.
    warm_url = "https://www.turkishairlines.com/en-us/miles-and-smiles/book-award-tickets/"
    headless = False
    nav_wait_s = 12.0  # let the PerimeterX sensor run + settle before the first fetch

    # On the Azure IP, PerimeterX challenges the availability call with an HTTP 428 crypto
    # challenge (``sec-cp-challenge``); PX's own JS solves it in the background within a few
    # seconds, after which a retry returns data. We retry up to this many times, waiting between.
    _px_retries = 4
    _px_wait_s = 10.0

    # Conservative cadence (mirrors Delta): light window, gentle pacing.
    min_delay_s = 8.0
    block_threshold = 4
    refresh_interval_min = 360  # 6 hours
    scrape_days_ahead = 21
    dense_days = 10
    sparse_step = 4
    max_routes_per_run = 12

    def _ensure_loop(self):
        """Drive the browser on nodriver's OWN event loop. nodriver binds its CDP connection
        reader to ``uc.loop()``; using BrowserScraper's default fresh ``new_event_loop()`` trips
        ``AssertionError: cannot call get() concurrently`` on the 2nd+ CDP op (this scraper makes
        several: one availability fetch per cabin, plus PerimeterX retries). Delta/Southwest only
        do a single fetch per scrape so never hit it; we do, so align with nodriver's loop."""
        import nodriver as uc

        if self._loop is None or self._loop.is_closed():
            self._loop = uc.loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def _build_js(self, origin: str, dest: str, travel_date: date) -> str:
        """One self-contained in-page async script that fetches BOTH cabins' availability and
        retries PerimeterX 428 challenges — all inside the browser. Doing it in a single
        tab.evaluate keeps it to ONE CDP op per scrape: nodriver 0.50.3 + websockets 16.0 trip
        "cannot call get() concurrently" when several CDP ops run within one scrape, so we
        consolidate (and the session UUIDs / PX-solve all happen browser-side, which is faster)."""
        cabins = [api for api, _ in _CABINS]
        date_str = travel_date.strftime("%d-%m-%Y")
        return (
            "(async () => {"
            f"  const URL={json.dumps(_API_URL)}, CABINS={json.dumps(cabins)};"
            f"  const O={json.dumps(origin.upper())}, D={json.dumps(dest.upper())}, DT={json.dumps(date_str)};"
            f"  const RETRIES={self._px_retries}, WAIT={int(self._px_wait_s * 1000)};"
            "   const uuid=()=>crypto.randomUUID();"
            "   const sleep=ms=>new Promise(r=>setTimeout(r,ms));"
            "   const hdrs=()=>({'Accept':'application/json','Content-Type':'application/json',"
            "     'Accept-Language':'en','X-clientId':uuid(),'X-requestId':uuid(),'X-country':'us',"
            "     'X-platform':'WEB','X-conversationId':uuid()});"
            "   const body=(c)=>({selectedBookerSearch:'O',selectedCabinClass:c,moduleType:'AWARD',"
            "     passengerTypeList:[{quantity:1,code:'ADULT'}],originDestinationInformationList:"
            "     [{originAirportCode:O,destinationAirportCode:D,departureDate:DT}],"
            "     savedDate:new Date().toISOString()});"
            "   async function getCabin(c){for(let i=0;i<=RETRIES;i++){let r,t;"
            "     try{r=await fetch(URL,{method:'POST',headers:hdrs(),body:JSON.stringify(body(c)),"
            "       credentials:'include'});t=await r.text();}catch(e){return null;}"
            "     if(r.status===428||t.indexOf('sec-cp-challenge')>=0){await sleep(WAIT);continue;}"
            "     try{return JSON.parse(t);}catch(e){return null;}}return null;}"
            "   const out={};"
            "   for(const c of CABINS){out[c.toLowerCase()]=await getCabin(c);await sleep(1000);}"
            "   return JSON.stringify(out);"
            "})()"
        )

    async def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """Run the consolidated availability script as ONE in-page evaluate in the warmed session.
        Returns {"economy": <resp|None>, "business": <resp|None>} ({} on transport failure)."""
        tab = await self._ensure_browser()
        await asyncio.sleep(random.uniform(self.min_delay_s, self.min_delay_s * 2))  # pacing
        out = await tab.evaluate(self._build_js(origin, dest, travel_date), await_promise=True)
        if not isinstance(out, str):
            logger.warning("[TK] in-page evaluate returned non-str (JS error?): %r", out)
            return {}
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return {}

    def normalize(
        self, raw: dict, origin: str, dest: str, travel_date: date
    ) -> list[FlightRecord]:
        """Map the per-cabin availability responses → FlightRecords (one per priced option)."""
        if not raw:
            return []
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=TTL_HOURS[PriorityTier.MED])
        records: list[FlightRecord] = []

        for cabin, resp in raw.items():
            if not isinstance(resp, dict):
                continue
            data = resp.get("data")
            if not isinstance(data, dict):
                continue  # success:false / empty / soft-blocked
            od_list = data.get("originDestinationInformationList") or []
            if not od_list or not isinstance(od_list[0], dict):
                continue
            options = od_list[0].get("originDestinationOptionList") or []
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                try:
                    rec = self._build_record(
                        opt, cabin, origin, dest, travel_date, now, expires_at
                    )
                except Exception as exc:  # noqa: BLE001 — one bad option must not sink the run
                    logger.warning("[TK] error on option: %s", exc, exc_info=True)
                    rec = None
                if rec is not None:
                    records.append(rec)
        return records

    def _build_record(
        self,
        opt: dict,
        cabin: str,
        origin: str,
        dest: str,
        travel_date: date,
        now: datetime,
        expires_at: datetime,
    ) -> FlightRecord | None:
        segs = [s for s in (opt.get("segmentList") or []) if isinstance(s, dict)]
        if not segs:
            return None

        price = opt.get("startingPrice") or {}
        if price.get("currencyCode") != "MILE":
            return None
        miles = price.get("amount")
        if not isinstance(miles, (int, float)) or miles <= 0:
            return None

        stops = max(0, len(segs) - 1)
        dep_time = _parse_tk_dt(segs[0].get("departureDateTime"), origin)
        arr_time = _parse_tk_dt(segs[-1].get("arrivalDateTime"), dest)

        duration_mins: int | None = None
        if dep_time and arr_time:
            d = int((arr_time - dep_time).total_seconds() / 60)
            duration_mins = d if d > 0 else None

        nums = [n for n in (_flight_number(s) for s in segs) if n]
        raw_fn = "+".join(nums) if nums else "UNKNOWN"

        aircraft = segs[0].get("equipmentCode")
        aircraft_str = aircraft[:10] if isinstance(aircraft, str) and aircraft else None

        layovers = [
            s.get("arrivalAirportCode")
            for s in segs[:-1]
            if isinstance(s.get("arrivalAirportCode"), str)
        ]
        layover_str = ",".join(layovers) if layovers else None

        seats = opt.get("lastSeatCount")
        seats_int = int(seats) if isinstance(seats, int) and seats >= 0 else -1

        # operating carrier: flag a partner if any leg isn't flown by TK
        operating = {
            s.get("carrierAirline")
            for s in segs
            if isinstance(s.get("carrierAirline"), str)
        }
        partner = next((c for c in operating if c and c != self.airline_code), None)

        brand = opt.get("selectedBrandCode")
        fare_class = brand[:10] if isinstance(brand, str) and brand else None

        try:
            return FlightRecord(
                origin=origin.upper(),
                destination=dest.upper(),
                date=travel_date,
                airline=self.airline_code,
                program=self.program_name,
                source=self.source,
                points_cost=int(miles),
                cash_cost=0.0,  # taxes/fees not in the availability response (priced at booking)
                cabin_class=cabin,
                stops=stops,
                available_seats=seats_int,
                scraped_at_utc=now,
                expires_at_utc=expires_at,
                raw_flight_number=raw_fn,
                partner_airline=partner,
                departure_time_local=dep_time,
                arrival_time_local=arr_time,
                duration_minutes=duration_mins,
                aircraft_type=aircraft_str,
                is_saver=False,
                fare_class=fare_class,
                layover_airports=layover_str,
                layover_duration_minutes=None,
                next_day_arrival=bool(arr_time and arr_time.date() > travel_date),
                mixed_cabin=False,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[TK] dropping invalid record %s→%s: %s", origin, dest, exc)
            return None
