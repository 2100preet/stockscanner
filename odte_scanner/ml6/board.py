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
) -> dict[str, Any]:
    """Score ML6 watchlist and return board payload for CLI/UI."""
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
            0 if r.get("status") == "WAIT_FOR_CONFIRMATION" else 1 if r.get("status") == "BUY_ONLY_IF_ACCEPTED" else 2,
            -(r.get("ensemble_score") or 0),
        )
    )

    return {
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
        },
        "disclaimer": (
            "ML6 is a research watch model with hard reaction gates. "
            "Educational only — not auto-execution signals."
        ),
    }


def run_ml6_scan(
    config_path: str | None = None,
    *,
    place_paper: bool = False,  # noqa: ARG001 — ML6 never auto-papers on print alone
) -> dict[str, Any]:
    """Fetch history for ML6 sleeve + liquid context; write board to outputs."""
    cfg = load_config(config_path)
    aliases = {str(k).upper(): str(v) for k, v in (cfg.get("symbol_aliases") or {}).items()}
    tickers = ml6_tickers()
    logger.info("ML6 scan — %d neocloud/earnings names", len(tickers))
    histories = fetch_many(tickers, period="1y", aliases=aliases)

    # Optional live quotes (best-effort)
    quotes: dict[str, dict[str, Any]] = {}
    try:
        from odte_scanner.data.live_quotes import fetch_live_quote

        for sym in tickers:
            q = fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))
            if q:
                quotes[sym] = q.to_dict() if hasattr(q, "to_dict") else dict(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ML6 live quotes skipped: %s", exc)

    board = build_ml6_board(histories, quotes=quotes, symbols=tickers)

    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"ml6_scan_{stamp}.json"
    latest = out_dir / "latest_ml6.json"
    payload = {
        **board,
        "universe_mode": "ml6",
        "universe_size": len(tickers),
        "paper_trades": [],  # never auto-buy on earnings print alone
        "session_weekday": datetime.now(timezone.utc).strftime("%A"),
    }
    path.write_text(json.dumps(payload, indent=2))
    latest.write_text(json.dumps(payload, indent=2))
    # Also merge a compact ml6 block into latest_scan if present (UI convenience)
    latest_scan = out_dir / "latest_scan.json"
    if latest_scan.exists():
        try:
            scan = json.loads(latest_scan.read_text())
            scan["ml6"] = board
            horizons = dict(scan.get("horizons") or {})
            horizons["ml6"] = board.get("watchlist") or []
            scan["horizons"] = horizons
            latest_scan.write_text(json.dumps(scan, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not merge ML6 into latest_scan: %s", exc)

    logger.info("Wrote %s (n=%d)", path, len(tickers))
    return payload
