"""ML6 board builder + scan entrypoint."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.config import load_config
from odte_scanner.data.fetcher import fetch_many
from odte_scanner.ml6.scoring import score_ml6_name
from odte_scanner.ml6.watchlist import BOTTOM_LINE_RULES, ML6_WATCHLIST, ml6_tickers

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def build_ml6_board(
    histories: dict[str, Any] | None = None,
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    symbols: list[str] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    min_buy_score: float = 70.0,
    attach_calls: bool = True,
) -> dict[str, Any]:
    """Score ML6 watchlist + BUY NOW / SELL NOW action board."""
    syms = [s.upper() for s in (symbols or ml6_tickers())]
    quotes = quotes or {}
    histories = histories or {}
    rows: list[dict[str, Any]] = []
    for sym in syms:
        if sym not in ML6_WATCHLIST:
            continue
        df = histories.get(sym)
        q = quotes.get(sym)
        rows.append(score_ml6_name(sym, df, quote=q))

    rows.sort(
        key=lambda r: (
            0
            if r.get("status") == "WAIT_FOR_CONFIRMATION"
            else 1
            if r.get("status") == "BUY_ONLY_IF_ACCEPTED"
            else 2,
            -(r.get("ensemble_score") or 0),
        )
    )

    board: dict[str, Any] = {
        "horizon": "ml6",
        "label": "ML6 — earnings-catalyst neocloud / AI infra",
        "purpose": (
            "Beaten-down AI/neocloud / data-center earnings upside (NBIS/CRWV style). "
            "NOT the 0DTE technical ensemble. Do not auto BUY on the report alone — "
            "prefer WAIT until confirmed reaction (open/hold above key level, or AH high/VWAP)."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bottom_line_rules": BOTTOM_LINE_RULES,
        "watchlist": rows,
        "counts": {
            "names": len(rows),
            "watch": sum(1 for r in rows if r.get("status") == "WATCH"),
            "wait": sum(1 for r in rows if r.get("status") == "WAIT_FOR_CONFIRMATION"),
            "buy_if": sum(1 for r in rows if r.get("status") == "BUY_ONLY_IF_ACCEPTED"),
            "liquidity_ok": sum(1 for r in rows if r.get("liquidity_ok")),
            "buy_now": 0,
            "sell_now": 0,
        },
        "disclaimer": (
            "ML6 BUY NOW / SELL NOW automation only fires after reaction confirmation "
            "(paper journal when enabled). Never auto-buys the print blindly."
        ),
    }

    try:
        from odte_scanner.ml6.actions import build_ml6_action_board

        actions = build_ml6_action_board(
            rows,
            quotes=quotes,
            open_trades=list(open_trades or []),
            min_score=float(min_buy_score),
            attach_calls=bool(attach_calls),
        )
        board["actions"] = actions
        board["counts"]["buy_now"] = int((actions.get("counts") or {}).get("buy_now") or 0)
        board["counts"]["sell_now"] = int((actions.get("counts") or {}).get("sell_now") or 0)
        by_act = {str(a.get("symbol")): a for a in (actions.get("all") or [])}
        for r in rows:
            a = by_act.get(str(r.get("symbol")))
            if not a:
                continue
            r["trade_action"] = a.get("action")
            r["trade_detail"] = a.get("detail")
            r["trade_contract"] = a.get("contract")
            r["trade_ask"] = a.get("ask")
            r["buy_now_at"] = a.get("signaled_at")
            r["buy_now_at_cst"] = a.get("signaled_at_cst")
            if a.get("accepted"):
                r["accepted"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML6 action board failed: %s", exc)
        board["actions"] = {
            "buy_now": [],
            "sell_now": [],
            "wait": [],
            "watch": [],
            "hold": [],
            "counts": {},
            "error": str(exc),
        }

    return board


def run_ml6_scan(
    config_path: str | None = None,
    *,
    place_paper: bool = False,  # noqa: ARG001
) -> dict[str, Any]:
    """Fetch history for ML6 sleeve; write board (+ optional paper BUY/SELL sync)."""
    cfg = load_config(config_path)
    aliases = {str(k).upper(): str(v) for k, v in (cfg.get("symbol_aliases") or {}).items()}
    tickers = ml6_tickers()
    ml6_cfg = cfg.get("ml6") or {}
    logger.info("ML6 scan — %d neocloud/earnings names", len(tickers))
    histories = fetch_many(tickers, period="1y", aliases=aliases)

    quotes: dict[str, dict[str, Any]] = {}
    try:
        from odte_scanner.data.live_quotes import fetch_live_quote

        for sym in tickers:
            q = fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))
            if q:
                quotes[sym] = q.to_dict() if hasattr(q, "to_dict") else dict(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML6 live quotes skipped: %s", exc)

    open_trades: list[dict[str, Any]] = []
    journal = None
    jcfg = cfg.get("journal") or {}
    if jcfg.get("enabled", True):
        try:
            from odte_scanner.trading.journal import SignalJournal

            jpath = Path(jcfg.get("path", "outputs/signal_journal.json"))
            if not jpath.is_absolute():
                jpath = ROOT / jpath
            journal = SignalJournal(jpath, starting_cash=float(jcfg.get("starting_cash", 5000)))
            open_trades = [t.to_dict() for t in journal.book.trades if t.status == "open"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("ML6 journal open trades skipped: %s", exc)

    board = build_ml6_board(
        histories,
        quotes=quotes,
        symbols=tickers,
        open_trades=open_trades,
        min_buy_score=float(ml6_cfg.get("min_buy_score", 70)),
        attach_calls=bool(ml6_cfg.get("attach_calls", True)),
    )

    paper_trades: list[dict[str, Any]] = []
    # Automation: paper enter/exit when journal auto flags on (still reaction-gated)
    if journal is not None and bool(ml6_cfg.get("auto_trade", True)):
        try:
            sync = journal.sync_from_actions(
                {"buy_now": [], "sell_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
                max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                auto_enter=bool(jcfg.get("auto_enter", True)),
                auto_exit=bool(jcfg.get("auto_exit", True)),
                ml6=board.get("actions"),
            )
            paper_trades = list(sync.get("entered") or []) + list(sync.get("exited") or [])
            board["journal_sync"] = {
                "entered": len(sync.get("entered") or []),
                "exited": len(sync.get("exited") or []),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("ML6 paper sync failed: %s", exc)

    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"ml6_scan_{stamp}.json"
    latest = out_dir / "latest_ml6.json"
    payload = {
        **board,
        "universe_mode": "ml6",
        "universe_size": len(tickers),
        "paper_trades": paper_trades,
        "session_weekday": datetime.now(timezone.utc).strftime("%A"),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    latest.write_text(json.dumps(payload, indent=2, default=str))
    latest_scan = out_dir / "latest_scan.json"
    if latest_scan.exists():
        try:
            scan = json.loads(latest_scan.read_text())
            scan["ml6"] = board
            horizons = dict(scan.get("horizons") or {})
            horizons["ml6"] = board.get("watchlist") or []
            scan["horizons"] = horizons
            latest_scan.write_text(json.dumps(scan, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not merge ML6 into latest_scan: %s", exc)

    logger.info(
        "Wrote %s (n=%d buy_now=%d sell_now=%d)",
        path,
        len(tickers),
        board["counts"].get("buy_now", 0),
        board["counts"].get("sell_now", 0),
    )
    return payload
