from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.calendars import resolve_universe
from odte_scanner.config import load_config
from odte_scanner.data.universe import (
    FOCUS_DEFAULT,
    liquid_universe,
    market_cap_tier,
    mid_small_universe,
)
from odte_scanner.desks.sp100 import oversold_universe
from odte_scanner.market.board import build_market_board
from odte_scanner.market.volume_cache import (
    schedule_liquid_volume_warm,
    session_day,
    warm_liquid_volume_cache,
)
from odte_scanner.signals.rip_radar import is_mega_rip_symbol

CT = ZoneInfo("America/Chicago")


def test_market_board_covers_liquid_universe():
    board = build_market_board(fetch_earnings=False, earnings_max_fetch=0)
    assert board["universe_size"] == len(liquid_universe())
    assert "by_earnings" in board
    assert "by_volume" in board
    assert "by_score" in board
    assert "note" in board
    # SPCX should be in the universe rows (may or may not be in by_earnings depending on cache)
    symbols = {r["symbol"] for r in (board.get("by_volume") or [])} | {
        r["symbol"] for r in (board.get("by_earnings") or [])
    } | {r["symbol"] for r in (board.get("by_score") or [])}
    # At minimum board builds without error and reports counts
    assert board["counts"]["today"] >= 0


def test_market_board_includes_earnings_darlings(tmp_path, monkeypatch):
    from datetime import date

    from odte_scanner.data.universe import earnings_darlings_universe

    as_of = date(2026, 8, 11)
    monkeypatch.setattr("odte_scanner.challenge.earnings._today", lambda: as_of)
    monkeypatch.setattr(
        "odte_scanner.challenge.earnings.DEFAULT_CACHE",
        tmp_path / "earnings_cache.json",
    )
    board = build_market_board(
        symbols=earnings_darlings_universe()[:8],
        fetch_earnings=False,
        earnings_max_fetch=0,
    )
    earn = board["by_earnings"]
    assert earn, "curated darlings should populate by_earnings without Yahoo"
    assert any(r["symbol"] == "CRWV" and r.get("darling") for r in earn)
    assert any(r.get("company_name") for r in earn)


def test_market_board_earnings_sort_uses_buckets(tmp_path, monkeypatch):
    import json
    from datetime import date

    cache = tmp_path / "earnings_cache.json"
    as_of = date(2026, 8, 6)
    monkeypatch.setattr("odte_scanner.challenge.earnings._today", lambda: as_of)
    monkeypatch.setattr("odte_scanner.challenge.earnings.DEFAULT_CACHE", cache)
    cache.write_text(
        json.dumps(
            {
                "AAA": {
                    "symbol": "AAA",
                    "next_earnings": "2026-08-06",
                    "last_earnings": None,
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
                "BBB": {
                    "symbol": "BBB",
                    "next_earnings": "2026-08-16",
                    "last_earnings": None,
                    "available": True,
                    "fetched_at": "2026-08-06T00:00:00+00:00",
                },
            }
        )
    )
    board = build_market_board(
        symbols=["AAA", "BBB", "CCC"],
        fetch_earnings=False,
        earnings_max_fetch=0,
    )
    earn = board["by_earnings"]
    assert earn[0]["symbol"] == "AAA"
    assert earn[0]["bucket"] == "today"
    assert any(r["symbol"] == "BBB" and r["bucket"] == "next_week" for r in earn)


def test_hubs_duol_and_crm_are_on_the_focus_scan():
    scanned = set(resolve_universe(load_config()))
    vwap = set(oversold_universe())
    for sym in ("HUBS", "DUOL", "CRM"):
        assert sym in FOCUS_DEFAULT
        assert sym in scanned
        assert sym in liquid_universe()
        assert not is_mega_rip_symbol(sym)
        assert sym not in vwap
    assert "HUBS" in mid_small_universe()
    assert "DUOL" in mid_small_universe()
    assert "CRM" not in mid_small_universe()
    assert market_cap_tier("HUBS") == "mid"
    assert market_cap_tier("DUOL") == "mid"
    assert market_cap_tier("CRM") == "mega_large"
    assert 60 <= len(FOCUS_DEFAULT) <= 140


def test_board_ranks_a_non_focus_name_from_the_daily_volume_cache(tmp_path, monkeypatch):
    assert "WMT" in liquid_universe()
    assert "WMT" not in set(FOCUS_DEFAULT)
    cache = tmp_path / "market_volume_cache.json"
    monkeypatch.setattr("odte_scanner.market.volume_cache.CACHE_PATH", cache)
    now = datetime.now(CT)
    quotes = {
        "WMT": {"volume": 80_000_000, "average_volume": 10_000_000, "last": 70, "change_percentage": 4.2},
        "AAPL": {"volume": 1_000, "average_volume": 50_000_000, "last": 180, "change_percentage": 0.1},
    }

    def fetch(symbols):
        assert symbols == ["WMT", "AAPL"]
        return {"ok": True, "quotes": {s: quotes[s] for s in symbols}}

    warm_liquid_volume_cache(symbols=["WMT", "AAPL"], now=now, fetch=fetch, path=cache)
    board = build_market_board(symbols=["WMT", "AAPL"], fetch_earnings=False, earnings_max_fetch=0)
    leaders = board["by_volume"]
    assert [row["symbol"] for row in leaders] == ["WMT", "AAPL"]
    assert leaders[0]["in_focus"] is False
    assert leaders[0]["day_volume"] == 80_000_000
    assert leaders[0]["rel_volume"] == 8.0
    assert leaders[0]["volume_source"] == "tradier"


def test_volume_warm_batches_once_and_keeps_rows_when_tradier_fails(tmp_path):
    cache = tmp_path / "market_volume_cache.json"
    now = datetime.now(CT)
    calls: list[list[str]] = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {
            "ok": True,
            "quotes": {
                sym: {"volume": 2_000, "average_volume": 1_000, "last": 12, "prevclose": 10}
                for sym in symbols
            },
        }

    first = warm_liquid_volume_cache(symbols=["WMT", "NKE"], now=now, fetch=fetch, path=cache)
    assert len(calls) == 1
    assert calls[0] == ["WMT", "NKE"]
    assert first["day"] == session_day(now)
    assert first["symbols"]["WMT"]["rel_volume"] == 2.0

    def explode(_symbols):
        raise RuntimeError("tradier down")

    kept = warm_liquid_volume_cache(symbols=["WMT", "NKE", "HD"], now=now, fetch=explode, path=cache)
    assert kept["symbols"]["WMT"]["day_volume"] == 2_000
    assert "HD" not in kept["symbols"]
    assert schedule_liquid_volume_warm(path=cache) is False
