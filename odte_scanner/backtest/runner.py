from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from odte_scanner.algos.engine import score_ticker

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    symbol: str
    trades: int
    wins: int
    win_rate: float
    avg_next_day_ret: float
    hit_1pct: float
    hit_2pct: float
    expectancy_option: float
    equity_curve: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 3),
            "avg_next_day_ret_pct": round(self.avg_next_day_ret, 3),
            "hit_rate_1pct": round(self.hit_1pct, 3),
            "hit_rate_2pct": round(self.hit_2pct, 3),
            "expectancy_option_R": round(self.expectancy_option, 3),
            "final_equity": round(self.equity_curve[-1], 3) if self.equity_curve else 1.0,
        }


def backtest_symbol(
    symbol: str,
    df: pd.DataFrame,
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights: dict[str, float] | None = None,
    min_score: float = 62.0,
    option_payoff_multiplier: float = 3.0,
    option_loss_fraction: float = 0.65,
) -> BacktestResult:
    """
    Walk-forward daily: score on bars[:i], check next-day close vs close.
    Option PnL is a simplified proxy:
      - if next day >= +1%: +option_payoff_multiplier R
      - elif next day > 0: +0.4 R
      - else: -option_loss_fraction R
    """
    rets: list[float] = []
    option_r: list[float] = []
    equity = [1.0]

    # Align optional series
    for i in range(60, len(df) - 1):
        window = df.iloc[: i + 1]
        spy_w = spy_df.loc[: window.index[-1]] if spy_df is not None else None
        vix_w = vix_df.loc[: window.index[-1]] if vix_df is not None else None
        try:
            ts = score_ticker(symbol, window, spy_df=spy_w, vix_df=vix_w, weights=weights)
        except Exception:  # noqa: BLE001
            continue
        if ts.ensemble_score < min_score:
            continue

        entry = float(df["Close"].iloc[i])
        nxt = float(df["Close"].iloc[i + 1])
        day_ret = (nxt - entry) / entry * 100
        rets.append(day_ret)

        if day_ret >= 1.0:
            r = option_payoff_multiplier
        elif day_ret > 0:
            r = 0.4
        else:
            r = -option_loss_fraction
        option_r.append(r)
        equity.append(equity[-1] * (1 + r * 0.15))  # risk 15% of book per trade proxy

    trades = len(rets)
    wins = sum(1 for r in rets if r > 0)
    return BacktestResult(
        symbol=symbol,
        trades=trades,
        wins=wins,
        win_rate=(wins / trades) if trades else 0.0,
        avg_next_day_ret=float(np.mean(rets)) if rets else 0.0,
        hit_1pct=(sum(1 for r in rets if r >= 1.0) / trades) if trades else 0.0,
        hit_2pct=(sum(1 for r in rets if r >= 2.0) / trades) if trades else 0.0,
        expectancy_option=float(np.mean(option_r)) if option_r else 0.0,
        equity_curve=equity,
    )


def run_backtest(
    histories: dict[str, pd.DataFrame],
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights: dict[str, float] | None = None,
    min_score: float = 62.0,
    option_payoff_multiplier: float = 3.0,
    option_loss_fraction: float = 0.65,
    start: str | None = None,
) -> list[BacktestResult]:
    results: list[BacktestResult] = []
    for symbol, df in histories.items():
        data = df
        if start:
            data = df.loc[df.index >= pd.Timestamp(start)]
        if len(data) < 80:
            continue
        results.append(
            backtest_symbol(
                symbol,
                data,
                spy_df=spy_df,
                vix_df=vix_df,
                weights=weights,
                min_score=min_score,
                option_payoff_multiplier=option_payoff_multiplier,
                option_loss_fraction=option_loss_fraction,
            )
        )
    results.sort(key=lambda r: r.expectancy_option, reverse=True)
    return results
