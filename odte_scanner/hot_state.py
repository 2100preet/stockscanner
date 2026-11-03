"""In-memory Sniper/Diamond board published by the fast loop.

``/api/snapshot`` can keep serving the slow cached scan. This overlay swaps in
the latest hot boards and the Webull paper ledger so the portal moves within
one client poll, without another focus scan or another quote fan-out.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.trading.portal_book import annotate_board, annotate_sure, public_order, reconcile_pnl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "outputs" / "webull_orders.json"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "updated_at": None,
    "sniper": None,
    "diamond": None,
    "afternoon_puts": None,
    "lunch_credit": None,
}


def publish_hot(
    *,
    sniper: dict[str, Any] | None = None,
    diamond: dict[str, Any] | None = None,
    afternoon: dict[str, Any] | None = None,
    lunch: dict[str, Any] | None = None,
) -> None:
    """Remember the latest fast-loop boards.

    A failed Sniper or Diamond load does not wipe a good one. An Afternoon
    Puts cycle that crashed does: ``{ok: false, error: ...}`` replaces null
    so /api/snapshot can tell a crash from an empty candidate list.
    """
    with _lock:
        if isinstance(sniper, dict) and sniper.get("ok") is not False:
            _state["sniper"] = sniper
        if isinstance(diamond, dict) and diamond.get("ok") is not False:
            _state["diamond"] = diamond
        if isinstance(afternoon, dict) and _publish_afternoon(afternoon):
            _state["afternoon_puts"] = afternoon
        if isinstance(lunch, dict) and _publish_afternoon(lunch):
            _state["lunch_credit"] = lunch
        _state["updated_at"] = datetime.now(timezone.utc).isoformat()


def _publish_afternoon(board: dict[str, Any]) -> bool:
    """Keep a good board. A crash board (ok false plus error) is still published."""
    if board.get("ok") is not False:
        return True
    return bool(board.get("error"))


def clear_hot() -> None:
    with _lock:
        _state["updated_at"] = None
        _state["sniper"] = None
        _state["diamond"] = None
        _state["afternoon_puts"] = None
        _state["lunch_credit"] = None


def hot_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "updated_at": _state.get("updated_at"),
            "sniper": _state.get("sniper"),
            "diamond": _state.get("diamond"),
            "afternoon_puts": _state.get("afternoon_puts"),
            "lunch_credit": _state.get("lunch_credit"),
        }


def read_ledger_orders(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Paper ledger orders. Missing file is an empty book, not an error."""
    dest = Path(path) if path else DEFAULT_LEDGER
    if not dest.exists():
        return []
    try:
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("orders") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def apply_hot_overlay(
    snapshot: dict[str, Any] | None,
    *,
    orders: list[dict[str, Any]] | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy ``snapshot`` and stamp Webull truth onto Sniper, Diamond, and Gold."""
    body = dict(snapshot or {})
    hot = hot_snapshot()
    if isinstance(hot.get("sniper"), dict):
        body["sniper"] = hot["sniper"]
    if isinstance(hot.get("diamond"), dict):
        body["diamond"] = hot["diamond"]
    if isinstance(hot.get("afternoon_puts"), dict):
        body["afternoon_puts"] = hot["afternoon_puts"]
    if isinstance(hot.get("lunch_credit"), dict):
        body["lunch_credit"] = hot["lunch_credit"]
    book = list(orders) if orders is not None else read_ledger_orders(ledger_path)
    body["sniper"] = annotate_board(body.get("sniper") if isinstance(body.get("sniper"), dict) else None, book, "sniper")
    body["diamond"] = annotate_board(body.get("diamond") if isinstance(body.get("diamond"), dict) else None, book, "diamond")
    body["afternoon_puts"] = annotate_board(
        body.get("afternoon_puts") if isinstance(body.get("afternoon_puts"), dict) else None,
        book,
        "afternoon",
    )
    body["lunch_credit"] = annotate_board(
        body.get("lunch_credit") if isinstance(body.get("lunch_credit"), dict) else None,
        book,
        "lunch",
    )
    if isinstance(body.get("sure_winners"), dict):
        body["sure_winners"] = annotate_sure(body.get("sure_winners"), book)
    report = reconcile_pnl(book)
    body["pnl_reconcile"] = report
    body["webull_book"] = {
        "orders": [public_order(row) for row in book if public_order(row).get("tier")],
        "trips": report["trips"],
    }
    body["hot_at"] = hot.get("updated_at")
    try:
        from odte_scanner.scorecard.daily_pnl import build_scorecard_daily_pnl

        modeled: list[dict[str, Any]] = []
        for key in ("diamond", "sniper", "afternoon_puts", "lunch_credit"):
            board = body.get(key)
            if isinstance(board, dict):
                modeled.extend(row for row in (board.get("spx_exits") or []) if isinstance(row, dict))
        body["scorecard_daily_pnl"] = build_scorecard_daily_pnl(
            book,
            spx_exits=modeled,
            load_side=True,
            ledger_path=Path(ledger_path) if ledger_path else None,
        )
    except Exception as exc:  # noqa: BLE001
        body["scorecard_daily_pnl"] = {"ok": False, "error": str(exc)[:200]}
    return body
