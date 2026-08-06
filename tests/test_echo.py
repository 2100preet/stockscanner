"""Echo Desk — TradeEcho-style flow / GEX / algo board."""
from odte_scanner.echo.flow import build_option_flow, score_flow_print
from odte_scanner.echo.gex import compute_gex_profile
from odte_scanner.echo.board import build_echo_board, _build_algo_edge


def test_flow_tiers_golden_on_large_premium():
    scored = score_flow_print(
        {
            "right": "C",
            "strike": 500,
            "mid": 2.5,
            "volume": 2000,
            "open_interest": 1000,
            "premium_notional": 500_000,
        },
        spot=498,
    )
    assert scored["tier"] == "golden"
    assert scored["flow_score"] > 0
    assert scored["sentiment"] == "bullish"


def test_put_flow_is_bearish_signed():
    scored = score_flow_print(
        {
            "right": "P",
            "strike": 490,
            "mid": 1.2,
            "volume": 800,
            "open_interest": 2000,
            "premium_notional": 96_000,
        },
        spot=498,
    )
    assert scored["flow_score"] < 0
    assert scored["sentiment"] == "bearish"


def test_build_option_flow_sorts_and_counts():
    ladders = [
        {
            "symbol": "SPY",
            "expiry": "2026-08-06",
            "dte": 0,
            "spot": 500,
            "calls": [
                {
                    "right": "C",
                    "strike": 505,
                    "mid": 1.0,
                    "volume": 6000,
                    "open_interest": 8000,
                    "premium_notional": 600_000,
                    "contract": "SPYC",
                }
            ],
            "puts": [
                {
                    "right": "P",
                    "strike": 495,
                    "mid": 0.8,
                    "volume": 300,
                    "open_interest": 4000,
                    "premium_notional": 24_000,
                    "contract": "SPYP",
                }
            ],
        }
    ]
    flow = build_option_flow(ladders, min_volume=100, min_premium=10_000)
    assert flow["counts"]["all"] >= 1
    assert flow["prints"][0]["symbol"] == "SPY"
    assert flow["counts"]["golden"] >= 1


def test_gex_walls_and_regime():
    ladder = {
        "symbol": "QQQ",
        "expiry": "2026-08-06",
        "dte": 1,
        "spot": 450,
        "calls": [
            {"strike": 455, "open_interest": 5000, "volume": 200, "iv": 0.25},
            {"strike": 460, "open_interest": 12000, "volume": 100, "iv": 0.28},
        ],
        "puts": [
            {"strike": 445, "open_interest": 9000, "volume": 150, "iv": 0.3},
            {"strike": 440, "open_interest": 3000, "volume": 80, "iv": 0.32},
        ],
    }
    gex = compute_gex_profile(ladder)
    assert gex["call_wall"] == 460
    assert gex["put_wall"] == 445
    assert gex["by_strike"]
    assert gex["regime"] in {"positive_gamma", "negative_gamma"}


def test_algo_edge_channels():
    scores = [
        {
            "symbol": "NVDA",
            "horizon": "0dte",
            "ensemble_score": 78,
            "confirms": 4,
            "quality": True,
            "signals": [
                {"name": "gap_and_go", "score": 80, "bullish": True},
                {"name": "momentum_breakout", "score": 70, "bullish": True},
                {"name": "rsi_bounce", "score": 40, "bullish": False},
            ],
        }
    ]
    algo = _build_algo_edge(scores)
    assert algo["channels"]["quality_stack"]
    assert algo["channels"]["momentum"]
    assert algo["channels"]["0dte_speed"]


def test_echo_board_without_network_ladders():
    """Board still returns structure when ladder fetch finds nothing."""
    board = build_echo_board(
        scores=[
            {
                "symbol": "FAKE",
                "horizon": "0dte",
                "ensemble_score": 70,
                "confirms": 3,
                "quality": False,
                "signals": [{"name": "ema_stack", "score": 70, "bullish": True}],
            }
        ],
        candidates=[],
        quotes={"FAKE": {"last": 10, "session_change_pct": 0.2, "mom_5m_pct": 0.1}},
        aliases={},
        insights={"summary": "test", "open_positions": [], "closed_trades": [], "performance": {}},
        actions={"buy_now": [], "sell_now": []},
        lottery={},
        max_symbols=1,
        fetch_ladders=False,
    )
    assert "option_flow" in board
    assert board["dark_pool"]["available"] is False
    assert "algo_edge" in board
    assert "cortex" in board
    assert "disclaimer" in board


def test_echo_board_candidate_fallback_builds_flow():
    board = build_echo_board(
        scores=[{"symbol": "SPY", "horizon": "0dte", "ensemble_score": 75, "confirms": 3, "quality": True, "signals": []}],
        candidates=[
            {
                "symbol": "SPY",
                "expiry": "2026-08-06",
                "dte": 0,
                "strike": 500,
                "spot": 499,
                "ask": 1.5,
                "bid": 1.4,
                "volume": 8000,
                "open_interest": 2000,
                "contract": "SPYTEST",
                "moneyness_pct": 0.2,
            }
        ],
        quotes={"SPY": {"last": 499, "session_change_pct": 0.3, "mom_5m_pct": 0.1}},
        fetch_ladders=False,
        max_symbols=1,
    )
    assert board["ladder_source"] == "candidates_fallback"
    assert board["ladder_count"] >= 1
    assert board["option_flow"]["counts"]["all"] >= 1
