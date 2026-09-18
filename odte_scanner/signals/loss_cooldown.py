"""Shared loss cooldown for BUY NOW across Options / lottery / challenge desks.

Challenge sleeve already blocked re-entry after losers. Options BUY NOW did not —
META weekly ``META260921C00682500`` reappeared after a −$738 close and kept
bleeding the journal. This module builds symbol + contract blocklists from
recommendation log, paper journal, and challenge ledger closes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _is_real_loss(row: dict[str, Any]) -> bool:
    """True when a close booked a real negative P&L (ignore flat/$0 scratches)."""
    status = str(row.get("status") or "closed").lower()
    if status in {"lapsed", "open", "scratch"}:
        return False
    pnl = row.get("pnl_usd")
    pct = row.get("profit_pct")
    if pnl is not None and float(pnl) < -1.0:  # ignore noise / rounding
        return True
    if pct is not None and float(pct) < -1.0:
        return True
    return False


def _exit_ts(row: dict[str, Any]) -> datetime | None:
    for key in ("exited_at", "closed_at", "updated_at", "at", "signaled_at"):
        ts = _parse_ts(row.get(key))
        if ts:
            return ts
    return None


def collect_loss_blocks(
    rows: Iterable[dict[str, Any]] | None,
    *,
    cooldown_days: float = 5.0,
    contract_cooldown_days: float = 45.0,
    now: datetime | None = None,
) -> tuple[set[str], set[str]]:
    """Return ``(symbols, contracts)`` blocked from new BUY NOW / ENTRY.

    - Symbol: blocked for ``cooldown_days`` after any real loss.
    - Contract: blocked for ``contract_cooldown_days`` (longer) so the same
      losing OCC symbol cannot reappear as BUY NOW (META weekly case).
    """
    now = now or datetime.now(timezone.utc)
    symbols: set[str] = set()
    contracts: set[str] = set()
    if not rows:
        return symbols, contracts
    for row in rows:
        if not isinstance(row, dict) or not _is_real_loss(row):
            continue
        exited = _exit_ts(row) or now
        age_days = (now - exited).total_seconds() / 86400.0
        sym = str(row.get("symbol") or "").upper().strip()
        contract = str(row.get("contract") or "").upper().strip()
        if contract.endswith("_SYN"):
            contract = ""
        if sym and age_days <= float(cooldown_days):
            symbols.add(sym)
        if contract and age_days <= float(contract_cooldown_days):
            contracts.add(contract)
    return symbols, contracts


def loss_rows_from_rec_log(rec_log: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten closed recommendation rows (all sections) into loss candidates."""
    if not isinstance(rec_log, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("closed_recs", "all"):
        for r in rec_log.get(key) or []:
            if isinstance(r, dict):
                rows.append(r)
    by = rec_log.get("by_section") or {}
    if isinstance(by, dict):
        for sec in by.values():
            if not isinstance(sec, dict):
                continue
            for key in ("closed_recs", "all"):
                for r in sec.get(key) or []:
                    if isinstance(r, dict):
                        rows.append(r)
    return rows


def loss_rows_from_journal_trades(trades: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "").lower() != "closed":
            continue
        out.append(t)
    return out


def apply_loss_cooldown(
    *,
    action: str,
    symbol: str,
    contract: str | None,
    detail: str,
    headline: str,
    loss_cooldown_symbols: set[str] | None,
    loss_cooldown_contracts: set[str] | None,
) -> tuple[str, str, str]:
    """Demote BUY_NOW / ENTRY → WAIT when symbol or contract is on cooldown."""
    if action not in {"BUY_NOW", "ENTRY", "BUY"}:
        return action, headline, detail
    sym = str(symbol or "").upper()
    occ = str(contract or "").upper()
    blocked_syms = {str(s).upper() for s in (loss_cooldown_symbols or set())}
    blocked_ct = {str(c).upper() for c in (loss_cooldown_contracts or set())}
    if occ and occ in blocked_ct:
        return (
            "WAIT",
            headline.replace("BUY NOW", "WAIT").replace("ENTRY", "WAIT"),
            f"{detail} · blocked: contract {occ} on loss cooldown (do not rebuy losers)",
        )
    if sym and sym in blocked_syms:
        return (
            "WAIT",
            headline.replace("BUY NOW", "WAIT").replace("ENTRY", "WAIT"),
            f"{detail} · blocked: {sym} on loss cooldown (recent losing flip)",
        )
    return action, headline, detail
