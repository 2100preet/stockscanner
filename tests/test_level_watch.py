"""Sticky TA level-watch lane."""
from odte_scanner.signals.level_watch import (
    LEVEL_SETUPS,
    build_level_board,
    decide_level_action,
    level_symbols,
)


def test_level_symbols_cover_user_notes():
    must = {"ALAB", "AMAT", "AMD", "AXTI", "BE", "BMNR", "CAT", "DELL", "FPS"}
    assert must <= set(level_symbols())
    assert LEVEL_SETUPS["AMD"]["support"] == 620.0
    assert LEVEL_SETUPS["ALAB"]["breakout"] == 371.0
    assert LEVEL_SETUPS["BMNR"]["breakout"] == 26.55


def test_buy_level_when_above_breakout():
    act = decide_level_action("ALAB", spot=375.0, quote={"session_change_pct": 1.2}, ensemble_score=70)
    assert act is not None
    assert act.action == "BUY_LEVEL"
    assert act.breakout == 371.0


def test_watch_near_breakout():
    act = decide_level_action("AXTI", spot=79.5, quote={"session_change_pct": 0.4}, ensemble_score=60)
    assert act is not None
    assert act.action == "WATCH_LEVEL"


def test_cool_when_support_fails():
    act = decide_level_action("BE", spot=250.0, quote={"session_change_pct": -3.0}, ensemble_score=40)
    assert act is not None
    assert act.action == "LEVEL_COOL"


def test_build_level_board_counts():
    board = build_level_board(
        quotes={
            "AMD": {"last": 650.0, "session_change_pct": 2.0},
            "FPS": {"last": 37.0, "session_change_pct": 0.5},
            "CAT": {"last": 700.0, "session_change_pct": -1.0},
        },
        scores=[{"symbol": "AMD", "ensemble_score": 72}],
        signal_times_path=None,
    )
    assert board["counts"]["buy_level"] >= 1
    assert any(r["symbol"] == "AMD" for r in board["buy_level"])
    assert any(r["symbol"] == "FPS" for r in board["watch"])
    assert any(r["symbol"] == "CAT" for r in board["cool"])
    amd = next(r for r in board["buy_level"] if r["symbol"] == "AMD")
    assert amd.get("signaled_at")
    assert amd.get("signaled_at_cst")


def test_focus_includes_level_watch_names():
    from odte_scanner.data.universe import FOCUS_DEFAULT, liquid_universe

    for sym in ("AXTI", "BE", "BMNR", "CAT", "FPS", "ALAB", "AMAT", "AMD", "DELL"):
        assert sym in FOCUS_DEFAULT, f"{sym} missing from FOCUS_DEFAULT"
        assert sym in liquid_universe(), f"{sym} missing from liquid_universe"
