"""Tests for classic chart-pattern detectors (double bottom/top + triangles)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from odte_scanner.signals.chart_patterns import (
    build_pattern_board,
    classify_pattern,
    detect_double_bottom,
    detect_double_top,
    detect_patterns_from_ohlc,
    detect_triangles,
    find_swing_pivots,
)


def _ohlc_from_close(closes: list[float], *, noise: float = 0.4) -> pd.DataFrame:
    """Build a simple OHLC frame around closes (High=close+noise, Low=close-noise)."""
    rows = []
    for i, c in enumerate(closes):
        rows.append(
            {
                "Open": c,
                "High": c + noise,
                "Low": c - noise,
                "Close": c,
                "Volume": 1_000_000 + i * 1000,
            }
        )
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(rows, index=idx)


def test_swing_pivots_find_local_extrema():
    highs = np.array([10, 11, 12, 11, 10, 11, 13, 11, 10], dtype=float)
    lows = np.array([9, 8, 7, 8, 9, 8, 7.5, 8, 9], dtype=float)
    sh, sl = find_swing_pivots(highs, lows, left=1, right=1)
    assert any(i == 2 and abs(p - 12) < 1e-9 for i, p in sh)
    assert any(i == 2 and abs(p - 7) < 1e-9 for i, p in sl)


def test_detect_double_bottom_and_buy_on_neckline_break():
    # Down → bottom1 → bounce → bottom2 near bottom1 → break neckline
    closes = (
        [20, 19, 18, 17, 16, 15.0, 15.2, 16.5, 17.5, 18.0]  # first bottom ~15
        + [16.8, 15.1, 15.0, 15.3, 16.2, 17.0, 17.8, 18.2, 18.5, 19.0]  # second bottom ~15, break
    )
    df = _ohlc_from_close(closes, noise=0.25)
    # Force exact lows at bottoms so pivots lock
    df.iloc[5, df.columns.get_loc("Low")] = 14.9
    df.iloc[5, df.columns.get_loc("Close")] = 15.0
    df.iloc[12, df.columns.get_loc("Low")] = 14.95
    df.iloc[12, df.columns.get_loc("Close")] = 15.05
    # Neckline bump between bottoms
    df.iloc[8, df.columns.get_loc("High")] = 18.4
    df.iloc[8, df.columns.get_loc("Close")] = 18.0

    pats = detect_patterns_from_ohlc(df, pivot_left=2, pivot_right=2, double_tol_pct=2.5)
    db = next((p for p in pats if p["pattern"] == "DOUBLE_BOTTOM"), None)
    assert db is not None, pats
    assert db["side"] == "bull"
    assert db["neckline"] > db["support"]

    # Spot still below neckline → WATCH
    watch = classify_pattern("TEST", db, spot=float(db["neckline"]) * 0.995)
    assert watch.action == "WATCH_PATTERN"
    # Break above neckline → BUY
    buy = classify_pattern("TEST", db, spot=float(db["neckline"]) * 1.01, quote={"session_change_pct": 1.2})
    assert buy.action == "BUY_PATTERN"
    assert buy.right == "C"
    assert buy.target and buy.target > buy.neckline


def test_detect_double_top_and_short_on_breakdown():
    closes = (
        [10, 11, 12, 13, 14, 15.0, 14.8, 13.5, 12.5, 12.0]
        + [13.2, 14.9, 15.0, 14.7, 13.8, 13.0, 12.2, 11.8, 11.5, 11.0]
    )
    df = _ohlc_from_close(closes, noise=0.25)
    df.iloc[5, df.columns.get_loc("High")] = 15.2
    df.iloc[5, df.columns.get_loc("Close")] = 15.0
    df.iloc[12, df.columns.get_loc("High")] = 15.15
    df.iloc[12, df.columns.get_loc("Close")] = 14.95
    df.iloc[8, df.columns.get_loc("Low")] = 11.6
    df.iloc[8, df.columns.get_loc("Close")] = 12.0

    pats = detect_patterns_from_ohlc(df, pivot_left=2, pivot_right=2, double_tol_pct=2.5)
    dt = next((p for p in pats if p["pattern"] == "DOUBLE_TOP"), None)
    assert dt is not None, pats
    short = classify_pattern("TEST", dt, spot=float(dt["neckline"]) * 0.99, quote={"session_change_pct": -1.0})
    assert short.action == "SHORT_PATTERN"
    assert short.right == "P"


def test_ascending_golden_triangle():
    # Longer series: flat resistance ~100, rising higher lows
    n = 40
    highs = np.full(n, 100.0)
    for i in range(0, n, 5):
        highs[i] = 100.4  # small spikes still flat overall
    lows = np.linspace(80.0, 96.0, n)
    # Carve swing structure every 4 bars
    for i in range(3, n - 3, 4):
        highs[i] = 100.2
        lows[i] = lows[i] - 0.5
        lows[i - 1] = lows[i] + 0.8
        lows[i + 1] = lows[i] + 0.8
    closes = (highs + lows) / 2.0
    df = pd.DataFrame({"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": np.full(n, 1e6)})
    sh, sl = find_swing_pivots(highs, lows, left=1, right=1)
    tri = detect_triangles(highs, lows, swing_highs=sh, swing_lows=sl, lookback=40, flat_tol_pct=2.5)
    assert tri is not None, (sh, sl)
    assert tri["pattern"] in {"ASCENDING_TRIANGLE", "SYMMETRICAL_TRIANGLE"}
    if tri["pattern"] == "ASCENDING_TRIANGLE":
        assert tri.get("golden") is True
        buy = classify_pattern("NVDA", tri, spot=float(tri["resistance"]) * 1.01)
        assert buy.action == "BUY_PATTERN"
        assert buy.golden is True


def test_build_pattern_board_with_injected_history():
    # Longer double-bottom so default detectors fire
    closes = (
        [30, 28, 26, 24, 22, 20.0, 20.5, 23, 25, 26, 25.5, 24.5]
        + [24, 21, 20.1, 20.2, 22, 24, 25.5, 26.5, 27, 28, 28.5, 29]
        + [29.2, 29.5, 30.0, 30.2]
    )
    df = _ohlc_from_close(closes, noise=0.3)
    df.iloc[5, df.columns.get_loc("Low")] = 19.8
    df.iloc[14, df.columns.get_loc("Low")] = 19.9
    df.iloc[9, df.columns.get_loc("High")] = 26.8

    board = build_pattern_board(
        symbols=["AAA", "BBB"],
        quotes={"AAA": {"last": 27.5, "session_change_pct": 1.5}, "BBB": {"last": 50.0}},
        scores=[{"symbol": "AAA", "ensemble_score": 70}],
        histories={"AAA": df, "BBB": _ohlc_from_close([50] * 40)},
        fetch_bars=False,
        max_symbols=10,
        double_tol_pct=2.5,
    )
    assert board["counts"]["scanned"] == 2
    syms = {r["symbol"] for r in (board["buy_pattern"] + board["watch"] + board["short_pattern"] + board["cool"])}
    assert "AAA" in syms
    assert "rules" in board and any("DOUBLE_BOTTOM" in r for r in board["rules"])
    assert board["purpose"]

def test_detect_helpers_export():
    # Smoke: helpers don't crash on tiny frames
    assert detect_double_bottom(np.array([1.0]), np.array([1.0]), np.array([1.0]), swing_lows=[]) is None
    assert detect_double_top(np.array([1.0]), np.array([1.0]), np.array([1.0]), swing_highs=[]) is None
