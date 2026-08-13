from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.algos.engine import (
    DEFAULT_WEIGHTS,
    QUALITY_GATES,
    scan_all_horizons,
    summarize_scan,
)
from odte_scanner.calendars import (
    WEEKDAY_NAMES,
    expiry_tags,
    mon_wed_priority_symbols,
    resolve_universe,
    resolve_yahoo_symbol,
    today_weekday,
)
from odte_scanner.config import load_config
from odte_scanner.data.fetcher import fetch_history, fetch_many
from odte_scanner.data.universe import resolve_scan_universe
from odte_scanner.options.selector import CallCandidate, select_calls
from odte_scanner.trading.paper import PaperTrader

logger = logging.getLogger(__name__)


def _weights_by_horizon(cfg: dict[str, Any]) -> dict[str, dict[str, float]]:
    algo = cfg.get("algos") or {}
    by_hz = algo.get("weights_by_horizon") or {}
    legacy = algo.get("weights") or {}
    out: dict[str, dict[str, float]] = {}
    for hz, defaults in DEFAULT_WEIGHTS.items():
        merged = dict(defaults)
        merged.update(legacy)
        if hz in by_hz:
            merged.update({k: float(v) for k, v in (by_hz[hz] or {}).items()})
        out[hz] = merged
    return out


