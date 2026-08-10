"""Persistent recommendation logger across lottery / challenge / action desks.

Recommendations are ephemeral on each snapshot rebuild. This log keeps an
append-friendly history of when the desk recommended ENTRY/BUY and EXIT/SELL,
plus estimated P&L when both prices are known — so yesterday's challenge pick
still shows today even if it dropped off the live board.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "outputs" / "recommendation_log.json"

# Actions that open a recommendation
_OPEN_ACTIONS = {
    "BUY_NOW",
    "ENTRY",
    "BUY",
    "LONG",
}
# Actions that close / take profit on a recommendation
_CLOSE_ACTIONS = {
    "SELL_NOW",
    "EXIT",
    "SELL",
    "CLOSE",
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
    section: str  # lottery | challenge | odte | weekly | swing | actions
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
        open_n = sum(1 for r in self.recommendations if r.status == "open")
        closed = [r for r in self.recommendations if r.status == "closed"]
        wins = sum(1 for r in closed if (r.profit_pct or 0) > 0)
        losses = sum(1 for r in closed if (r.profit_pct or 0) <= 0 and r.profit_pct is not None)
        pnl = sum(r.pnl_usd or 0 for r in closed)
        return {
            "updated_at": self.updated_at,
            "open": open_n,
            "closed": len(closed),
            "wins": wins,
            "losses": losses,
            "closed_pnl_usd": round(pnl, 2),
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommendation log load failed: %s", exc)
            self.book = RecBook()

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
    ) -> Recommendation | None:
        sym = str(symbol or "").upper()
        if not sym:
            return None
        act = str(action or "ENTRY").upper()
        if act not in _OPEN_ACTIONS:
            act = "ENTRY"
        existing = self._open_for(section, sym, right)
        now = _now()
        if existing:
            existing.last_recommended_at = now
            existing.on_board = True
            if price and (existing.entry_price is None or existing.entry_price <= 0):
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

        rec = Recommendation(
            id=f"rec-{uuid.uuid4().hex[:10]}",
            section=str(section).lower(),
            symbol=sym,
            right=str(right or "C").upper(),
            open_action=act,
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
        )
        self._append_event(rec, action=act, price=price, detail=reason or headline)
        self.book.recommendations.insert(0, rec)
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
    ) -> Recommendation | None:
        sym = str(symbol or "").upper()
        if not sym:
            return None
        rec = self._open_for(section, sym, right)
        if not rec:
            # No open entry — still record a closed stub so SELL-only shows in log
            now = _now()
            rec = Recommendation(
                id=f"rec-{uuid.uuid4().hex[:10]}",
                section=str(section).lower(),
                symbol=sym,
                right=str(right or "C").upper(),
                open_action="ENTRY",
                recommended_at=now,
                last_recommended_at=now,
                entry_price=None,
                status="closed",
                on_board=True,
                close_action=str(action or "EXIT").upper(),
                closed_at=now,
                exit_price=price,
                exit_spot=spot,
                exit_reason=(reason or "")[:320],
                events=[],
            )
            self._append_event(rec, action=str(action or "EXIT").upper(), price=price, detail=reason)
            self.book.recommendations.insert(0, rec)
            return rec

        if rec.status == "closed":
            return rec

        act = str(action or "EXIT").upper()
        if act not in _CLOSE_ACTIONS:
            act = "EXIT"
        now = _now()
        rec.status = "closed"
        rec.on_board = True
        rec.close_action = act
        rec.closed_at = now
        rec.exit_price = price
        rec.exit_spot = spot
        rec.exit_reason = (reason or "")[:320]
        rec.last_recommended_at = now
        if rec.entry_price and price is not None and rec.entry_price > 0:
            rec.profit_pct = round(((price - rec.entry_price) / rec.entry_price) * 100.0, 2)
            rec.pnl_usd = round((price - rec.entry_price) * 100.0, 2)  # 1 contract
        try:
            t0 = datetime.fromisoformat(rec.recommended_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(now.replace("Z", "+00:00"))
            rec.hold_hours = round((t1 - t0).total_seconds() / 3600.0, 2)
        except Exception:  # noqa: BLE001
            rec.hold_hours = None
        self._append_event(rec, action=act, price=price, detail=reason)
        return rec

    def mark_off_board(self, section: str, live_keys: set[str]) -> None:
        """Flag open recs not on today's board; keep history (do not delete)."""
        sec = str(section).lower()
        for r in self.book.recommendations:
            if r.section != sec or r.status != "open":
                continue
            key = self._key(r.section, r.symbol, r.right)
            r.on_board = key in live_keys

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
            right = str(row.get("right") or "C").upper()
            action = str(row.get("action") or "WAIT").upper()
            if not sym:
                continue
            live.add(self._key("challenge", sym, right))
            price = _f(row.get("ask") or row.get("entry_ask") or row.get("mark"))
            bid = _f(row.get("bid") or row.get("exit_bid") or row.get("ask"))
            reason = " · ".join(
                str(x) for x in (row.get("reasons") or [])[:3] if x
            ) or str(row.get("enter_plan") or row.get("exit_plan") or row.get("headline") or "")
            if action == "EXIT":
                self.note_exit(
                    section="challenge",
                    symbol=sym,
                    action="EXIT",
                    right=right,
                    price=bid if bid is not None else price,
                    spot=_f(row.get("spot")),
                    reason=str(row.get("exit_plan") or reason),
                )
                n += 1
                continue
            # ENTRY / HOLD / WAIT — keep on the recommendation log so names don't vanish overnight
            self.note_entry(
                section="challenge",
                symbol=sym,
                action="ENTRY" if action in ("ENTRY", "HOLD", "WAIT") else "ENTRY",
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
            self.note_entry(
                section="lottery",
                symbol=sym,
                action="BUY_NOW",
                right="C",
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
            live.add(self._key("lottery", sym, "C"))
            n += 1
        for row in lottery.get("sell_now") or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            self.note_exit(
                section="lottery",
                symbol=sym,
                action="SELL_NOW",
                right="C",
                price=_f(row.get("bid") or row.get("ask") or row.get("mark")),
                spot=_f(row.get("spot") or row.get("live_last")),
                reason=str(row.get("detail") or row.get("headline") or ""),
            )
            live.add(self._key("lottery", sym, "C"))
            n += 1
        # Top WAIT tickets still get logged so the desk history isn't empty on quiet tapes
        for row in (lottery.get("wait") or [])[:5]:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            self.note_entry(
                section="lottery",
                symbol=sym,
                action="BUY_NOW",
                right="C",
                price=_f(row.get("ask") or row.get("entry_ask")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon="0dte",
                reason=str(row.get("detail") or "WAIT — gated lottery") ,
                headline=str(row.get("headline") or "WAIT lottery"),
            )
            live.add(self._key("lottery", sym, "C"))
            n += 1
        self.mark_off_board("lottery", live)
        return n

    def sync_radar(self, radar: dict[str, Any] | None) -> int:
        """Discord-style radar HOT/WATCH — separate section from lottery BUY NOW."""
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
            action = str(row.get("action") or "RADAR_WATCH")
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
            )
            live.add(self._key("radar", sym, "C"))
            n += 1
        self.mark_off_board("radar", live)
        return n

    def sync_actions(self, actions: dict[str, Any] | None) -> int:
        """Main 0DTE / weekly action board BUY_NOW / SELL_NOW."""
        if not isinstance(actions, dict):
            return 0
        n = 0
        live: set[str] = set()

        def _section_for(row: dict[str, Any]) -> str:
            bucket = str(row.get("dte_bucket") or row.get("horizon") or "odte").lower()
            if "swing" in bucket:
                return "swing"
            if "week" in bucket:
                return "weekly"
            return "odte"

        for row in actions.get("buy_now") or []:
            if not isinstance(row, dict):
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            self.note_entry(
                section=sec,
                symbol=sym,
                action="BUY_NOW",
                right="C",
                price=_f(row.get("ask") or row.get("entry_ask")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon=str(row.get("dte_bucket") or row.get("horizon") or "") or None,
                reason=str(row.get("thesis") or row.get("reason") or row.get("detail") or ""),
                headline=str(row.get("headline") or "BUY NOW"),
            )
            live.add(self._key(sec, sym, "C"))
            n += 1
        for row in actions.get("sell_now") or []:
            if not isinstance(row, dict):
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            self.note_exit(
                section=sec,
                symbol=sym,
                action="SELL_NOW",
                right="C",
                price=_f(row.get("bid") or row.get("ask") or row.get("mark")),
                spot=_f(row.get("spot") or row.get("live_last")),
                reason=str(row.get("thesis") or row.get("reason") or row.get("detail") or ""),
            )
            live.add(self._key(sec, sym, "C"))
            n += 1
        # WAIT rows still tracked (top of board) so section logs aren't empty
        for row in (actions.get("wait") or actions.get("all") or [])[:8]:
            if not isinstance(row, dict):
                continue
            if str(row.get("action") or "").upper() in _CLOSE_ACTIONS:
                continue
            sec = _section_for(row)
            sym = str(row.get("symbol") or "")
            if not sym or self._key(sec, sym, "C") in live:
                continue
            self.note_entry(
                section=sec,
                symbol=sym,
                action="BUY_NOW",
                right="C",
                price=_f(row.get("ask") or row.get("entry_ask")),
                spot=_f(row.get("spot") or row.get("live_last")),
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                dte=int(row["dte"]) if row.get("dte") is not None else None,
                horizon=str(row.get("dte_bucket") or row.get("horizon") or "") or None,
                reason=str(row.get("thesis") or row.get("reason") or "WAIT desk"),
                headline=str(row.get("headline") or row.get("action") or "WAIT"),
            )
            live.add(self._key(sec, sym, "C"))
            n += 1
        for sec in ("odte", "weekly", "swing"):
            self.mark_off_board(sec, {k for k in live if k.startswith(sec.upper())})
        return n

    def sync_action_cards(self, action_cards: dict[str, Any] | None) -> int:
        """Quality action cards (0DTE / weekly / swing) as lasting recommendations."""
        if not isinstance(action_cards, dict):
            return 0
        mapping = {
            "0dte_quality": "odte",
            "weekly_quality": "weekly",
            "swing_quality": "swing",
        }
        n = 0
        live: dict[str, set[str]] = {v: set() for v in mapping.values()}
        for key, section in mapping.items():
            for row in (action_cards.get(key) or [])[:10]:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "")
                if not sym:
                    continue
                self.note_entry(
                    section=section,
                    symbol=sym,
                    action="BUY_NOW",
                    right="C",
                    price=_f(row.get("entry") or row.get("last_price") or row.get("ask")),
                    spot=_f(row.get("last_price") or row.get("entry")),
                    horizon=section,
                    reason=" · ".join(str(x) for x in (row.get("reasons") or [])[:3] if x)
                    or str(row.get("thesis") or "quality card"),
                    headline=f"QUALITY {section.upper()} {sym}",
                )
                live[section].add(self._key(section, sym, "C"))
                n += 1
        for sec, keys in live.items():
            # Merge with existing live marks — don't wipe action-synced opens
            existing_live = {
                self._key(r.section, r.symbol, r.right)
                for r in self.book.recommendations
                if r.section == sec and r.status == "open" and r.on_board
            }
            self.mark_off_board(sec, existing_live | keys)
        return n

    def sync_all(
        self,
        *,
        lottery: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
        actions: dict[str, Any] | None = None,
        action_cards: dict[str, Any] | None = None,
        radar: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        n_lot = self.sync_lottery(lottery)
        n_ch = self.sync_challenge(challenge)
        n_act = self.sync_actions(actions)
        n_cards = self.sync_action_cards(action_cards)
        n_radar = self.sync_radar(radar)
        self.save()
        return {
            "lottery": n_lot,
            "challenge": n_ch,
            "actions": n_act,
            "action_cards": n_cards,
            "radar": n_radar,
            **self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        d = self.book.to_dict()
        return {
            "open": d["open"],
            "closed": d["closed"],
            "wins": d["wins"],
            "losses": d["losses"],
            "closed_pnl_usd": d["closed_pnl_usd"],
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
        closed_rows = [r.to_dict() for r in recs if r.status == "closed"][:limit]
        closed_all = [r for r in recs if r.status == "closed"]
        wins = sum(1 for r in closed_all if (r.profit_pct or 0) > 0)
        losses = sum(1 for r in closed_all if (r.profit_pct or 0) <= 0 and r.profit_pct is not None)
        pnl = sum(r.pnl_usd or 0 for r in closed_all)
        return {
            "section": section,
            "open": sum(1 for r in recs if r.status == "open"),
            "closed": len(closed_all),
            "wins": wins,
            "losses": losses,
            "closed_pnl_usd": round(pnl, 2),
            "updated_at": self.book.updated_at,
            "open_recs": open_rows,
            "closed_recs": closed_rows,
            "all": [r.to_dict() for r in recs[:limit]],
        }
