"""ZeroLoss catalyst scorer — MRNA 2026-08-19 must never be silent."""

from __future__ import annotations

import pandas as pd

from odte_scanner.data.universe import catalyst_universe
from odte_scanner.zeroloss.board import build_zeroloss_board
from odte_scanner.zeroloss.catalyst import LANE_DO_NOT_MISS, score_session


def _mrna_bars() -> pd.DataFrame:
    # Yahoo: Aug 18 close 62.96; Aug 19 open 116.25 close 174.38 vol 185_057_621
    return pd.DataFrame(
        {
            "Open": [64.0, 63.04, 116.25],
            "High": [65.5, 64.46, 176.66],
            "Low": [62.9, 62.13, 114.46],
            "Close": [64.46, 62.96, 174.38],
            "Volume": [3_979_600, 4_305_000, 185_057_621],
        }
    )


def test_mrna_phase3_is_do_not_miss():
    row = score_session(
        _mrna_bars(),
        symbol="MRNA",
        news_titles=[
            "Merck and Moderna Announce Phase 3 INTerpath-001 Trial Met Endpoints in Melanoma"
        ],
    )
    assert row["lane"] == LANE_DO_NOT_MISS
    assert row["miss_score"] >= 0.85
    assert row["gap_pct"] and row["gap_pct"] > 80
    assert row["day_change_pct"] and row["day_change_pct"] > 150
    assert row["rel_volume"] and row["rel_volume"] > 20
    assert row["news_hits"]


def test_quiet_name_stays_quiet():
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.4],
            "High": [101.0, 101.2],
            "Low": [99.5, 99.8],
            "Close": [100.2, 100.5],
            "Volume": [1_000_000, 1_050_000],
        }
    )
    row = score_session(df, symbol="KO")
    assert row["lane"] == "QUIET"
    assert row["miss_score"] < 0.2


def test_catalyst_universe_includes_mrna():
    uni = catalyst_universe()
    assert "MRNA" in uni
    assert "MRK" in uni
    assert "BNTX" in uni
    assert "XBI" in uni
    from odte_scanner.data.universe import FOCUS_DEFAULT, LIQUID_UNIVERSE

    assert "MRNA" in FOCUS_DEFAULT
    assert "MRNA" in LIQUID_UNIVERSE


def test_resolve_catalyst_mode_includes_mrna():
    from odte_scanner.data.universe import resolve_scan_universe

    uni = resolve_scan_universe({"tickers": ["SPY"]}, mode="catalyst")
    assert "MRNA" in uni
    assert "SPY" in uni


def test_board_ranks_mrna_first():
    quiet = pd.DataFrame(
        {
            "Open": [10.0, 10.1],
            "High": [10.2, 10.3],
            "Low": [9.9, 10.0],
            "Close": [10.05, 10.12],
            "Volume": [800_000, 810_000],
        }
    )
    board = build_zeroloss_board(
        {"KO": quiet, "MRNA": _mrna_bars()},
        headlines_by_symbol={
            "MRNA": ["Moderna Phase 3 cancer vaccine trial meets endpoints"]
        },
        fetch_news=False,
    )
    assert board["counts"]["do_not_miss"] >= 1
    assert board["do_not_miss"][0]["symbol"] == "MRNA"
    assert board["brand"] == "ZeroLoss"
