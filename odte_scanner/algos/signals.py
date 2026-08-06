from __future__ import annotations

import numpy as np
import pandas as pd

from odte_scanner.algos.base import AlgoSignal


def _safe_pct(a: float, b: float) -> float:
    if b == 0 or np.isnan(b):
        return 0.0
    return (a - b) / b * 100.0


def ema_stack(df: pd.DataFrame) -> AlgoSignal:
    """Bullish when Close > EMA9 > EMA21 > EMA50 with rising short EMA."""
    close = df["Close"]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    c, e9, e21, e50 = close.iloc[-1], ema9.iloc[-1], ema21.iloc[-1], ema50.iloc[-1]
    slope = _safe_pct(float(e9), float(ema9.iloc[-5])) if len(ema9) >= 5 else 0.0

    stacked = c > e9 > e21 > e50
    score = 35.0
    if stacked:
        score = 75.0
    elif c > e9 > e21:
        score = 62.0
    elif c > e9:
        score = 48.0

    if slope > 0.3:
        score = min(100.0, score + 12)
    elif slope < -0.3:
        score = max(0.0, score - 15)

    return AlgoSignal(
        name="ema_stack",
        score=score,
        bullish=score >= 60,
        details={"ema9": float(e9), "ema21": float(e21), "ema50": float(e50), "slope_pct": slope},
    )


def momentum_breakout(df: pd.DataFrame, lookback: int = 20) -> AlgoSignal:
    """Score high when price breaks recent highs with volume confirmation."""
    if len(df) < lookback + 2:
        return AlgoSignal("momentum_breakout", 40.0, False, {"reason": "insufficient_data"})

    window = df.iloc[-(lookback + 1) : -1]
    prior_high = float(window["High"].max())
    last = df.iloc[-1]
    close = float(last["Close"])
    vol = float(last["Volume"])
    avg_vol = float(window["Volume"].mean())

    breakout = close >= prior_high * 0.998
    vol_ratio = vol / avg_vol if avg_vol else 1.0
    dist = _safe_pct(close, prior_high)

    score = 40.0
    if breakout and vol_ratio >= 1.2:
        score = 88.0
    elif breakout:
        score = 72.0
    elif dist > -1.0 and vol_ratio >= 1.3:
        score = 65.0
    elif dist > -2.0:
        score = 50.0

    return AlgoSignal(
        name="momentum_breakout",
        score=score,
        bullish=score >= 65,
        details={
            "prior_high": prior_high,
            "close": close,
            "dist_pct": dist,
            "vol_ratio": round(vol_ratio, 2),
        },
    )


def rsi_bounce(df: pd.DataFrame, period: int = 14) -> AlgoSignal:
    """Favor RSI rising through 40–55 (bounce) or holding 50–65 (trend)."""
    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    cur = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0
    prev = float(rsi.iloc[-3]) if len(rsi) >= 3 and not np.isnan(rsi.iloc[-3]) else cur
    rising = cur > prev

    score = 45.0
    if 40 <= cur <= 55 and rising:
        score = 82.0
    elif 50 <= cur <= 65 and rising:
        score = 74.0
    elif 55 < cur <= 70:
        score = 60.0
    elif cur < 35 and rising:
        score = 68.0
    elif cur > 75:
        score = 28.0

    return AlgoSignal(
        name="rsi_bounce",
        score=score,
        bullish=score >= 60,
        details={"rsi": round(cur, 2), "rising": rising},
    )


def macd_momentum(df: pd.DataFrame) -> AlgoSignal:
    close = df["Close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal

    h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2]) if len(hist) > 1 else 0.0
    m0, s0 = float(macd.iloc[-1]), float(signal.iloc[-1])

    score = 40.0
    if h0 > 0 and h1 <= 0:
        score = 86.0  # fresh cross
    elif h0 > 0 and h0 > h1:
        score = 72.0
    elif m0 > s0:
        score = 58.0
    elif h0 < 0 and h0 > h1:
        score = 52.0  # improving

    return AlgoSignal(
        name="macd_momentum",
        score=score,
        bullish=score >= 58,
        details={"macd": m0, "signal": s0, "hist": h0},
    )


