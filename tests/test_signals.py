"""Unit tests for signal math (no network required)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from odte_scanner.algos.engine import score_ticker
from odte_scanner.algos.signals import ema_stack, momentum_breakout, rsi_bounce


def _synthetic_uptrend(n: int = 100, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rets = rng.normal(0.004, 0.01, size=n)  # mild uptrend
    close = start * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0.001, 0.01, n))
    low = close * (1 - rng.uniform(0.001, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    vol = rng.integers(1_000_000, 5_000_000, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def test_ema_stack_scores_uptrend_high():
    df = _synthetic_uptrend()
    sig = ema_stack(df)
    assert 0 <= sig.score <= 100
    assert sig.name == "ema_stack"


def test_momentum_breakout_on_new_high():
    df = _synthetic_uptrend()
    # Force a breakout bar
    df.iloc[-1, df.columns.get_loc("Close")] = df["High"].iloc[-21:-1].max() * 1.01
    df.iloc[-1, df.columns.get_loc("High")] = df.iloc[-1]["Close"] * 1.001
    df.iloc[-1, df.columns.get_loc("Volume")] = int(df["Volume"].iloc[-21:-1].mean() * 2)
    sig = momentum_breakout(df)
    assert sig.score >= 65
    assert sig.bullish


def test_rsi_bounce_bounds():
    df = _synthetic_uptrend()
    sig = rsi_bounce(df)
    assert 0 <= sig.score <= 100
    assert "rsi" in sig.details


def test_score_ticker_ensemble():
    df = _synthetic_uptrend()
    spy = _synthetic_uptrend(start=400)
    vix = _synthetic_uptrend(start=15)
    # Invert VIX to declining
    vix["Close"] = 20 - np.linspace(0, 5, len(vix))
    ts = score_ticker("TEST", df, spy_df=spy, vix_df=vix)
    assert ts.symbol == "TEST"
    assert 0 <= ts.ensemble_score <= 100
    assert len(ts.signals) >= 8
    assert ts.last_price > 0
    assert ts.horizon == "0dte"


def test_score_ticker_horizons_differ():
    df = _synthetic_uptrend(n=220)
    spy = _synthetic_uptrend(n=220, start=400)
    vix = _synthetic_uptrend(n=220, start=15)
    vix["Close"] = 18 - np.linspace(0, 3, len(vix))
    a = score_ticker("TEST", df, spy_df=spy, vix_df=vix, horizon="0dte")
    b = score_ticker("TEST", df, spy_df=spy, vix_df=vix, horizon="swing")
    assert a.horizon == "0dte"
    assert b.horizon == "swing"
    assert {s.name for s in a.signals} != {s.name for s in b.signals}
    assert "stage_analysis" in {s.name for s in b.signals}
    assert b.entry is not None and b.stop is not None


def test_config_loads():
    from odte_scanner.config import load_config

    cfg = load_config()
    assert "tickers" in cfg
    assert "SPY" in cfg["tickers"]
    assert cfg["paper_trading"]["enabled"] is True
    assert cfg["live_trading"]["enabled"] is False
    assert "universe" in cfg


def test_liquid_universe():
    from odte_scanner.data.universe import liquid_universe, resolve_scan_universe
    from odte_scanner.config import load_config

    liq = liquid_universe()
    assert len(liq) >= 80
    assert "NVDA" in liq
    cfg = load_config()
    focus = resolve_scan_universe(cfg, mode="focus")
    wide = resolve_scan_universe(cfg, mode="liquid")
    assert len(wide) > len(focus)
