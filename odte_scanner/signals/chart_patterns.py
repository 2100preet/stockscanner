"""Classic chart-pattern lane — double bottom/top + triangle setups on stocks.

Scans daily OHLC for:
  - DOUBLE_BOTTOM / DOUBLE_TOP
  - ASCENDING_TRIANGLE (aka golden triangle)
  - DESCENDING_TRIANGLE
  - SYMMETRICAL_TRIANGLE

Surfaces BUY_PATTERN (bullish break), SHORT_PATTERN (bearish break),
WATCH_PATTERN (forming / near break), PATTERN_COOL (failed / invalidated).

Board-only like Levels — feeds BUY/SELL NOW as the Patterns desk (not hist-gated
Options BUY NOW). Research / paper only.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PATTERN_TYPES = (
    "DOUBLE_BOTTOM",
    "DOUBLE_TOP",
    "ASCENDING_TRIANGLE",  # golden triangle
    "DESCENDING_TRIANGLE",
    "SYMMETRICAL_TRIANGLE",
)


@dataclass
class PatternAction:
    action: str  # BUY_PATTERN | SHORT_PATTERN | WATCH_PATTERN | PATTERN_COOL
    symbol: str
    pattern: str
    strength: float
    headline: str
    detail: str
    lane: str = "patterns"
    risk_tag: str = "forming"  # forming | breakout | breakdown | failed
    side: str = "C"  # C bullish / P bearish bias for options desk mapping
    right: str = "C"
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    spot: float | None = None
    neckline: float | None = None
    support: float | None = None
    resistance: float | None = None
    target: float | None = None
    stop: float | None = None
    pattern_low: float | None = None
    pattern_high: float | None = None
    bars_span: int | None = None
    pivot_count: int | None = None
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    dte_bucket: str = "swing"
    hold_style: str = "pattern"
    golden: bool = False  # ascending / golden triangle

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _col(df: pd.DataFrame, name: str) -> pd.Series | None:
    for c in df.columns:
        if str(c).lower() == name.lower():
            return df[c]
    return None


def find_swing_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    left: int = 3,
    right: int = 3,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as (index, price) lists."""
    n = len(highs)
    sh: list[tuple[int, float]] = []
    sl: list[tuple[int, float]] = []
    if n < left + right + 1:
        return sh, sl
    for i in range(left, n - right):
        window_h = highs[i - left : i + right + 1]
        window_l = lows[i - left : i + right + 1]
        if highs[i] >= np.max(window_h) - 1e-12:
            sh.append((i, float(highs[i])))
        if lows[i] <= np.min(window_l) + 1e-12:
            sl.append((i, float(lows[i])))
    return sh, sl