def gap_and_go(df: pd.DataFrame) -> AlgoSignal:
    """Positive gap that holds above prior close — classic day-trade long setup."""
    if len(df) < 3:
        return AlgoSignal("gap_and_go", 40.0, False, {})

    prev = df.iloc[-2]
    last = df.iloc[-1]
    gap = _safe_pct(float(last["Open"]), float(prev["Close"]))
    hold = float(last["Close"]) >= float(last["Open"])
    above_prior = float(last["Close"]) > float(prev["Close"])

    score = 40.0
    if 0.3 <= gap <= 2.0 and hold and above_prior:
        score = 85.0
    elif 0.15 <= gap <= 2.5 and above_prior:
        score = 70.0
    elif gap > 2.5:
        score = 45.0  # gap too large — fade risk
    elif gap < 0 and above_prior:
        score = 55.0  # recovery day

    return AlgoSignal(
        name="gap_and_go",
        score=score,
        bullish=score >= 65,
        details={"gap_pct": round(gap, 3), "hold": hold, "above_prior": above_prior},
    )


def relative_strength(df: pd.DataFrame, bench: pd.DataFrame, window: int = 5) -> AlgoSignal:
    """Outperformance vs SPY over a short window favors directional calls."""
    if len(df) < window + 1 or len(bench) < window + 1:
        return AlgoSignal("relative_strength", 45.0, False, {})

    aligned = df[["Close"]].join(bench[["Close"]].rename(columns={"Close": "Bench"}), how="inner")
    if len(aligned) < window + 1:
        return AlgoSignal("relative_strength", 45.0, False, {})

    ret = aligned["Close"].pct_change(window).iloc[-1] * 100
    bret = aligned["Bench"].pct_change(window).iloc[-1] * 100
    rs = float(ret - bret)

    score = 45.0 + max(-25.0, min(40.0, rs * 8))
    if ret > 0 and rs > 0:
        score = min(100.0, score + 8)

    return AlgoSignal(
        name="relative_strength",
        score=float(np.clip(score, 0, 100)),
        bullish=score >= 58,
        details={"rs_vs_spy_pct": round(rs, 3), "ret_pct": round(float(ret), 3)},
    )


