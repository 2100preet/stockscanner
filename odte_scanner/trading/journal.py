from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.time_cst import to_cst_label

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_LIVE_ASK_RE = re.compile(
    r"live ask\s*\$?\s*([0-9]*\.?[0-9]+)\s*vs\s*entry\s*\$?\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)
_UNREAL_AT_RE = re.compile(
    r"unreal\s*([+-]?[0-9]*\.?[0-9]+)%\s*@\s*\$?\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)


def _parse_live_exit_from_reason(reason: str | None) -> float | None:
    """Pull a live exit print out of SELL reason text when signal bid was stale."""
    if not reason:
        return None
    m = _LIVE_ASK_RE.search(reason)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = _UNREAL_AT_RE.search(reason)
    if m2:
        try:
            return float(m2.group(2))
        except ValueError:
            pass
    return None


@dataclass
class JournalTrade:
    id: str
    symbol: str
    contract: str
    expiry: str | None
    strike: float | None
    dte_bucket: str | None
    # Entry (BUY NOW suggestion)
    entered_at: str
    entry_ask: float
    entry_score: float | None
    entry_reason: str
    entry_spot: float | None = None
    entered_at_cst: str | None = None
    right: str = "C"  # C | P
    # Exit (SELL NOW / force close)
    status: str = "open"  # open | closed
    exited_at: str | None = None
    exit_bid: float | None = None
    exit_reason: str | None = None
    exit_spot: float | None = None
    # Results
    contracts: int = 1
    cost: float = 0.0
    proceeds: float | None = None
    pnl_usd: float | None = None
    profit_pct: float | None = None
    hold_minutes: float | None = None
    # Live mark for open trades
    mark: float | None = None
    unrealized_pnl_usd: float | None = None
    unrealized_pct: float | None = None
    # Cash / equity snapshot at enter or exit (challenge-style)
    cash_before: float | None = None
    cash_after: float | None = None
    equity_after: float | None = None
    balance_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeJournal:
    starting_cash: float
    cash: float
    trades: list[JournalTrade] = field(default_factory=list)
    balance_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        open_mtm = sum(
            (t.mark or t.entry_ask) * 100 * t.contracts for t in self.trades if t.status == "open"
        )
        return {
            "starting_cash": self.starting_cash,
            "cash": round(self.cash, 2),
            "equity": round(self.cash + open_mtm, 2),
            "open_trades": sum(1 for t in self.trades if t.status == "open"),
            "closed_trades": sum(1 for t in self.trades if t.status == "closed"),
            "trades": [t.to_dict() for t in self.trades],
            "balance_log": list(self.balance_log[-40:]),
        }


class SignalJournal:
    """
    Paper journal driven by BUY NOW / SELL NOW suggestions.
    Profit% = (exit - entry) / entry on the option premium.
    Each enter/exit records cash before→after, equity, and P&L like the challenge sleeve.
    """

    def __init__(self, path: str | Path, starting_cash: float = 5000.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_cash = starting_cash
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            known = {f.name for f in fields(JournalTrade)}
            trades: list[JournalTrade] = []
            for t in raw.get("trades") or []:
                if not isinstance(t, dict):
                    continue
                payload = {k: v for k, v in t.items() if k in known}
                try:
                    trades.append(JournalTrade(**payload))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skip bad journal trade: %s", exc)
            self.book = TradeJournal(
                starting_cash=float(raw.get("starting_cash", starting_cash)),
                cash=float(raw.get("cash", starting_cash)),
                trades=trades,
                balance_log=list(raw.get("balance_log") or []),
            )
        else:
            self.book = TradeJournal(starting_cash=starting_cash, cash=starting_cash)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.book.to_dict(), indent=2))

    def open_symbols(self) -> set[str]:
        return {t.symbol for t in self.book.trades if t.status == "open"}

    def open_by_symbol(self, symbol: str) -> list[JournalTrade]:
        return [t for t in self.book.trades if t.status == "open" and t.symbol == symbol]

    def enter_from_signal(
        self,
        signal: dict[str, Any],
        *,
        max_risk_usd: float = 250,
        max_open: int = 5,
        max_per_day: int = 5,
        require_flow_gate: bool = False,
    ) -> JournalTrade | None:
        if signal.get("action") != "BUY_NOW":
            return None
        if require_flow_gate:
            detail = str(signal.get("detail") or "")
            # Hard veto only — soft "flow n/a" / "flow soft" still allow paper enter
            if "blocked:" in detail and "flow" in detail.lower():
                logger.info("Journal flow gate — skip enter %s", signal.get("symbol"))
                return None
        symbol = str(signal.get("symbol") or "")
        ask = float(signal.get("ask") or 0)
        contract = str(signal.get("contract") or "")
        if not symbol or ask <= 0 or not contract or contract.endswith("_SYN"):
            return None
        if symbol in self.open_symbols():
            return None  # one open call per symbol

        today = datetime.now(timezone.utc).date().isoformat()
        opened_today = sum(1 for t in self.book.trades if t.entered_at.startswith(today))
        open_count = sum(1 for t in self.book.trades if t.status == "open")
        if opened_today >= max_per_day or open_count >= max_open:
            logger.info("Journal risk cap — skip enter %s", symbol)
            return None

        contracts = 1
        cost = ask * 100 * contracts
        if cost > max_risk_usd or cost > self.book.cash:
            logger.info("Journal skip %s cost $%.2f", symbol, cost)
            return None

        cash_before = round(self.book.cash, 2)
        right = str(signal.get("right") or "C").upper()
        if right not in {"C", "P"}:
            right = "C"
        entered = signal.get("signaled_at") or _now()
        trade = JournalTrade(
            id=f"{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            symbol=symbol,
            contract=contract,
            expiry=signal.get("expiry"),
            strike=signal.get("strike"),
            dte_bucket=signal.get("dte_bucket"),
            entered_at=entered,
            entry_ask=ask,
            entry_score=signal.get("score"),
            entry_reason=signal.get("detail") or signal.get("headline") or "BUY_NOW",
            entry_spot=signal.get("live_last"),
            entered_at_cst=signal.get("signaled_at_cst") or to_cst_label(entered),
            right=right,
            contracts=contracts,
            cost=cost,
            mark=ask,
            cash_before=cash_before,
        )
        self.book.cash -= cost
        cash_after = round(self.book.cash, 2)
        equity_after = round(cash_after + ask * 100 * contracts, 2)
        trade.cash_after = cash_after
        trade.equity_after = equity_after
        trade.balance_note = (
            f"Balance after ENTRY: cash ${cash_after:,.2f} · sleeve equity ${equity_after:,.2f} "
            f"(was ${cash_before:,.2f})"
        )
        self.book.trades.append(trade)
        self.book.balance_log.append(
            {
                "at": trade.entered_at,
                "action": "ENTRY",
                "symbol": symbol,
                "dte_bucket": trade.dte_bucket,
                "trade_id": trade.id,
                "cash_before": cash_before,
                "cash_after": cash_after,
                "equity_after": equity_after,
                "debit_usd": cost,
                "pnl_usd": None,
            }
        )
        self.save()
        logger.info(
            "JOURNAL ENTER %s %s @ %.2f cash $%.2f→$%.2f",
            symbol,
            contract,
            ask,
            cash_before,
            cash_after,
        )
        return trade

    def exit_trade(
        self,
        trade_id: str,
        *,
        exit_bid: float,
        reason: str,
        exit_spot: float | None = None,
    ) -> JournalTrade | None:
        for t in self.book.trades:
            if t.id != trade_id or t.status != "open":
                continue
            bid = max(0.0, float(exit_bid))
            proceeds = bid * 100 * t.contracts
            cash_before = round(self.book.cash, 2)
            t.exit_bid = bid
            t.proceeds = proceeds
            t.pnl_usd = round(proceeds - t.cost, 2)
            t.profit_pct = round(((bid - t.entry_ask) / t.entry_ask) * 100, 2) if t.entry_ask else None
            t.exited_at = _now()
            t.exit_reason = reason
            t.exit_spot = exit_spot
            t.status = "closed"
            try:
                entered = datetime.fromisoformat(t.entered_at.replace("Z", "+00:00"))
                exited = datetime.fromisoformat(t.exited_at.replace("Z", "+00:00"))
                t.hold_minutes = round((exited - entered).total_seconds() / 60.0, 1)
            except Exception:  # noqa: BLE001
                t.hold_minutes = None
            t.mark = bid
            t.unrealized_pnl_usd = None
            t.unrealized_pct = None
            t.cash_before = cash_before
            self.book.cash += proceeds
            cash_after = round(self.book.cash, 2)
            t.cash_after = cash_after
            t.equity_after = cash_after
            t.balance_note = (
                f"Balance after EXIT: cash/equity ${cash_after:,.2f} "
                f"(was cash ${cash_before:,.2f} + open mark) · P&L ${t.pnl_usd:,.2f} "
                f"({t.profit_pct:+.1f}%)" if t.profit_pct is not None else
                f"Balance after EXIT: cash/equity ${cash_after:,.2f} · P&L ${t.pnl_usd:,.2f}"
            )
            self.book.balance_log.append(
                {
                    "at": t.exited_at,
                    "action": "EXIT",
                    "symbol": t.symbol,
                    "dte_bucket": t.dte_bucket,
                    "trade_id": t.id,
                    "cash_before": cash_before,
                    "cash_after": cash_after,
                    "equity_after": cash_after,
                    "debit_usd": None,
                    "pnl_usd": t.pnl_usd,
                    "profit_pct": t.profit_pct,
                    "proceeds": proceeds,
                }
            )
            self.save()
            logger.info(
                "JOURNAL EXIT %s @ %.2f pnl=$%.2f (%.1f%%) cash $%.2f→$%.2f",
                t.symbol,
                bid,
                t.pnl_usd or 0,
                t.profit_pct or 0,
                cash_before,
                cash_after,
            )
            return t
        return None

    def exit_from_signal(self, signal: dict[str, Any]) -> list[JournalTrade]:
        """Close open trades for symbol when SELL NOW fires."""
        if signal.get("action") != "SELL_NOW":
            return []
        symbol = str(signal.get("symbol") or "")
        # Prefer live bid/mark on the signal — never silently use entry ask (0% P&L bug)
        px = signal.get("bid")
        if px is None or float(px or 0) <= 0:
            px = signal.get("mark")
        if px is None or float(px or 0) <= 0:
            px = signal.get("ask")
        closed = []
        for t in list(self.open_by_symbol(symbol)):
            exit_px = px
            reason = signal.get("detail") or signal.get("headline") or "SELL_NOW"
            parsed_live = _parse_live_exit_from_reason(reason)
            # If signal still has no usable price, fall back to trade mark
            if exit_px is None or float(exit_px or 0) <= 0:
                exit_px = t.mark
            priced_at_entry = False
            if exit_px is None or float(exit_px or 0) <= 0:
                if parsed_live is not None and parsed_live >= 0:
                    exit_px = parsed_live
                else:
                    # Still exit so losers aren't held forever when Yahoo option quote is blank
                    exit_px = t.entry_ask
                    priced_at_entry = True
                    reason = f"{reason} · exit priced at entry (no live bid/mark)"
            # Prefer live mark over a stale signal still pinned to entry ask
            if (
                not priced_at_entry
                and t.mark is not None
                and float(t.mark) > 0
                and abs(float(exit_px) - float(t.entry_ask)) < 1e-9
                and abs(float(t.mark) - float(t.entry_ask)) > 1e-9
            ):
                exit_px = t.mark
            # Reason text cited a live ask (e.g. lottery premium_decay) but signal/mark
            # were still entry-priced → book the live print so P&L is real.
            if (
                not priced_at_entry
                and parsed_live is not None
                and parsed_live >= 0
                and abs(float(exit_px) - float(t.entry_ask)) < 1e-9
                and abs(float(parsed_live) - float(t.entry_ask)) > 1e-9
            ):
                exit_px = parsed_live
            out = self.exit_trade(
                t.id,
                exit_bid=float(exit_px),
                reason=reason,
                exit_spot=signal.get("live_last"),
            )
            if out:
                closed.append(out)
        return closed

    def reprice_flat_exits_from_reasons(self) -> int:
        """Fix closed trades booked at entry ($0 P&L) when the exit reason had a live ask.

        Cash is rebuilt from starting_cash + chronological entry/exit events so
        open-position capital still matches. Returns number of trades corrected.
        """
        fixed = 0
        for t in self.book.trades:
            if t.status != "closed":
                continue
            if t.exit_bid is None or t.entry_ask is None:
                continue
            if abs(float(t.exit_bid) - float(t.entry_ask)) >= 1e-9:
                continue
            if t.pnl_usd is not None and abs(float(t.pnl_usd)) >= 1e-9:
                continue
            live_px = _parse_live_exit_from_reason(t.exit_reason)
            if live_px is None or abs(float(live_px) - float(t.entry_ask)) < 1e-9:
                continue
            bid = max(0.0, float(live_px))
            proceeds = bid * 100 * t.contracts
            t.exit_bid = bid
            t.proceeds = proceeds
            t.pnl_usd = round(proceeds - t.cost, 2)
            t.profit_pct = (
                round(((bid - t.entry_ask) / t.entry_ask) * 100, 2) if t.entry_ask else None
            )
            t.mark = bid
            if t.exit_reason and "repriced from live ask" not in t.exit_reason:
                t.exit_reason = f"{t.exit_reason} · repriced from live ask ${bid:.2f}"
            fixed += 1
        if not fixed:
            return 0
        # Rebuild cash + balance_log cash_after / pnl from chronological events
        cash = float(self.starting_cash)
        new_log: list[dict[str, Any]] = []
        events: list[tuple[str, JournalTrade, str]] = []
        for t in self.book.trades:
            events.append((t.entered_at or "", t, "ENTRY"))
            if t.status == "closed" and t.exited_at:
                events.append((t.exited_at, t, "EXIT"))
        events.sort(key=lambda x: x[0])
        for _, t, kind in events:
            if kind == "ENTRY":
                cash_before = round(cash, 2)
                cash -= float(t.cost or 0)
                cash_after = round(cash, 2)
                t.cash_before = cash_before
                t.cash_after = cash_after
                t.equity_after = round(cash_after + float(t.cost or 0), 2)
                t.balance_note = (
                    f"Balance after ENTRY: cash ${cash_after:,.2f} · sleeve equity "
                    f"${t.equity_after:,.2f} (was ${cash_before:,.2f})"
                )
                new_log.append(
                    {
                        "at": t.entered_at,
                        "action": "ENTRY",
                        "symbol": t.symbol,
                        "dte_bucket": t.dte_bucket,
                        "trade_id": t.id,
                        "cash_before": cash_before,
                        "cash_after": cash_after,
                        "equity_after": t.equity_after,
                        "debit_usd": t.cost,
                        "pnl_usd": None,
                    }
                )
            else:
                cash_before = round(cash, 2)
                proceeds = float(t.proceeds if t.proceeds is not None else (t.exit_bid or 0) * 100 * t.contracts)
                cash += proceeds
                cash_after = round(cash, 2)
                t.cash_before = cash_before
                t.cash_after = cash_after
                t.equity_after = cash_after
                t.balance_note = (
                    f"Balance after EXIT: cash/equity ${cash_after:,.2f} "
                    f"(was cash ${cash_before:,.2f} + open mark) · P&L ${t.pnl_usd:,.2f} "
                    f"({t.profit_pct:+.1f}%)"
                    if t.profit_pct is not None
                    else f"Balance after EXIT: cash/equity ${cash_after:,.2f} · P&L ${t.pnl_usd:,.2f}"
                )
                new_log.append(
                    {
                        "at": t.exited_at,
                        "action": "EXIT",
                        "symbol": t.symbol,
                        "dte_bucket": t.dte_bucket,
                        "trade_id": t.id,
                        "cash_before": cash_before,
                        "cash_after": cash_after,
                        "equity_after": cash_after,
                        "debit_usd": None,
                        "pnl_usd": t.pnl_usd,
                        "profit_pct": t.profit_pct,
                        "proceeds": proceeds,
                    }
                )
        # Subtract still-open costs already in cash path (entries deducted, exits not yet)
        self.book.cash = round(cash, 2)
        self.book.balance_log = new_log[-40:]
        self.save()
        logger.info("JOURNAL repriced %d flat exits from live-ask reasons", fixed)
        return fixed

    def mark_open(self, marks: dict[str, float]) -> None:
        """marks: contract -> mid/bid price for open MTM."""
        changed = False
        for t in self.book.trades:
            if t.status != "open":
                continue
            px = marks.get(t.contract) or marks.get(t.symbol)
            if px is None and t.mark is not None:
                px = t.mark
            if px is None and t.entry_ask:
                px = t.entry_ask
            if px is None:
                continue
            t.mark = float(px)
            t.unrealized_pnl_usd = round((t.mark - t.entry_ask) * 100 * t.contracts, 2) if t.entry_ask else 0.0
            t.unrealized_pct = round(((t.mark - t.entry_ask) / t.entry_ask) * 100, 2) if t.entry_ask else 0.0
            # Keep equity_after current while open (cash + mark)
            t.equity_after = round(
                self.book.cash + sum(
                    (x.mark or x.entry_ask) * 100 * x.contracts
                    for x in self.book.trades
                    if x.status == "open"
                ),
                2,
            )
            changed = True
        if changed:
            self.save()

    def performance(self) -> dict[str, Any]:
        closed = [t for t in self.book.trades if t.status == "closed"]
        open_t = [t for t in self.book.trades if t.status == "open"]
        wins = [t for t in closed if (t.profit_pct or 0) > 0]
        losses = [t for t in closed if (t.profit_pct or 0) <= 0]
        win_rate = (len(wins) / len(closed) * 100) if closed else None
        avg_win = (
            sum(t.profit_pct or 0 for t in wins) / len(wins) if wins else None
        )
        avg_loss = (
            sum(t.profit_pct or 0 for t in losses) / len(losses) if losses else None
        )
        avg_all = (
            sum(t.profit_pct or 0 for t in closed) / len(closed) if closed else None
        )
        realized = sum(t.pnl_usd or 0 for t in closed)
        unrealized = sum(t.unrealized_pnl_usd or 0 for t in open_t)

        # Equity curve from closed trades chronologically
        equity = self.starting_cash
        curve = [{"t": None, "equity": equity, "event": "start"}]
        for t in sorted(closed, key=lambda x: x.exited_at or x.entered_at):
            equity += t.pnl_usd or 0
            curve.append(
                {
                    "t": t.exited_at,
                    "equity": round(equity, 2),
                    "event": f"exit {t.symbol}",
                    "profit_pct": t.profit_pct,
                    "pnl_usd": t.pnl_usd,
                    "cash_after": t.cash_after,
                }
            )

        return {
            "starting_cash": self.starting_cash,
            "cash": round(self.book.cash, 2),
            "equity": round(self.book.cash + sum((t.mark or t.entry_ask) * 100 * t.contracts for t in open_t), 2),
            "closed_trades": len(closed),
            "open_trades": len(open_t),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "avg_profit_pct": round(avg_all, 2) if avg_all is not None else None,
            "avg_win_pct": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
            "realized_pnl_usd": round(realized, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "best_trade_pct": max((t.profit_pct or 0) for t in closed) if closed else None,
            "worst_trade_pct": min((t.profit_pct or 0) for t in closed) if closed else None,
            "equity_curve": curve,
            "balance_log": list(self.book.balance_log[-40:]),
            "open": [t.to_dict() for t in open_t],
            "closed": [t.to_dict() for t in sorted(closed, key=lambda x: x.exited_at or "", reverse=True)],
        }

    def sync_from_actions(
        self,
        actions: dict[str, Any],
        *,
        max_risk_usd: float = 250,
        auto_enter: bool = True,
        auto_exit: bool = True,
        require_flow_gate: bool = False,
        lottery: dict[str, Any] | None = None,
        ml6: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply BUY NOW / SELL NOW from the action board (+ lottery / ML6) to the journal."""
        entered, exited = [], []
        sell_rows = list(actions.get("sell_now") or [])
        if lottery:
            sell_rows.extend(lottery.get("sell_now") or [])
        if ml6:
            sell_rows.extend(ml6.get("sell_now") or [])
        if auto_exit:
            for sig in sell_rows:
                exited.extend(self.exit_from_signal(sig))
        if auto_enter:
            # Prefer 0DTE buys first, then weekly, then lottery, then ML6
            ordered = list(actions.get("buy_now_0dte") or []) + list(actions.get("buy_now_weekly") or [])
            if not ordered:
                ordered = list(actions.get("buy_now") or [])
            if lottery:
                ordered = list(ordered) + list(lottery.get("buy_now") or [])
            if ml6:
                ordered = list(ordered) + list(ml6.get("buy_now") or [])
            for sig in ordered:
                if str(sig.get("action") or "").upper() not in {"BUY_NOW", "BUY", ""}:
                    continue
                # Never paper-fill raw chain candidates missing a desk headline/detail
                if not (sig.get("headline") or sig.get("detail") or sig.get("exit_plan")):
                    continue
                t = self.enter_from_signal(
                    sig,
                    max_risk_usd=max_risk_usd,
                    require_flow_gate=require_flow_gate,
                )
                if t:
                    entered.append(t.to_dict())
        # Always persist so Pages export has a journal file even at 0 fills
        self.save()
        return {
            "entered": entered,
            "exited": [t.to_dict() for t in exited],
            "performance": self.performance(),
        }
