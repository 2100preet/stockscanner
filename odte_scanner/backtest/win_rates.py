from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from odte_scanner.algos.engine import QUALITY_GATES, score_ticker
from odte_scanner.config import load_config
from odte_scanner.data.fetcher import fetch_history, fetch_many

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = ROOT / "outputs" / "win_rates.json"

# Forward windows match hold horizons
HORIZON_FORWARD: dict[str, int] = {
    "0dte": 1,  # next session
    "weekly": 5,  # ~1 week
    "monthly": 21,  # ~1 month (~21 sessions) — challenge swing/leap band
    "swing": 42,  # ~2 months inside 1–3 month swing band
}

# UI / challenge aliases → win-rate bucket
_HORIZON_ALIASES: dict[str, str] = {
    "1w": "weekly",
    "week": "weekly",
    "1wk": "weekly",
    "1m": "monthly",
    "1mo": "monthly",
    "1-month": "monthly",
    "1_month": "monthly",
    "month": "monthly",
    "monthly": "monthly",
    "leap": "monthly",  # challenge leap holds ≈ 1-month strike-rate window
    "swing": "swing",
    "0dte": "0dte",
    "odte": "0dte",
}


def _stats_from_returns(rets: list[float]) -> dict[str, Any]:
    n = len(rets)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "win_pct": None,
            "hit_1pct": None,
            "hit_2pct": None,
            "avg_ret_pct": None,
        }
    wins = sum(1 for r in rets if r > 0)
    return {
        "trades": n,
        "wins": wins,
        "win_pct": round(100.0 * wins / n, 1),
        "hit_1pct": round(100.0 * sum(1 for r in rets if r >= 1.0) / n, 1),
        "hit_2pct": round(100.0 * sum(1 for r in rets if r >= 2.0) / n, 1),
        "avg_ret_pct": round(float(np.mean(rets)), 3),
    }


def compute_symbol_win_rate(
    symbol: str,
    df: pd.DataFrame,
    *,
    spy_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    weights: dict[str, float] | None,
    min_score: float,
    forward_days: int = 1,
    horizon: str = "0dte",
    require_quality: bool = True,
    step: int = 1,
) -> dict[str, Any]:
    """Walk-forward: when quality signal fires, measure forward return over N sessions."""
    rets: list[float] = []
    min_hist = 80 if horizon != "swing" else 220
    if df is None or len(df) < min_hist:
        return {
            "symbol": symbol,
            "horizon": horizon,
            "forward_days": forward_days,
            **_stats_from_returns([]),
        }

    last_i = len(df) - forward_days
    start_i = 60 if horizon != "swing" else 210
    for i in range(start_i, last_i, max(1, step)):
        window = df.iloc[: i + 1]
        spy_w = spy_df.loc[: window.index[-1]] if spy_df is not None else None
        vix_w = vix_df.loc[: window.index[-1]] if vix_df is not None else None
        try:
            ts = score_ticker(
                symbol,
                window,
                spy_df=spy_w,
                vix_df=vix_w,
                weights=weights,
                horizon=horizon,
            )
        except Exception:  # noqa: BLE001
            continue
        if require_quality:
            if not ts.quality:
                continue
        elif ts.ensemble_score < min_score:
            continue
        entry = float(df["Close"].iloc[i])
        fut = float(df["Close"].iloc[i + forward_days])
        if entry <= 0:
            continue
        rets.append((fut - entry) / entry * 100)

    return {
        "symbol": symbol,
        "horizon": horizon,
        "forward_days": forward_days,
        "min_score": min_score,
        "require_quality": require_quality,
        **_stats_from_returns(rets),
    }


