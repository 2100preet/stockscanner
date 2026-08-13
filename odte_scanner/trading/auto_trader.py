"""Route Signal Desk recommendations to Webull by trade type.

Desks:
  lottery  → 0DTE/1DTE BUY_NOW / SELL_NOW calls
  odte     → 0DTE action-board calls
  weekly   → weekly action-board calls
  swing    → swing quality / action-board calls
  challenge→ ENTRY / EXIT calls or puts ($1k→$1M sleeve)

Live gate (user request for “100% win rate”):
  Only auto-submit when hist_win_pct == 100 and samples ≥ min_samples.
  This is a *historical* quality filter — it does not guarantee future results.
"""
from __future__ import annotations

import logging
from typing import Any

from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent

logger = logging.getLogger(__name__)


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def clears_perfect_hist_win(
    row: dict[str, Any],
    *,
    min_pct: float = 100.0,
    min_samples: int = 3,
) -> tuple[bool, str]:
    """Return (ok, reason) for the 100% hist-win live gate."""
    win = _f(
        row.get("hist_win_pct")
        if row.get("hist_win_pct") is not None
        else row.get("win_pct")
    )
    n = _i(
        row.get("hist_samples")
        if row.get("hist_samples") is not None
        else row.get("win_samples")
        if row.get("win_samples") is not None
        else row.get("hist_win_samples")
    ) or 0
    if win is None:
        return False, "no hist-win sample (new/IPO names like SPCX fail this gate)"
    if n < min_samples:
        return False, f"hist n={n} < {min_samples}"
    if float(win) + 1e-9 < float(min_pct):
        return False, f"hist win {win:.0f}% < {min_pct:.0f}% perfect gate"
    return True, f"hist win {win:.0f}% n={n}"


