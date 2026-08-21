from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from odte_scanner.algos.base import HORIZONS, TECH_HORIZONS, AlgoSignal, TickerScore
from odte_scanner.algos import signals as S

logger = logging.getLogger(__name__)

# Horizon-specific algo sets — different tools for different holds
# (Signa Action Card + Intellectia SwingMax style separation)
# ML6 is earnings/catalyst scoring (see odte_scanner.ml6) — not this technical set.
HORIZON_ALGOS: dict[str, list[str]] = {
    "0dte": [
        "gap_and_go",
        "momentum_breakout",
        "volume_thrust",
        "rsi_bounce",
        "macd_momentum",
        "squeeze_release",
        "relative_strength",
        "vix_regime",
        "ema_stack",
        "grind_continuation",
    ],
    "weekly": [
        "ema_stack",
        "macd_momentum",
        "relative_strength",
        "momentum_breakout",
        "squeeze_release",
        "rsi_bounce",
        "volume_thrust",
        "vix_regime",
        "pullback_entry",
        "grind_continuation",
    ],
    "swing": [
        "stage_analysis",
        "trend_structure",
        "pullback_entry",
        "relative_strength_medium",
        "ema_stack",
        "macd_momentum",
        "mean_reversion_bottom",
        "rsi_bounce",
        "vix_regime",
    ],
    # Soft technical backdrop only — primary ML6 score lives in odte_scanner.ml6
    "ml6": [
        "mean_reversion_bottom",
        "pullback_entry",
        "relative_strength_medium",
        "stage_analysis",
        "rsi_bounce",
        "volume_thrust",
        "vix_regime",
    ],
}

DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "0dte": {
        "gap_and_go": 1.5,
        "momentum_breakout": 1.5,
        "volume_thrust": 1.4,
        "rsi_bounce": 0.9,
        "macd_momentum": 1.0,
        "squeeze_release": 1.1,
        "relative_strength": 1.2,
        "vix_regime": 1.2,
        "ema_stack": 0.8,
        "grind_continuation": 1.2,
    },
    "weekly": {
        "ema_stack": 1.4,
        "macd_momentum": 1.3,
        "relative_strength": 1.4,
        "momentum_breakout": 1.1,
        "squeeze_release": 1.2,
        "rsi_bounce": 1.0,
        "volume_thrust": 1.0,
        "vix_regime": 0.9,
        "pullback_entry": 1.3,
        "grind_continuation": 1.5,
    },
    "swing": {
        "stage_analysis": 1.6,
        "trend_structure": 1.5,
        "pullback_entry": 1.5,
        "relative_strength_medium": 1.4,
        "ema_stack": 1.2,
        "macd_momentum": 1.1,
        "mean_reversion_bottom": 1.2,
        "rsi_bounce": 0.9,
        "vix_regime": 0.8,
    },
    "ml6": {
        "mean_reversion_bottom": 1.6,
        "pullback_entry": 1.4,
        "relative_strength_medium": 1.3,
        "stage_analysis": 1.2,
        "rsi_bounce": 1.1,
        "volume_thrust": 1.0,
        "vix_regime": 0.8,
    },
}

# Stricter gates → fewer signals, higher measured win rates
QUALITY_GATES: dict[str, dict[str, float | int]] = {
    "0dte": {"min_score": 72.0, "min_confirms": 3},
    "weekly": {"min_score": 70.0, "min_confirms": 3},
    "swing": {"min_score": 74.0, "min_confirms": 4},
    # ML6 quality is driven by reaction gate in odte_scanner.ml6 — soft tech gate only
    "ml6": {"min_score": 62.0, "min_confirms": 2},
}


def _expected_move(df: pd.DataFrame, horizon: str = "0dte") -> float:
    """Estimate move % over the horizon from ATR."""
    if len(df) < 15:
        return 1.0
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.tail(14).mean())
    close = float(df["Close"].iloc[-1])
    if close <= 0:
        return 1.0
    mult = {"0dte": 0.85, "weekly": 2.2, "swing": 6.0, "ml6": 4.0}.get(horizon, 1.0)
    raw = (atr / close) * 100 * mult
    caps = {"0dte": (0.5, 4.0), "weekly": (1.0, 8.0), "swing": (3.0, 25.0), "ml6": (2.0, 20.0)}
    lo, hi = caps.get(horizon, (0.5, 10.0))
    return max(lo, min(hi, raw))


