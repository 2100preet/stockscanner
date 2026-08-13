"""Assemble TradeEcho-style Echo Desk payload for the UI tab."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from odte_scanner.echo.chain_ladder import fetch_option_ladder
from odte_scanner.echo.darkpool import build_darkpool_board
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
    prefer_symbols: list[str] | None = None,
) -> list[str]:
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    # Earnings / darling names first so Flow Desk mirrors Bullflow "earnings soon" focus
    for sym in prefer_symbols or []:
        u = str(sym or "").upper()
        if not u or u in seen:
            continue
        seen.add(u)
        ranked.append((200.0, u))
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
    dark_pool: dict[str, Any] | None = None,
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
    dp = dark_pool or {}
    if dp.get("available"):
        dc = dp.get("counts") or {}
        bits.append(
            f"FINRA ATS week {dp.get('week_start')}: {dc.get('symbols', 0)} symbols · "
            f"{dc.get('surges', 0)} surges / {dc.get('drops', 0)} drops"
        )
        if dp.get("surges"):
            top = dp["surges"][0]
            sh = int(top.get("shares") or 0)
            bits.append(
                f"DP surge {top.get('symbol')} {top.get('surge_ratio')}× ({sh:,} shares)"
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
        "note": "Cortex proxy: rule-based briefing from flow + GEX + dark pool + algos — not an LLM agent.",
    }


def _ladders_from_candidates(candidates: list[dict[str, Any]], quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback mini-ladders from already-fetched call candidates (no extra Yahoo hits)."""
    by_sym: dict[str, dict[str, Any]] = {}
    for c in candidates or []:
        sym = str(c.get("symbol") or "")
        if not sym:
            continue
        spot = float(
            (quotes.get(sym) or {}).get("last")
            or c.get("live_spot")
            or c.get("spot")
            or 0
        )
        bucket = by_sym.setdefault(
            sym,
            {
                "symbol": sym,
                "expiry": c.get("expiry"),
                "dte": c.get("dte"),
                "spot": spot,
                "calls": [],
                "puts": [],
                "source": "candidates",
            },
        )
        mid = float(c.get("ask") or c.get("mid") or 0)
        vol = int(c.get("volume") or 0)
        oi = int(c.get("open_interest") or 0)
        bucket["calls"].append(
            {
                "right": "C",
                "strike": float(c.get("strike") or 0),
                "bid": float(c.get("bid") or 0),
                "ask": float(c.get("ask") or 0),
                "last": mid,
                "mid": mid,
                "volume": vol,
                "open_interest": oi,
                "iv": c.get("iv"),
                "moneyness_pct": c.get("moneyness_pct"),
                "contract": c.get("contract") or "",
                "premium_notional": round(mid * 100 * vol, 2),
            }
        )
        if c.get("expiry") and (bucket.get("dte") is None or int(c.get("dte") or 99) < int(bucket.get("dte") or 99)):
            bucket["expiry"] = c.get("expiry")
            bucket["dte"] = c.get("dte")
    return list(by_sym.values())


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
    fetch_ladders: bool = True,
    include_darkpool: bool = True,
    prefer_symbols: list[str] | None = None,
) -> dict[str, Any]:
    scores = scores or []
    candidates = candidates or []
    quotes = quotes or {}
    aliases = aliases or {}

    prefer = list(prefer_symbols or [])
    if not prefer:
        try:
            from odte_scanner.challenge.earnings import CURATED_EARNINGS
            from odte_scanner.data.universe import earnings_darlings_universe

            prefer = list(earnings_darlings_universe())[:10] + list(CURATED_EARNINGS.keys())[:12]
        except Exception:  # noqa: BLE001
            prefer = []

    symbols = _pick_echo_symbols(
        scores=scores,
        candidates=candidates,
        quotes=quotes,
        max_symbols=max_symbols,
        prefer_symbols=prefer,
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
            use_cache=True,
        )

    if fetch_ladders and symbols:
        # Keep concurrency low — Yahoo rate-limits hard under UI polling
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(symbols)))) as pool:
            futs = {pool.submit(_one, s): s for s in symbols}
            for fut in as_completed(futs):
                try:
                    ladder = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("echo ladder worker failed %s: %s", futs[fut], exc)
                    continue
                if ladder:
                    ladders.append(ladder)

    ladder_source = "yahoo_or_cache"
    if not ladders:
        ladders = _ladders_from_candidates(candidates, quotes)
        ladder_source = "candidates_fallback"

    flow = build_option_flow(ladders)
    dealer = build_dealer_edge(ladders)
    algo = _build_algo_edge(scores)
    pulse = _build_pulse(quotes, scores)
    mirror = _build_mirror(insights, journal_sync)

    # Dark pool: FINRA ATS weekly + Yahoo volume magnets
    dark_pool: dict[str, Any]
    if not include_darkpool:
        dark_pool = {
            "available": False,
            "reason": "Dark pool fetch skipped",
            "source_url": "https://www.finra.org/filing-reporting/otc-transparency",
        }
    else:
        try:
            from odte_scanner.data.fetcher import fetch_history

            dp_syms = list(dict.fromkeys(symbols + ["SPY", "QQQ", "IWM", "NVDA", "TSLA", "AAPL"]))[:12]
            hist_map: dict[str, Any] = {}
            for sym in dp_syms:
                try:
                    df = fetch_history(sym, period="3mo", yahoo_symbol=aliases.get(sym))
                    if df is not None and not df.empty:
                        hist_map[sym] = df
                except Exception:  # noqa: BLE001
                    continue
            dark_pool = build_darkpool_board(dp_syms, histories=hist_map, quotes=quotes, max_symbols=12)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dark pool board failed: %s", exc)
            dark_pool = {
                "available": False,
                "reason": f"FINRA ATS fetch failed: {exc}",
                "source_url": "https://www.finra.org/filing-reporting/otc-transparency",
            }

    cortex = _build_cortex(
        flow=flow, dealer=dealer, algo=algo, insights=insights, actions=actions, dark_pool=dark_pool
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "ladder_count": len(ladders),
        "ladder_source": ladder_source,
        "option_flow": flow,
        "dealer_edge": dealer,
        "dark_pool": dark_pool,
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
