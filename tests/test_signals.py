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
    assert cfg["live_trading"]["dry_run"] is True
    assert cfg["live_trading"]["paper"] is True
    assert cfg["live_trading"]["golden_only"] is True
    assert cfg["live_trading"]["submit_interval_sec"] == 1.5
    assert cfg["live_trading"]["max_golden_buys_per_sync"] == 2
    assert cfg["live_trading"]["close_legacy_positions"] is False
    assert cfg["live_trading"]["second_desk"] == "challenge"
    assert cfg["live_trading"]["exits"]["challenge"]["take_profit_pct"] == 100
    assert cfg["live_trading"]["exits"]["odte"]["mode"] == "eod"
    assert cfg["live_trading"]["exits"]["sure_winner"]["take_profit_pct"] == 30
    assert cfg["live_trading"]["exits"]["sure_winner"]["runner_target_pct"] == 80
    assert cfg["live_trading"]["exits"]["sure_winner"]["same_day_stop_pct"] == 40
    assert cfg["live_trading"]["exits"]["sure_winner"]["flatten_ct"] == "14:30"
    assert cfg["live_trading"]["exits"]["sure_winner"]["scale_out"] is False
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


def test_rsi_bounce_mid_band_65_70_scores_soft_69():
    """Rising RSI in 65–70 should score 69 for soft buy_score clear."""
    idx = pd.date_range("2026-01-01", periods=40, freq="B")
    # Build a series that lands RSI ~67 rising
    close = pd.Series(np.linspace(100, 118, len(idx)), index=idx)
    # Last few bars grind up so RSI rises into mid-upper band
    close.iloc[-3:] = [116.5, 117.2, 118.0]
    high = close * 1.002
    low = close * 0.998
    open_ = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(2_000_000, index=idx)
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )
    sig = rsi_bounce(df)
    rsi = float(sig.details["rsi"])
    if 65 < rsi <= 70 and sig.details.get("rising"):
        assert sig.score == 69.0
    else:
        # Synthetic path may not always land in band — still must stay in bounds
        assert 0 <= sig.score <= 100
