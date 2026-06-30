from config.airport_tz import AIRPORT_TZ as CONFIG_AIRPORT_TZ
from pp_db.airport_tz import AIRPORT_TZ as CASH_AIRPORT_TZ


def test_eze_timezone_is_available_for_jetblue_and_cash_matching():
    assert CONFIG_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
    assert CASH_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