def build_win_rate_table(
    symbols: list[str],
    *,
    config_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    force: bool = False,
    max_age_hours: float = 12.0,
    horizons: list[str] | None = None,
) -> dict[str, Any]:
    """
    Per-symbol historical win% when quality ensemble would have fired.
    Horizons: 0dte (1d), weekly (5d), swing (~42d / 1–3mo band).
    """
    cfg = load_config(config_path)
    cache = Path(cache_path) if cache_path else DEFAULT_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    horizons = horizons or list(HORIZON_FORWARD.keys())

    cached_raw: dict[str, Any] | None = None
    need_force_monthly = False
    if cache.exists() and not force:
        try:
            raw = json.loads(cache.read_text())
            age_h = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(raw.get("generated_at").replace("Z", "+00:00"))
            ).total_seconds() / 3600
            have = set(raw.get("symbols", {}).keys())
            # Accept cache if symbols covered and swing + monthly buckets present
            sample = next(iter(raw.get("symbols", {}).values()), {})
            has_swing = "swing" in sample
            has_monthly = "monthly" in sample
            if (
                age_h <= max_age_hours
                and set(symbols).issubset(have)
                and has_swing
                and has_monthly
            ):
                return raw
            if age_h <= max_age_hours and has_swing:
                cached_raw = raw
                need_force_monthly = not has_monthly
        except Exception:  # noqa: BLE001
            pass

    # Only compute missing symbols when a fresh-enough cache already exists
    need = list(symbols)
    if cached_raw is not None and not need_force_monthly:
        have = set((cached_raw.get("symbols") or {}).keys())
        need = [s for s in symbols if s not in have]
        if not need:
            return cached_raw
    elif cached_raw is not None and need_force_monthly:
        # Recompute all requested symbols so monthly strike-rate lands in cache
        need = list(symbols)

    scan_cfg = cfg.get("scan") or {}
    actions_cfg = cfg.get("actions") or {}
    min_score = float(actions_cfg.get("buy_score") or scan_cfg.get("min_score") or 70)
    algo_cfg = cfg.get("algos") or {}
    weights_by_hz = algo_cfg.get("weights_by_horizon") or {}
    legacy_w = algo_cfg.get("weights") or {}
    aliases = {str(k).upper(): str(v) for k, v in (cfg.get("symbol_aliases") or {}).items()}
    regime = cfg.get("regime") or {}

    histories = fetch_many(need, period="2y", aliases=aliases)
    spy = fetch_history(regime.get("spy", "SPY"), period="2y")
    vix = fetch_history(regime.get("vix", "^VIX"), period="2y", yahoo_symbol=regime.get("vix", "^VIX"))

    out_syms: dict[str, Any] = dict((cached_raw or {}).get("symbols") or {})
    for sym in need:
        df = histories.get(sym)
        if df is None or df.empty:
            out_syms[sym] = {hz: {"trades": 0, "win_pct": None} for hz in horizons}
            continue
        row: dict[str, Any] = {}
        for hz in horizons:
            # monthly uses swing quality gate / weights; forward window is 21d
            score_hz = "swing" if hz == "monthly" else hz
            w = weights_by_hz.get(score_hz) or weights_by_hz.get(hz) or legacy_w
            fwd = int(HORIZON_FORWARD.get(hz, 1))
            # Step larger for longer horizons to keep runtime reasonable
            step = 3 if score_hz == "swing" else (2 if hz == "weekly" else 1)
            if hz == "monthly":
                step = 2
            gate = QUALITY_GATES.get(score_hz, QUALITY_GATES.get("0dte", {}))
            stats = compute_symbol_win_rate(
                sym,
                df,
                spy_df=spy,
                vix_df=vix,
                weights=w,
                min_score=float(gate.get("min_score", min_score)),
                forward_days=fwd,
                horizon=score_hz,
                require_quality=True,
                step=step,
            )
            stats = {**stats, "horizon": hz, "forward_days": fwd}
            row[hz] = stats
            logger.info(
                "Win%% %s %s=%s (n=%s)",
                sym,
                hz,
                stats.get("win_pct"),
                stats.get("trades"),
            )
        out_syms[sym] = row

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_score": min_score,
        "quality_gates": QUALITY_GATES,
        "forward_days": HORIZON_FORWARD,
        "note": (
            "win_pct = historical % of QUALITY signals (score+confirms gate) where "
            "underlying was green over the forward window: 1 session (0DTE), "
            "5 sessions (1W), ~21 sessions (≈1 month / challenge leap), "
            "~42 sessions (swing 1–3mo). Strike rate = % of those signals with "
            "underlying ≥1% / ≥2% over the same window. Not option P&L."
        ),
        "symbols": out_syms,
    }
    cache.write_text(json.dumps(payload, indent=2))
    return payload


def normalize_horizon_bucket(dte_bucket: str | None) -> str:
    """Map UI / challenge labels (1m, leap, 1w, …) onto win-rate buckets."""
    raw = str(dte_bucket or "0dte").strip().lower()
    if raw in _HORIZON_ALIASES:
        return _HORIZON_ALIASES[raw]
    if "leap" in raw or "month" in raw or raw in {"1m", "1mo"}:
        return "monthly"
    if "swing" in raw:
        return "swing"
    if "week" in raw or raw == "1w":
        return "weekly"
    if raw in HORIZON_FORWARD:
        return raw
    return "0dte"


def lookup_win_stats(
    table: dict[str, Any] | None,
    symbol: str,
    dte_bucket: str | None,
) -> dict[str, Any]:
    if not table:
        return {"win_pct": None, "trades": 0, "hit_1pct": None, "hit_2pct": None}
    bucket = normalize_horizon_bucket(dte_bucket)
    row = (table.get("symbols") or {}).get(str(symbol or "").upper()) or {}
    stats = row.get(bucket) or {}
    # Fall back: monthly → swing (older caches) · swing → monthly if present
    if not stats and bucket == "monthly":
        stats = row.get("swing") or {}
        bucket = "swing" if stats else bucket
    if not stats and bucket == "swing":
        stats = row.get("monthly") or {}
    return {
        "win_pct": stats.get("win_pct"),
        "trades": stats.get("trades") or 0,
        "wins": stats.get("wins"),
        "hit_1pct": stats.get("hit_1pct"),
        "hit_2pct": stats.get("hit_2pct"),
        "avg_ret_pct": stats.get("avg_ret_pct"),
        "forward_days": stats.get("forward_days") or HORIZON_FORWARD.get(bucket),
        "horizon": bucket,
    }


