"""Phase 3: Delta per-shard leg cap raised to use its proven per-session WAF headroom."""


def test_delta_max_legs_per_shard_is_18():
    from config.settings import CRON_MAX_LEGS_PER_SHARD

    assert CRON_MAX_LEGS_PER_SHARD["delta"] == 18
    # Southwest dropped 20->10 after Phase-3 live validation showed a 403 block storm at ~13 legs.
    assert CRON_MAX_LEGS_PER_SHARD["southwest"] == 10
    assert CRON_MAX_LEGS_PER_SHARD["turkish"] == 20
    assert CRON_MAX_LEGS_PER_SHARD["etihad"] == 20
