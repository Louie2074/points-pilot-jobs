import asyncio
import json
import logging
from datetime import date

import pytest

import browser_scrape_common as common
from scrapers.base import ScraperBlockedError
from scrapers.turkish import (
    TurkishResponseError,
    TurkishScraper,
    _cabin_miles,
    _flight_number,
    _parse_tk_dt,
)

TRAVEL = date(2026, 7, 7)


def _fare(amount, currency="MILE", brand="Y"):
    """A fareCategory cabin entry priced at `amount` miles."""
    return {
        "bookingPriceInfoList": [
            {
                "referencePassengerFare": {
                    "totalFare": {"currencyCode": currency, "amount": amount}
                },
                "brandCode": brand,
            }
        ]
    }


def _seg(o="JFK", d="IST", dep="07-07-2026 00:25", arr="07-07-2026 17:15", num="0012"):
    return {
        "departureAirportCode": o,
        "arrivalAirportCode": d,
        "departureDateTime": dep,
        "arrivalDateTime": arr,
        "flightCode": {"airlineCode": "TK", "flightNumber": num},
        "carrierAirline": "TK",
        "equipmentCode": "77W",
    }


def _opt(segs=None, fares=None, seats=4):
    return {
        "segmentList": segs or [_seg()],
        "lastSeatCount": seats,
        "fareCategory": fares
        if fares is not None
        else {"ECONOMY": _fare(55000), "BUSINESS": _fare(135000, brand="C")},
    }


def _resp(options):
    return {
        "data": {"originDestinationInformationList": [{"originDestinationOptionList": options}]},
        "success": True,
    }


# ---------------------------------------------------------------- pure helpers
def test_flight_number_strips_leading_zeros():
    assert _flight_number({"flightCode": {"airlineCode": "TK", "flightNumber": "0012"}}) == "TK 12"


def test_flight_number_missing_returns_none():
    assert _flight_number({"flightCode": {}}) is None and _flight_number({}) is None


def test_parse_tk_dt_attaches_origin_timezone():
    dt = _parse_tk_dt("07-07-2026 19:15", "JFK")
    assert dt is not None and dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 7, 19, 15)


def test_parse_tk_dt_unmapped_airport_is_none():
    # BKK is an onward award dest not in airport_tz (IST is now mapped → Europe/Istanbul)
    assert _parse_tk_dt("07-07-2026 17:15", "BKK") is None and _parse_tk_dt("", "JFK") is None


def test_cabin_miles_reads_mile_total_fare():
    assert _cabin_miles(_fare(135000)) == 135000
    assert _cabin_miles(_fare(500, currency="USD")) is None  # cash, not miles
    assert _cabin_miles(_fare(0)) is None
    assert _cabin_miles({}) is None


# ---------------------------------------------------------------- normalize
def test_normalize_reads_distinct_per_cabin_prices():
    # the bug this guards: economy 55k and business 135k must NOT both come out 55k
    recs = TurkishScraper().normalize(_resp([_opt()]), "JFK", "IST", TRAVEL)
    by_cabin = {r.cabin_class: r for r in recs}
    assert set(by_cabin) == {"economy", "business"}
    assert by_cabin["economy"].points_cost == 55000
    assert by_cabin["business"].points_cost == 135000
    eco = by_cabin["economy"]
    assert eco.origin == "JFK" and eco.destination == "IST" and eco.airline == "TK"
    assert eco.source == "turkish" and eco.program == "Miles&Smiles"
    assert eco.stops == 0 and eco.raw_flight_number == "TK 12" and eco.available_seats == 4
    assert eco.departure_time_local is not None  # JFK is mapped


