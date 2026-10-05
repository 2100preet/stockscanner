"""Drop stale BUY-side desk rows for contracts already closed in journal / rec-log.

The aggregated BUY/SELL NOW board merges Options, Lottery, RIP, Challenge, etc.
Recommendation log can show CLOSED while Lottery/RIP still promote the same OCC
because those lanes only checked open *symbols*, not settled contracts, and
journal could briefly carry duplicate open+closed rows with the same trade id.

Re-entry of the same OCC after a real close is allowed when the BUY row carries a
fresh ``signaled_at`` newer than the close (sticky stamps are cleared on exit).
Intentional loss cooldowns live in ``loss_cooldown`` and are unchanged here.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def _closed_at_from_rec(rec_log: dict[str, Any] | None) -> dict[str, str]:
    """Most recent close timestamp per OCC from rec-log closed rows."""
    if not isinstance(rec_log, dict):
        return {}
    out: dict[str, str] = {}

    def _consume(rows: Any) -> None:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if str(r.get("status") or "").lower() not in {"closed", "lapsed"}:
                continue
            occ = _norm_contract(r.get("contract"))
            if not occ:
                continue
            closed = r.get("closed_at") or r.get("exited_at") or r.get("last_recommended_at")
            if not closed:
                continue
            prev = out.get(occ)
            if prev is None or str(closed) > str(prev):
                out[occ] = str(closed)

    _consume(rec_log.get("closed_recs"))
    by_sec = rec_log.get("by_section")
    if isinstance(by_sec, dict):
        for sec in by_sec.values():
            if isinstance(sec, dict):
                _consume(sec.get("closed_recs"))
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


def _closed_at_from_journal(journal: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in _journal_trades(journal):
        if str(getattr(t, "status", "") or "") != "closed":
            continue
        occ = _norm_contract(getattr(t, "contract", None))
        if not occ:
            continue
        closed = getattr(t, "exited_at", None) or getattr(t, "closed_at", None)
        if not closed:
            continue
        prev = out.get(occ)
        if prev is None or str(closed) > str(prev):
            out[occ] = str(closed)
    return out


def closed_at_by_contract(
    *,
    journal: Any = None,
    rec_log: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Merge journal + rec-log close times (latest wins per OCC)."""
    out = dict(_closed_at_from_rec(rec_log))
    for occ, ts in _closed_at_from_journal(journal).items():
        prev = out.get(occ)
        if prev is None or str(ts) > str(prev):
            out[occ] = str(ts)
    return out


def open_contracts(
    *,
    journal: Any = None,
    rec_log: dict[str, Any] | None = None,
) -> set[str]:
    return open_contracts_from_journal(journal) | _open_contracts_from_rec(rec_log)


def buy_row_is_stale_after_close(
    row: dict[str, Any] | None,
    *,
    open_occs: set[str],
    closed_at: dict[str, str],
) -> bool:
    """True when a BUY row is left over from before the OCC closed (not a fresh re-enter)."""
    if not isinstance(row, dict):
        return False
    occ = _norm_contract(row.get("contract"))
    if not occ:
        return False
    if occ in open_occs:
        return False
    closed = closed_at.get(occ)
    if not closed:
        return False
    signaled = row.get("signaled_at") or row.get("recommended_at") or row.get("entered_at")
    sig_dt = _parse_ts(signaled)
    closed_dt = _parse_ts(closed)
    if sig_dt is not None and closed_dt is not None and sig_dt > closed_dt:
        # Fresh BUY pulse after the close → allow re-entry (loss cooldown is separate).
        return False
    return True


def settled_buy_contracts(
    *,
    journal: Any = None,
    rec_log: dict[str, Any] | None = None,
) -> set[str]:
    """OCC roots closed in rec-log or journal with no active open clone.

    Used as a coarse set for callers that do not have per-row signaled_at.
    Prefer ``filter_*`` helpers which allow fresh re-entry stamps after close.
    """
    open_j = open_contracts_from_journal(journal)
    closed_j = set(_closed_at_from_journal(journal)) | {
        _norm_contract(getattr(t, "contract", None))
        for t in _journal_trades(journal)
        if str(getattr(t, "status", "") or "") == "closed"
        and _norm_contract(getattr(t, "contract", None))
    }
    open_r = _open_contracts_from_rec(rec_log)
    closed_r = _closed_contracts_from_rec(rec_log)
    settled = {c for c in closed_r if c not in open_r}
    settled |= {c for c in closed_j if c not in open_j}
    return settled


