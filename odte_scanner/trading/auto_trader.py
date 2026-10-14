"""Route Signal Desk recommendations to Webull by trade type.

Default (golden_only):
  sure_winner → Sure Winner gold tier only (confidence MAX / color gold)
  golden_flow → Unusual options flow tape rows with tier golden

Other desks stay on the board and are not staged. HIGH/SOLID sure winners
and aggressive/unusual flow prints are included in that exclusion.

When golden_only is false, the older desk router is unchanged:
  lottery  → 0DTE/1DTE BUY_NOW / SELL_NOW calls
  odte     → 0DTE action-board calls
  weekly   → weekly action-board calls
  swing    → swing quality / action-board calls
  challenge→ ENTRY / EXIT calls or puts ($1k→$1M sleeve)
  and the 100% hist-win gate applies. That gate is a historical filter,
  not a guarantee. Golden-only routing does not also require 100% hist-win,
  because Golden flow prints have no hist sample.
"""
from __future__ import annotations

import logging
from typing import Any

from odte_scanner.trading.golden import golden_signal_kind
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


def _row_has_option_fields(row: dict[str, Any], *price_keys: str) -> tuple[bool, str]:
    """Field check used before staging so incomplete golden prints are not ledger spam."""
    if not str(row.get("symbol") or "").strip():
        return False, "symbol required"
    if _f(row.get("strike")) is None:
        return False, "strike required"
    expiry = str(row.get("expiry") or "").strip()
    if len(expiry) < 10 or expiry[4:5] != "-" or expiry[7:8] != "-":
        return False, "expiry must be YYYY-MM-DD"
    price = None
    for key in price_keys:
        price = _f(row.get(key))
        if price is not None and price > 0:
            break
    if price is None or price <= 0:
        return False, "limit price required (Webull options do not accept MARKET)"
    return True, ""