def _near(a: float, b: float, tol_pct: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    mid = (a + b) / 2.0
    return abs(a - b) / mid * 100.0 <= tol_pct


def detect_double_bottom(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    swing_lows: list[tuple[int, float]],
    tol_pct: float = 1.8,
    min_sep: int = 5,
    max_sep: int = 60,
) -> dict[str, Any] | None:
    if len(swing_lows) < 2 or len(closes) < 10:
        return None
    best: dict[str, Any] | None = None
    for i in range(len(swing_lows) - 1):
        i1, p1 = swing_lows[i]
        for j in range(i + 1, len(swing_lows)):
            i2, p2 = swing_lows[j]
            sep = i2 - i1
            if sep < min_sep or sep > max_sep:
                continue
            if not _near(p1, p2, tol_pct):
                continue
            # Second low should not undercut first by more than tol
            if p2 < p1 * (1.0 - tol_pct / 100.0):
                continue
            neck = float(np.max(highs[i1 : i2 + 1]))
            bottom = min(p1, p2)
            if neck <= bottom:
                continue
            height = neck - bottom
            if height / bottom < 0.02:  # too shallow
                continue
            score = 55.0 + max(0.0, 15.0 - abs(p1 - p2) / bottom * 100.0 * 4)
            score += min(15.0, height / bottom * 100.0)
            cand = {
                "pattern": "DOUBLE_BOTTOM",
                "side": "bull",
                "support": bottom,
                "resistance": neck,
                "neckline": neck,
                "pattern_low": bottom,
                "pattern_high": neck,
                "target": neck + height,
                "stop": bottom * 0.985,
                "bars_span": sep,
                "pivot_count": 2,
                "score": score,
                "pivots": [(i1, p1), (i2, p2)],
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def detect_double_top(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    swing_highs: list[tuple[int, float]],
    tol_pct: float = 1.8,
    min_sep: int = 5,
    max_sep: int = 60,
) -> dict[str, Any] | None:
    if len(swing_highs) < 2 or len(closes) < 10:
        return None
    best: dict[str, Any] | None = None
    for i in range(len(swing_highs) - 1):
        i1, p1 = swing_highs[i]
        for j in range(i + 1, len(swing_highs)):
            i2, p2 = swing_highs[j]
            sep = i2 - i1
            if sep < min_sep or sep > max_sep:
                continue
            if not _near(p1, p2, tol_pct):
                continue
            if p2 > p1 * (1.0 + tol_pct / 100.0):
                continue
            neck = float(np.min(lows[i1 : i2 + 1]))
            top = max(p1, p2)
            if top <= neck:
                continue
            height = top - neck
            if height / top < 0.02:
                continue
            score = 55.0 + max(0.0, 15.0 - abs(p1 - p2) / top * 100.0 * 4)
            score += min(15.0, height / top * 100.0)
            cand = {
                "pattern": "DOUBLE_TOP",
                "side": "bear",
                "support": neck,
                "resistance": top,
                "neckline": neck,
                "pattern_low": neck,
                "pattern_high": top,
                "target": neck - height,
                "stop": top * 1.015,
                "bars_span": sep,
                "pivot_count": 2,
                "score": score,
                "pivots": [(i1, p1), (i2, p2)],
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def _linreg_slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if np.std(x) < 1e-9:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def detect_triangles(
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
    lookback: int = 40,
    flat_tol_pct: float = 1.2,
) -> dict[str, Any] | None:
    """Detect ascending / descending / symmetrical triangles from recent swings."""
    if len(highs) < 20:
        return None
    n = len(highs)
    start = max(0, n - lookback)
    sh = [(i, p) for i, p in swing_highs if i >= start]
    sl = [(i, p) for i, p in swing_lows if i >= start]
    if len(sh) < 2 or len(sl) < 2:
        return None

    # Use last up to 4 highs / lows
    sh = sh[-4:]
    sl = sl[-4:]
    hi_xs = [float(i) for i, _ in sh]
    hi_ys = [float(p) for _, p in sh]
    lo_xs = [float(i) for i, _ in sl]
    lo_ys = [float(p) for _, p in sl]
    hi_slope = _linreg_slope(hi_xs, hi_ys)
    lo_slope = _linreg_slope(lo_xs, lo_ys)
    if hi_slope is None or lo_slope is None:
        return None

    last_hi = hi_ys[-1]
    last_lo = lo_ys[-1]
    mid = (last_hi + last_lo) / 2.0
    if mid <= 0 or last_hi <= last_lo:
        return None
    width_pct = (last_hi - last_lo) / mid * 100.0
    if width_pct < 1.0 or width_pct > 25.0:
        return None

    hi_range_pct = (max(hi_ys) - min(hi_ys)) / mid * 100.0
    lo_range_pct = (max(lo_ys) - min(lo_ys)) / mid * 100.0
    flat_hi = hi_range_pct <= flat_tol_pct or abs(hi_slope) / mid * 100.0 < 0.05
    flat_lo = lo_range_pct <= flat_tol_pct or abs(lo_slope) / mid * 100.0 < 0.05
    rising_lo = lo_slope > 0 and lo_ys[-1] > lo_ys[0]
    falling_hi = hi_slope < 0 and hi_ys[-1] < hi_ys[0]
    falling_lo = lo_slope < 0 and lo_ys[-1] < lo_ys[0]
    rising_hi = hi_slope > 0 and hi_ys[-1] > hi_ys[0]

    resistance = float(np.mean(hi_ys[-2:])) if flat_hi else float(last_hi)
    support = float(np.mean(lo_ys[-2:])) if flat_lo else float(last_lo)
    height = resistance - support
    bars_span = int(max(hi_xs[-1], lo_xs[-1]) - min(hi_xs[0], lo_xs[0]))

    pattern = None
    side = None
    golden = False
    score = 50.0

    if flat_hi and rising_lo and not falling_lo:
        pattern = "ASCENDING_TRIANGLE"
        side = "bull"
        golden = True
        score = 70.0 + min(15.0, (lo_ys[-1] - lo_ys[0]) / mid * 100.0 * 2)
        target = resistance + height
        stop = support * 0.99
    elif flat_lo and falling_hi and not rising_hi:
        pattern = "DESCENDING_TRIANGLE"
        side = "bear"
        score = 68.0 + min(15.0, (hi_ys[0] - hi_ys[-1]) / mid * 100.0 * 2)
        target = support - height
        stop = resistance * 1.01
    elif falling_hi and rising_lo:
        pattern = "SYMMETRICAL_TRIANGLE"
        # Bias from which side is closer to break (spot later) — default watch both
        side = "neutral"
        score = 62.0 + min(12.0, (20.0 - width_pct))
        target = None  # set at classify time from break direction
        stop = None
    else:
        return None

    return {
        "pattern": pattern,
        "side": side,
        "support": support,
        "resistance": resistance,
        "neckline": resistance if side == "bull" else support if side == "bear" else mid,
        "pattern_low": support,
        "pattern_high": resistance,
        "target": target if pattern != "SYMMETRICAL_TRIANGLE" else None,
        "stop": stop if pattern != "SYMMETRICAL_TRIANGLE" else None,
        "bars_span": max(1, bars_span),
        "pivot_count": len(sh) + len(sl),
        "score": score,
        "golden": golden,
        "hi_slope": hi_slope,
        "lo_slope": lo_slope,
    }


def detect_patterns_from_ohlc(
    df: pd.DataFrame,
    *,
    pivot_left: int = 2,
    pivot_right: int = 2,
    double_tol_pct: float = 1.8,
    triangle_lookback: int = 45,
) -> list[dict[str, Any]]:
    """Run classic pattern detectors on an OHLC frame. Returns ranked pattern dicts."""
    if df is None or df.empty or len(df) < 20:
        return []
    high = _col(df, "High")
    low = _col(df, "Low")
    close = _col(df, "Close")
    if high is None or low is None or close is None:
        return []
    highs = high.to_numpy(dtype=float)
    lows = low.to_numpy(dtype=float)
    closes = close.to_numpy(dtype=float)
    # Drop NaNs
    mask = np.isfinite(highs) & np.isfinite(lows) & np.isfinite(closes)
    if mask.sum() < 20:
        return []
    highs, lows, closes = highs[mask], lows[mask], closes[mask]

    sh, sl = find_swing_pivots(highs, lows, left=pivot_left, right=pivot_right)
    found: list[dict[str, Any]] = []
    db = detect_double_bottom(
        closes, highs, lows, swing_lows=sl, tol_pct=double_tol_pct
    )
    if db:
        found.append(db)
    dt = detect_double_top(
        closes, highs, lows, swing_highs=sh, tol_pct=double_tol_pct
    )
    if dt:
        found.append(dt)
    tri = detect_triangles(
        highs,
        lows,
        swing_highs=sh,
        swing_lows=sl,
        lookback=triangle_lookback,
    )
    if tri:
        found.append(tri)
    found.sort(key=lambda r: -float(r.get("score") or 0))
    return found


def classify_pattern(
    symbol: str,
    pattern: dict[str, Any],
    *,
    spot: float | None,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    near_break_pct: float = 1.0,
) -> PatternAction:
    """Map a detected pattern + live spot into BUY/SHORT/WATCH/COOL."""
    sym = str(symbol or "").upper()
    pname = str(pattern.get("pattern") or "PATTERN")
    support = pattern.get("support")
    resistance = pattern.get("resistance")
    neckline = pattern.get("neckline")
    target = pattern.get("target")
    stop = pattern.get("stop")
    side = str(pattern.get("side") or "neutral")
    golden = bool(pattern.get("golden"))
    base_score = float(pattern.get("score") or 50)
    live = None
    if quote:
        for k in ("session_change_pct", "change_pct", "live_change_pct"):
            if quote.get(k) is not None:
                try:
                    live = float(quote[k])
                    break
                except (TypeError, ValueError):
                    pass
    ens = float(ensemble_score or 0)

    label = "GOLDEN TRIANGLE" if golden else pname.replace("_", " ")
    playbook = [
        f"pattern {label}",
        f"support ${float(support):.2f}" if support else "support —",
        f"resist ${float(resistance):.2f}" if resistance else "resist —",
    ]
    if target:
        playbook.append(f"tgt ${float(target):.2f}")
    if stop:
        playbook.append(f"stop ${float(stop):.2f}")

    confirms = 0
    if spot and support and spot >= float(support) * 0.995:
        confirms += 1
    if live is not None and ((side == "bull" and live >= 0) or (side == "bear" and live <= 0)):
        confirms += 1
    if ens >= 65:
        confirms += 1

    strength = min(100.0, base_score + confirms * 5 + max(0.0, ens) * 0.15)

    def _mk(
        action: str,
        *,
        risk: str,
        headline: str,
        detail: str,
        right: str,
        strength_v: float,
    ) -> PatternAction:
        return PatternAction(
            action=action,
            symbol=sym,
            pattern=pname,
            strength=round(strength_v, 1),
            headline=headline,
            detail=detail,
            risk_tag=risk,
            side=right,
            right=right,
            playbook=playbook,
            confirms=confirms,
            spot=spot,
            neckline=float(neckline) if neckline is not None else None,
            support=float(support) if support is not None else None,
            resistance=float(resistance) if resistance is not None else None,
            target=float(target) if target is not None else None,
            stop=float(stop) if stop is not None else None,
            pattern_low=float(pattern["pattern_low"]) if pattern.get("pattern_low") is not None else None,
            pattern_high=float(pattern["pattern_high"]) if pattern.get("pattern_high") is not None else None,
            bars_span=int(pattern["bars_span"]) if pattern.get("bars_span") is not None else None,
            pivot_count=int(pattern["pivot_count"]) if pattern.get("pivot_count") is not None else None,
            ensemble_score=ens or None,
            live_change_pct=live,
            golden=golden,
        )

    if spot is None or spot <= 0:
        return _mk(
            "WATCH_PATTERN",
            risk="forming",
            headline=f"WATCH {label} {sym}",
            detail=f"{label} detected · need live spot to confirm break",
            right="C" if side != "bear" else "P",
            strength_v=min(strength, 55),
        )

    # Double bottom / ascending / bullish break
    if side == "bull" and neckline is not None:
        nl = float(neckline)
        pct_to = (nl - spot) / nl * 100.0
        if spot >= nl:
            tgt = float(target) if target else nl + (nl - float(support or spot))
            return _mk(
                "BUY_PATTERN",
                risk="breakout",
                headline=f"BUY {label} {sym}",
                detail=(
                    f"{label} · spot ${spot:.2f} cleared neckline/resist ${nl:.2f}"
                    + (f" · tgt ${tgt:.2f}" if tgt else "")
                    + (f" · stop ${float(stop):.2f}" if stop else "")
                ),
                right="C",
                strength_v=strength + 8,
            )
        if 0 < pct_to <= near_break_pct:
            return _mk(
                "WATCH_PATTERN",
                risk="forming",
                headline=f"WATCH {label} {sym}",
                detail=f"{label} · spot ${spot:.2f} within {pct_to:.1f}% of break ${nl:.2f}",
                right="C",
                strength_v=min(strength, 78),
            )
        if support and spot < float(support) * 0.97:
            return _mk(
                "PATTERN_COOL",
                risk="failed",
                headline=f"COOL {label} {sym}",
                detail=f"{label} failed · spot ${spot:.2f} undercut support ${float(support):.2f}",
                right="C",
                strength_v=max(8.0, strength * 0.3),
            )
        return _mk(
            "WATCH_PATTERN",
            risk="forming",
            headline=f"WATCH {label} {sym}",
            detail=f"{label} forming · spot ${spot:.2f} · need break ≥ ${nl:.2f} ({pct_to:.1f}% away)",
            right="C",
            strength_v=min(strength, 65),
        )

    # Double top / descending / bearish break
    if side == "bear" and neckline is not None:
        nl = float(neckline)
        pct_to = (spot - nl) / nl * 100.0
        if spot <= nl:
            tgt = float(target) if target else nl - (float(resistance or spot) - nl)
            return _mk(
                "SHORT_PATTERN",
                risk="breakdown",
                headline=f"SHORT {label} {sym}",
                detail=(
                    f"{label} · spot ${spot:.2f} broke neckline/support ${nl:.2f}"
                    + (f" · tgt ${tgt:.2f}" if tgt else "")
                    + (f" · stop ${float(stop):.2f}" if stop else "")
                ),
                right="P",
                strength_v=strength + 8,
            )
        if 0 < pct_to <= near_break_pct:
            return _mk(
                "WATCH_PATTERN",
                risk="forming",
                headline=f"WATCH {label} {sym}",
                detail=f"{label} · spot ${spot:.2f} within {pct_to:.1f}% of break ${nl:.2f}",
                right="P",
                strength_v=min(strength, 78),
            )
        if resistance and spot > float(resistance) * 1.03:
            return _mk(
                "PATTERN_COOL",
                risk="failed",
                headline=f"COOL {label} {sym}",
                detail=f"{label} failed · spot ${spot:.2f} above resist ${float(resistance):.2f}",
                right="P",
                strength_v=max(8.0, strength * 0.3),
            )
        return _mk(
            "WATCH_PATTERN",
            risk="forming",
            headline=f"WATCH {label} {sym}",
            detail=f"{label} forming · spot ${spot:.2f} · need break ≤ ${nl:.2f}",
            right="P",
            strength_v=min(strength, 65),
        )

    # Symmetrical — break either way
    res = float(resistance) if resistance is not None else None
    sup = float(support) if support is not None else None
    if res and spot >= res:
        height = res - (sup or spot * 0.97)
        return _mk(
            "BUY_PATTERN",
            risk="breakout",
            headline=f"BUY SYMMETRICAL TRIANGLE {sym}",
            detail=f"Symmetrical triangle upside break · spot ${spot:.2f} ≥ ${res:.2f} · tgt ${res + height:.2f}",
            right="C",
            strength_v=strength + 5,
        )
    if sup and spot <= sup:
        height = (res or spot * 1.03) - sup
        return _mk(
            "SHORT_PATTERN",
            risk="breakdown",
            headline=f"SHORT SYMMETRICAL TRIANGLE {sym}",
            detail=f"Symmetrical triangle downside break · spot ${spot:.2f} ≤ ${sup:.2f} · tgt ${sup - height:.2f}",
            right="P",
            strength_v=strength + 5,
        )
    return _mk(
        "WATCH_PATTERN",
        risk="forming",
        headline=f"WATCH SYMMETRICAL TRIANGLE {sym}",
        detail=(
            f"Symmetrical triangle coiling · spot ${spot:.2f}"
            + (f" · range ${sup:.2f}–${res:.2f}" if sup and res else "")
        ),
        right="C",
        strength_v=min(strength, 62),
    )


def _spot_from(quote: dict[str, Any] | None, score: dict[str, Any] | None = None) -> float | None:
    for src in (quote or {}, score or {}):
        for k in ("last", "live_last", "spot", "price", "close", "last_price"):
            v = src.get(k)
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f and f > 0:
                return f
    return None


def build_pattern_board(
    *,
    symbols: Iterable[str] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    scores: list[dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    histories: dict[str, pd.DataFrame] | None = None,
    fetch_bars: bool = True,
    max_symbols: int = 36,
    period: str = "6mo",
    near_break_pct: float = 1.0,
    double_tol_pct: float = 1.8,
) -> dict[str, Any]:
    """Build BUY/SHORT/WATCH/COOL pattern board for liquid stocks."""
    quotes = quotes or {}
    aliases = aliases or {}
    histories = dict(histories or {})
    score_by = {
        str(r.get("symbol") or "").upper(): r
        for r in (scores or [])
        if r.get("symbol")
    }

    # Universe priority: explicit list → scored symbols → quote keys
    ordered: list[str] = []
    seen: set[str] = set()
    for src in (symbols or [], [r.get("symbol") for r in (scores or [])], list(quotes.keys())):
        for s in src:
            sym = str(s or "").upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            ordered.append(sym)
    ordered = ordered[: max(8, int(max_symbols))]

    if fetch_bars:
        missing = [s for s in ordered if s not in histories or histories[s] is None or histories[s].empty]
        if missing:
            try:
                from odte_scanner.data.fetcher import fetch_history

                for sym in missing[:max_symbols]:
                    try:
                        df = fetch_history(
                            sym,
                            period=period,
                            interval="1d",
                            yahoo_symbol=aliases.get(sym),
                            cache_max_age_hours=8,
                        )
                        if df is not None and not df.empty:
                            histories[sym] = df
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("pattern bars %s: %s", sym, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pattern fetch_history unavailable: %s", exc)

    buy: list[dict[str, Any]] = []
    short: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    cool: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    by_pattern: dict[str, int] = {p: 0 for p in PATTERN_TYPES}

    for sym in ordered:
        df = histories.get(sym)
        if df is None or getattr(df, "empty", True):
            continue
        try:
            detected = detect_patterns_from_ohlc(df, double_tol_pct=double_tol_pct)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pattern detect %s: %s", sym, exc)
            continue
        if not detected:
            continue
        # Prefer the strongest pattern that is actionable, else best overall
        q = quotes.get(sym) or {}
        sc = score_by.get(sym) or {}
        spot = _spot_from(q, sc)
        ens = sc.get("ensemble_score") or sc.get("score")
        actions = [
            classify_pattern(
                sym,
                p,
                spot=spot,
                quote=q,
                ensemble_score=float(ens) if ens is not None else None,
                near_break_pct=near_break_pct,
            )
            for p in detected[:3]
        ]
        # Pick best action: BUY/SHORT first, then WATCH, then COOL
        rank = {"BUY_PATTERN": 0, "SHORT_PATTERN": 0, "WATCH_PATTERN": 1, "PATTERN_COOL": 2}
        actions.sort(key=lambda a: (rank.get(a.action, 9), -a.strength))
        act = actions[0]
        row = act.to_dict()
        all_rows.append(row)
        by_pattern[act.pattern] = by_pattern.get(act.pattern, 0) + 1
        if act.action == "BUY_PATTERN":
            buy.append(row)
        elif act.action == "SHORT_PATTERN":
            short.append(row)
        elif act.action == "PATTERN_COOL":
            cool.append(row)
        else:
            watch.append(row)

    buy.sort(key=lambda r: -float(r.get("strength") or 0))
    short.sort(key=lambda r: -float(r.get("strength") or 0))
    watch.sort(key=lambda r: -float(r.get("strength") or 0))
    cool.sort(key=lambda r: -float(r.get("strength") or 0))

    primary = next(iter(buy or short or watch), None)
    return {
        "buy_pattern": buy,
        "buy_now": buy,  # alias for NOW board
        "short_pattern": short,
        "sell_now": short,  # bearish bias → SELL/SHORT side of NOW board
        "watch": watch,
        "cool": cool,
        "primary": primary,
        "counts": {
            "buy_pattern": len(buy),
            "short_pattern": len(short),
            "watch": len(watch),
            "cool": len(cool),
            "scanned": len(ordered),
            "hits": len(all_rows),
            **{f"pat_{k.lower()}": v for k, v in by_pattern.items()},
        },
        "by_pattern": by_pattern,
        "symbols_scanned": ordered,
        "purpose": (
            "Classic stock chart patterns on daily bars: double bottom/top, "
            "ascending (golden) / descending / symmetrical triangles. "
            "BUY on bullish neckline/resist break; SHORT on bearish support break."
        ),
        "rules": [
            "DOUBLE_BOTTOM: two lows within ~1.8% → BUY when spot clears the neckline high between them.",
            "DOUBLE_TOP: two highs within ~1.8% → SHORT when spot breaks the neckline low between them.",
            "ASCENDING_TRIANGLE (golden): flat resistance + rising lows → BUY on upside break.",
            "DESCENDING_TRIANGLE: flat support + falling highs → SHORT on downside break.",
            "SYMMETRICAL_TRIANGLE: converging highs/lows → trade the break either way.",
            "WATCH = forming / within ~1% of break. COOL = pattern invalidated.",
            "Board-only Patterns desk — not hist-gated Options BUY NOW. Research / paper only.",
        ],
        "disclaimer": (
            "Pattern recognition is heuristic on daily OHLC pivots — not a guarantee. "
            "False breaks are common. Options can expire worthless. Not financial advice."
        ),
    }