def run_scan(
    config_path: str | None = None,
    *,
    place_paper: bool = True,
    universe_mode: str | None = None,
    horizon: str | None = None,
) -> dict[str, Any]:
    # ML6 is a dedicated earnings/catalyst mode — not the 0DTE ensemble
    hz = (horizon or "").lower().strip()
    if hz == "ml6" or (universe_mode or "").lower() == "ml6":
        from odte_scanner.ml6.board import run_ml6_scan

        return run_ml6_scan(config_path, place_paper=False)

    cfg = load_config(config_path)
    uni_mode = universe_mode or (cfg.get("universe") or {}).get("mode") or "focus"
    tickers = resolve_scan_universe(cfg, mode=uni_mode)
    focus = resolve_universe(cfg)
    regime = cfg.get("regime", {})
    scan_cfg = cfg.get("scan", {})
    opt_cfg = cfg.get("options", {})
    risk = cfg.get("risk", {})
    weights_hz = _weights_by_horizon(cfg)
    aliases = {str(k).upper(): str(v) for k, v in (cfg.get("symbol_aliases") or {}).items()}
    lookback = int(scan_cfg.get("lookback_days", 120))
    # Swing needs longer history
    period = "2y" if lookback > 180 or uni_mode in ("liquid", "screener", "all") else "1y"

    weekday = today_weekday()
    priority = mon_wed_priority_symbols(cfg, weekday)
    bonus = float(scan_cfg.get("mon_wed_score_bonus", 4.0))
    prefer_cal = bool(scan_cfg.get("prefer_mon_wed_calendar", True))

    histories = fetch_many(tickers, period=period, aliases=aliases)
    spy_sym = regime.get("spy", "SPY")
    spy = fetch_history(
        spy_sym,
        period=period,
        yahoo_symbol=resolve_yahoo_symbol(spy_sym, cfg),
    )
    vix_sym = regime.get("vix", "^VIX")
    vix = fetch_history(vix_sym, period=period, yahoo_symbol=vix_sym)

    by_horizon = scan_all_horizons(
        histories,
        spy_df=spy,
        vix_df=vix,
        weights_by_horizon=weights_hz,
        min_score=0.0,
    )

    # Calendar boost on 0DTE focus names
    for ts in by_horizon.get("0dte", []):
        tags = expiry_tags(ts.symbol, cfg)
        ts.reasons = list(ts.reasons)
        if prefer_cal and ts.symbol in priority and weekday <= 4:
            ts.ensemble_score = min(100.0, ts.ensemble_score + bonus)
            ts.reasons.append(f"calendar_{WEEKDAY_NAMES[weekday]}=+{bonus:.0f}")
        ts.reasons.insert(0, "/".join(tags))

    min_score = float(scan_cfg.get("min_score", 62))
    lo = float(scan_cfg.get("target_move_pct_min", 1.0))
    max_show = int(opt_cfg.get("max_candidates_shown", 12))

    # Options only for focus / high-score 0DTE+weekly (avoid blasting Yahoo on 100+ names)
    option_syms = set(focus)
    for hz_name in ("0dte", "weekly"):
        for ts in by_horizon.get(hz_name, []):
            if ts.quality or ts.ensemble_score >= min_score:
                if len(option_syms) < 40:
                    option_syms.add(ts.symbol)

    candidates: list[CallCandidate] = []
    odte_scores = {t.symbol: t for t in by_horizon.get("0dte", [])}
    for sym in sorted(option_syms):
        ts = odte_scores.get(sym)
        if ts is None:
            continue
        if ts.ensemble_score < min_score and not ts.quality:
            continue
        if ts.expected_move_pct < lo * 0.6:
            continue
        picked = select_calls(
            ts.symbol,
            ts.last_price,
            ts.ensemble_score,
            ts.reasons,
            max_dte=int(opt_cfg.get("max_dte", 7)),
            odte_max_dte=int(opt_cfg.get("odte_max_dte", 1)),
            otm_pct_max=float(opt_cfg.get("otm_pct_max", 3.0)),
            itm_pct_max=float(opt_cfg.get("itm_pct_max", 1.5)),
            max_ask=float(opt_cfg.get("max_ask", 25.0)),
            min_open_interest=int(opt_cfg.get("min_open_interest", 50)),
            min_volume=int(opt_cfg.get("min_volume", 10)),
            yahoo_symbol=resolve_yahoo_symbol(ts.symbol, cfg),
            per_bucket=int(opt_cfg.get("per_bucket", 1)),
        )
        candidates.extend(picked)

    candidates.sort(key=lambda c: (c.score, -c.dte), reverse=True)
    zero = [c for c in candidates if c.dte_bucket == "0dte"]
    week = [c for c in candidates if c.dte_bucket == "weekly"]
    top = (zero[: max_show // 2] + week[: max_show // 2]) or candidates[:max_show]

    # Swing action cards (shares / LEAPs style — no short DTE required)
    swing_cards = []
    for ts in by_horizon.get("swing", []):
        if not ts.quality and ts.ensemble_score < float(QUALITY_GATES["swing"]["min_score"]):
            continue
        swing_cards.append(ts.to_dict())
        if len(swing_cards) >= max_show:
            break

    weekly_cards = []
    for ts in by_horizon.get("weekly", []):
        if not ts.quality and ts.ensemble_score < float(QUALITY_GATES["weekly"]["min_score"]):
            continue
        weekly_cards.append(ts.to_dict())
        if len(weekly_cards) >= max_show:
            break

    paper_trades = []
    paper_cfg = cfg.get("paper_trading", {})
    max_trades = int(risk.get("max_trades_per_day", 3))
    if place_paper and paper_cfg.get("enabled", True) and not cfg.get("live_trading", {}).get("enabled"):
        trader = PaperTrader(
            starting_cash=float(paper_cfg.get("starting_cash", 5000)),
            ledger_path=paper_cfg.get("ledger_path", "outputs/paper_ledger.json"),
        )
        for c in (zero + week)[:max_trades]:
            if c.score < min_score or c.synthetic:
                continue
            trade = trader.open_call(
                c,
                contracts=int(risk.get("max_contracts_per_trade", 1)),
                max_risk_usd=float(risk.get("max_risk_per_trade_usd", 250)),
                max_trades_today=max_trades,
            )
            if trade:
                paper_trades.append(trade.__dict__)

    ranked_0dte = sorted(by_horizon.get("0dte", []), key=lambda s: s.ensemble_score, reverse=True)

    # Always attach ML6 board (cheap — curated sleeve only)
    ml6_board = None
    try:
        from odte_scanner.ml6.board import build_ml6_board
        from odte_scanner.ml6.watchlist import ml6_tickers as _ml6_syms

        ml6_syms = _ml6_syms()
        missing = [s for s in ml6_syms if s not in histories or histories[s] is None or len(histories[s]) < 5]
        if missing:
            extra = fetch_many(missing, period="1y", aliases=aliases)
            histories.update(extra)
        ml6_board = build_ml6_board(histories, symbols=ml6_syms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML6 board failed: %s", exc)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_weekday": WEEKDAY_NAMES[weekday],
        "universe_mode": uni_mode,
        "universe_size": len(tickers),
        "focus_size": len(focus),
        "fridays_included": True,
        "mon_wed_priority_count": len(priority),
        "min_score": min_score,
        "quality_gates": QUALITY_GATES,
        "horizons": {
            "0dte": summarize_scan(ranked_0dte),
            "weekly": summarize_scan(by_horizon.get("weekly", [])),
            "swing": summarize_scan(by_horizon.get("swing", [])),
            "ml6": (ml6_board or {}).get("watchlist") or [],
        },
        "ml6": ml6_board,
        "scores": summarize_scan(ranked_0dte),  # backward compat
        "action_cards": {
            "0dte_quality": [t.to_dict() for t in ranked_0dte if t.quality][:max_show],
            "weekly_quality": weekly_cards,
            "swing_quality": swing_cards,
            "ml6_watch": (ml6_board or {}).get("watchlist") or [],
        },
        "call_candidates": [c.to_dict() for c in top],
        "call_candidates_0dte": [c.to_dict() for c in zero[:max_show]],
        "call_candidates_weekly": [c.to_dict() for c in week[:max_show]],
        "paper_trades": paper_trades,
        "disclaimer": (
            "Educational / research tool only. Options can expire worthless. "
            "Past signals do not guarantee future results. Quality gates filter for "
            "higher historical win rates (fewer trades). Not affiliated with Signa or Intellectia. "
            "ML6 never auto-BUYs on an earnings print alone — reaction confirmation required."
        ),
    }

    try:
        from odte_scanner.backtest.win_rates import build_win_rate_table

        wr_syms = sorted(
            {c.symbol for c in top}
            | {c["symbol"] for c in swing_cards[:8]}
            | set(focus[:15])
        )
        win_table = build_win_rate_table(wr_syms, config_path=config_path)
        report["win_rates"] = win_table
    except Exception as exc:  # noqa: BLE001
        logger.warning("win rate table failed: %s", exc)

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"scan_{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    (out_dir / "latest_scan.json").write_text(json.dumps(report, indent=2))
    if ml6_board:
        (out_dir / "latest_ml6.json").write_text(json.dumps(ml6_board, indent=2))
    logger.info(
        "Wrote %s (universe=%s n=%d 0DTE_calls=%d weekly=%d swing_cards=%d ml6=%d)",
        path,
        uni_mode,
        len(tickers),
        len(zero),
        len(week),
        len(swing_cards),
        len((ml6_board or {}).get("watchlist") or []),
    )
    return report