class AutoTrader:
    def __init__(
        self,
        broker: WebullBroker,
        *,
        require_perfect_hist: bool = True,
        min_hist_win_pct: float = 100.0,
        min_hist_win_samples: int = 3,
        desks: dict[str, bool] | None = None,
        max_contracts: int = 1,
        max_orders_per_sync: int = 3,
    ):
        self.broker = broker
        self.require_perfect_hist = bool(require_perfect_hist)
        self.min_hist_win_pct = float(min_hist_win_pct)
        self.min_hist_win_samples = int(min_hist_win_samples)
        self.desks = desks or {
            "lottery": True,
            "odte": True,
            "weekly": True,
            "swing": True,
            "challenge": True,
        }
        self.max_contracts = max(1, int(max_contracts))
        self.max_orders_per_sync = max(1, int(max_orders_per_sync))

    def _already_open(self, symbol: str, desk: str, right: str = "C") -> bool:
        sym = symbol.upper()
        for o in self.broker.orders:
            if o.status not in ("staged", "dry_run", "submitted"):
                continue
            if o.action != "BUY":
                continue
            if o.symbol == sym and o.desk == desk and o.right == right.upper():
                return True
        return False

    def _route_buy(
        self,
        *,
        desk: str,
        row: dict[str, Any],
        right: str = "C",
        price_key: str = "ask",
    ) -> WebullOrderIntent | None:
        if not self.desks.get(desk, False):
            return None
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return None
        if self._already_open(sym, desk, right):
            return None

        ok, why = (True, "gate off")
        if self.require_perfect_hist:
            ok, why = clears_perfect_hist_win(
                row,
                min_pct=self.min_hist_win_pct,
                min_samples=self.min_hist_win_samples,
            )
        if not ok:
            intent = self.broker.stage(
                desk=desk,
                action="BUY",
                symbol=sym,
                right=right,
                contract=str(row.get("contract") or "") or None,
                expiry=str(row.get("expiry") or "") or None,
                strike=_f(row.get("strike")),
                quantity=self.max_contracts,
                limit_price=_f(row.get(price_key) or row.get("ask") or row.get("mark")),
                reason=str(row.get("detail") or row.get("headline") or row.get("thesis") or ""),
                hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
                hist_samples=_i(row.get("hist_samples") or row.get("win_samples") or row.get("hist_win_samples")),
                meta={"gate": "blocked", "gate_reason": why},
            )
            intent.status = "skipped"
            intent.error = f"100% hist-win gate: {why}"
            intent.updated_at = intent.created_at
            self.broker.save()
            return intent

        intent = self.broker.stage(
            desk=desk,
            action="BUY",
            symbol=sym,
            right=right,
            contract=str(row.get("contract") or "") or None,
            expiry=str(row.get("expiry") or "") or None,
            strike=_f(row.get("strike")),
            quantity=self.max_contracts,
            limit_price=_f(row.get(price_key) or row.get("ask") or row.get("mark")),
            reason=str(row.get("detail") or row.get("headline") or row.get("thesis") or why),
            hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
            hist_samples=_i(row.get("hist_samples") or row.get("win_samples") or row.get("hist_win_samples")),
            meta={"gate": "passed", "gate_reason": why},
        )
        return self.broker.submit(intent)

    def _route_sell(
        self,
        *,
        desk: str,
        row: dict[str, Any],
        right: str = "C",
    ) -> WebullOrderIntent | None:
        if not self.desks.get(desk, False):
            return None
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return None
        intent = self.broker.stage(
            desk=desk,
            action="SELL",
            symbol=sym,
            right=right,
            contract=str(row.get("contract") or "") or None,
            expiry=str(row.get("expiry") or "") or None,
            strike=_f(row.get("strike")),
            quantity=self.max_contracts,
            limit_price=_f(row.get("bid") or row.get("ask") or row.get("mark")),
            reason=str(row.get("detail") or row.get("headline") or row.get("exit_plan") or "SELL/EXIT"),
            hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
            hist_samples=_i(row.get("hist_samples") or row.get("win_samples")),
            meta={"gate": "exit", "gate_reason": "exit signals bypass entry hist gate"},
        )
        return self.broker.submit(intent)

    def sync(
        self,
        *,
        actions: dict[str, Any] | None = None,
        lottery: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        budget = self.max_orders_per_sync

        def _take(intent: WebullOrderIntent | None) -> None:
            nonlocal budget
            if intent is None:
                return
            d = intent.to_dict()
            if intent.status in ("dry_run", "submitted", "staged"):
                submitted.append(d)
                budget -= 1
            else:
                skipped.append(d)

        # --- Lottery (0DTE convex calls) ---
        if lottery and budget > 0:
            for row in lottery.get("sell_now") or []:
                if budget <= 0:
                    break
                if isinstance(row, dict):
                    _take(self._route_sell(desk="lottery", row=row, right="C"))
            for row in lottery.get("buy_now") or []:
                if budget <= 0:
                    break
                if isinstance(row, dict) and str(row.get("action") or "").upper() in (
                    "BUY_NOW",
                    "BUY",
                    "",
                ):
                    _take(self._route_buy(desk="lottery", row=row, right="C"))

        # --- Main action board by DTE bucket ---
        if actions and budget > 0:
            for row in actions.get("sell_now") or []:
                if budget <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                bucket = str(row.get("dte_bucket") or row.get("horizon") or "odte").lower()
                desk = "weekly" if "week" in bucket else ("swing" if "swing" in bucket else "odte")
                right = str(row.get("right") or "C").upper()
                if right not in {"C", "P"}:
                    right = "C"
                _take(self._route_sell(desk=desk, row=row, right=right))
            for row in actions.get("buy_now") or []:
                if budget <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                bucket = str(row.get("dte_bucket") or row.get("horizon") or "odte").lower()
                desk = "weekly" if "week" in bucket else ("swing" if "swing" in bucket else "odte")
                right = str(row.get("right") or "C").upper()
                if right not in {"C", "P"}:
                    right = "C"
                _take(self._route_buy(desk=desk, row=row, right=right))

        # --- Challenge sleeve (calls & puts) ---
        if challenge and budget > 0:
            for row in challenge.get("exit") or []:
                if budget <= 0:
                    break
                if isinstance(row, dict):
                    _take(
                        self._route_sell(
                            desk="challenge",
                            row=row,
                            right=str(row.get("right") or "C"),
                        )
                    )
            for row in list(challenge.get("entry") or []) + [
                t
                for t in (challenge.get("tickets") or [])
                if isinstance(t, dict) and str(t.get("action") or "").upper() == "ENTRY"
            ]:
                if budget <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                # Dedupe if already in entry list
                _take(
                    self._route_buy(
                        desk="challenge",
                        row=row,
                        right=str(row.get("right") or "C"),
                        price_key="ask",
                    )
                )

        return {
            "broker": self.broker.status(),
            "status": self.broker.status(),
            "require_perfect_hist": self.require_perfect_hist,
            "min_hist_win_pct": self.min_hist_win_pct,
            "min_hist_win_samples": self.min_hist_win_samples,
            "desks": self.desks,
            "submitted": submitted,
            "skipped": skipped[:40],
            "submitted_n": len(submitted),
            "skipped_n": len(skipped),
            "recent": self.broker.recent(40),
            "activity": self.broker.activity(40),
            "disclaimer": self.broker.status()["disclaimer"],
        }
