"""Assemble TradeEcho-style Echo Desk payload for the UI tab."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from odte_scanner.echo.chain_ladder import fetch_option_ladder
from odte_scanner.echo.flow import build_option_flow
from odte_scanner.echo.gex import build_dealer_edge

logger = logging.getLogger(__name__)

# AlgoEdge channels — map our ensemble names to TradeEcho-style buckets
ALGO_CHANNELS: dict[str, list[str]] = {
    "momentum": ["momentum_breakout", "macd_momentum", "volume_thrust"],
    "directional": ["ema_stack", "trend_structure", "stage_analysis"],
    "0dte_speed": ["gap_and_go", "volume_thrust", "vix_regime"],
    "mean_reversion": ["rsi_bounce", "mean_reversion_bottom", "pullback_entry"],
    "relative_strength": ["relative_strength", "relative_strength_medium"],
    "squeeze": ["squeeze_release"],
}


def _pick_echo_symbols(
    *,
    scores: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    max_symbols: int = 10,
) -> list[str]:
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for s in scores or []:
        sym = str(s.get("symbol") or "")
        if not sym or sym in seen:
            continue
        if s.get("horizon") not in (None, "0dte", "weekly"):
            # still allow, but prefer 0dte later via score
            pass
        seen.add(sym)
        ranked.append((float(s.get("ensemble_score") or 0), sym))
    for c in candidates or []:
        sym = str(c.get("symbol") or "")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        ranked.append((float(c.get("score") or 0), sym))
    # Always include liquid index names if present in quotes
    for prefer in ("SPY", "QQQ", "IWM", "SPX", "XSP"):
        if prefer in quotes and prefer not in seen:
            seen.add(prefer)
            ranked.append((90.0, prefer))
    ranked.sort(reverse=True)
    out: list[str] = []
    for _, sym in ranked:
        if sym not in out:
            out.append(sym)
        if len(out) >= max_symbols:
            break
    return out


def _build_algo_edge(scores: list[dict[str, Any]], *, max_rows: int = 30) -> dict[str, Any]:
    channels: dict[str, list[dict[str, Any]]] = {k: [] for k in ALGO_CHANNELS}
    channels["quality_stack"] = []
    rows: list[dict[str, Any]] = []

    for s in scores or []:
        sigs = s.get("signals") or []
        by_name = {str(x.get("name")): x for x in sigs}
        active = [x for x in sigs if x.get("bullish") and float(x.get("score") or 0) >= 65]
        row = {
            "symbol": s.get("symbol"),
            "horizon": s.get("horizon"),
            "ensemble_score": s.get("ensemble_score"),
            "confirms": s.get("confirms"),
            "quality": s.get("quality"),
            "entry": s.get("entry"),
            "stop": s.get("stop"),
            "target": s.get("target"),
            "risk_reward": s.get("risk_reward"),
            "reasons": (s.get("reasons") or [])[:6],
            "active_algos": [a.get("name") for a in active],
        }
        rows.append(row)
        if s.get("quality"):
            channels["quality_stack"].append(row)
        for ch, names in ALGO_CHANNELS.items():
            if any(
                (by_name.get(n) or {}).get("bullish") and float((by_name.get(n) or {}).get("score") or 0) >= 65
                for n in names
            ):
                channels[ch].append(row)

    rows.sort(key=lambda r: float(r.get("ensemble_score") or 0), reverse=True)
    for k in channels:
        channels[k] = channels[k][:12]

    return {
        "rows": rows[:max_rows],
        "channels": channels,
        "channel_names": list(ALGO_CHANNELS.keys()) + ["quality_stack"],
        "note": (
            "AlgoEdge proxy from Signal Desk multi-algo ensemble "
            "(gap-and-go, breakout, RS, squeeze, stage, etc.) — not Trade Echo's institutional tape classifier."
        ),
    }


def _build_pulse(quotes: dict[str, dict[str, Any]], scores: list[dict[str, Any]]) -> dict[str, Any]:
    score_map = {}
    for s in scores or []:
        sym = str(s.get("symbol") or "")
        if not sym:
            continue
        prev = score_map.get(sym)
        if prev is None or (s.get("horizon") == "0dte") or float(s.get("ensemble_score") or 0) > float(
            prev.get("ensemble_score") or 0
        ):
            score_map[sym] = s

    tape = []
    for sym, q in (quotes or {}).items():
        s = score_map.get(sym) or {}
        tape.append(
            {
                "symbol": sym,
                "last": q.get("last"),
                "session_change_pct": q.get("session_change_pct", q.get("change_pct")),
                "mom_5m_pct": q.get("mom_5m_pct"),
                "mom_15m_pct": q.get("mom_15m_pct"),
                "day_high": q.get("day_high"),
                "day_low": q.get("day_low"),
                "dist_from_day_high_pct": q.get("dist_from_day_high_pct"),
                "entry": s.get("entry"),
                "stop": s.get("stop"),
                "target": s.get("target"),
                "ensemble_score": s.get("ensemble_score"),
            }
        )
    tape.sort(key=lambda r: abs(float(r.get("session_change_pct") or 0)), reverse=True)
    return {
        "tape": tape[:40],
        "note": "Pulse proxy: live/extended Yahoo quotes + ATR key levels from the ensemble.",
    }


def _build_mirror(insights: dict[str, Any] | None, journal_sync: dict[str, Any] | None) -> dict[str, Any]:
    open_pos = (insights or {}).get("open_positions") or []
    closed = (insights or {}).get("closed_trades") or []
    return {
        "mode": "paper_journal",
        "open": open_pos[:20],
        "closed": closed[:20],
        "sync": journal_sync or {},
        "performance": (insights or {}).get("performance") or {},
        "note": (
            "Copy Trading proxy: Signal Desk paper journal mirrors BUY NOW / SELL NOW. "
            "Not broker-linked multi-pilot copy trading."
        ),
    }


def _build_cortex(
    *,
    flow: dict[str, Any],
    dealer: dict[str, Any],
    algo: dict[str, Any],
    insights: dict[str, Any] | None,
    actions: dict[str, Any] | None,
) -> dict[str, Any]:
    bits: list[str] = []
    fc = flow.get("counts") or {}
    if fc.get("golden"):
        bits.append(f"{fc['golden']} golden-tier flow prints (Yahoo proxy)")
    if fc.get("bullish") or fc.get("bearish"):
        bits.append(f"flow skew bull {fc.get('bullish', 0)} / bear {fc.get('bearish', 0)}")
    prim = dealer.get("primary") or {}
    if prim:
        bits.append(
            f"{prim.get('symbol')} GEX {prim.get('regime')} · flip {prim.get('flip')} · "
            f"call wall {prim.get('call_wall')} / put wall {prim.get('put_wall')}"
        )
    qn = len((algo.get("channels") or {}).get("quality_stack") or [])
    if qn:
        bits.append(f"{qn} quality algo stacks")
    buys = len((actions or {}).get("buy_now") or [])
    sells = len((actions or {}).get("sell_now") or [])
    bits.append(f"desk actions BUY {buys} / SELL {sells}")
    summary = (insights or {}).get("summary") or ""
    headline = (insights or {}).get("headline") or "Echo Desk briefing"
    return {
        "headline": headline,
        "summary": summary,
        "bullets": bits,
        "note": "Cortex proxy: rule-based briefing from flow + GEX + algos + journal — not an LLM agent.",
    }


def build_echo_board(
    *,
    scores: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    aliases: dict[str, str] | None = None,
    insights: dict[str, Any] | None = None,
    journal_sync: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    lottery: dict[str, Any] | None = None,
    max_symbols: int = 8,
    max_dte: int = 5,
) -> dict[str, Any]:
    scores = scores or []
    candidates = candidates or []
    quotes = quotes or {}
    aliases = aliases or {}

    symbols = _pick_echo_symbols(
        scores=scores, candidates=candidates, quotes=quotes, max_symbols=max_symbols
    )

    ladders: list[dict[str, Any]] = []

    def _one(sym: str) -> dict[str, Any] | None:
        spot = None
        q = quotes.get(sym) or {}
        if q.get("last") is not None:
            spot = float(q["last"])
        return fetch_option_ladder(
            sym,
            yahoo_symbol=aliases.get(sym),
            spot=spot,
            max_dte=max_dte,
            prefer_dte=0,
        )

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(symbols)))) as pool:
        futs = {pool.submit(_one, s): s for s in symbols}
        for fut in as_completed(futs):
            try:
                ladder = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("echo ladder worker failed %s: %s", futs[fut], exc)
                continue
            if ladder:
                ladders.append(ladder)

    flow = build_option_flow(ladders)
    dealer = build_dealer_edge(ladders)
    algo = _build_algo_edge(scores)
    pulse = _build_pulse(quotes, scores)
    mirror = _build_mirror(insights, journal_sync)
    cortex = _build_cortex(flow=flow, dealer=dealer, algo=algo, insights=insights, actions=actions)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "ladder_count": len(ladders),
        "option_flow": flow,
        "dealer_edge": dealer,
        "dark_pool": {
            "available": False,
            "reason": "No ATS / FINRA dark-pool feed on free Yahoo — Trade Echo Darkpool requires a paid print source.",
            "proxy_note": "Use OptionFlow large-premium rows as a weak substitute for block interest only.",
        },
        "algo_edge": algo,
        "pulse": pulse,
        "mirror": mirror,
        "cortex": cortex,
        "actions_ref": {
            "buy_now": (actions or {}).get("buy_now") or [],
            "sell_now": (actions or {}).get("sell_now") or [],
            "hist_win_gate": (actions or {}).get("hist_win_gate"),
        },
        "lottery_ref": {
            "buy_now": (lottery or {}).get("buy_now") or [],
            "sell_now": (lottery or {}).get("sell_now") or [],
            "primary": (lottery or {}).get("primary"),
        },
        "disclaimer": (
            "Echo Desk is inspired by Trade Echo modules (OptionFlow, DealerEdge, Darkpool, AlgoEdge, "
            "Copy Trading, Cortex) but is NOT affiliated with Trade Echo. Data is Yahoo-derived research "
            "proxies except where noted unavailable."
        ),
    }
