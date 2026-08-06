from odte_scanner.challenge.million import (
    build_challenge_board,
    compound_path,
    path_table,
)


def test_compound_path_12_flips():
    p = compound_path(start_usd=1000, target_usd=1_000_000, flips=12)
    assert p["pct_per_flip"] > 70  # ~78%
    assert p["schedule"][-1]["equity"] >= 999_000


def test_path_table_10_to_15():
    rows = path_table()
    assert [r["flips"] for r in rows] == list(range(10, 16))
    assert rows[0]["pct_per_flip"] > rows[-1]["pct_per_flip"]


def test_challenge_board_picks_perfect_hist():
    win_table = {
        "symbols": {
            "JPM": {
                "weekly": {
                    "win_pct": 100.0,
                    "trades": 4,
                    "wins": 4,
                    "hit_1pct": 75.0,
                    "hit_2pct": 50.0,
                },
                "swing": {"win_pct": 50.0, "trades": 8, "wins": 4},
            },
            "SLV": {
                "swing": {
                    "win_pct": 90.9,
                    "trades": 11,
                    "wins": 10,
                    "hit_1pct": 90.9,
                    "hit_2pct": 90.9,
                }
            },
            "BAD": {"swing": {"win_pct": 40.0, "trades": 20, "wins": 8}},
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {"symbol": "JPM", "horizon": "weekly", "ensemble_score": 72, "quality": True, "last_price": 200},
            {"symbol": "SLV", "horizon": "swing", "ensemble_score": 76, "quality": True, "last_price": 28},
        ],
        quotes={"JPM": {"last": 200}, "SLV": {"last": 28}},
        fetch_contracts=False,
        flips=12,
    )
    assert board["path"]["flips"] == 12
    syms = [t["symbol"] for t in board["tickets"]]
    assert "JPM" in syms
    assert "SLV" in syms
    assert "BAD" not in syms
    assert board["primary"]["certainty_tier"] in {"perfect", "elite", "strong"}
    assert board["primary"]["symbol"] == "JPM"
    assert "disclaimer" in board
