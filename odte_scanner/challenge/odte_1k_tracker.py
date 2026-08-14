"""Paper ledger for the 0DTE $1K Challenge sleeve.

Separate from swing $1k→$1M challenge — tracks $1k start, ~$850 size, max 2 trades/day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import to_cst_label

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "outputs" / "odte_1k_ledger.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Odte1kTrade:
    id: str
    symbol: str
    right: str  # P | C
    contract: str | None
    expiry: str | None
    strike: float | None
    entered_at: str
    entry_ask: float
    entry_spot: float | None
    entry_reason: str
    contracts: int
    cost: float
    status: str = "open"
    exited_at: str | None = None
    exit_bid: float | None = None
    exit_reason: str | None = None
    exit_spot: float | None = None
    proceeds: float | None = None
    pnl_usd: float | None = None
    profit_pct: float | None = None
    mark: float | None = None
    unrealized_pct: float | None = None
    cash_before: float | None = None
    cash_after: float | None = None
    equity_after: float | None = None
    orb_low: float | None = None
    orb_high: float | None = None
    green_friday: bool = False
    entered_at_cst: str | None = None
    exited_at_cst: str | None = None
    balance_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Odte1kBook:
    starting_cash: float = 1000.0
    cash: float = 1000.0
    wins: int = 0
    losses: int = 0
    trades_closed: int = 0
    trades: list[Odte1kTrade] = field(default_factory=list)
    balance_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        open_mtm = sum(
            (t.mark or t.entry_ask) * 100 * t.contracts for t in self.trades if t.status == "open"
        )
        equity = round(self.cash + open_mtm, 2)
        today = datetime.now(ET).date().isoformat()
        trades_today = 0
        for t in self.trades:
            try:
                dt = datetime.fromisoformat(t.entered_at.replace("Z", "+00:00")).astimezone(ET)
                if dt.date().isoformat() == today:
                    trades_today += 1
            except Exception:  # noqa: BLE001
                if (t.entered_at or "").startswith(today):
                    trades_today += 1
        return {
            "starting_cash": self.starting_cash,
            "cash": round(self.cash, 2),
            "equity": equity,
            "doubled": equity >= self.starting_cash * 2,
            "progress_2x_pct": round((equity / (self.starting_cash * 2)) * 100.0, 2),
            "wins": self.wins,
            "losses": self.losses,
            "trades_closed": self.trades_closed,
            "open_trades": sum(1 for t in self.trades if t.status == "open"),
            "trades_today": trades_today,
            "trades": [t.to_dict() for t in self.trades],
            "balance_log": list(self.balance_log[-40:]),
        }


class Odte1kTracker:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        starting_cash: float = 1000.0,
        max_trades_per_day: int = 2,
        default_size_usd: float = 850.0,
    ):
        self.path = Path(path) if path else DEFAULT_PATH
        self.max_trades_per_day = max_trades_per_day
        self.default_size_usd = default_size_usd
        self.book = Odte1kBook(starting_cash=starting_cash, cash=starting_cash)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            known = {f.name for f in fields(Odte1kTrade)}
            trades: list[Odte1kTrade] = []
            for t in raw.get("trades") or []:
                if not isinstance(t, dict):
                    continue
                payload = {k: v for k, v in t.items() if k in known}
                try:
                    trades.append(Odte1kTrade(**payload))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skip bad odte_1k trade: %s", exc)
            self.book = Odte1kBook(
                starting_cash=float(raw.get("starting_cash") or self.book.starting_cash),
                cash=float(raw.get("cash") or self.book.starting_cash),
                wins=int(raw.get("wins") or 0),
                losses=int(raw.get("losses") or 0),
                trades_closed=int(raw.get("trades_closed") or 0),
                trades=trades,
                balance_log=list(raw.get("balance_log") or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("odte_1k ledger load failed: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "starting_cash": self.book.starting_cash,
            "cash": self.book.cash,
            "wins": self.book.wins,
            "losses": self.book.losses,
            "trades_closed": self.book.trades_closed,
            "trades": [t.to_dict() for t in self.book.trades],
            "balance_log": self.book.balance_log,
        }
        self.path.write_text(json.dumps(payload, indent=2))

    def open_trades(self) -> list[Odte1kTrade]:
        return [t for t in self.book.trades if t.status == "open"]

    def trades_today_count(self) -> int:
        return int(self.book.to_dict().get("trades_today") or 0)

    def enter(self, signal: dict[str, Any], *, size_usd: float | None = None) -> Odte1kTrade | None:
        if self.trades_today_count() >= self.max_trades_per_day:
            logger.info("odte_1k day cap reached")
            return None
        ask = signal.get("ask")
        if ask is None or float(ask) <= 0:
            logger.info("odte_1k enter skipped — no live ask")
            return None
        ask_f = float(ask)
        budget = float(size_usd if size_usd is not None else signal.get("position_size_usd") or self.default_size_usd)
        budget = min(budget, self.book.cash)
        contracts = int(budget // (ask_f * 100))
        if contracts < 1:
            logger.info("odte_1k enter skipped — size $%.2f can't buy 1ct @ $%.2f", budget, ask_f)
            return None
        cost = round(ask_f * 100 * contracts, 2)
        if cost > self.book.cash:
            return None
        cash_before = round(self.book.cash, 2)
        entered = signal.get("signaled_at") or _now()
        trade = Odte1kTrade(
            id=f"1K-{signal.get('symbol')}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            symbol=str(signal.get("symbol") or "SPY").upper(),
            right=str(signal.get("right") or "P").upper(),
            contract=signal.get("contract"),
            expiry=signal.get("expiry"),
            strike=float(signal["strike"]) if signal.get("strike") is not None else None,
            entered_at=entered,
            entry_ask=ask_f,
            entry_spot=float(signal["spot"]) if signal.get("spot") is not None else None,
            entry_reason=str(signal.get("detail") or signal.get("headline") or "PUT_NOW"),
            contracts=contracts,
            cost=cost,
            mark=ask_f,
            cash_before=cash_before,
            orb_low=float(signal["orb_low"]) if signal.get("orb_low") is not None else None,
            orb_high=float(signal["orb_high"]) if signal.get("orb_high") is not None else None,
            green_friday=bool(signal.get("green_friday")),
            entered_at_cst=signal.get("signaled_at_cst") or to_cst_label(entered),
        )
        self.book.cash = round(self.book.cash - cost, 2)
        trade.cash_after = round(self.book.cash, 2)
        trade.equity_after = round(self.book.cash + cost, 2)
        trade.balance_note = (
            f"Balance after ENTRY: cash ${trade.cash_after:,.2f} · sleeve equity ${trade.equity_after:,.2f}"
        )
        self.book.trades.append(trade)
        self.book.balance_log.append(
            {
                "at": entered,
                "action": "ENTRY",
                "symbol": trade.symbol,
                "cash_before": cash_before,
                "cash_after": trade.cash_after,
                "equity_after": trade.equity_after,
                "pnl_usd": None,
            }
        )
        self.save()
        return trade

    def exit_trade(
        self,
        trade_id: str,
        *,
        exit_bid: float,
        reason: str = "EXIT",
        exit_spot: float | None = None,
    ) -> Odte1kTrade | None:
        for t in self.book.trades:
            if t.id != trade_id or t.status != "open":
                continue
            bid = float(exit_bid)
            proceeds = round(bid * 100 * t.contracts, 2)
            cash_before = round(self.book.cash, 2)
            self.book.cash = round(self.book.cash + proceeds, 2)
            pnl = round(proceeds - t.cost, 2)
            t.status = "closed"
            t.exited_at = _now()
            t.exited_at_cst = to_cst_label(t.exited_at)
            t.exit_bid = bid
            t.exit_reason = reason
            t.exit_spot = exit_spot
            t.proceeds = proceeds
            t.pnl_usd = pnl
            t.profit_pct = round((bid - t.entry_ask) / t.entry_ask * 100.0, 2) if t.entry_ask else None
            t.cash_before = cash_before
            t.cash_after = round(self.book.cash, 2)
            t.equity_after = t.cash_after
            t.balance_note = (
                f"Balance after EXIT: cash ${t.cash_after:,.2f} · P&L ${pnl:,.2f}"
            )
            self.book.trades_closed += 1
            if pnl >= 0:
                self.book.wins += 1
            else:
                self.book.losses += 1
            self.book.balance_log.append(
                {
                    "at": t.exited_at,
                    "action": "EXIT",
                    "symbol": t.symbol,
                    "cash_before": cash_before,
                    "cash_after": t.cash_after,
                    "equity_after": t.equity_after,
                    "pnl_usd": pnl,
                }
            )
            self.save()
            return t
        return None

    def mark_open(self, marks: dict[str, float]) -> None:
        for t in self.open_trades():
            m = marks.get(t.symbol) or marks.get(t.id)
            if m is None:
                continue
            t.mark = float(m)
            if t.entry_ask:
                t.unrealized_pct = round((t.mark - t.entry_ask) / t.entry_ask * 100.0, 2)
        self.save()