def _option_order_ready(intent: WebullOrderIntent) -> tuple[bool, str]:
    """Reject rows that cannot become a Webull single-leg option order."""
    from odte_scanner.trading.webull import build_option_place_order

    try:
        build_option_place_order(intent)
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def _golden_rows(
    sure_winners: dict[str, Any] | None,
    option_flow: dict[str, Any] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Sure Winner tickets + unusual-flow prints. The golden gate filters these."""
    rows: list[dict[str, Any]] = []
    if isinstance(sure_winners, dict):
        rows.extend([r for r in (sure_winners.get("winners") or []) if isinstance(r, dict)])
        rows.extend([r for r in (sure_winners.get("buy_now") or []) if isinstance(r, dict)])
    if isinstance(option_flow, dict):
        prints = option_flow.get("prints")
        if not prints:
            prints = list(option_flow.get("golden") or [])
        rows.extend([r for r in (prints or []) if isinstance(r, dict)])
    elif isinstance(option_flow, list):
        rows.extend([r for r in option_flow if isinstance(r, dict)])
    return rows


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
        golden_only: bool = True,
    ):
        self.broker = broker
        self.require_perfect_hist = bool(require_perfect_hist)
        self.min_hist_win_pct = float(min_hist_win_pct)
        self.min_hist_win_samples = int(min_hist_win_samples)
        self.golden_only = bool(golden_only)
        merged = {
            "lottery": True,
            "odte": True,
            "weekly": True,
            "swing": True,
            "challenge": True,
            "sure_winner": True,
            "golden_flow": True,
        }
        if desks:
            merged.update(desks)
        self.desks = merged
        self.max_contracts = max(1, int(max_contracts))
        self.max_orders_per_sync = max(1, int(max_orders_per_sync))
        self.filtered: list[dict[str, Any]] = []

    def _already_open(
        self,
        symbol: str,
        desk: str,
        right: str = "C",
        contract: str | None = None,
    ) -> bool:
        sym = symbol.upper()
        occ = str(contract or "")
        for o in self.broker.orders:
            if o.status not in ("staged", "dry_run", "submitted"):
                continue
            if o.action != "BUY":
                continue
            if occ and o.contract and occ == o.contract:
                return True
            if o.symbol == sym and o.desk == desk and o.right == right.upper():
                if occ and o.contract and occ != o.contract:
                    continue
                return True
        return False

    def _open_golden_buys(self) -> list[WebullOrderIntent]:
        out: list[WebullOrderIntent] = []
        for o in self.broker.orders:
            if o.action != "BUY" or o.status not in ("staged", "dry_run", "submitted"):
                continue
            if (o.meta or {}).get("golden_kind"):
                out.append(o)
        return out

    def _matches_open_golden(self, row: dict[str, Any], right: str) -> bool:
        sym = str(row.get("symbol") or "").upper()
        contract = str(row.get("contract") or "")
        right_u = str(right or "C").upper()
        for o in self._open_golden_buys():
            if o.symbol != sym:
                continue
            if contract and o.contract and contract == o.contract:
                return True
            if o.right == right_u and not (contract and o.contract and contract != o.contract):
                if not contract or not o.contract:
                    return True
        return False

    @staticmethod
    def _order_key(row: dict[str, Any], right: str, action: str) -> str:
        contract = str(row.get("contract") or "")
        if contract:
            return f"{action}|occ:{contract}"
        return (
            f"{action}|{str(row.get('symbol') or '').upper()}|{right}|"
            f"{row.get('expiry')}|{row.get('strike')}"
        )

    def _note_filtered(self, row: dict[str, Any], reason: str) -> None:
        if len(self.filtered) >= 80:
            return
        self.filtered.append(
            {
                "symbol": str(row.get("symbol") or "").upper(),
                "reason": reason,
                "tier": row.get("confidence_tier") or row.get("tier") or row.get("color"),
            }
        )

    def _route_buy(
        self,
        *,
        desk: str,
        row: dict[str, Any],
        right: str = "C",
        price_key: str = "ask",
    ) -> WebullOrderIntent | None:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return None
        kind = golden_signal_kind(row)
        if self.golden_only:
            if kind is None:
                self._note_filtered(row, "not a golden signal")
                return None
            desk = "sure_winner" if kind == "sure_winner_gold" else "golden_flow"
            ready_row, missing = _row_has_option_fields(row, price_key, "ask", "mid", "mark")
            if not ready_row:
                self._note_filtered(row, missing)
                return None
        if not self.desks.get(desk, False):
            return None
        if self._already_open(sym, desk, right, str(row.get("contract") or "") or None):
            return None

        ok, why = (True, "gate off")
        if self.golden_only and kind:
            ok, why = True, f"golden gate: {kind}"
        elif self.require_perfect_hist:
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
            limit_price=_f(row.get(price_key) or row.get("ask") or row.get("mid") or row.get("mark")),
            reason=str(row.get("detail") or row.get("headline") or row.get("thesis") or why),
                hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
                hist_samples=_i(row.get("hist_samples") or row.get("win_samples") or row.get("hist_win_samples")),
                meta={"gate": "passed", "gate_reason": why, "golden_kind": kind},
            )
        ready, missing = _option_order_ready(intent)
        if not ready:
            intent.status = "skipped"
            intent.error = missing
            intent.updated_at = intent.created_at
            self.broker.save()
            return intent
        return self.broker.submit(intent)

    def _route_sell(
        self,
        *,
        desk: str,
        row: dict[str, Any],
        right: str = "C",
    ) -> WebullOrderIntent | None:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return None
        kind = golden_signal_kind(row)
        if self.golden_only and kind is None and not self._matches_open_golden(row, right):
            self._note_filtered(row, "not a golden signal")
            return None
        if self.golden_only:
            ready_row, missing = _row_has_option_fields(row, "bid", "ask", "mid", "mark")
            if not ready_row:
                self._note_filtered(row, missing)
                return None
        if self.golden_only and kind == "sure_winner_gold":
            desk = "sure_winner"
        elif self.golden_only and kind == "golden_flow":
            desk = "golden_flow"
        elif self.golden_only:
            desk = "golden_exit"
        if desk != "golden_exit" and not self.desks.get(desk, False):
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
            limit_price=_f(row.get("bid") or row.get("ask") or row.get("mid") or row.get("mark")),
            reason=str(row.get("detail") or row.get("headline") or row.get("exit_plan") or "SELL/EXIT"),
            hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
            hist_samples=_i(row.get("hist_samples") or row.get("win_samples")),
            meta={
                "gate": "exit",
                "gate_reason": "exit signals bypass entry hist gate",
                "golden_kind": kind or ("golden_exit" if self.golden_only else None),
            },
        )
        ready, missing = _option_order_ready(intent)
        if not ready:
            intent.status = "skipped"
            intent.error = missing
            intent.updated_at = intent.created_at
            self.broker.save()
            return intent
        return self.broker.submit(intent)

    def sync(
        self,
        *,
        actions: dict[str, Any] | None = None,
        lottery: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
        sure_winners: dict[str, Any] | None = None,
        option_flow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        self.filtered = []
        seen: set[str] = set()
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

        if self.golden_only:
            for row in _golden_rows(sure_winners, option_flow):
                if budget <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                right = str(row.get("right") or "C").upper()
                if right not in {"C", "P"}:
                    right = "C"
                _take(self._route_buy(desk="sure_winner", row=row, right=right))

        return {
            "broker": self.broker.status(),
            "status": self.broker.status(),
            "require_perfect_hist": self.require_perfect_hist,
            "min_hist_win_pct": self.min_hist_win_pct,
            "min_hist_win_samples": self.min_hist_win_samples,
            "desks": self.desks,
            "golden_only": self.golden_only,
            "filtered": self.filtered[:40],
            "filtered_n": len(self.filtered),
            "submitted": submitted,
            "skipped": skipped[:40],
            "submitted_n": len(submitted),
            "skipped_n": len(skipped),
            "recent": self.broker.recent(40),
            "activity": self.broker.activity(40),
            "disclaimer": self.broker.status()["disclaimer"],
        }
