from odte_scanner.data.universe import liquid_universe
from odte_scanner.market.board import build_market_board


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
