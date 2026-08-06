from datetime import date, timedelta

from odte_scanner.challenge.earnings import classify_earnings
from odte_scanner.data.universe import market_cap_tier, mid_small_universe


def test_mid_small_universe_present():
    ms = mid_small_universe()
    assert "DKNG" in ms
    assert "IONQ" in ms
    assert market_cap_tier("DKNG") == "mid"
    assert market_cap_tier("IONQ") == "small"
    assert market_cap_tier("AAPL") == "mega_large"
    assert market_cap_tier("SPY") == "etf"


def test_classify_post_earnings():
    as_of = date(2026, 8, 5)
    last = (as_of - timedelta(days=3)).isoformat()
    nxt = (as_of + timedelta(days=90)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": last},
        as_of=as_of,
    )
    assert c["window"] == "post_earnings"
    assert c["boost"] == 2
    assert c["strategy_bias"] == "prefer_post"


def test_classify_pre_earnings():
    as_of = date(2026, 8, 5)
    nxt = (as_of + timedelta(days=4)).isoformat()
    last = (as_of - timedelta(days=80)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": last},
        as_of=as_of,
    )
    assert c["window"] == "pre_earnings"
    assert c["prefer_leap"] is True
    assert c["boost"] == -1


def test_classify_earnings_day():
    as_of = date(2026, 8, 5)
    c = classify_earnings(
        {"next_earnings": as_of.isoformat(), "last_earnings": None},
        as_of=as_of,
    )
    assert c["window"] == "earnings_day"
    assert c["boost"] == -2
