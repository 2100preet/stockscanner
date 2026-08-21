"""Tests for RSI oversold / overbought desk."""

from __future__ import annotations

import numpy as np
import pandas as pd

from odte_scanner.signals.rsi_desk import (
    build_rsi_board,
    classify_rsi,
    decide_rsi,
    resolve_rsi_symbols,
    wilder_rsi,
)


def _bars_trending(n: int = 80, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * n,
        }
    )


def test_wilder_rsi_bounds():
    df = _bars_trending(80, step=0.8)
    rsi = wilder_rsi(df["Close"], 14)
    cur = float(rsi.iloc[-1])
    assert 0 <= cur <= 100
    assert cur > 70  # strong uptrend → overbought


def test_classify_oversold_buy():
    zone, action, strength, detail, reasons = classify_rsi(28.0, prev_rsi=25.0)
    assert zone == "oversold"
    assert action == "BUY"
    assert strength >= 70
    assert any("oversold" in r.lower() or "Oversold" in r for r in reasons)


def test_classify_overbought_sell():
    zone, action, strength, detail, reasons = classify_rsi(72.0, prev_rsi=75.0)
    assert zone == "overbought"
    assert action == "SELL"
    assert strength >= 70


def test_classify_neutral_watch():
    zone, action, *_ = classify_rsi(52.0, prev_rsi=50.0)
    assert zone == "neutral"
    assert action == "WATCH"


def test_decide_rsi_downtrend_oversold():
    # Force oversold with a sharp selloff after a flat base
    closes = [100.0] * 30 + [100 - i * 3 for i in range(1, 25)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 0.5 for c in closes],
            "Low": [c - 0.5 for c in closes],
            "Close": closes,
            "Volume": [1e6] * len(closes),
        }
    )
    sig = decide_rsi("SOFI", df)
    assert sig.rsi is not None
    assert sig.rsi < 40
    # Deep selloff should land oversold BUY or approaching
    assert sig.action in {"BUY", "WATCH"}
    assert sig.symbol == "SOFI"


def test_resolve_prefers_sticky():
    cfg = {"tickers": ["ZZZZ", "SOFI", "AVGO"], "actions": {"rsi_desk_symbols": "focus"}}
    syms = resolve_rsi_symbols("focus", config=cfg)
    assert syms.index("SOFI") < syms.index("ZZZZ")
    assert "AVGO" in syms


def test_board_buckets():
    # Synthetic oversold + overbought frames
    down = [100.0] * 20 + [100 - i * 4 for i in range(1, 30)]
    up = [50.0] * 20 + [50 + i * 4 for i in range(1, 30)]

    def frame(closes):
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [c + 1 for c in closes],
                "Low": [c - 1 for c in closes],
                "Close": closes,
                "Volume": [1e6] * len(closes),
            }
        )

    board = build_rsi_board(
        symbols=["SOFI", "NVDA"],
        bars_map={"SOFI": frame(down), "NVDA": frame(up)},
        fetch_bars=False,
        config={"actions": {}},
    )
    assert board["counts"]["names"] == 2
    by = {r["symbol"]: r for r in board["all"]}
    assert by["SOFI"]["action"] in {"BUY", "WATCH"}
    assert by["NVDA"]["action"] in {"SELL", "WATCH"}
    assert "playbook" in board
