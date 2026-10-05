"""Drop BUY-side desk rows for contracts already closed in journal / rec-log.

The aggregated BUY/SELL NOW board merges Options, Lottery, RIP, Challenge, etc.
Recommendation log can show CLOSED while Lottery/RIP still promote the same OCC
because those lanes only checked open *symbols*, not settled contracts, and
journal could briefly carry duplicate open+closed rows with the same trade id.
"""
from __future__ import annotations

from typing import Any

_BUY_LIST_KEYS = (
    "buy_now",
    "buy_rip",
    "buy_beauty",
    "buy_level",
    "entry",
    "put_now",
    "in",
)


def _norm_contract(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _open_contracts_from_rec(rec_log: dict[str, Any] | None) -> set[str]:
    if not isinstance(rec_log, dict):
        return set()
    out: set[str] = set()
    for r in rec_log.get("open_recs") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("status") or "").lower() != "open":
            continue
        occ = _norm_contract(r.get("contract"))
        if occ:
            out.add(occ)
    by_sec = rec_log.get("by_section")
    if isinstance(by_sec, dict):
        for sec in by_sec.values():
            if not isinstance(sec, dict):
                continue
            for r in sec.get("open_recs") or []:
                if not isinstance(r, dict):
                    continue
                if str(r.get("status") or "").lower() != "open":
                    continue
                occ = _norm_contract(r.get("contract"))
                if occ:
                    out.add(occ)
    return out


def _closed_contracts_from_rec(rec_log: dict[str, Any] | None) -> set[str]:
    if not isinstance(rec_log, dict):
        return set()
    out: set[str] = set()
    for r in rec_log.get("closed_recs") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("status") or "").lower() not in {"closed", "lapsed"}:
            continue
        occ = _norm_contract(r.get("contract"))
        if occ:
            out.add(occ)
    by_sec = rec_log.get("by_section")
    if isinstance(by_sec, dict):
        for sec in by_sec.values():
            if not isinstance(sec, dict):
                continue
            for r in sec.get("closed_recs") or []:
                if not isinstance(r, dict):
                    continue
                if str(r.get("status") or "").lower() not in {"closed", "lapsed"}:
                    continue
                occ = _norm_contract(r.get("contract"))
                if occ:
                    out.add(occ)
    return out


def _journal_trades(journal: Any) -> list[Any]:
    if journal is None:
        return []
    book = getattr(journal, "book", None)
    trades = list(getattr(book, "trades", None) or [])
    if not trades:
        return []
    try:
        from odte_scanner.trading.journal import _dedupe_journal_trades

        return _dedupe_journal_trades(trades)
    except Exception:  # noqa: BLE001
        return trades


def open_contracts_from_journal(journal: Any) -> set[str]:
    out: set[str] = set()
    for t in _journal_trades(journal):
        if str(getattr(t, "status", "") or "") != "open":
            continue
        occ = _norm_contract(getattr(t, "contract", None))
        if occ:
            out.add(occ)
    return out


def settled_buy_contracts(
    *,
    journal: Any = None,
    rec_log: dict[str, Any] | None = None,
) -> set[str]:
    """OCC roots closed in rec-log or journal that should not appear as BUY NOW.

    Rec-log CLOSED (Options tab) wins even if a stale journal open row remains.
    Journal CLOSED (after id-dedupe) also settles when that OCC is not open.
    """
    open_j = open_contracts_from_journal(journal)
    closed_j: set[str] = set()
    for t in _journal_trades(journal):
        if str(getattr(t, "status", "") or "") != "closed":
            continue
        occ = _norm_contract(getattr(t, "contract", None))
        if occ:
            closed_j.add(occ)
    open_r = _open_contracts_from_rec(rec_log)
    closed_r = _closed_contracts_from_rec(rec_log)
    settled = {c for c in closed_r if c not in open_r}
    settled |= {c for c in closed_j if c not in open_j}
    return settled


def _filter_rows(rows: list[Any], settled: set[str]) -> list[Any]:
    if not settled or not rows:
        return rows
    kept: list[Any] = []
    for r in rows:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        occ = _norm_contract(r.get("contract"))
        if occ and occ in settled:
            continue
        kept.append(r)
    return kept


def filter_board_buys(board: dict[str, Any] | None, settled: set[str]) -> dict[str, Any] | None:
    if not isinstance(board, dict) or not settled:
        return board
    for key in _BUY_LIST_KEYS:
        rows = board.get(key)
        if isinstance(rows, list):
            board[key] = _filter_rows(rows, settled)
    counts = board.get("counts")
    if isinstance(counts, dict):
        for key in _BUY_LIST_KEYS:
            if key in counts and isinstance(board.get(key), list):
                counts[key] = len(board[key])
        if "buy_rip" in counts and isinstance(board.get("buy_rip"), list):
            counts["buy_rip"] = len(board["buy_rip"])
    primary = board.get("primary")
    if isinstance(primary, dict):
        occ = _norm_contract(primary.get("contract"))
        if occ and occ in settled:
            board["primary"] = None
    return board


def filter_actions_buys(actions: dict[str, Any] | None, settled: set[str]) -> dict[str, Any] | None:
    if not isinstance(actions, dict) or not settled:
        return actions
    for key in ("buy_now", "buy_now_0dte", "buy_now_weekly"):
        rows = actions.get(key)
        if isinstance(rows, list):
            actions[key] = _filter_rows(rows, settled)
    counts = actions.get("counts")
    if isinstance(counts, dict):
        for key in ("buy_now", "buy_now_0dte", "buy_now_weekly"):
            if key in counts and isinstance(actions.get(key), list):
                counts[key] = len(actions[key])
    primary = actions.get("primary")
    if isinstance(primary, dict) and str(primary.get("action") or "").startswith("BUY"):
        occ = _norm_contract(primary.get("contract"))
        if occ and occ in settled:
            actions["primary"] = None
    return actions


def apply_settled_contract_filter(
    *,
    settled: set[str],
    actions: dict[str, Any] | None = None,
    lottery: dict[str, Any] | None = None,
    rip_radar: dict[str, Any] | None = None,
    beauty_monthly: dict[str, Any] | None = None,
    level_watch: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    odte_1k: dict[str, Any] | None = None,
) -> None:
    if not settled:
        return
    filter_actions_buys(actions, settled)
    filter_board_buys(lottery, settled)
    filter_board_buys(rip_radar, settled)
    filter_board_buys(beauty_monthly, settled)
    filter_board_buys(level_watch, settled)
    filter_board_buys(challenge, settled)
    filter_board_buys(odte_1k, settled)