def _filter_rows(
    rows: list[Any],
    *,
    open_occs: set[str],
    closed_at: dict[str, str],
) -> list[Any]:
    if not closed_at or not rows:
        return rows
    kept: list[Any] = []
    for r in rows:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        if buy_row_is_stale_after_close(r, open_occs=open_occs, closed_at=closed_at):
            continue
        kept.append(r)
    return kept


def filter_board_buys(
    board: dict[str, Any] | None,
    *,
    open_occs: set[str],
    closed_at: dict[str, str],
) -> dict[str, Any] | None:
    if not isinstance(board, dict) or not closed_at:
        return board
    for key in _BUY_LIST_KEYS:
        rows = board.get(key)
        if isinstance(rows, list):
            board[key] = _filter_rows(rows, open_occs=open_occs, closed_at=closed_at)
    counts = board.get("counts")
    if isinstance(counts, dict):
        for key in _BUY_LIST_KEYS:
            if key in counts and isinstance(board.get(key), list):
                counts[key] = len(board[key])
        if "buy_rip" in counts and isinstance(board.get("buy_rip"), list):
            counts["buy_rip"] = len(board["buy_rip"])
    primary = board.get("primary")
    if isinstance(primary, dict) and buy_row_is_stale_after_close(
        primary, open_occs=open_occs, closed_at=closed_at
    ):
        board["primary"] = None
    return board


def filter_actions_buys(
    actions: dict[str, Any] | None,
    *,
    open_occs: set[str],
    closed_at: dict[str, str],
) -> dict[str, Any] | None:
    if not isinstance(actions, dict) or not closed_at:
        return actions
    for key in ("buy_now", "buy_now_0dte", "buy_now_weekly"):
        rows = actions.get(key)
        if isinstance(rows, list):
            actions[key] = _filter_rows(rows, open_occs=open_occs, closed_at=closed_at)
    counts = actions.get("counts")
    if isinstance(counts, dict):
        for key in ("buy_now", "buy_now_0dte", "buy_now_weekly"):
            if key in counts and isinstance(actions.get(key), list):
                counts[key] = len(actions[key])
    primary = actions.get("primary")
    if isinstance(primary, dict) and str(primary.get("action") or "").startswith("BUY"):
        if buy_row_is_stale_after_close(primary, open_occs=open_occs, closed_at=closed_at):
            actions["primary"] = None
    return actions


def apply_settled_contract_filter(
    *,
    settled: set[str] | None = None,
    journal: Any = None,
    rec_log: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    lottery: dict[str, Any] | None = None,
    rip_radar: dict[str, Any] | None = None,
    beauty_monthly: dict[str, Any] | None = None,
    level_watch: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    odte_1k: dict[str, Any] | None = None,
) -> None:
    """Strip stale BUY rows after close; allow fresh re-entry stamps.

    ``settled`` is accepted for backward compatibility but ignored when journal/rec_log
    are provided (per-row signaled_at vs closed_at is used instead). When only a bare
    settled set is passed, rows whose OCC is in the set are dropped (legacy).
    """
    if journal is not None or isinstance(rec_log, dict):
        open_occs = open_contracts(journal=journal, rec_log=rec_log)
        closed_at = closed_at_by_contract(journal=journal, rec_log=rec_log)
        if not closed_at:
            return
        filter_actions_buys(actions, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(lottery, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(rip_radar, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(beauty_monthly, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(level_watch, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(challenge, open_occs=open_occs, closed_at=closed_at)
        filter_board_buys(odte_1k, open_occs=open_occs, closed_at=closed_at)
        return

    # Legacy path: drop any BUY whose OCC is in the settled set.
    if not settled:
        return
    open_occs: set[str] = set()
    closed_at = {c: "1970-01-01T00:00:00+00:00" for c in settled}
    filter_actions_buys(actions, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(lottery, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(rip_radar, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(beauty_monthly, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(level_watch, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(challenge, open_occs=open_occs, closed_at=closed_at)
    filter_board_buys(odte_1k, open_occs=open_occs, closed_at=closed_at)