def test_normalize_connection_sets_stops_and_layover():
    segs = [
        _seg("ORD", "IST", "07-07-2026 21:55", "08-07-2026 16:30", "0006"),
        _seg("IST", "BKK", "08-07-2026 20:10", "09-07-2026 09:25", "0068"),
    ]
    recs = TurkishScraper().normalize(_resp([_opt(segs=segs)]), "ORD", "BKK", TRAVEL)
    assert recs
    r = recs[0]
    assert r.stops == 1 and r.layover_airports == "IST" and r.raw_flight_number == "TK 6+TK 68"


def test_normalize_skips_cash_and_unmapped_cabins():
    fares = {"ECONOMY": _fare(500, currency="USD"), "WEIRDCABIN": _fare(99000)}
    assert TurkishScraper().normalize(_resp([_opt(fares=fares)]), "JFK", "IST", TRAVEL) == []


def test_normalize_accepts_valid_empty_options():
    assert TurkishScraper().normalize(_resp([]), "JFK", "IST", TRAVEL) == []


def test_provider_failure_is_not_retried(monkeypatch):
    payload = {"success": False, "actionCode": "NO_AVAILABILITY", "data": None}
    wire = json.dumps(
        {
            "kind": "response",
            "status": 200,
            "attempts": 1,
            "statuses": [200],
            "text": json.dumps(payload),
        }
    )
    scraper, raw = _fetch_with_scraper(monkeypatch, wire)

    with pytest.raises(TurkishResponseError) as exc:
        scraper.normalize(raw, "JFK", "IST", TRAVEL)

    assert exc.value.category == "provider_failed"
    assert scraper.request_stats().attempts == 1
    assert scraper.request_stats().retries == 0


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        ({"success": False, "data": None, "message": "secret-body"}, "provider_failed"),
        ({"success": True, "data": {}}, "missing_envelope"),
        ({"success": True}, "missing_envelope"),
        ({}, "missing_envelope"),
        (_resp([{"segmentList": "not-a-list", "fareCategory": {}}]), "malformed_options"),
        (_resp([{"segmentList": [_seg()], "fareCategory": "not-a-map"}]), "malformed_options"),
    ],
)
def test_normalize_classifies_provider_and_option_failures(raw, category):
    with pytest.raises(TurkishResponseError) as exc:
        TurkishScraper().normalize(raw, "JFK", "IST", TRAVEL)

    assert exc.value.category == category
    diagnostic = str(exc.value)
    assert len(diagnostic) <= 200
    assert "secret-body" not in diagnostic
    assert "not-a-list" not in diagnostic
    assert "not-a-map" not in diagnostic


def test_response_diagnostic_reports_only_bounded_contract_shape(monkeypatch):
    payload = {
        "zKey": 1,
        "success": False,
        "statusCode": 451,
        "status": "ERROR",
        "message": "provider-message-must-not-appear",
        "errorCode": "TK_AVAIL_42",
        "actionCode": "NO_AVAILABILITY",
        "actionData": {"code": "ACTION_DATA_SECRET"},
        "statusDetailList": [
            {
                "code": "TK_DETAIL_1",
                "status": "FAILED",
                "message": "DETAIL_MESSAGE_SECRET",
                "nested": {"code": "NESTED_CODE_SECRET"},
            },
            {"statusCode": 451},
        ],
        "data": None,
    }
    wire = json.dumps(
        {
            "kind": "response",
            "status": 503,
            "attempts": 1,
            "statuses": [503],
            "text": json.dumps(payload),
        }
    )
    raw = _fetch_result(monkeypatch, wire)

    with pytest.raises(TurkishResponseError) as exc:
        TurkishScraper().normalize(raw, "JFK", "IST", TRAVEL)

    diagnostic = str(exc.value)
    assert "category=provider_failed" in diagnostic
    assert "http_status=503" in diagnostic
    assert "actionCode=NO_AVAILABILITY" in diagnostic
    assert "statusDetailList.code=TK_DETAIL_1" in diagnostic
    assert "statusDetailList.status=FAILED" in diagnostic
    assert "statusDetailList.statusCode=451" in diagnostic
    assert "data_type=NoneType" in diagnostic
    assert "errorCode=TK_AVAIL_42" not in diagnostic
    assert "status=ERROR" not in diagnostic
    assert "provider-message-must-not-appear" not in diagnostic
    assert "ACTION_DATA_SECRET" not in diagnostic
    assert "DETAIL_MESSAGE_SECRET" not in diagnostic
    assert "NESTED_CODE_SECRET" not in diagnostic


