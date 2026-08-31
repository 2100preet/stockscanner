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


def test_ddog_in_focus_universe():
    from odte_scanner.data.universe import FOCUS_DEFAULT, liquid_universe

    # Already liquid/mid; elevated to focus + Friday weeklies for 0DTE/earnings
    assert "DDOG" in liquid_universe()
    assert "DDOG" in FOCUS_DEFAULT
    assert market_cap_tier("DDOG") == "mid"


def test_earnings_darlings_in_universe():
    from odte_scanner.challenge.earnings import CURATED_EARNINGS
    from odte_scanner.data.universe import (
        FOCUS_DEFAULT,
        earnings_darlings_universe,
        liquid_universe,
    )

    darlings = earnings_darlings_universe()
    # CoreWeave / Cerebras / Firefly / Figure / Gemini — week’s anticipated set
    for sym in ("CRWV", "CBRS", "FLY", "FIGR", "GEMI", "NBIS", "BETA", "XE"):
        assert sym in darlings
        assert sym in liquid_universe()
        assert sym in FOCUS_DEFAULT
        assert market_cap_tier(sym) in {"mid", "small"}
        assert sym in CURATED_EARNINGS
    assert "TMS" in darlings
    assert "INFQ" in darlings
    assert "FAC" in liquid_universe()
    assert "CRCL" in darlings
    assert "CRCL" in liquid_universe()
    assert "CRCL" in FOCUS_DEFAULT
    assert "SMCI" in darlings
    assert "SMCI" in liquid_universe()
    assert "SMCI" in FOCUS_DEFAULT
    assert "SMCI" in CURATED_EARNINGS
    assert market_cap_tier("SMCI") == "mid"
    # CRM / NOW are liquid megas — must stay on focus scans
    assert "CRM" in liquid_universe()
    assert "CRM" in FOCUS_DEFAULT
    assert market_cap_tier("CRM") == "mega_large"
    assert "NOW" in liquid_universe()
    assert "NOW" in FOCUS_DEFAULT
    assert market_cap_tier("NOW") == "mega_large"


def test_curated_darlings_show_on_earnings_calendar(monkeypatch):
    from odte_scanner.challenge.earnings import scan_earnings_calendar

    as_of = date(2026, 8, 11)
    monkeypatch.setattr(
        "odte_scanner.challenge.earnings._today",
        lambda: as_of,
    )
    rows = scan_earnings_calendar(
        ["CRWV", "CBRS", "FLY", "FIGR", "GEMI", "TMS", "SPY"],
        fetch=False,
        max_fetch=0,
    )
    by_sym = {r["symbol"]: r for r in rows}
    assert "CRWV" in by_sym
    assert by_sym["CRWV"]["bucket"] == "today"
    assert by_sym["CRWV"]["darling"] is True
    assert by_sym["CRWV"]["earnings_session"] == "amc"
    assert by_sym["CBRS"]["bucket"] == "this_week"
    assert by_sym["FIGR"]["next_earnings"] == "2026-08-13"
    assert by_sym["TMS"]["next_earnings"] == "2026-08-14"
    # SPY has no curated date and no cache → absent from watch
    assert "SPY" not in by_sym


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


def test_iv_move_earnings_week_curated():
    from odte_scanner.challenge.earnings import CURATED_EARNINGS, scan_earnings_calendar
    from odte_scanner.data.universe import FOCUS_DEFAULT

    must = {
        "MDB": 20.6, "AVGO": 8.1, "SNOW": 12.3, "NTAP": 12.2, "AMBA": 11.8,
        "YEXT": 19.9, "ASAN": 19.2, "GTLB": 16.0, "AI": 13.9, "ZS": 13.7,
        "PATH": 13.5, "CIEN": 12.7, "PVH": 12.6, "HPE": 11.6, "DELL": 11.6,
        "DOCU": 11.1, "FIVE": 10.6, "NIO": 9.7, "PANW": 9.6, "LULU": 9.5, "MDT": 5.1,
    }
    for sym, iv in must.items():
        assert sym in CURATED_EARNINGS, sym
        assert float(CURATED_EARNINGS[sym]["iv_move_pct"]) == iv
        assert sym in FOCUS_DEFAULT, f"{sym} missing from focus"
    rows = scan_earnings_calendar(list(must), fetch=False, max_fetch=0)
    by = {r["symbol"]: r for r in rows}
    assert by["MDB"]["iv_move_pct"] == 20.6
    assert by["AVGO"]["bucket"] in {"this_week", "today", "next_week"}