def squeeze_release(df: pd.DataFrame, length: int = 20) -> AlgoSignal:
    """Bollinger inside Keltner (TTM squeeze) then release upward."""
    if len(df) < length + 5:
        return AlgoSignal("squeeze_release", 40.0, False, {})

    close = df["Close"]
    mid = close.rolling(length).mean()
    std = close.rolling(length).std()
    upper_bb = mid + 2 * std
    lower_bb = mid - 2 * std

    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - close.shift()).abs(),
            (df["Low"] - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(length).mean()
    upper_kc = mid + 1.5 * atr
    lower_kc = mid - 1.5 * atr

    in_squeeze = (lower_bb > lower_kc) & (upper_bb < upper_kc)
    was_squeeze = bool(in_squeeze.iloc[-3]) if len(in_squeeze) >= 3 else False
    now_squeeze = bool(in_squeeze.iloc[-1])
    mom = float(close.iloc[-1] - mid.iloc[-1])

    score = 45.0
    if was_squeeze and not now_squeeze and mom > 0:
        score = 90.0
    elif now_squeeze:
        score = 58.0  # coiled — watchlist
    elif mom > 0 and not now_squeeze:
        score = 55.0

    return AlgoSignal(
        name="squeeze_release",
        score=score,
        bullish=score >= 58,
        details={"in_squeeze": now_squeeze, "momentum": mom},
    )


def volume_thrust(df: pd.DataFrame) -> AlgoSignal:
    """Up-day with elevated volume — institutional participation proxy."""
    if len(df) < 21:
        return AlgoSignal("volume_thrust", 40.0, False, {})

    last = df.iloc[-1]
    avg = float(df["Volume"].iloc[-21:-1].mean())
    up = float(last["Close"]) > float(last["Open"])
    ratio = float(last["Volume"]) / avg if avg else 1.0
    body = abs(_safe_pct(float(last["Close"]), float(last["Open"])))

    score = 40.0
    if up and ratio >= 1.5 and body >= 0.6:
        score = 88.0
    elif up and ratio >= 1.2:
        score = 72.0
    elif up and ratio >= 1.0:
        score = 58.0
    elif not up and ratio >= 1.5:
        score = 25.0

    return AlgoSignal(
        name="volume_thrust",
        score=score,
        bullish=score >= 58,
        details={"vol_ratio": round(ratio, 2), "up_day": up, "body_pct": round(body, 3)},
    )


def vix_regime(vix_df: pd.DataFrame) -> AlgoSignal:
    """Falling / moderate VIX is friendlier for long calls."""
    if vix_df is None or vix_df.empty or len(vix_df) < 5:
        return AlgoSignal("vix_regime", 50.0, True, {"reason": "no_vix"})

    close = vix_df["Close"]
    cur = float(close.iloc[-1])
    chg = _safe_pct(cur, float(close.iloc[-5]))

    score = 50.0
    if cur < 15 and chg <= 0:
        score = 80.0
    elif cur < 18 and chg < 0:
        score = 72.0
    elif 18 <= cur <= 22 and chg < -5:
        score = 68.0
    elif cur > 28:
        score = 25.0
    elif cur > 22 and chg > 5:
        score = 30.0

    return AlgoSignal(
        name="vix_regime",
        score=score,
        bullish=score >= 55,
        details={"vix": round(cur, 2), "vix_5d_chg_pct": round(chg, 2)},
    )


# --- Swing / multi-week signals (Intellectia / Signa stage-style) ---


def stage_analysis(df: pd.DataFrame) -> AlgoSignal:
    """Weinstein-inspired stage: prefer Stage 2 (price > rising 30W/150d MA)."""
    if len(df) < 160:
        return AlgoSignal("stage_analysis", 45.0, False, {"reason": "insufficient_data"})

    close = df["Close"]
    ma150 = close.rolling(150).mean()
    ma50 = close.rolling(50).mean()
    c = float(close.iloc[-1])
    m150 = float(ma150.iloc[-1])
    m50 = float(ma50.iloc[-1])
    ma_slope = _safe_pct(m150, float(ma150.iloc[-20])) if len(ma150) >= 20 else 0.0
    above = c > m150
    rising = ma_slope > 0.5

    score = 35.0
    stage = 1
    if above and rising and c > m50:
        score = 88.0
        stage = 2
    elif above and rising:
        score = 78.0
        stage = 2
    elif above and c > m50:
        score = 68.0
        stage = 2
    elif c > m50 > m150 * 0.98:
        score = 58.0
        stage = 2
    elif c < m150 and ma_slope < -1:
        score = 22.0
        stage = 4
    elif c < m150:
        score = 38.0
        stage = 3

    return AlgoSignal(
        name="stage_analysis",
        score=score,
        bullish=score >= 65,
        details={"stage": stage, "ma150": m150, "ma_slope_pct": round(ma_slope, 2)},
    )


def trend_structure(df: pd.DataFrame) -> AlgoSignal:
    """Higher highs/lows + above MA200 — swing trend confirmation."""
    if len(df) < 210:
        return AlgoSignal("trend_structure", 45.0, False, {"reason": "insufficient_data"})

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    ma200 = close.rolling(200).mean()
    c = float(close.iloc[-1])
    m200 = float(ma200.iloc[-1])

    hh = float(high.iloc[-20:].max()) >= float(high.iloc[-60:-20].max()) * 0.995
    hl = float(low.iloc[-20:].min()) >= float(low.iloc[-60:-20].min()) * 0.995
    above = c > m200

    score = 40.0
    if above and hh and hl:
        score = 90.0
    elif above and (hh or hl):
        score = 75.0
    elif above:
        score = 62.0
    elif hh and hl:
        score = 55.0
    else:
        score = 32.0

    return AlgoSignal(
        name="trend_structure",
        score=score,
        bullish=score >= 62,
        details={"above_ma200": above, "higher_highs": hh, "higher_lows": hl},
    )


def pullback_entry(df: pd.DataFrame) -> AlgoSignal:
    """Buy-the-dip in uptrend — Intellectia SwingMax-style relative low entry."""
    if len(df) < 55:
        return AlgoSignal("pullback_entry", 40.0, False, {})

    close = df["Close"]
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    c = float(close.iloc[-1])
    e21 = float(ema21.iloc[-1])
    e50 = float(ema50.iloc[-1])
    recent_high = float(close.iloc[-20:].max())
    pullback = _safe_pct(c, recent_high)
    trend_ok = e21 > e50 and c > e50

    score = 40.0
    if trend_ok and -6.0 <= pullback <= -1.5 and c >= e21 * 0.985:
        score = 92.0  # ideal dip into rising EMA
    elif trend_ok and -8.0 <= pullback <= -0.5:
        score = 78.0
    elif trend_ok and pullback > -0.5:
        score = 55.0  # extended — wait for pullback
    elif trend_ok:
        score = 48.0
    elif c < e50:
        score = 28.0

    return AlgoSignal(
        name="pullback_entry",
        score=score,
        bullish=score >= 70,
        details={"pullback_pct": round(pullback, 2), "trend_ok": trend_ok},
    )


def relative_strength_medium(
    df: pd.DataFrame, bench: pd.DataFrame, window: int = 42
) -> AlgoSignal:
    """Medium-horizon RS vs SPY (~2 months) for swing ranking."""
    if len(df) < window + 1 or len(bench) < window + 1:
        return AlgoSignal("relative_strength_medium", 45.0, False, {})

    aligned = df[["Close"]].join(bench[["Close"]].rename(columns={"Close": "Bench"}), how="inner")
    if len(aligned) < window + 1:
        return AlgoSignal("relative_strength_medium", 45.0, False, {})

    ret = aligned["Close"].pct_change(window).iloc[-1] * 100
    bret = aligned["Bench"].pct_change(window).iloc[-1] * 100
    rs = float(ret - bret)

    score = 45.0 + max(-30.0, min(45.0, rs * 3.5))
    if ret > 0 and rs > 2:
        score = min(100.0, score + 10)

    return AlgoSignal(
        name="relative_strength_medium",
        score=float(np.clip(score, 0, 100)),
        bullish=score >= 60,
        details={"rs_vs_spy_pct": round(rs, 3), "ret_pct": round(float(ret), 3), "window": window},
    )


def mean_reversion_bottom(df: pd.DataFrame) -> AlgoSignal:
    """Oversold bounce setup for swing entries (not 0DTE chase)."""
    if len(df) < 30:
        return AlgoSignal("mean_reversion_bottom", 40.0, False, {})

    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    cur = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0
    prev = float(rsi.iloc[-3]) if len(rsi) >= 3 and not np.isnan(rsi.iloc[-3]) else cur
    ma20 = float(close.rolling(20).mean().iloc[-1])
    c = float(close.iloc[-1])
    rising = cur > prev

    score = 40.0
    if cur < 35 and rising and c > ma20 * 0.97:
        score = 86.0
    elif cur < 40 and rising:
        score = 74.0
    elif 40 <= cur <= 50 and rising and c >= ma20:
        score = 68.0
    elif cur > 70:
        score = 30.0

    return AlgoSignal(
        name="mean_reversion_bottom",
        score=score,
        bullish=score >= 68,
        details={"rsi": round(cur, 2), "rising": rising},
    )