def test_response_diagnostic_redacts_secret_values_from_exception_and_logs(monkeypatch, caplog):
    markers = {
        "provider_message": "MESSAGE_SECRET_MARKER",
        "action_data": "ACTION_DATA_SECRET_MARKER",
        "body": "BODY_SECRET_MARKER",
        "header": "HEADER_SECRET_MARKER",
        "cookie": "COOKIE_SECRET_MARKER",
        "nested": "NESTED_SECRET_MARKER",
        "request": "REQUEST_SECRET_MARKER",
        "uuid": "123e4567-e89b-12d3-a456-426614174000",
    }
    payload = {
        "success": False,
        "data": {"deep": {"value": markers["nested"]}},
        "message": markers["provider_message"],
        "actionData": {"value": markers["action_data"]},
        "body": markers["body"],
        "headers": {"Authorization": markers["header"]},
        "cookies": markers["cookie"],
        "requestData": {"payload": markers["request"]},
        "requestId": markers["uuid"],
        "errorCode": "LEGACY_CODE_MUST_NOT_APPEAR",
        "actionCode": "TK_BLOCKED",
    }
    wire = json.dumps(
        {
            "kind": "response",
            "status": 429,
            "attempts": 1,
            "statuses": [429],
            "text": json.dumps(payload),
        }
    )
    raw = _fetch_result(monkeypatch, wire)

    with pytest.raises(TurkishResponseError) as exc:
        TurkishScraper().normalize(raw, "JFK", "IST", TRAVEL)

    with caplog.at_level(logging.WARNING, logger="test-turkish-diagnostic"):
        logging.getLogger("test-turkish-diagnostic").warning("Turkish failed: %s", exc.value)

    rendered = f"{exc.value}\n{caplog.text}"
    for marker in markers.values():
        assert marker not in rendered
    for forbidden_field in (
        "actionData",
        "message",
        "body",
        "headers",
        "cookies",
        "requestData",
        "requestId",
    ):
        assert forbidden_field not in rendered
    assert "actionCode=TK_BLOCKED" in rendered
    assert "LEGACY_CODE_MUST_NOT_APPEAR" not in rendered
    assert len(str(exc.value)) <= 320


def _perimeterx_block_payload() -> tuple[dict, list[str]]:
    values = [
        "ALT_SCRIPT_SECRET",
        "APP_ID_SECRET",
        "BLOCK_SCRIPT_SECRET",
        "CUSTOM_LOGO_SECRET",
        "HOST_URL_SECRET",
        "JS_CLIENT_SECRET",
        "123e4567-e89b-12d3-a456-426614174000",
        "VID_SECRET",
    ]
    return (
        {
            "altBlockScript": values[0],
            "appId": values[1],
            "blockScript": values[2],
            "customLogo": values[3],
            "firstPartyEnabled": True,
            "hostUrl": values[4],
            "jsClientSrc": values[5],
            "uuid": values[6],
            "vid": values[7],
        },
        values,
    )


def test_http_403_perimeterx_envelope_raises_shared_block_error(monkeypatch):
    payload, secret_values = _perimeterx_block_payload()
    wire = json.dumps(
        {
            "kind": "response",
            "status": 403,
            "attempts": 1,
            "statuses": [403],
            "text": json.dumps(payload),
        }
    )
    raw = _fetch_result(monkeypatch, wire)

    with pytest.raises(ScraperBlockedError) as exc:
        TurkishScraper().normalize(raw, "JFK", "IST", TRAVEL)

    rendered = str(exc.value)
    assert "PerimeterX" in rendered
    for value in secret_values:
        assert value not in rendered


