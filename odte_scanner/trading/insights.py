from __future__ import annotations

from typing import Any

from odte_scanner.trading.journal import SignalJournal


def build_insights(
    *,
    journal: SignalJournal,
    actions: dict[str, Any] | None,
    win_rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Intellectia-style insights payload: performance + today's signals + journal."""
    perf = journal.performance()
    buys = (actions or {}).get("buy_now") or []
    sells = (actions or {}).get("sell_now") or []
    waits = (actions or {}).get("wait") or []
    primary = (actions or {}).get("primary")

    # AI picks card list
    picks = []
    for s in buys[:8]:
        picks.append(
            {
                "type": "BUY",
                "symbol": s.get("symbol"),
                "bucket": "0DTE" if s.get("dte_bucket") != "weekly" else "1W",
                "strike": s.get("strike"),
                "expiry": s.get("expiry"),
                "ask": s.get("ask"),
                "score": s.get("score"),
                "hist_win_pct": s.get("win_pct"),
                "reason": s.get("detail"),
            }
        )
    for s in sells[:5]:
        picks.append(
            {
                "type": "SELL",
                "symbol": s.get("symbol"),
                "bucket": s.get("dte_bucket"),
                "strike": s.get("strike"),
                "expiry": s.get("expiry"),
                "ask": s.get("ask"),
                "bid": s.get("bid"),
                "score": s.get("score"),
                "hist_win_pct": s.get("win_pct"),
                "reason": s.get("detail"),
            }
        )

    summary_bits = []
    if perf["win_rate_pct"] is not None:
        summary_bits.append(f"Journal win rate {perf['win_rate_pct']:.0f}% on {perf['closed_trades']} closed trades")
    if perf["avg_profit_pct"] is not None:
        summary_bits.append(f"avg option P&L {perf['avg_profit_pct']:+.1f}%")
    if primary:
        summary_bits.append(f"Top signal: {primary.get('headline')}")
    if not summary_bits:
        summary_bits.append("No closed journal trades yet — BUY NOW entries will start the track record.")

    return {
        "headline": "ODTE AI Insights",
        "summary": " · ".join(summary_bits),
        "performance": {
            "win_rate_pct": perf["win_rate_pct"],
            "avg_profit_pct": perf["avg_profit_pct"],
            "avg_win_pct": perf["avg_win_pct"],
            "avg_loss_pct": perf["avg_loss_pct"],
            "realized_pnl_usd": perf["realized_pnl_usd"],
            "unrealized_pnl_usd": perf["unrealized_pnl_usd"],
            "cash": perf["cash"],
            "equity": perf["equity"],
            "starting_cash": perf["starting_cash"],
            "return_pct": round(
                ((perf["equity"] - perf["starting_cash"]) / perf["starting_cash"]) * 100, 2
            )
            if perf["starting_cash"]
            else None,
            "closed_trades": perf["closed_trades"],
            "open_trades": perf["open_trades"],
            "wins": perf["wins"],
            "losses": perf["losses"],
            "best_trade_pct": perf["best_trade_pct"],
            "worst_trade_pct": perf["worst_trade_pct"],
        },
        "equity_curve": perf["equity_curve"],
        "balance_log": perf.get("balance_log") or [],
        "today_picks": picks,
        "wait_count": len(waits),
        "primary": primary,
        "open_positions": perf["open"],
        "closed_trades": perf["closed"][:20],
        "hist_win_note": (win_rates or {}).get("note"),
    }
