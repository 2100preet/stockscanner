from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.options.selector import CallCandidate

logger = logging.getLogger(__name__)


@dataclass
class PaperTrade:
    id: str
    opened_at: str
    symbol: str
    contract: str
    side: str
    contracts: int
    entry: float
    cost: float
    score: float
    thesis: str
    status: str = "open"
    exit: float | None = None
    closed_at: str | None = None
    pnl: float | None = None


@dataclass
class PaperLedger:
    cash: float
    starting_cash: float
    trades: list[PaperTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "equity": self.cash + sum(
                (t.cost if t.status == "open" else 0) for t in self.trades
            ),
            "open_trades": sum(1 for t in self.trades if t.status == "open"),
            "closed_trades": sum(1 for t in self.trades if t.status == "closed"),
            "realized_pnl": sum(t.pnl or 0 for t in self.trades if t.status == "closed"),
            "trades": [asdict(t) for t in self.trades],
        }


class PaperTrader:
    def __init__(self, starting_cash: float, ledger_path: str | Path):
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.ledger = PaperLedger(
                cash=float(raw.get("cash", starting_cash)),
                starting_cash=float(raw.get("starting_cash", starting_cash)),
                trades=[PaperTrade(**t) for t in raw.get("trades", [])],
            )
        else:
            self.ledger = PaperLedger(cash=starting_cash, starting_cash=starting_cash)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.ledger.to_dict(), indent=2))

    def open_call(
        self,
        candidate: CallCandidate,
        *,
        contracts: int = 1,
        max_risk_usd: float = 150,
        max_trades_today: int = 2,
    ) -> PaperTrade | None:
        today = datetime.now(timezone.utc).date().isoformat()
        opened_today = sum(
            1 for t in self.ledger.trades if t.opened_at.startswith(today) and t.side == "buy_call"
        )
        if opened_today >= max_trades_today:
            logger.info("Max paper trades for today reached (%s)", max_trades_today)
            return None

        cost = candidate.ask * 100 * contracts
        if cost > max_risk_usd:
            # shrink to 1 contract if still within risk, else skip
            contracts = 1
            cost = candidate.ask * 100
            if cost > max_risk_usd:
                logger.info(
                    "Skip %s — premium $%.2f exceeds risk cap $%.2f",
                    candidate.symbol,
                    cost,
                    max_risk_usd,
                )
                return None

        if cost > self.ledger.cash:
            logger.info("Insufficient paper cash for %s", candidate.symbol)
            return None

        trade = PaperTrade(
            id=f"{candidate.symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            opened_at=datetime.now(timezone.utc).isoformat(),
            symbol=candidate.symbol,
            contract=candidate.contract,
            side="buy_call",
            contracts=contracts,
            entry=candidate.ask,
            cost=cost,
            score=candidate.score,
            thesis=candidate.thesis,
        )
        self.ledger.cash -= cost
        self.ledger.trades.append(trade)
        self.save()
        logger.info(
            "PAPER BUY %s x%s @ %.2f (cost $%.2f) score=%.1f",
            candidate.contract,
            contracts,
            candidate.ask,
            cost,
            candidate.score,
        )
        return trade

    def close_trade(self, trade_id: str, exit_price: float) -> PaperTrade | None:
        for t in self.ledger.trades:
            if t.id == trade_id and t.status == "open":
                proceeds = exit_price * 100 * t.contracts
                t.exit = exit_price
                t.pnl = proceeds - t.cost
                t.status = "closed"
                t.closed_at = datetime.now(timezone.utc).isoformat()
                self.ledger.cash += proceeds
                self.save()
                return t
        return None
