"""Flow Desk / unusual options flow helpers."""
from odte_scanner.challenge.earnings import CURATED_EARNINGS
from odte_scanner.data.universe import FOCUS_DEFAULT, liquid_universe
from odte_scanner.echo.board import _pick_echo_symbols
from odte_scanner.echo.flow import build_option_flow, score_flow_print


def test_ew_liquid_names_curated_and_focus():
    for sym in ("SMCI", "CRWV", "NBIS", "SE", "HIMS", "RKLB", "ASTS", "PLUG", "CSCO", "AMAT"):
        assert sym in liquid_universe()
        assert sym in CURATED_EARNINGS
    for sym in ("SMCI", "SE", "HIMS", "RKLB", "ASTS", "PLUG", "CSCO", "IREN"):
        assert sym in FOCUS_DEFAULT
    assert "IREN" in liquid_universe()


def test_curated_high_potential_on_focus():
    """Selective AI/memory/power names — not full S&P 500."""
    must = (
        "MU", "DELL",  # already core
        "SNDK", "WDC", "STX",
        "LRCX", "KLAC", "MRVL", "ANET", "ALAB", "CRDO", "APP",
        "VRT", "CEG", "GEV", "APLD", "CIFR", "WULF",
    )
    for sym in must:
        assert sym in FOCUS_DEFAULT, f"{sym} missing from FOCUS_DEFAULT"
        assert sym in liquid_universe(), f"{sym} missing from liquid_universe"
    # Focus stays bounded (rate limits) — curated adds, not entire SPX
    assert 60 <= len(FOCUS_DEFAULT) <= 140


def test_sticky_note_watch_on_focus():
    """Sticky watch + core names that must stay on every focus scan."""
    # Sticky note: SPCX/INTC already present; IBIT/MRNA elevated in this PR.
    # HOOD / AVGO / COST were already on focus — keep locked.
    for sym in ("SPCX", "INTC", "IBIT", "MRNA", "HOOD", "AVGO", "COST", "SOFI", "OSCR", "NVTS", "USAR", "MP"):
        assert sym in FOCUS_DEFAULT, f"{sym} missing from FOCUS_DEFAULT"
        assert sym in liquid_universe(), f"{sym} missing from liquid_universe"
    from odte_scanner.data.universe import market_cap_tier

    assert market_cap_tier("IBIT") == "etf"
    assert market_cap_tier("MRNA") == "mega_large"
    assert market_cap_tier("HOOD") == "mid"
    assert market_cap_tier("AVGO") in {"mega_large", "dram_memory"}
    assert market_cap_tier("COST") == "mega_large"


def test_flow_score_flags_and_tiers():
    golden = score_flow_print(
        {"right": "C", "strike": 100, "volume": 25000, "open_interest": 10000, "mid": 1.5, "premium_notional": 300000},
        spot=100,
    )
    assert golden["tier"] == "golden"
    assert "vol_gt_oi" in golden["flags"]
    assert golden["sentiment"] == "bullish"

    put = score_flow_print(
        {"right": "P", "strike": 95, "volume": 6000, "open_interest": 8000, "mid": 2.0, "premium_notional": 120000},
        spot=100,
    )
    assert put["tier"] == "unusual"
    assert put["sentiment"] == "bearish"


def test_pick_echo_prefers_earnings_darlings():
    syms = _pick_echo_symbols(
        scores=[{"symbol": "AAA", "ensemble_score": 99, "horizon": "0dte"}],
        candidates=[],
        quotes={"SPY": {"last": 1}},
        max_symbols=5,
        prefer_symbols=["SMCI", "CRWV", "NBIS"],
    )
    assert syms[0] in {"SMCI", "CRWV", "NBIS"}
    assert "AAA" in syms or len(syms) == 5


def test_build_option_flow_buckets():
    board = build_option_flow(
        [
            {
                "symbol": "SMCI",
                "spot": 50,
                "expiry": "2026-08-14",
                "dte": 3,
                "calls": [
                    {"right": "C", "strike": 50, "volume": 22000, "open_interest": 5000, "mid": 2.0},
                ],
                "puts": [
                    {"right": "P", "strike": 48, "volume": 800, "open_interest": 2000, "mid": 1.0},
                ],
            }
        ],
        min_volume=100,
        min_premium=1000,
    )
    assert board["counts"]["golden"] >= 1
    assert board["prints"][0]["symbol"] == "SMCI"
    assert board["prints"][0]["flags"]