def load_win_rate_table(cache_path: str | Path | None = None) -> dict[str, Any] | None:
    path = Path(cache_path) if cache_path else DEFAULT_CACHE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


def ensure_challenge_win_table(
    win_table: dict[str, Any] | None,
    *,
    config_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    max_age_hours: float = 168.0,
) -> dict[str, Any]:
    """Ensure hist win rates cover the challenge sleeve (not just today's focus scan)."""
    from odte_scanner.data.universe import challenge_hist_universe

    target = challenge_hist_universe()
    have = set((win_table or {}).get("symbols") or {})
    if have and set(target).issubset(have):
        return win_table or {}
    need = sorted(set(target) | have)
    built = build_win_rate_table(
        need,
        config_path=config_path,
        cache_path=cache_path,
        max_age_hours=max_age_hours,
    )
    if not win_table:
        return built
    merged_syms = dict((built.get("symbols") or {}))
    merged_syms.update((win_table.get("symbols") or {}))
    return {**built, **win_table, "symbols": merged_syms}


def summarize_hist_win_gate(
    table: dict[str, Any] | None,
    *,
    min_hist_win_pct: float = 80.0,
    min_hist_win_samples: int = 5,
    horizons: list[str] | None = None,
) -> dict[str, Any]:
    """Pooled backtest stats for symbols that clear the ≥80% hist-win gate.

    By construction, eligible symbol/horizon rows each have win_pct ≥ target and
    n ≥ min samples. Pooled win% is sample-weighted across those rows — this is
    the measured win rate of the tradeable set the portal will promote.
    """
    horizons = horizons or ["0dte", "weekly", "swing"]
    eligible: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    if not table:
        return {
            "eligible_count": 0,
            "eligible": [],
            "pooled_win_pct": None,
            "pooled_trades": 0,
            "pooled_hit_1pct": None,
            "ungated_pooled_win_pct": None,
            "ungated_pooled_trades": 0,
            "target_met": False,
            "note": (
                f"BUY NOW requires hist win ≥{min_hist_win_pct:.0f}% with "
                f"n≥{min_hist_win_samples} quality signals (walk-forward)."
            ),
        }

    for sym, row in (table.get("symbols") or {}).items():
        for hz in horizons:
            stats = row.get(hz) or {}
            n = int(stats.get("trades") or 0)
            win = stats.get("win_pct")
            if win is None or n <= 0:
                continue
            item = {
                "symbol": sym,
                "horizon": hz,
                "win_pct": float(win),
                "trades": n,
                "wins": int(stats.get("wins") or 0),
                "hit_1pct": stats.get("hit_1pct"),
                "hit_2pct": stats.get("hit_2pct"),
                "avg_ret_pct": stats.get("avg_ret_pct"),
            }
            all_rows.append(item)
            if float(win) >= float(min_hist_win_pct) and n >= int(min_hist_win_samples):
                eligible.append(item)

    def _pool(rows: list[dict[str, Any]]) -> tuple[float | None, int, float | None]:
        tot_n = sum(int(r["trades"]) for r in rows)
        if tot_n <= 0:
            return None, 0, None
        # Prefer explicit wins count; fall back from win_pct
        tot_wins = 0
        hit1_num = 0.0
        hit1_den = 0
        for r in rows:
            n = int(r["trades"])
            if r.get("wins") is not None:
                tot_wins += int(r["wins"])
            else:
                tot_wins += int(round(float(r["win_pct"]) / 100.0 * n))
            if r.get("hit_1pct") is not None:
                hit1_num += float(r["hit_1pct"]) / 100.0 * n
                hit1_den += n
        win_pct = round(100.0 * tot_wins / tot_n, 1)
        hit1 = round(100.0 * hit1_num / hit1_den, 1) if hit1_den else None
        return win_pct, tot_n, hit1

    pooled_win, pooled_n, pooled_hit1 = _pool(eligible)
    ungated_win, ungated_n, _ = _pool(all_rows)
    eligible.sort(key=lambda r: (r["win_pct"], r["trades"]), reverse=True)

    return {
        "eligible_count": len(eligible),
        "eligible": eligible[:40],
        "pooled_win_pct": pooled_win,
        "pooled_trades": pooled_n,
        "pooled_hit_1pct": pooled_hit1,
        "ungated_pooled_win_pct": ungated_win,
        "ungated_pooled_trades": ungated_n,
        "target_met": bool(pooled_win is not None and pooled_win >= float(min_hist_win_pct) and pooled_n > 0),
        "note": (
            f"BUY NOW requires hist win ≥{min_hist_win_pct:.0f}% with "
            f"n≥{min_hist_win_samples} quality signals (walk-forward). "
            f"Eligible pooled win={pooled_win}% on n={pooled_n} "
            f"(ungated all quality signals={ungated_win}% on n={ungated_n}). "
            "Underlying direction, not option P&L. Past ≠ future."
        ),
    }