def test_http_403_perimeterx_envelope_reports_blocked_run_without_secret_logs(monkeypatch, caplog):
    payload, secret_values = _perimeterx_block_payload()
    wire = json.dumps(
        {
            "kind": "response",
            "status": 403,
            "attempts": 1,
            "statuses": [403],
            "text": json.dumps(payload),
        }
    )
    raw = _fetch_result(monkeypatch, wire)
    metrics = []
    scraper = TurkishScraper()

    monkeypatch.setattr(
        scraper,
        "scrape",
        lambda origin, dest, travel: scraper.normalize(raw, origin, dest, travel),
    )
    monkeypatch.setattr(scraper, "close", lambda: None)
    monkeypatch.setattr("pipeline.obs.ship_metric", lambda metric: metrics.append(metric))
    monkeypatch.setattr(common, "freshness", lambda *args, **kwargs: {})
    monkeypatch.setattr("pp_db.autocommit.close_connection", lambda: None)

    with caplog.at_level(logging.WARNING):
        outcome = common.run_scrape(
            scraper,
            [("JFK", "IST")],
            [TRAVEL],
            source="turkish",
            service="point-pilot-turkish",
            airline="TK",

            logger=logging.getLogger("test-turkish-block"),
        )

    assert outcome.status == "blocked"
    assert outcome.exit_code == 1
    assert metrics[0]["status"] == "blocked"
    assert metrics[0]["blocked"] is True
    assert metrics[0]["data_health_confirmed"] is False
    for value in secret_values:
        assert value not in caplog.text


def test_non_2xx_valid_shaped_response_is_bounded_http_error(monkeypatch):
    payload = _resp([_opt()])
    payload["message"] = "NON_2XX_BODY_SECRET"
    wire = json.dumps(
        {
            "kind": "response",
            "status": 503,
            "attempts": 1,
            "statuses": [503],
            "text": json.dumps(payload),
        }
    )
    raw = _fetch_result(monkeypatch, wire)

    with pytest.raises(TurkishResponseError) as exc:
        TurkishScraper().normalize(raw, "JFK", "IST", TRAVEL)

    assert exc.value.category == "http_error"
    diagnostic = str(exc.value)
    assert "http_status=503" in diagnostic
    assert "NON_2XX_BODY_SECRET" not in diagnostic


class _EvaluateTab:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def evaluate(self, *_args, **_kwargs):
        self.calls += 1
        return self.value


def _fetch_result(monkeypatch, evaluated):
    scraper = TurkishScraper()

    return _fetch_with_existing_scraper(monkeypatch, scraper, evaluated)


def _fetch_with_scraper(monkeypatch, evaluated):
    scraper = TurkishScraper()
    return scraper, _fetch_with_existing_scraper(monkeypatch, scraper, evaluated)


def _fetch_with_existing_scraper(monkeypatch, scraper, evaluated):
    async def fake_ensure_browser():
        return _EvaluateTab(evaluated)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scraper, "_ensure_browser", fake_ensure_browser)
    monkeypatch.setattr("scrapers.turkish.asyncio.sleep", no_sleep)
    return asyncio.run(scraper.fetch_raw("JFK", "IST", TRAVEL))


def test_turkish_challenge_has_one_retry_maximum(monkeypatch):
    scraper = TurkishScraper()
    assert scraper._px_retries == 1
    wire = json.dumps(
        {
            "kind": "challenge",
            "status": 428,
            "attempts": 2,
            "statuses": [428, 428],
        }
    )

    tab = _EvaluateTab(wire)

    async def fake_ensure_browser():
        return tab

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scraper, "_ensure_browser", fake_ensure_browser)
    monkeypatch.setattr("scrapers.turkish.asyncio.sleep", no_sleep)

    with pytest.raises(TurkishResponseError, match="category=challenge"):
        asyncio.run(scraper.fetch_raw("JFK", "IST", TRAVEL))

    stats = scraper.request_stats()
    assert stats.attempts == 2
    assert stats.retries == 1
    assert tab.calls == 1