def _levels(df: pd.DataFrame, horizon: str) -> tuple[float, float, float, float | None]:
    """Signa-style entry / stop / target / R:R from ATR."""
    close = float(df["Close"].iloc[-1])
    if len(df) < 15:
        return close, close * 0.97, close * 1.03, 1.0
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(tr.tail(14).mean())
    stop_mult = {"0dte": 0.6, "weekly": 1.2, "swing": 2.0, "ml6": 1.8}.get(horizon, 1.0)
    tgt_mult = {"0dte": 1.2, "weekly": 2.5, "swing": 4.5, "ml6": 3.5}.get(horizon, 2.0)
    entry = close
    stop = close - atr * stop_mult
    target = close + atr * tgt_mult
    risk = entry - stop
    rr = ((target - entry) / risk) if risk > 0 else None
    return entry, stop, target, rr


def _build_signals(
    df: pd.DataFrame,
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    horizon: str,
) -> list[AlgoSignal]:
    names = HORIZON_ALGOS.get(horizon, HORIZON_ALGOS["0dte"])
    builders: dict[str, Any] = {
        "ema_stack": lambda: S.ema_stack(df),
        "momentum_breakout": lambda: S.momentum_breakout(df),
        "rsi_bounce": lambda: S.rsi_bounce(df),
        "macd_momentum": lambda: S.macd_momentum(df),
        "gap_and_go": lambda: S.gap_and_go(df),
        "squeeze_release": lambda: S.squeeze_release(df),
        "volume_thrust": lambda: S.volume_thrust(df),
        "vix_regime": lambda: S.vix_regime(vix_df if vix_df is not None else pd.DataFrame()),
        "pullback_entry": lambda: S.pullback_entry(df),
        "grind_continuation": lambda: S.grind_continuation(df),
        "stage_analysis": lambda: S.stage_analysis(df),
        "trend_structure": lambda: S.trend_structure(df),
        "mean_reversion_bottom": lambda: S.mean_reversion_bottom(df),
        "relative_strength": lambda: (
            S.relative_strength(df, spy_df)
            if spy_df is not None and not spy_df.empty
            else AlgoSignal("relative_strength", 50.0, True, {"reason": "no_bench"})
        ),
        "relative_strength_medium": lambda: (
            S.relative_strength_medium(df, spy_df)
            if spy_df is not None and not spy_df.empty
            else AlgoSignal("relative_strength_medium", 50.0, True, {"reason": "no_bench"})
        ),
    }
    out: list[AlgoSignal] = []
    for name in names:
        fn = builders.get(name)
        if fn:
            out.append(fn())
    return out


