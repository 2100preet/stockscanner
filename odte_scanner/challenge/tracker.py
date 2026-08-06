"""Challenge paper ledger — ENTRY / HOLD / EXIT with hold-period rules.

Tracks the $1k→$1M sleeve separately from the main signal journal.
Supports long calls and long puts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "outputs" / "challenge_ledger.json"

# Hold windows (calendar days) by horizon / style
HOLD_PERIODS: dict[str, dict[str, int]] = {
    "weekly": {"min_days": 5, "max_days": 14, "ideal_days": 8},
    "swing": {"min_days": 20, "max_days": 60, "ideal_days": 35},
    "leap": {"min_days": 30, "max_days": 90, "ideal_days": 55},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hold_period_for(horizon: str | None, dte: int | None = None) -> dict[str, Any]:
    hz = (horizon or "swing").lower()
    if dte is not None and int(dte) >= 180:
        key = "leap"
    elif hz == "weekly":
        key = "weekly"
    else:
        key = "swing"
    cfg = HOLD_PERIODS[key]
    return {
        "style": key,
        "min_days": cfg["min_days"],
        "max_days": cfg["max_days"],
        "ideal_days": cfg["ideal_days"],
        "label": f"{cfg['min_days']}–{cfg['max_days']}d (ideal ~{cfg['ideal_days']}d)",
    }


@dataclass
class ChallengeTrade:
    id: str
    symbol: str
    right: str  # C | P
    contract: str
    expiry: str | None
    strike: float | None
    horizon: str | None
    dte_at_entry: int | None
    entered_at: str
    entry_ask: float
    entry_spot: float | None
    entry_reason: str
    hold_min_days: int
    hold_max_days: int
    hold_ideal_days: int
    target_premium_mult: float
    stop_loss_pct: float = 45.0
    status: str = "open"  # open | closed
    exited_at: str | None = None
    exit_bid: float | None = None
    exit_reason: str | None = None
    exit_spot: float | None = None
    contracts: int = 1
    cost: float = 0.0
    proceeds: float | None = None
    pnl_usd: float | None = None
    profit_pct: float | None = None
    hold_days: float | None = None
    mark: float | None = None
    unrealized_pct: float | None = None
    last_action: str = "ENTRY"  # ENTRY | HOLD | EXIT
    last_action_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChallengeBook:
    starting_cash: float = 1000.0
    cash: float = 1000.0
    target_usd: float = 1_000_000.0
    flips_closed: int = 0
    wins: int = 0
    losses: int = 0
    trades: list[ChallengeTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        open_mtm = sum(
            (t.mark or t.entry_ask) * 100 * t.contracts for t in self.trades if t.status == "open"
        )
        return {
            "starting_cash": self.starting_cash,
            "cash": round(self.cash, 2),
            "equity": round(self.cash + open_mtm, 2),
            "target_usd": self.target_usd,
            "flips_closed": self.flips_closed,
            "wins": self.wins,
            "losses": self.losses,
            "open_trades": sum(1 for t in self.trades if t.status == "open"),
            "trades": [t.to_dict() for t in self.trades],
        }


class ChallengeTracker:
    def __init__(self, path: str | Path | None = None, *, starting_cash: float = 1000.0):
        self.path = Path(path) if path else DEFAULT_PATH
        self.book = ChallengeBook(starting_cash=starting_cash, cash=starting_cash)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            trades = [ChallengeTrade(**t) for t in raw.get("trades") or []]
            self.book = ChallengeBook(
                starting_cash=float(raw.get("starting_cash") or self.book.starting_cash),
                cash=float(raw.get("cash") or self.book.starting_cash),
                target_usd=float(raw.get("target_usd") or 1_000_000),
                flips_closed=int(raw.get("flips_closed") or 0),
                wins=int(raw.get("wins") or 0),
                losses=int(raw.get("losses") or 0),
                trades=trades,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("challenge ledger load failed: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.book.to_dict()
        # preserve counters explicitly
        payload["flips_closed"] = self.book.flips_closed
        payload["wins"] = self.book.wins
        payload["losses"] = self.book.losses
        self.path.write_text(json.dumps(payload, indent=2))

    def open_trades(self) -> list[ChallengeTrade]:
        return [t for t in self.book.trades if t.status == "open"]

    def open_by_symbol_right(self, symbol: str, right: str) -> ChallengeTrade | None:
        for t in self.open_trades():
            if t.symbol == symbol and t.right == right:
                return t
        return None

    def enter(
        self,
        ticket: dict[str, Any],
        *,
        max_open: int = 1,
    ) -> ChallengeTrade | None:
        if ticket.get("action") not in {"ENTRY", "BUY_NOW"}:
            return None
        symbol = str(ticket.get("symbol") or "")
        right = str(ticket.get("right") or "C").upper()
        ask = float(ticket.get("ask") or 0)
        contract = str(ticket.get("contract") or "")
        if not symbol or ask <= 0 or not contract or contract.endswith("_SYN"):
            return None
        if self.open_by_symbol_right(symbol, right):
            return None
        if len(self.open_trades()) >= max_open:
            return None

        hp = hold_period_for(ticket.get("horizon"), ticket.get("dte"))
        contracts = int(ticket.get("contracts_for_bankroll") or 1)
        cost = ask * 100 * contracts
        if cost > self.book.cash:
            contracts = max(1, int(self.book.cash // (ask * 100)))
            cost = ask * 100 * contracts
            if cost <= 0 or cost > self.book.cash:
                return None

        trade = ChallengeTrade(
            id=f"CH-{symbol}{right}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            symbol=symbol,
            right=right,
            contract=contract,
            expiry=ticket.get("expiry"),
            strike=ticket.get("strike"),
            horizon=ticket.get("horizon"),
            dte_at_entry=ticket.get("dte"),
            entered_at=_now(),
            entry_ask=ask,
            entry_spot=ticket.get("spot") or ticket.get("live_last"),
            entry_reason=ticket.get("thesis") or ticket.get("detail") or "CHALLENGE ENTRY",
            hold_min_days=int(hp["min_days"]),
            hold_max_days=int(hp["max_days"]),
            hold_ideal_days=int(hp["ideal_days"]),
            target_premium_mult=float(ticket.get("target_premium_mult") or 1.78),
            contracts=contracts,
            cost=cost,
            mark=ask,
            last_action="ENTRY",
            last_action_detail=f"Entered {right} @ ${ask:.2f}",
        )
        self.book.cash -= cost
        self.book.trades.append(trade)
        self.save()
        return trade

    def _days_held(self, trade: ChallengeTrade) -> float:
        try:
            entered = datetime.fromisoformat(trade.entered_at.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - entered).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001
            return 0.0

    def evaluate_open(
        self,
        trade: ChallengeTrade,
        *,
        mark: float | None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return ENTRY/HOLD/EXIT recommendation for an open challenge trade."""
        days = self._days_held(trade)
        trade.hold_days = round(days, 2)
        bid = float(mark) if mark and mark > 0 else float(trade.mark or trade.entry_ask)
        trade.mark = bid
        unreal = ((bid - trade.entry_ask) / trade.entry_ask * 100.0) if trade.entry_ask else None
        trade.unrealized_pct = round(unreal, 2) if unreal is not None else None

        target_pct = (trade.target_premium_mult - 1.0) * 100.0
        mom5 = quote.get("mom_5m_pct") if quote else None
        live = None
        if quote:
            live = quote.get("session_change_pct")
            if live is None:
                live = quote.get("change_pct")

        reasons: list[str] = []
        action = "HOLD"

        if unreal is not None and unreal >= target_pct:
            action = "EXIT"
            reasons.append(f"hit challenge target +{unreal:.0f}% (≥{target_pct:.0f}%)")
        if unreal is not None and unreal <= -trade.stop_loss_pct:
            action = "EXIT"
            reasons.append(f"stop −{abs(unreal):.0f}%")
        if days >= trade.hold_max_days:
            action = "EXIT"
            reasons.append(f"max hold {trade.hold_max_days}d reached ({days:.1f}d)")
        # Thesis fail: call vs dump / put vs rip
        if trade.right == "C" and mom5 is not None and mom5 <= -0.35 and days >= trade.hold_min_days:
            action = "EXIT"
            reasons.append(f"call tape fail 5m {mom5:+.2f}% after min hold")
        if trade.right == "P" and mom5 is not None and mom5 >= 0.35 and days >= trade.hold_min_days:
            action = "EXIT"
            reasons.append(f"put tape fail 5m {mom5:+.2f}% after min hold")
        if (
            action == "HOLD"
            and days >= trade.hold_ideal_days
            and unreal is not None
            and unreal >= target_pct * 0.7
        ):
            action = "EXIT"
            reasons.append(f"ideal hold + {unreal:.0f}% (≥70% of target) — bank & roll")

        if action == "HOLD":
            reasons.append(
                f"holding {days:.1f}d / max {trade.hold_max_days}d · "
                f"unreal {unreal:+.1f}%" if unreal is not None else f"holding {days:.1f}d"
            )

        trade.last_action = action
        trade.last_action_detail = "; ".join(reasons)
        return {
            "action": action,
            "trade_id": trade.id,
            "symbol": trade.symbol,
            "right": trade.right,
            "hold_days": round(days, 2),
            "hold_max_days": trade.hold_max_days,
            "hold_min_days": trade.hold_min_days,
            "unrealized_pct": trade.unrealized_pct,
            "mark": bid,
            "detail": trade.last_action_detail,
        }

    def exit_trade(self, trade_id: str, *, exit_bid: float, reason: str) -> ChallengeTrade | None:
        for t in self.book.trades:
            if t.id != trade_id or t.status != "open":
                continue
            bid = max(0.0, float(exit_bid))
            proceeds = bid * 100 * t.contracts
            t.exit_bid = bid
            t.proceeds = proceeds
            t.pnl_usd = round(proceeds - t.cost, 2)
            t.profit_pct = (
                round(((bid - t.entry_ask) / t.entry_ask) * 100, 2) if t.entry_ask else None
            )
            t.exited_at = _now()
            t.exit_reason = reason
            t.status = "closed"
            t.hold_days = round(self._days_held(t), 2)
            t.last_action = "EXIT"
            t.last_action_detail = reason
            self.book.cash += proceeds
            self.book.flips_closed += 1
            if (t.pnl_usd or 0) > 0:
                self.book.wins += 1
            else:
                self.book.losses += 1
            self.save()
            return t
        return None

    def sync_from_tickets(
        self,
        tickets: list[dict[str, Any]],
        *,
        quotes: dict[str, dict[str, Any]] | None = None,
        auto_enter: bool = True,
        auto_exit: bool = True,
        max_open: int = 1,
    ) -> dict[str, Any]:
        quotes = quotes or {}
        entered: list[str] = []
        exited: list[str] = []
        holds: list[dict[str, Any]] = []

        # Evaluate opens first
        for t in list(self.open_trades()):
            # Prefer mark from matching ticket bid
            mark = t.mark
            for tk in tickets:
                if tk.get("symbol") == t.symbol and str(tk.get("right") or "C").upper() == t.right:
                    if tk.get("bid"):
                        mark = float(tk["bid"])
                    elif tk.get("ask"):
                        mark = float(tk["ask"]) * 0.95
                    break
            ev = self.evaluate_open(t, mark=mark, quote=quotes.get(t.symbol))
            holds.append(ev)
            if auto_exit and ev["action"] == "EXIT":
                out = self.exit_trade(t.id, exit_bid=float(ev["mark"]), reason=ev["detail"])
                if out:
                    exited.append(out.id)

        # Auto-enter top ENTRY ticket if flat
        if auto_enter and len(self.open_trades()) < max_open:
            for tk in tickets:
                if tk.get("action") != "ENTRY":
                    continue
                if not tk.get("contract") or tk.get("ask") in (None, 0):
                    continue
                tr = self.enter(tk, max_open=max_open)
                if tr:
                    entered.append(tr.id)
                    break

        self.save()
        return {
            "entered": entered,
            "exited": exited,
            "open_evals": holds,
            "book": self.book.to_dict(),
        }