def test_fetch_raw_returns_populated_response(monkeypatch):
    response = _resp([_opt()])
    wire = json.dumps(
        {
            "kind": "response",
            "status": 200,
            "attempts": 1,
            "statuses": [200],
            "text": json.dumps(response),
        }
    )

    assert _fetch_result(monkeypatch, wire) == response


def test_fetch_raw_records_each_bounded_attempt_and_status(monkeypatch):
    response = _resp([_opt()])
    wire = json.dumps(
        {
            "kind": "response",
            "status": 503,
            "attempts": 2,
            "statuses": [428, 503],
            "text": json.dumps(response),
        }
    )

    scraper, raw = _fetch_with_scraper(monkeypatch, wire)

    assert raw == response
    stats = scraper.request_stats()
    assert stats.attempts == 2
    assert stats.retries == 1
    assert stats.server_errors == 1


def test_fetch_raw_records_transport_attempt_without_an_http_status(monkeypatch):
    wire = json.dumps({"kind": "transport", "attempts": 2, "statuses": [428]})
    scraper = TurkishScraper()

    with pytest.raises(TurkishResponseError, match="category=transport"):
        _fetch_with_existing_scraper(monkeypatch, scraper, wire)

    stats = scraper.request_stats()
    assert stats.attempts == 2
    assert stats.retries == 1


@pytest.mark.parametrize(
    ("attempts", "statuses", "status"),
    [
        (None, [], 200),
        (True, [], 200),
        (-1, [], 200),
        (3, [], 200),
        (1.0, [], 200),
        (1, [True], 200),
        (1, ["200"], 200),
        (1, [200, 200], 200),
        (1, [200], True),
    ],
)
def test_fetch_raw_rejects_malformed_attempt_metadata_without_counting(
    monkeypatch, attempts, statuses, status
):
    scraper = TurkishScraper()
    wire = {
        "kind": "response",
        "status": status,
        "statuses": statuses,
        "text": json.dumps(_resp([])),
    }
    if attempts is not None:
        wire["attempts"] = attempts

    with pytest.raises(TurkishResponseError, match="category=transport"):
        _fetch_with_existing_scraper(monkeypatch, scraper, json.dumps(wire))

    assert scraper.request_stats().attempts == 0


@pytest.mark.parametrize(
    ("evaluated", "category"),
    [
        (None, "transport"),
        ("<html>cookie=super-secret</html>", "non_json"),
        (
            json.dumps(
                {
                    "kind": "challenge",
                    "status": 428,
                    "attempts": 2,
                    "statuses": [428, 428],
                }
            ),
            "challenge",
        ),
        (json.dumps({"kind": "transport", "attempts": 1, "statuses": []}), "transport"),
    ],
)
def test_fetch_raw_classifies_transport_non_json_and_exhausted_challenge(
    monkeypatch, evaluated, category
):
    with pytest.raises(TurkishResponseError) as exc:
        _fetch_result(monkeypatch, evaluated)

    assert exc.value.category == category
    diagnostic = str(exc.value)
    assert len(diagnostic) <= 200
    assert "super-secret" not in diagnostic
    assert "cookie" not in diagnostic.lower()
    assert "header" not in diagnostic.lower()


# ---------------------------------------------------------------- request builder
def test_build_js_encodes_award_route_and_date():
    js = TurkishScraper()._build_js("JFK", "IST", TRAVEL)
    assert "moduleType" in js and "AWARD" in js
    assert '"JFK"' in js and '"IST"' in js and "07-07-2026" in js
    assert "/api/v1/availability" in js