def score_ticker(
    symbol: str,
    df: pd.DataFrame,
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights: dict[str, float] | None = None,
    horizon: str = "0dte",
    min_confirms: int | None = None,
    quality_min_score: float | None = None,
) -> TickerScore:
    horizon = horizon if horizon in HORIZONS else "0dte"
    if horizon == "ml6":
        # Technical backdrop only; primary ML6 score is odte_scanner.ml6.scoring
        pass
    base_w = dict(DEFAULT_WEIGHTS.get(horizon, DEFAULT_WEIGHTS["0dte"]))
    if weights:
        base_w.update({k: float(v) for k, v in weights.items()})

    sigs = _build_signals(df, spy_df=spy_df, vix_df=vix_df, horizon=horizon)

    total_w = 0.0
    weighted = 0.0
    reasons: list[str] = []
    confirms = 0
    for sig in sigs:
        w = float(base_w.get(sig.name, 1.0))
        total_w += w
        weighted += sig.score * w
        if sig.bullish and sig.score >= 65:
            confirms += 1
            reasons.append(f"{sig.name}={sig.score:.0f}")

    ensemble = weighted / total_w if total_w else 0.0
    # COST-class grinders: breakout/gap algos stay cold on a slow bid day.
    # When grind_continuation fires hard, floor the ensemble into the option-pick
    # / weekly soft-buy band so focus names are not silently dropped.
    grind = next((s for s in sigs if s.name == "grind_continuation"), None)
    if (
        grind is not None
        and grind.bullish
        and float(grind.score) >= 68.0
        and horizon in {"0dte", "weekly"}
    ):
        # 65 clears weekly_buy_score soft floor; 63 alone still stalled on WAIT
        ensemble = max(ensemble, 65.0)
        if "grind_continuation" not in "".join(reasons):
            reasons.append(f"grind_continuation={grind.score:.0f}")
            confirms = max(confirms, 1)

    gate = QUALITY_GATES.get(horizon, QUALITY_GATES["0dte"])
    need_confirms = int(min_confirms if min_confirms is not None else gate["min_confirms"])
    need_score = float(quality_min_score if quality_min_score is not None else gate["min_score"])
    quality = ensemble >= need_score and confirms >= need_confirms

    # Soft penalty when quality fails — keeps ranking honest but BUY needs quality
    if not quality and ensemble >= need_score - 5:
        ensemble = max(0.0, ensemble - 4.0)

    last = float(df["Close"].iloc[-1])
    entry, stop, target, rr = _levels(df, horizon)
    return TickerScore(
        symbol=symbol,
        ensemble_score=ensemble,
        signals=sigs,
        last_price=last,
        expected_move_pct=_expected_move(df, horizon),
        reasons=reasons,
        horizon=horizon,
        confirms=confirms,
        quality=quality,
        entry=entry,
        stop=stop,
        target=target,
        risk_reward=rr,
    )


def scan_universe(
    histories: dict[str, pd.DataFrame],
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights: dict[str, float] | None = None,
    min_score: float = 62.0,
    horizon: str = "0dte",
    quality_only: bool = False,
) -> list[TickerScore]:
    results: list[TickerScore] = []
    min_bars = 210 if horizon == "swing" else 30
    for symbol, df in histories.items():
        if df is None or len(df) < min_bars:
            if df is not None and len(df) >= 30 and horizon == "swing":
                # Soft path: still score with shorter history
                pass
            elif df is None or len(df) < 30:
                logger.warning("Skipping %s — insufficient history", symbol)
                continue
        try:
            results.append(
                score_ticker(
                    symbol,
                    df,
                    spy_df=spy_df,
                    vix_df=vix_df,
                    weights=weights,
                    horizon=horizon,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed scoring %s: %s", symbol, exc)

    results.sort(key=lambda t: (t.quality, t.ensemble_score), reverse=True)
    if quality_only:
        q = [r for r in results if r.quality]
        return q or results[:5]
    return [r for r in results if r.ensemble_score >= min_score] or results[:5]


def scan_all_horizons(
    histories: dict[str, pd.DataFrame],
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights_by_horizon: dict[str, dict[str, float]] | None = None,
    min_score: float = 0.0,
    include_ml6_tech: bool = False,
) -> dict[str, list[TickerScore]]:
    """Score every symbol on 0DTE, weekly, and swing algo sets.

    ML6 primary scoring is in odte_scanner.ml6 — optional soft technical backdrop
    only when include_ml6_tech=True (avoids doubling runtime on full liquid scans).
    """
    weights_by_horizon = weights_by_horizon or {}
    out: dict[str, list[TickerScore]] = {}
    for hz in TECH_HORIZONS:
        out[hz] = scan_universe(
            histories,
            spy_df=spy_df,
            vix_df=vix_df,
            weights=weights_by_horizon.get(hz),
            min_score=min_score,
            horizon=hz,
            quality_only=False,
        )
    if include_ml6_tech:
        out["ml6"] = scan_universe(
            histories,
            spy_df=spy_df,
            vix_df=vix_df,
            weights=weights_by_horizon.get("ml6"),
            min_score=min_score,
            horizon="ml6",
            quality_only=False,
        )
    return out


def summarize_scan(scores: list[TickerScore]) -> list[dict[str, Any]]:
    return [s.to_dict() for s in scores]
