"""Persistent recommendation logger across lottery / challenge / action desks.

Recommendations are ephemeral on each snapshot rebuild. This log keeps an
append-friendly history of when the desk recommended ENTRY/BUY and EXIT/SELL,
plus estimated P&L when both prices are known — so yesterday's challenge pick
still shows today even if it dropped off the live board.

P&L rule (1 contract × 100):
  profit = (SELL NOW bid − BUY NOW / ENTRY ask) × 100
Never invent exit = entry for a clock flatten — that produced fake $0 "losses".
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.trading.journal import _parse_live_exit_from_reason

logger = logging.getLogger(__name__)

_LIVE_ENTRY_RE = re.compile(
    r"vs entry\s*\$?\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)


def _parse_live_entry_from_reason(reason: str | None) -> float | None:
    if not reason:
        return None
    m = _LIVE_ENTRY_RE.search(reason)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "outputs" / "recommendation_log.json"

# Actions that open a recommendation
_OPEN_ACTIONS = {
    "BUY_NOW",
    "ENTRY",
    "BUY",
    "LONG",
    "PUT_NOW",
    "CALL_NOW",
    "BUY_PUT",
    "BUY_CALL",
}
# Actions that close / take profit on a recommendation
_CLOSE_ACTIONS = {
    "SELL_NOW",
    "EXIT",
    "SELL",
    "CLOSE",
    "SELL_PUT",
    "SELL_CALL",
}

# Soft opens (radar / quality / WAIT) — track on board, never auto P&L-close at entry
_SOFT_OPEN_ACTIONS = {
    "RADAR_HOT",
    "RADAR_WATCH",
    "RADAR",
    "WATCH",
    "QUALITY",
    "QUALITY_CARD",
    "WAIT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _section_from_bucket(bucket: Any) -> str:
    b = str(bucket or "odte").lower()
    if "swing" in b:
        return "swing"
    if "week" in b:
        return "weekly"
    if "lottery" in b or "explosive" in b:
        return "lottery"
    if "challenge" in b:
        return "challenge"
    return "odte"


def _right_of(row: dict[str, Any], default: str = "C") -> str:
    right = str(row.get("right") or "").upper()
    if right in {"C", "P"}:
        return right
    # Infer from OCC-style contract …C00123000 / …P00123000
    contract = str(row.get("contract") or "")
    if len(contract) >= 15:
        # Yahoo OCC: ROOT + YYMMDD + C/P + strike
        for i, ch in enumerate(contract):
            if ch in {"C", "P"} and i >= 6 and contract[i - 6 : i].isdigit():
                return ch
        # Fallback: last C/P before trailing digits
        for ch in reversed(contract):
            if ch in {"C", "P"}:
                return ch
    headline = str(row.get("headline") or row.get("action") or "").upper()
    if " PUT" in f" {headline}" or headline.endswith("PUT"):
        return "P"
    return default


def _metrics(recs: list[Recommendation]) -> dict[str, Any]:
    open_n = sum(1 for r in recs if r.status == "open")
    closed = [r for r in recs if r.status == "closed"]
    lapsed = [r for r in recs if r.status == "lapsed"]
    wins = sum(1 for r in closed if r.profit_pct is not None and r.profit_pct > 0)
    losses = sum(1 for r in closed if r.profit_pct is not None and r.profit_pct < 0)
    scratches = sum(
        1
        for r in closed
        if r.profit_pct is not None and abs(float(r.profit_pct)) < 1e-9
    )
    # Closed with no priced P&L (missing entry or exit) — not a win/loss
    unpriced = sum(1 for r in closed if r.profit_pct is None)
    pnl = sum(r.pnl_usd or 0 for r in closed if r.pnl_usd is not None)
    journal_pnl = sum(
        r.pnl_usd or 0 for r in closed if r.pnl_usd is not None and r.source == "journal"
    )
    board_pnl = sum(
        r.pnl_usd or 0 for r in closed if r.pnl_usd is not None and r.source != "journal"
    )
    return {
        "open": open_n,
        "closed": len(closed),
        "lapsed": len(lapsed),
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "unpriced": unpriced,
        "closed_pnl_usd": round(pnl, 2),
        "journal_pnl_usd": round(journal_pnl, 2),
        "board_signal_pnl_usd": round(board_pnl, 2),
        "paper_pnl_usd": round(journal_pnl, 2),
    }


def _resolve_exit_price(
    price: float | None,
    *,
    entry: float | None,
    reason: str | None,
    source: str,
) -> float | None:
    """Price board/journal exits — parse live ask from reason; lapse stale clock exits."""
    parsed = _parse_live_exit_from_reason(reason)
    px = price
    if px is None or float(px or 0) <= 0:
        px = parsed
    elif (
        parsed is not None
        and entry is not None
        and float(entry) > 0
        and abs(float(px) - float(entry)) < 1e-9
        and abs(float(parsed) - float(entry)) > 1e-9
    ):
        px = parsed
    if px is None or float(px) <= 0:
        return None
    if entry is not None and float(entry) > 0:
        loss_pct = (float(px) - float(entry)) / float(entry) * 100.0
        reason_l = (reason or "").lower()
        has_live = "live ask" in reason_l or ("unreal " in reason_l and "@" in reason_l)
        clockish = any(x in reason_l for x in ("time-stop", "15:45", "flatten lottery", "max hold"))
        # Board-only clock flatten with a stale/wrong bid (common $1.50 echo) — do not count as loss
        if (
            source == "board"
            and loss_pct <= -30.0
            and clockish
            and not has_live
        ):
            return None
    return float(px)


@dataclass
class RecEvent:
    """Single recommend pulse (board said ENTRY/EXIT at this moment)."""

    at: str
    action: str
    price: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Recommendation:
    id: str
    section: str  # lottery | challenge | odte | weekly | swing | actions | radar
    symbol: str
    right: str = "C"  # C | P
    open_action: str = "ENTRY"  # BUY_NOW | ENTRY
    recommended_at: str = ""
    last_recommended_at: str = ""
    entry_price: float | None = None
    entry_spot: float | None = None
    contract: str | None = None
    expiry: str | None = None
    strike: float | None = None
    dte: int | None = None
    horizon: str | None = None
    reason: str = ""
    headline: str = ""
    status: str = "open"  # open | closed | lapsed
    on_board: bool = True
    close_action: str | None = None
    closed_at: str | None = None
    exit_price: float | None = None
    exit_spot: float | None = None
    exit_reason: str | None = None
    profit_pct: float | None = None
    pnl_usd: float | None = None  # 1 contract × 100 multiplier
    hold_hours: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    source: str = "board"  # board | journal | quality | radar

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Recommendation:
        allowed = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in allowed}
        data.setdefault("events", [])
        return cls(**data)


@dataclass
class RecBook:
    updated_at: str = ""
    recommendations: list[Recommendation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        m = _metrics(self.recommendations)
        return {
            "updated_at": self.updated_at,
            **m,
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


class RecommendationLog:
    """Disk-backed recommendation history shared by all Signal Desk sections."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.book = RecBook()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            recs = [Recommendation.from_dict(r) for r in (raw.get("recommendations") or [])]
            self.book = RecBook(updated_at=str(raw.get("updated_at") or ""), recommendations=recs)
            scrubbed = self._scrub_bogus_zero_closes()
            scrubbed = self._scrub_stale_board_exits() or scrubbed
            scrubbed = self._scrub_entry_drift_from_reason() or scrubbed
            scrubbed = self._scrub_fake_quality_opens() or scrubbed
            if scrubbed:
                self.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommendation log load failed: %s", exc)
            self.book = RecBook()

    def _scrub_fake_quality_opens(self) -> bool:
        """Lapse leftover QUALITY card opens that used stock last as option entry."""
        changed = False
        for r in self.book.recommendations:
            if r.status != "open":
                continue
            headline = (r.headline or "").upper()
            is_quality = headline.startswith("QUALITY") or r.source == "quality"
            no_contract = not r.contract
            stockish = r.entry_price is not None and float(r.entry_price) >= 50.0 and no_contract
            if is_quality or (stockish and r.open_action == "BUY_NOW" and r.strike is None):
                r.status = "lapsed"
                r.close_action = "LAPSE"
                r.on_board = False
                r.closed_at = _now()
                r.exit_price = None
                r.profit_pct = None
                r.pnl_usd = None
                r.exit_reason = "reclassed — quality/stock last was not an option BUY NOW"
                changed = True
        return changed

    def _scrub_bogus_zero_closes(self) -> bool:
        """Convert fake $0 clock-flatten closes (exit==entry) into lapses — not losses."""
        changed = False
        for r in self.book.recommendations:
            if r.status != "closed":
                continue
            reason = (r.exit_reason or "").lower()
            clockish = any(
                x in reason
                for x in ("time-stop", "flatten", "max hold", "clock", "15:45")
            )
            same_px = (
                r.entry_price is not None
                and r.exit_price is not None
                and abs(float(r.entry_price) - float(r.exit_price)) < 1e-9
            )
            zero_pnl = r.profit_pct is not None and abs(float(r.profit_pct)) < 1e-9
            if clockish and (same_px or zero_pnl):
                r.status = "lapsed"
                r.close_action = "LAPSE"
                r.profit_pct = None
                r.pnl_usd = None
                r.exit_price = None
                r.exit_reason = (r.exit_reason or "") + " · reclassed lapse (no priced EXIT)"
                changed = True
            # SELL-only stubs with no entry — unpriced, keep closed but null P&L stays
            if r.entry_price is None and r.profit_pct is not None and abs(float(r.profit_pct)) < 1e-9:
                r.profit_pct = None
                r.pnl_usd = None
                changed = True
        return changed

    def _scrub_stale_board_exits(self) -> bool:
        """Reclass board clock-exits with stale bids (e.g. weekly ALAB @ $1.50) as lapses."""
        changed = False
        for r in self.book.recommendations:
            if r.status != "closed" or r.source == "journal":
                continue
            if r.entry_price is None or r.exit_price is None:
                continue
            entry = float(r.entry_price)
            exit_px = float(r.exit_price)
            if entry <= 0:
                continue
            loss_pct = (exit_px - entry) / entry * 100.0
            reason = (r.exit_reason or "").lower()
            has_live = "live ask" in reason or ("unreal " in reason and "@" in reason)
            clockish = any(x in reason for x in ("time-stop", "15:45", "flatten lottery", "max hold"))
            if loss_pct <= -30.0 and clockish and not has_live:
                r.status = "lapsed"
                r.close_action = "LAPSE"
                r.profit_pct = None
                r.pnl_usd = None
                r.exit_price = None
                r.exit_reason = (r.exit_reason or "") + " · reclassed lapse (stale board exit bid)"
                changed = True
        return changed

    def _scrub_entry_drift_from_reason(self) -> bool:
        """Fix closed rows where entry_price drifted but exit reason cites the real entry."""
        changed = False
        for r in self.book.recommendations:
            if r.status != "closed" or r.exit_price is None:
                continue
            reason_entry = _parse_live_entry_from_reason(r.exit_reason)
            if reason_entry is None or reason_entry <= 0:
                continue
            if r.entry_price is None or abs(float(r.entry_price) - float(reason_entry)) / float(reason_entry) <= 0.25:
                continue
            r.entry_price = reason_entry
            self._apply_pnl(r, float(r.exit_price))
            changed = True
        return changed

    def save(self) -> None:
        self.book.updated_at = _now()
        self.path.write_text(json.dumps(self.book.to_dict(), indent=2))

    def _key(self, section: str, symbol: str, right: str = "C") -> str:
        return f"{section.upper()}|{str(symbol).upper()}|{str(right or 'C').upper()}"

    def _open_for(self, section: str, symbol: str, right: str = "C") -> Recommendation | None:
        key = self._key(section, symbol, right)
        for r in self.book.recommendations:
            if r.status != "open":
                continue
            if self._key(r.section, r.symbol, r.right) == key:
                return r
        return None

    def _open_any_section(self, symbol: str, right: str = "C") -> Recommendation | None:
        """Find any open rec for symbol/right (journal exits may not know section)."""
        sym = str(symbol or "").upper()
        rt = str(right or "C").upper()
        for r in self.book.recommendations:
            if r.status == "open" and r.symbol == sym and r.right == rt:
                return r
        return None

    def _append_event(
        self,
        rec: Recommendation,
        *,
        action: str,
        price: float | None,
        detail: str | None,
    ) -> None:
        rec.events.append(
            RecEvent(at=_now(), action=action, price=price, detail=(detail or "")[:240]).to_dict()
        )
        # Cap event spam
        if len(rec.events) > 40:
            rec.events = rec.events[-40:]

    def _apply_pnl(self, rec: Recommendation, exit_price: float | None) -> None:
        """P&L from recommended entry ask → recommended exit bid (1 contract)."""
        entry = rec.entry_price
        reason_entry = _parse_live_entry_from_reason(rec.exit_reason)
        if (
            reason_entry is not None
            and reason_entry > 0
            and entry is not None
            and float(entry) > 0
            and abs(float(entry) - float(reason_entry)) / float(reason_entry) > 0.25
        ):
            entry = reason_entry
            rec.entry_price = reason_entry
        if entry and exit_price is not None and float(entry) > 0:
            rec.profit_pct = round(((exit_price - float(entry)) / float(entry)) * 100.0, 2)
            rec.pnl_usd = round((exit_price - float(entry)) * 100.0, 2)
        else:
            rec.profit_pct = None
            rec.pnl_usd = None

    def note_entry(
        self,
        *,
        section: str,
        symbol: str,
        action: str = "ENTRY",
        right: str = "C",
        price: float | None = None,
        spot: float | None = None,
        contract: str | None = None,
        expiry: str | None = None,
        strike: float | None = None,
        dte: int | None = None,
        horizon: str | None = None,
        reason: str = "",
        headline: str = "",
        at: str | None = None,
        source: str = "board",
    ) -> Recommendation | None:
        sym = str(symbol or "").upper()
        if not sym:
            return None
        act = str(action or "ENTRY").upper()
        if act not in _OPEN_ACTIONS and act not in _SOFT_OPEN_ACTIONS:
            act = "ENTRY"
        existing = self._open_for(section, sym, right)
        now = at or _now()
        if existing:
            existing.last_recommended_at = now
            existing.on_board = True
            was_soft = existing.open_action in _SOFT_OPEN_ACTIONS
            # Upgrade WAIT → BUY_NOW / ENTRY: lock entry ask at the BUY pulse for P&L
            if act in _OPEN_ACTIONS:
                existing.open_action = act
                existing.source = source or existing.source
                # Lock entry once set — do not drift higher on every board refresh
                if price and price > 0 and (
                    existing.entry_price is None
                    or float(existing.entry_price or 0) <= 0
                    or was_soft
                ):
                    existing.entry_price = price
            elif price and (existing.entry_price is None or existing.entry_price <= 0):
                existing.entry_price = price
            if contract and not existing.contract:
                existing.contract = contract
            if expiry and not existing.expiry:
                existing.expiry = expiry
            if strike is not None and existing.strike is None:
                existing.strike = strike
            if reason:
                existing.reason = reason[:320]
            if headline:
                existing.headline = headline[:200]
            # Only log a pulse if last event was a while ago / different action
            last = (existing.events or [None])[-1]
            last_action = (last or {}).get("action") if isinstance(last, dict) else None
            if last_action != act:
                self._append_event(existing, action=act, price=price, detail=reason or headline)
            return existing

        open_act = act
        if act in _OPEN_ACTIONS:
            open_act = act
        elif act in _SOFT_OPEN_ACTIONS:
            open_act = act
        else:
            open_act = "ENTRY"

        rec = Recommendation(
            id=f"rec-{uuid.uuid4().hex[:10]}",
            section=str(section).lower(),
            symbol=sym,
            right=str(right or "C").upper(),
            open_action=open_act,
            recommended_at=now,
            last_recommended_at=now,
            entry_price=price,
            entry_spot=spot,
            contract=contract,
            expiry=expiry,
            strike=strike,
            dte=int(dte) if dte is not None else None,
            horizon=horizon,
            reason=(reason or "")[:320],
            headline=(headline or "")[:200],
            status="open",
            on_board=True,
            events=[],
            source=source,
        )
        self._append_event(rec, action=act, price=price, detail=reason or headline)
        self.book.recommendations.insert(0, rec)
        return rec

    def note_lapse(
        self,
        *,
        section: str,
        symbol: str,
        right: str = "C",
        reason: str = "",
    ) -> Recommendation | None:
        """Drop an open rec without inventing exit=entry P&L."""
        rec = self._open_for(section, symbol, right)
        if not rec or rec.status != "open":
            return rec
        now = _now()
        rec.status = "lapsed"
        rec.on_board = False
        rec.close_action = "LAPSE"
        rec.closed_at = now
        rec.exit_price = None
        rec.exit_reason = (reason or "lapsed off board — no priced SELL NOW")[:320]
        rec.profit_pct = None
        rec.pnl_usd = None
        rec.last_recommended_at = now
        try:
            t0 = datetime.fromisoformat(rec.recommended_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(now.replace("Z", "+00:00"))
            rec.hold_hours = round((t1 - t0).total_seconds() / 3600.0, 2)
        except Exception:  # noqa: BLE001
            rec.hold_hours = None
        self._append_event(rec, action="LAPSE", price=None, detail=reason)
        return rec

    def note_exit(
        self,
        *,
        section: str,
        symbol: str,
        action: str = "EXIT",
        right: str = "C",
        price: float | None = None,
        spot: float | None = None,
        reason: str = "",
        allow_stub: bool = False,
        at: str | None = None,
        entry_price: float | None = None,
    ) -> Recommendation | None:
        sym = str(symbol or "").upper()
        if not sym:
            return None
        rec = self._open_for(section, sym, right) or self._open_any_section(sym, right)
        if not rec:
            # Prefer not to invent SELL-only stubs (null entry → null/$0 P&L noise).
            # Only create a complete closed row when both prices are known.
            if entry_price is None or price is None or not allow_stub:
                return None
            now = at or _now()
            rec = Recommendation(
                id=f"rec-{uuid.uuid4().hex[:10]}",
                section=str(section).lower(),
                symbol=sym,
                right=str(right or "C").upper(),
                open_action="BUY_NOW",
                recommended_at=now,
                last_recommended_at=now,
                entry_price=entry_price,
                status="closed",
                on_board=True,
                close_action=str(action or "EXIT").upper(),
                closed_at=now,
                exit_price=price,
                exit_spot=spot,
                exit_reason=(reason or "")[:320],
                events=[],
                source="journal",
            )
            self._apply_pnl(rec, price)
            self._append_event(rec, action=str(action or "EXIT").upper(), price=price, detail=reason)
            self.book.recommendations.insert(0, rec)
            return rec

        if rec.status == "closed":
            return rec

        entry = float(rec.entry_price) if rec.entry_price is not None else None
        if entry_price is not None and entry is None:
            entry = float(entry_price)
        src = str(rec.source or "board")
        px = _resolve_exit_price(price, entry=entry, reason=reason, source=src)

        # No usable exit mark → lapse (do NOT close at entry)
        if px is None or float(px) <= 0:
            return self.note_lapse(
                section=rec.section,
                symbol=sym,
                right=rec.right,
                reason=(reason or "EXIT with no live bid/mark"),
            )

        act = str(action or "EXIT").upper()
        if act not in _CLOSE_ACTIONS:
            act = "EXIT"
        now = at or _now()
        rec.status = "closed"
        rec.on_board = True
        rec.close_action = act
        rec.closed_at = now
        rec.exit_price = px
        rec.exit_spot = spot
        rec.exit_reason = (reason or "")[:320]
        rec.last_recommended_at = now
        if rec.entry_price is None and entry_price is not None:
            rec.entry_price = entry_price
        self._apply_pnl(rec, px)
        # Clock-style exit that somehow still has exit==entry → scratch, not a "priced" trade win/loss
        # (kept as closed with 0% so history shows the EXIT pulse; metrics treat 0 as scratch)
        try:
            t0 = datetime.fromisoformat(rec.recommended_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(now.replace("Z", "+00:00"))
            rec.hold_hours = round((t1 - t0).total_seconds() / 3600.0, 2)
        except Exception:  # noqa: BLE001
            rec.hold_hours = None
        self._append_event(rec, action=act, price=price, detail=reason)
        return rec

    def mark_off_board(self, section: str, live_keys: set[str], *, soft: bool = False) -> None:
        """Flag open recs not on today's board; lapse (no P&L) when clock says flatten."""
        from odte_scanner.signals.hold_rules import time_stop_reason

        sec = str(section).lower()
        for r in self.book.recommendations:
            if r.section != sec or r.status != "open":
                continue
            key = self._key(r.section, r.symbol, r.right)
            r.on_board = key in live_keys
            if r.on_board:
                continue
            # WAIT left the live board → keep history as lapse (recommended ask preserved, no P&L)
            if r.open_action == "WAIT":
                self.note_lapse(
                    section=r.section,
                    symbol=r.symbol,
                    right=r.right,
                    reason="WAIT left live board — never upgraded to BUY NOW / ENTRY",
                )
                continue
            # Soft sources (radar / quality): just leave off-board — never invent a $0 loss
            if soft or r.source in {"radar", "quality"} or r.open_action in _SOFT_OPEN_ACTIONS:
                continue
            trade_like = {
                "dte_bucket": r.horizon
                or ("0dte" if r.section in {"odte", "odte_1k", "lottery", "radar"} else "weekly"),
                "dte": r.dte,
                "entered_at": r.recommended_at,
            }
            reason = time_stop_reason(trade_like)
            if reason:
                # Lapse without exit=entry — real P&L needs a priced SELL NOW / journal exit
                self.note_lapse(
                    section=r.section,
                    symbol=r.symbol,
                    right=r.right,
                    reason=f"{reason} · no priced SELL NOW (not counted as win/loss)",
                )

    def sync_challenge(self, challenge: dict[str, Any] | None) -> int:
        if not isinstance(challenge, dict):
            return 0
        n = 0
        live: set[str] = set()
        tickets = list(challenge.get("tickets") or [])
        # Prefer explicit buckets when present
        for bucket in ("entry", "hold", "exit", "wait"):
            for row in challenge.get(bucket) or []:
                if isinstance(row, dict) and row not in tickets:
                    tickets.append(row)
        # Primary focus ticket (often WAIT due to liquidity) still counts as a recommendation
        primary = challenge.get("primary")
        if isinstance(primary, dict) and primary.get("symbol"):
            if primary not in tickets:
                tickets.insert(0, primary)

        for row in tickets:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            action = str(row.get("action") or "WAIT").upper()
            if not sym:
                continue
            live.add(self._key("challenge", sym, right))
            price = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            bid = _f(row.get("bid") or row.get("exit_bid") or row.get("mark") or row.get("ask"))
            reason = " · ".join(
                str(x) for x in (row.get("reasons") or [])[:3] if x
            ) or str(row.get("enter_plan") or row.get("exit_plan") or row.get("headline") or "")
            if action == "EXIT":
                self.note_exit(
                    section="challenge",
                    symbol=sym,
                    action="EXIT",
                    right=right,
                    price=bid,
                    spot=_f(row.get("spot")),
                    reason=str(row.get("exit_plan") or reason),
                )
                n += 1
                continue
            if action in ("WAIT", "HOLD"):
                # Log WAIT as soft recommendation so challenge history isn't empty
                self.note_entry(
                    section="challenge",
                    symbol=sym,
                    action="WAIT",
                    right=right,
                    price=price,
                    spot=_f(row.get("spot")),
                    contract=str(row.get("contract") or "") or None,
                    expiry=str(row.get("expiry") or "") or None,
                    strike=_f(row.get("strike")),
                    dte=int(row["dte"]) if row.get("dte") is not None else None,
                    horizon=str(row.get("horizon") or "") or None,
                    reason=reason or f"challenge {action}",
                    headline=str(row.get("headline") or f"{action} {sym} {right}"),
                    source="board",
                )
                n += 1
                continue
            # ENTRY only opens a lasting recommendation
            self.note_entry(
                section="challenge",
                symbol=sym,
                action="ENTRY",
                right=right,
                price=price,
                spot=_f(row.get("spot")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon=str(row.get("horizon") or "") or None,
                reason=reason or f"challenge {action}",
                headline=f"{action} {sym} {right}",
            )
            n += 1
        self.mark_off_board("challenge", live)
        return n

    def sync_lottery(self, lottery: dict[str, Any] | None) -> int:
        if not isinstance(lottery, dict):
            return 0
        n = 0
        live: set[str] = set()
        for row in lottery.get("buy_now") or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            self.note_entry(
                section="lottery",
                symbol=sym,
                action="BUY_NOW",
                right=right,
                price=_f(row.get("ask") or row.get("entry_ask") or row.get("mark")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon="0dte",
                reason=str(row.get("detail") or row.get("thesis") or row.get("headline") or ""),
                headline=str(row.get("headline") or row.get("action") or "BUY NOW"),
            )
            live.add(self._key("lottery", sym, right))
            n += 1
        for row in lottery.get("sell_now") or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            self.note_exit(
                section="lottery",
                symbol=sym,
                action="SELL_NOW",
                right=right,
                price=_f(row.get("bid") or row.get("mark") or row.get("ask")),
                spot=_f(row.get("spot") or row.get("live_last")),
                reason=str(row.get("detail") or row.get("headline") or ""),
            )
            live.add(self._key("lottery", sym, right))
            n += 1
        # WAIT tickets: log recommended ask so Explosive history isn't empty while gated
        for row in (lottery.get("wait") or [])[:12]:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            right = _right_of(row)
            ask = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            self.note_entry(
                section="lottery",
                symbol=sym,
                action="WAIT",
                right=right,
                price=ask,
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon="0dte",
                reason=str(row.get("detail") or row.get("thesis") or row.get("headline") or "WAIT lottery"),
                headline=str(row.get("headline") or f"WAIT {sym}"),
                source="board",
            )
            live.add(self._key("lottery", sym, right))
            n += 1
        self.mark_off_board("lottery", live)
        return n

    def sync_radar(self, radar: dict[str, Any] | None) -> int:
        """Discord-style radar HOT/WATCH — soft opens only (no clock P&L closes)."""
        if not isinstance(radar, dict):
            return 0
        n = 0
        live: set[str] = set()
        for row in list(radar.get("hot") or []) + list(radar.get("watch") or [])[:6]:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            action = str(row.get("action") or "RADAR_WATCH").upper()
            if action not in _SOFT_OPEN_ACTIONS:
                action = "RADAR_WATCH"
            self.note_entry(
                section="radar",
                symbol=sym,
                action=action,
                right="C",
                price=_f(row.get("ask") or row.get("bid")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon="0dte",
                reason=str(row.get("detail") or row.get("headline") or "radar"),
                headline=str(row.get("headline") or action),
                source="radar",
            )
            live.add(self._key("radar", sym, "C"))
            n += 1
        # Soft: flag off-board only — never invent $0 losses from radar watches
        self.mark_off_board("radar", live, soft=True)
        return n

    def sync_actions(self, actions: dict[str, Any] | None) -> int:
        """Main 0DTE / weekly action board BUY_NOW / SELL_NOW."""
        if not isinstance(actions, dict):
            return 0
        n = 0
        live: set[str] = set()

        def _section_for(row: dict[str, Any]) -> str:
            return _section_from_bucket(row.get("dte_bucket") or row.get("horizon") or "odte")

        def _is_real_buy(row: dict[str, Any]) -> bool:
            # Require a positive option premium — never use bare stock last_price
            ask = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            if ask is None or ask <= 0:
                return False
            # Quality cards used underlying last (~$35–$300) as "entry". Real desk
            # options for this scanner sit well under ~$80; allow when strike/contract present.
            if ask >= 80.0 and not (row.get("contract") or row.get("strike")):
                return False
            return True

        for row in actions.get("buy_now") or []:
            if not isinstance(row, dict):
                continue
            if not _is_real_buy(row):
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            self.note_entry(
                section=sec,
                symbol=sym,
                action="BUY_NOW",
                right=right,
                price=_f(row.get("ask") or row.get("entry_ask") or row.get("mark")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon=str(row.get("dte_bucket") or row.get("horizon") or "") or None,
                reason=str(row.get("thesis") or row.get("reason") or row.get("detail") or ""),
                headline=str(row.get("headline") or "BUY NOW"),
            )
            live.add(self._key(sec, sym, right))
            n += 1
        for row in actions.get("sell_now") or []:
            if not isinstance(row, dict):
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            self.note_exit(
                section=sec,
                symbol=sym,
                action="SELL_NOW",
                right=right,
                price=_f(row.get("bid") or row.get("mark") or row.get("ask")),
                spot=_f(row.get("spot") or row.get("live_last")),
                reason=str(row.get("thesis") or row.get("reason") or row.get("detail") or ""),
                entry_price=_f(row.get("entry") or row.get("entry_ask")),
            )
            live.add(self._key(sec, sym, right))
            n += 1
        # also sync just_exited from journal/actions (priced exits)
        for row in actions.get("just_exited") or []:
            if not isinstance(row, dict):
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            right = _right_of(row)
            self.note_exit(
                section=sec,
                symbol=sym,
                action="SELL_NOW",
                right=right,
                price=_f(row.get("exit_bid") or row.get("bid") or row.get("mark")),
                spot=_f(row.get("exit_spot") or row.get("spot")),
                reason=str(row.get("exit_reason") or row.get("detail") or "just_exited"),
                entry_price=_f(row.get("entry_ask") or row.get("entry")),
                at=str(row.get("exited_at") or "") or None,
                allow_stub=True,
            )
            n += 1
        # WAIT / HOLD: log priced option recommendations into odte/weekly/swing history
        for row in (actions.get("wait") or []) + (actions.get("hold") or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("action") or "").upper() in _CLOSE_ACTIONS:
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            right = _right_of(row)
            ask = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            # Only log real option WAIT rows (ask + strike/contract) — skip bare stock
            if ask is not None and ask > 0 and (row.get("contract") or row.get("strike")):
                act = str(row.get("action") or "WAIT").upper()
                if act not in _SOFT_OPEN_ACTIONS:
                    act = "WAIT"
                self.note_entry(
                    section=sec,
                    symbol=sym,
                    action=act,
                    right=right,
                    price=ask,
                    spot=_f(row.get("spot") or row.get("live_last")),
                    contract=str(row.get("contract") or "") or None,
                    expiry=str(row.get("expiry") or "") or None,
                    strike=_f(row.get("strike")),
                    dte=int(row["dte"]) if row.get("dte") is not None else None,
                    horizon=str(row.get("dte_bucket") or row.get("horizon") or "") or None,
                    reason=str(row.get("thesis") or row.get("reason") or row.get("detail") or f"{act} desk"),
                    headline=str(row.get("headline") or f"{act} {sym}"),
                    source="board",
                )
                n += 1
            live.add(self._key(sec, sym, right))
        for sec in ("odte", "weekly", "swing"):
            self.mark_off_board(sec, {k for k in live if k.startswith(sec.upper())})
        return n

    def sync_action_cards(self, action_cards: dict[str, Any] | None) -> int:
        """Quality cards keep existing opens on-board — do NOT open fake BUY_NOW at stock last.

        Underlying last_price was previously logged as entry (e.g. CBRS @ $264) then
        clock-flattened at the same price → 0% 'losses'. Quality ≠ option BUY NOW.
        """
        if not isinstance(action_cards, dict):
            return 0
        mapping = {
            "0dte_quality": "odte",
            "weekly_quality": "weekly",
            "swing_quality": "swing",
        }
        live: dict[str, set[str]] = {v: set() for v in mapping.values()}
        for key, section in mapping.items():
            for row in (action_cards.get(key) or [])[:10]:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "")
                if not sym:
                    continue
                # Only keep *existing* open BUY_NOW on-board when the quality card still lists them
                if self._open_for(section, sym, "C") or self._open_for(section, sym, "P"):
                    live[section].add(self._key(section, sym, "C"))
                    live[section].add(self._key(section, sym, "P"))
        for sec, keys in live.items():
            existing_live = {
                self._key(r.section, r.symbol, r.right)
                for r in self.book.recommendations
                if r.section == sec and r.status == "open" and r.on_board
            }
            self.mark_off_board(sec, existing_live | keys)
        return 0

    def sync_from_journal(self, journal: Any | None) -> int:
        """Mirror paper journal BUY NOW / SELL NOW fills into section logs with real premiums.

        Entry price = journal entry_ask at BUY NOW time.
        Exit price  = journal exit_bid at SELL NOW time.
        P&L (1ct)   = (exit_bid − entry_ask) × 100.
        """
        if journal is None:
            return 0
        trades: list[Any] = []
        if hasattr(journal, "book"):
            trades = list(getattr(journal.book, "trades", []) or [])
        elif isinstance(journal, dict):
            trades = list(journal.get("trades") or [])
            # also accept performance payload shape
            if not trades:
                trades = list(journal.get("open") or []) + list(journal.get("closed") or [])
        elif isinstance(journal, list):
            trades = journal
        else:
            return 0

        n = 0
        seen_closed: set[str] = set()
        for t in trades:
            if isinstance(t, dict):
                td = t
            else:
                td = t.to_dict() if hasattr(t, "to_dict") else None
                if td is None:
                    continue
            sym = str(td.get("symbol") or "").upper()
            if not sym:
                continue
            right = str(td.get("right") or "C").upper()
            if right not in {"C", "P"}:
                right = "C"
            sec = _section_from_bucket(td.get("dte_bucket") or td.get("horizon"))
            status = str(td.get("status") or "open").lower()
            entry = _f(td.get("entry_ask") or td.get("entry"))
            contract = str(td.get("contract") or "") or None
            trade_id = str(td.get("id") or f"{sym}:{contract}:{td.get('entered_at')}")

            if status == "open":
                if entry is None or entry <= 0:
                    continue
                self.note_entry(
                    section=sec,
                    symbol=sym,
                    action="BUY_NOW",
                    right=right,
                    price=entry,
                    spot=_f(td.get("entry_spot")),
                    contract=contract,
                    expiry=str(td.get("expiry") or "") or None,
                    strike=_f(td.get("strike")),
                    horizon=str(td.get("dte_bucket") or "") or None,
                    reason=str(td.get("entry_reason") or "journal BUY NOW"),
                    headline=f"BUY NOW {sym} {right}",
                    at=str(td.get("entered_at") or "") or None,
                    source="journal",
                )
                n += 1
                continue

            if status != "closed":
                continue
            if trade_id in seen_closed:
                continue
            seen_closed.add(trade_id)
            exit_px = _f(td.get("exit_bid") or td.get("exit_price") or td.get("bid"))
            # Skip closed journal rows that were priced at entry (no live mark) — not a real P&L
            reason = str(td.get("exit_reason") or "journal SELL NOW")
            if "priced at entry" in reason.lower() and entry is not None and exit_px is not None:
                if abs(entry - exit_px) < 1e-9:
                    # Lapse matching open if any; don't create a $0 loss
                    if self._open_for(sec, sym, right):
                        self.note_lapse(
                            section=sec,
                            symbol=sym,
                            right=right,
                            reason=reason,
                        )
                        n += 1
                    continue
            if exit_px is None or exit_px <= 0:
                continue
            # Avoid duplicate closed rows for same contract+exit time
            already = False
            for r in self.book.recommendations:
                if (
                    r.status == "closed"
                    and r.symbol == sym
                    and r.right == right
                    and r.contract == contract
                    and r.exit_price is not None
                    and abs(float(r.exit_price) - float(exit_px)) < 1e-9
                    and r.entry_price is not None
                    and entry is not None
                    and abs(float(r.entry_price) - float(entry)) < 1e-9
                ):
                    already = True
                    break
            if already:
                continue
            self.note_exit(
                section=sec,
                symbol=sym,
                action="SELL_NOW",
                right=right,
                price=exit_px,
                spot=_f(td.get("exit_spot")),
                reason=reason,
                entry_price=entry,
                at=str(td.get("exited_at") or "") or None,
                allow_stub=True,
            )
            n += 1
        return n

    def sync_odte_1k(self, board: dict[str, Any] | None) -> int:
        """Persist 0DTE $1K IN (BUY PUT) / OUT (SELL PUT) recommendations."""
        if not isinstance(board, dict):
            return 0
        n = 0
        live: set[str] = set()
        section = "odte_1k"

        def _rows(*keys: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for k in keys:
                for row in board.get(k) or []:
                    if isinstance(row, dict) and row not in out:
                        out.append(row)
            return out

        # OUT first so same-tick flip closes before new IN
        for row in _rows("exit_now", "exit", "out"):
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            right = _right_of(row, default="P")
            live.add(self._key(section, sym, right))
            bid = _f(row.get("bid") or row.get("exit_bid") or row.get("mark") or row.get("ask"))
            reason = " · ".join(str(x) for x in (row.get("reasons") or [])[:3] if x) or str(
                row.get("detail") or row.get("headline") or "OUT · SELL PUT"
            )
            self.note_exit(
                section=section,
                symbol=sym,
                action="EXIT",
                right=right,
                price=bid,
                spot=_f(row.get("spot")),
                reason=reason,
            )
            n += 1

        for row in _rows("put_now", "entry", "in"):
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            right = _right_of(row, default="P")
            live.add(self._key(section, sym, right))
            ask = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            reason = " · ".join(str(x) for x in (row.get("reasons") or [])[:3] if x) or str(
                row.get("detail") or row.get("headline") or "IN · BUY PUT"
            )
            self.note_entry(
                section=section,
                symbol=sym,
                action="PUT_NOW",
                right=right,
                price=ask,
                spot=_f(row.get("spot")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else 0,
                horizon="0dte",
                reason=reason,
                headline=str(row.get("headline") or f"IN · BUY PUT {sym}"),
                source="odte_1k",
            )
            n += 1

        # Soft WATCH so the 1K desk isn't empty in history (HOLD stays as WAIT soft)
        # Cap soft rows — full focus sleeve would flood the log
        soft_rows = _rows("hold", "watch")[:10]
        for row in soft_rows:
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            right = _right_of(row, default="P")
            key = self._key(section, sym, right)
            if key in live:
                continue
            live.add(key)
            act = str(row.get("action") or "WATCH").upper()
            soft = "HOLD" if act == "HOLD" else "WATCH"
            # HOLD isn't a soft-open in the logger — map to WAIT for history only
            log_act = "WAIT" if soft == "HOLD" else "WATCH"
            self.note_entry(
                section=section,
                symbol=sym,
                action=log_act,
                right=right,
                price=_f(row.get("ask") or row.get("mark")),
                spot=_f(row.get("spot")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else 0,
                horizon="0dte",
                reason=str(row.get("detail") or row.get("headline") or soft),
                headline=str(row.get("headline") or f"{soft} {sym} PUT"),
                source="odte_1k",
            )
            n += 1

        self.mark_off_board(section, live, soft=True)
        return n

    def sync_all(
        self,
        *,
        lottery: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
        actions: dict[str, Any] | None = None,
        action_cards: dict[str, Any] | None = None,
        radar: dict[str, Any] | None = None,
        journal: Any | None = None,
        odte_1k: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Journal first so BUY NOW fills seed opens before board SELL NOW / just_exited
        n_j = self.sync_from_journal(journal)
        n_lot = self.sync_lottery(lottery)
        n_ch = self.sync_challenge(challenge)
        n_1k = self.sync_odte_1k(odte_1k)
        n_act = self.sync_actions(actions)
        n_cards = self.sync_action_cards(action_cards)
        n_radar = self.sync_radar(radar)
        self.save()
        return {
            "lottery": n_lot,
            "challenge": n_ch,
            "odte_1k": n_1k,
            "actions": n_act,
            "action_cards": n_cards,
            "radar": n_radar,
            "journal": n_j,
            **self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        d = self.book.to_dict()
        return {
            "open": d["open"],
            "closed": d["closed"],
            "lapsed": d.get("lapsed", 0),
            "wins": d["wins"],
            "losses": d["losses"],
            "scratches": d.get("scratches", 0),
            "closed_pnl_usd": d["closed_pnl_usd"],
            "journal_pnl_usd": d.get("journal_pnl_usd", 0),
            "board_signal_pnl_usd": d.get("board_signal_pnl_usd", 0),
            "paper_pnl_usd": d.get("paper_pnl_usd", d.get("journal_pnl_usd", 0)),
            "updated_at": d["updated_at"],
        }

    def board(
        self,
        *,
        section: str | None = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        recs = list(self.book.recommendations)
        if section:
            sec = section.lower()
            recs = [r for r in recs if r.section == sec]
        open_rows = [r.to_dict() for r in recs if r.status == "open"][:limit]
        closed_rows = [r.to_dict() for r in recs if r.status in {"closed", "lapsed"}][:limit]
        m = _metrics(recs)
        return {
            "section": section,
            **m,
            "updated_at": self.book.updated_at,
            "open_recs": open_rows,
            "closed_recs": closed_rows,
            "all": [r.to_dict() for r in recs[:limit]],
            "pnl_note": (
                "Paper journal P&L (actual auto-fills) is shown separately on the Journal tab. "
                "Signal-log P&L below counts hypothetical 1-contract tracks from every BUY/SELL pulse "
                "across desks — not your live portfolio. "
                "P&L (1ct) = (SELL NOW bid − BUY NOW/ENTRY ask) × 100 when both prices are real. "
                "Clock flatten without a live mark is a lapse — not a win/loss."
            ),
        }
