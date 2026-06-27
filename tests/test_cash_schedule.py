from pipeline.cash_schedule import cabins_for_run, sleep_seconds


def test_sleep_is_remainder_when_run_fits_interval():
    # interval 240 min, run took 150 min -> sleep the 90-min remainder.
    assert sleep_seconds(interval_s=240 * 60, elapsed_s=150 * 60, min_rest_s=10 * 60) == 90 * 60


def test_sleep_floors_at_min_rest_when_run_overruns():
    # run took longer than the interval -> never back-to-back; floor at min_rest.
    assert sleep_seconds(interval_s=240 * 60, elapsed_s=300 * 60, min_rest_s=10 * 60) == 10 * 60


def test_sleep_floors_at_min_rest_when_remainder_below_floor():
    # remainder (5 min) is below the floor (10 min) -> floor wins.
    assert sleep_seconds(interval_s=240 * 60, elapsed_s=235 * 60, min_rest_s=10 * 60) == 10 * 60


def test_sleep_returns_int():
    assert isinstance(sleep_seconds(interval_s=100.0, elapsed_s=10.0, min_rest_s=5.0), int)


BASE = ("economy", "business", "premium_economy")


def test_pe_kept_on_every_nth_run():
    assert cabins_for_run(BASE, run_index=0, pe_every_n=4) == BASE
    assert cabins_for_run(BASE, run_index=4, pe_every_n=4) == BASE


def test_pe_dropped_on_intermediate_runs():
    assert cabins_for_run(BASE, run_index=1, pe_every_n=4) == ("economy", "business")
    assert cabins_for_run(BASE, run_index=3, pe_every_n=4) == ("economy", "business")


def test_pe_every_n_of_one_keeps_pe_every_run():
    assert cabins_for_run(BASE, run_index=1, pe_every_n=1) == BASE


def test_base_without_pe_is_unchanged():
    assert cabins_for_run(("economy", "business"), run_index=1, pe_every_n=4) == (
        "economy",
        "business",
    )


WITH_FIRST = ("economy", "business", "premium_economy", "first")


def test_first_demoted_with_pe_on_intermediate_runs():
    # Both premium_economy and first drop on non-Nth runs; both return on every-Nth.
    assert cabins_for_run(WITH_FIRST, run_index=1, pe_every_n=4) == ("economy", "business")
    assert cabins_for_run(WITH_FIRST, run_index=3, pe_every_n=4) == ("economy", "business")
    assert cabins_for_run(WITH_FIRST, run_index=0, pe_every_n=4) == WITH_FIRST
    assert cabins_for_run(WITH_FIRST, run_index=4, pe_every_n=4) == WITH_FIRST


def test_first_kept_when_no_demotion():
    base = ("economy", "business", "first")
    assert cabins_for_run(base, run_index=1, pe_every_n=1) == base  # pe_every_n<=1 = no demotion
    assert cabins_for_run(base, run_index=0, pe_every_n=4) == base  # 0th run keeps all
