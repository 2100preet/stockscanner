from datetime import date, timedelta

from odte_scanner.challenge.earnings import classify_earnings, scan_earnings_calendar
from odte_scanner.data.universe import (
    dram_memory_universe,
    market_cap_tier,
    mid_small_universe,
)


def test_mid_small_universe_present():
    ms = mid_small_universe()
    assert "DKNG" in ms
    assert "IONQ" in ms
    assert market_cap_tier("DKNG") == "mid"
    assert market_cap_tier("IONQ") == "small"
    assert market_cap_tier("AAPL") == "mega_large"
    assert market_cap_tier("SPY") == "etf"


def test_dram_memory_universe_present():
    dram = dram_memory_universe()
    assert "DRAM" in dram
    assert "MU" in dram
    assert "WDC" in dram
    assert "AMAT" in dram
    # DRAM sleeve ticker itself is mid-tier; mega peers keep mega_large
    assert market_cap_tier("DRAM") == "mid"
    assert market_cap_tier("MU") == "mega_large"


def test_spcx_in_scan_universe():
    from odte_scanner.data.universe import FOCUS_DEFAULT, liquid_universe

    assert "SPCX" in liquid_universe()
    assert "SPCX" in FOCUS_DEFAULT
    assert market_cap_tier("SPCX") == "mega_large"


def test_classify_post_earnings():
    as_of = date(2026, 8, 5)
    last = (as_of - timedelta(days=3)).isoformat()
    nxt = (as_of + timedelta(days=90)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": last},
        as_of=as_of,
    )
    assert c["window"] == "post_earnings"
    assert c["bucket"] == "post"
    assert c["boost"] == 2
    assert c["strategy_bias"] == "prefer_post"


def test_classify_pre_earnings_this_week():
    as_of = date(2026, 8, 5)
    nxt = (as_of + timedelta(days=4)).isoformat()
    last = (as_of - timedelta(days=80)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": last},
        as_of=as_of,
    )
    assert c["window"] == "pre_earnings"
    assert c["bucket"] == "this_week"
    assert c["prefer_leap"] is True
    assert c["boost"] == -1


def test_classify_pre_earnings_next_week():
    as_of = date(2026, 8, 5)
    nxt = (as_of + timedelta(days=10)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": None},
        as_of=as_of,
    )
    assert c["window"] == "pre_earnings"
    assert c["bucket"] == "next_week"
    assert c["prefer_leap"] is True


def test_classify_earnings_soon():
    as_of = date(2026, 8, 5)
    nxt = (as_of + timedelta(days=18)).isoformat()
    c = classify_earnings(
        {"next_earnings": nxt, "last_earnings": None},
        as_of=as_of,
    )
    assert c["window"] == "earnings_soon"
    assert c["bucket"] == "soon"


def test_classify_earnings_day():
    as_of = date(2026, 8, 5)
    c = classify_earnings(
        {"next_earnings": as_of.isoformat(), "last_earnings": None},
        as_of=as_of,
    )
    assert c["window"] == "earnings_day"
    assert c["bucket"] == "today"
    assert c["boost"] == -2


def test_scan_earnings_calendar_sort_order(tmp_path, monkeypatch):
    # Build watch from pre-seeded cache (no network)
    cache = tmp_path / "earnings_cache.json"
    as_of = date(2026, 8, 6)
    monkeypatch.setattr(
        "odte_scanner.challenge.earnings._today",
        lambda: as_of,
    )
    import json

    cache.write_text(
        json.dumps(
            {
                "AAA": {
                    "symbol": "AAA",
                    "next_earnings": (as_of + timedelta(days=10)).isoformat(),
                    "last_earnings": None,
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
                "BBB": {
                    "symbol": "BBB",
                    "next_earnings": as_of.isoformat(),
                    "last_earnings": None,
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
                "CCC": {
                    "symbol": "CCC",
                    "next_earnings": (as_of + timedelta(days=3)).isoformat(),
                    "last_earnings": None,
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
                "DDD": {
                    "symbol": "DDD",
                    "next_earnings": (as_of + timedelta(days=90)).isoformat(),
                    "last_earnings": (as_of - timedelta(days=2)).isoformat(),
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
            }
        )
    )
    # Point DEFAULT_CACHE via cache_path on map — scan uses earnings_map_for without path.
    # Patch DEFAULT_CACHE instead.
    monkeypatch.setattr("odte_scanner.challenge.earnings.DEFAULT_CACHE", cache)
    rows = scan_earnings_calendar(["AAA", "BBB", "CCC", "DDD"], fetch=False, max_fetch=0)
    buckets = [r["bucket"] for r in rows]
    assert buckets[:4] == ["today", "this_week", "next_week", "post"]
    assert rows[0]["symbol"] == "BBB"
    assert rows[1]["symbol"] == "CCC"
    assert rows[2]["symbol"] == "AAA"
    assert rows[3]["symbol"] == "DDD"
