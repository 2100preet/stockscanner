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
from datetime import datetime
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


# A sell limit at one cent is marketable: the exchange fills at the bid if the
# bid is higher. Webull options reject MARKET, so this is the flatten when the
# desk has no live bid (or only the entry price echoed back).
FLATTEN_LIMIT = 0.01


def _is_clock_flatten(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("detail", "headline", "reason", "exit_reason", "exit_plan")
    ).lower()
    return any(token in text for token in ("15:45", "time-stop", "time stop", "flatten"))


def _exit_limit(row: dict[str, Any], opened: WebullOrderIntent | None) -> tuple[float | None, str]:
    """Price a golden exit. Clock flattens still leave when the bid is missing or stale."""
    from odte_scanner.trading.journal import _parse_live_exit_from_reason

    parsed = _parse_live_exit_from_reason(
        " ".join(str(row.get(key) or "") for key in ("detail", "reason", "headline"))
    )
    if parsed is not None and parsed > 0:
        return parsed, "live_reason"
    entry = _f(opened.limit_price) if opened is not None else None
    clock = _is_clock_flatten(row)
    for key in ("bid", "mark", "mid", "ask"):
        price = _f(row.get(key))
        if price is None or price <= 0:
            continue
        if clock and entry is not None and abs(price - entry) <= 1e-6:
            return FLATTEN_LIMIT, "flatten_floor"
        return price, key
    if clock:
        return FLATTEN_LIMIT, "flatten_floor"
    return None, "limit price required (Webull options do not accept MARKET)"


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
        max_golden_buys_per_sync: int = 2,
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
        # Separate from the HTTP budget so sells/cancels still run when buys are capped.
        self.max_golden_buys_per_sync = max(0, int(max_golden_buys_per_sync))
        self.filtered: list[dict[str, Any]] = []
        self._closed_contracts: set[str] = set()

    @staticmethod
    def _is_position_buy(order: WebullOrderIntent) -> bool:
        """A buy we can sell: a fill, a partial, or a simulated dry-run/staged row.

        A live SUBMITTED limit with no fill is not a position.
        """
        if order.action != "BUY":
            return False
        if order.status in {"filled", "partial", "dry_run", "staged"}:
            return True
        if order.status == "submitted":
            return str((order.meta or {}).get("fill_state") or "") in {"filled", "partial"}
        return False

    @staticmethod
    def _is_working_buy(order: WebullOrderIntent) -> bool:
        """Resting unfilled limit, or a deferred submit that must not be duplicated."""
        if order.action != "BUY":
            return False
        if order.status == "deferred":
            return True
        if order.status != "submitted":
            return False
        return str((order.meta or {}).get("fill_state") or "working") not in {"filled", "partial"}

    def _already_open(
        self,
        symbol: str,
        desk: str,
        right: str = "C",
        contract: str | None = None,
    ) -> bool:
        sym = symbol.upper()
        occ = str(contract or "")
        blocking = {"staged", "dry_run", "submitted", "deferred", "filled", "partial"}
        for o in self.broker.orders:
            if o.status not in blocking:
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
        return [
            o
            for o in self.broker.orders
            if self._is_position_buy(o) and (o.meta or {}).get("golden_kind")
        ]

    def _working_golden_buys(self) -> list[WebullOrderIntent]:
        return [
            o
            for o in self.broker.orders
            if self._is_working_buy(o) and (o.meta or {}).get("golden_kind")
        ]

    def _golden_buy_matching(
        self,
        row: dict[str, Any],
        right: str,
        orders: list[WebullOrderIntent],
    ) -> WebullOrderIntent | None:
        """The open golden buy this sell closes, or None.

        A contract match is required when either side has an OCC symbol.
        Otherwise symbol, right, expiry, and strike must all agree.
        """
        sym = str(row.get("symbol") or "").upper()
        contract = str(row.get("contract") or "")
        right_u = str(right or "C").upper()
        if right_u not in {"C", "P"}:
            right_u = "C"
        strike = _f(row.get("strike"))
        expiry = str(row.get("expiry") or "")[:10]
        for o in orders:
            if o.symbol != sym or o.right != right_u:
                continue
            if contract or o.contract:
                if contract and o.contract and contract == o.contract:
                    return o
                continue
            if strike is None or o.strike is None or abs(float(strike) - float(o.strike)) > 1e-6:
                continue
            if not expiry or not o.expiry or expiry != str(o.expiry)[:10]:
                continue
            return o
        return None

    def _find_open_golden(self, row: dict[str, Any], right: str) -> WebullOrderIntent | None:
        """Filled, partial, or simulated golden buy this sell can close."""
        return self._golden_buy_matching(row, right, self._open_golden_buys())

    def _find_working_golden(self, row: dict[str, Any], right: str) -> WebullOrderIntent | None:
        """Unfilled golden limit. Exit cancels it instead of selling it."""
        return self._golden_buy_matching(row, right, self._working_golden_buys())

    def _matches_open_golden(self, row: dict[str, Any], right: str) -> bool:
        return self._find_open_golden(row, right) is not None

    def _sell_already_working(self, row: dict[str, Any], right: str) -> bool:
        """Skip a second SELL while one for the same option is still open."""
        sym = str(row.get("symbol") or "").upper()
        contract = str(row.get("contract") or "")
        right_u = str(right or "C").upper()
        strike = _f(row.get("strike"))
        expiry = str(row.get("expiry") or "")[:10]
        for o in self.broker.orders:
            if o.action != "SELL" or o.status not in ("staged", "dry_run", "submitted"):
                continue
            if contract and o.contract and contract == o.contract:
                return True
            if o.symbol == sym and o.right == right_u:
                if contract or o.contract:
                    continue
                same_strike = (
                    strike is not None
                    and o.strike is not None
                    and abs(float(strike) - float(o.strike)) <= 1e-6
                )
                same_expiry = bool(expiry and o.expiry and expiry == str(o.expiry)[:10])
                if same_strike and same_expiry:
                    return True
        return False

    def _flatten_when_signal_leaves(self, now: datetime | None) -> list[dict[str, Any]]:
        """Close an open golden 0DTE after 15:45 ET even if the buy row is gone.

        Take-profit, stop, and reclaim still arrive as SELL NOW rows. This pass
        only covers the clock: the contract is no longer on the board, so
        nothing else will name it.
        """
        from odte_scanner.signals.hold_rules import expiry_is_today, past_odte_flatten

        if not past_odte_flatten(now):
            return []
        rows: list[dict[str, Any]] = []
        resting = self._open_golden_buys() + self._working_golden_buys()
        for order in resting:
            probe = {
                "symbol": order.symbol,
                "right": order.right,
                "contract": order.contract,
                "expiry": order.expiry,
                "strike": order.strike,
            }
            if self._sell_already_working(probe, order.right or "C"):
                continue
            if not expiry_is_today(order.expiry, now):
                continue
            rows.append(
                {
                    **probe,
                    "dte_bucket": "0dte",
                    "headline": f"SELL NOW {order.symbol}",
                    "detail": (
                        "0DTE time-stop — flatten by 15:45 ET "
                        "(open golden contract left the board)"
                    ),
                }
            )
        return rows

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
        contract_id = str(row.get("contract") or "")
        if contract_id and contract_id in self._closed_contracts:
            return None
        if self._already_open(sym, desk, right, contract_id or None):
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
        opened: WebullOrderIntent | None = None
        limit_price = _f(row.get("bid") or row.get("ask") or row.get("mid") or row.get("mark"))
        limit_source = "row"
        quantity = self.max_contracts
        contract = str(row.get("contract") or "") or None
        expiry = str(row.get("expiry") or "") or None
        strike = _f(row.get("strike"))
        if self.golden_only:
            # A golden label on the sell row is not enough. Only close a buy
            # this bridge already staged, and sell that exact contract.
            opened = self._find_open_golden(row, right)
            if opened is None:
                working = self._find_working_golden(row, right)
                if working is None:
                    self._note_filtered(row, "sell only closes an open golden order")
                    return None
                return self._cancel_unfilled(working, row)
            if self._sell_already_working(row, right):
                return None
            limit_price, limit_source = _exit_limit(row, opened)
            if limit_price is None:
                self._note_filtered(row, limit_source)
                return None
            # The fill has to be the option we bought, not a nearby strike on the sell row.
            contract = str(opened.contract or row.get("contract") or "") or None
            expiry = str(opened.expiry or row.get("expiry") or "") or None
            strike = opened.strike if opened.strike is not None else strike
            right = str(opened.right or right or "C").upper()
            quantity = max(1, int(opened.quantity or self.max_contracts))
            if opened.status == "partial":
                filled_qty = _f((opened.meta or {}).get("filled_qty"))
                if filled_qty is not None and filled_qty >= 1:
                    quantity = max(1, int(filled_qty))
            sym = opened.symbol or sym
            desk = "golden_exit"
        elif not self.desks.get(desk, False):
            return None
        intent = self.broker.stage(
            desk=desk,
            action="SELL",
            symbol=sym,
            right=right,
            contract=contract,
            expiry=expiry,
            strike=strike,
            quantity=quantity,
            limit_price=limit_price,
            reason=str(row.get("detail") or row.get("headline") or row.get("exit_plan") or "SELL/EXIT"),
            hist_win_pct=_f(row.get("hist_win_pct") if row.get("hist_win_pct") is not None else row.get("win_pct")),
            hist_samples=_i(row.get("hist_samples") or row.get("win_samples")),
            meta={
                "gate": "exit",
                "gate_reason": "exit signals bypass entry hist gate",
                "golden_kind": "golden_exit" if self.golden_only else kind,
                "limit_source": limit_source,
                "closes_order_id": opened.id if opened is not None else None,
            },
        )
        ready, missing = _option_order_ready(intent)
        if not ready:
            intent.status = "skipped"
            intent.error = missing
            intent.updated_at = intent.created_at
            self.broker.save()
            return intent
        sold = self.broker.submit(intent)
        if (
            opened is not None
            and opened.status == "partial"
            and sold.status in {"dry_run", "submitted", "staged"}
        ):
            self.broker.cancel(opened)
        return sold

    def _cancel_unfilled(self, order: WebullOrderIntent, row: dict[str, Any]) -> WebullOrderIntent:
        """Pull a resting limit. If detail now says it filled, sell that fill instead."""
        reason = str(row.get("detail") or row.get("headline") or row.get("exit_plan") or "cancel unfilled")
        order.meta["cancel_reason"] = reason[:320]
        order.reason = reason[:320]
        result = self.broker.cancel(order)
        if result.status == "cancelled":
            return result
        self.broker.refresh_fill(order)
        if self._is_position_buy(order):
            return self._route_sell(
                desk="golden_exit",
                row=row,
                right=str(order.right or "C"),
            ) or result
        return result

    def sync(
        self,
        *,
        actions: dict[str, Any] | None = None,
        lottery: dict[str, Any] | None = None,
        challenge: dict[str, Any] | None = None,
        sure_winners: dict[str, Any] | None = None,
        option_flow: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        self.filtered = []
        seen: set[str] = set()
        budget = self.max_orders_per_sync
        golden_buys = 0

        def _take(intent: WebullOrderIntent | None) -> None:
            nonlocal budget, golden_buys
            if intent is None:
                return
            d = intent.to_dict()
            if intent.status in ("dry_run", "submitted", "staged", "cancelled", "filled", "partial"):
                submitted.append(d)
                budget -= 1
            else:
                skipped.append(d)
                # A rejected or still-deferred HTTP call still hit Webull. Count
                # it, or one cycle will fire every golden row and trip 429s.
                if intent.status in ("rejected", "deferred"):
                    budget -= 1
                    err = str(intent.error or "").upper()
                    if "ACCOUNT_ACCESS_DENIED" in err or "INVALID ACCOUNT" in err:
                        budget = 0
            if (
                intent.action == "BUY"
                and (intent.meta or {}).get("golden_kind")
                and intent.status in ("dry_run", "submitted", "staged", "deferred", "rejected", "filled")
            ):
                golden_buys += 1
            # An exit this pass closes the contract. Do not open it again
            # because the golden print is still on the tape.
            if intent.contract and (
                intent.action == "SELL"
                or (intent.action == "BUY" and intent.status == "cancelled")
            ):
                self._closed_contracts.add(str(intent.contract))

        # Fill state first, so an exit sells a fill and cancels a resting limit.
        self._closed_contracts = set()
        self.broker.refresh_working_buys()

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

        if self.golden_only and budget > 0:
            for row in self._flatten_when_signal_leaves(now):
                if budget <= 0:
                    break
                right = str(row.get("right") or "C").upper()
                if right not in {"C", "P"}:
                    right = "C"
                _take(self._route_sell(desk="golden_exit", row=row, right=right))

        # Retry 429s after exits so a flatten can cancel a deferred id first.
        # The same client_order_id is submitted again. Golden buy retries stop
        # at the per-sync cap; a deferred sell is not held back by that cap.
        if budget > 0:
            for intent in [o for o in self.broker.orders if o.status == "deferred"]:
                if budget <= 0:
                    break
                if (
                    intent.action == "BUY"
                    and (intent.meta or {}).get("golden_kind")
                    and golden_buys >= self.max_golden_buys_per_sync
                ):
                    continue
                _take(self.broker.submit(intent))

        if self.golden_only:
            for row in _golden_rows(sure_winners, option_flow):
                if not isinstance(row, dict):
                    continue
                if budget <= 0 or golden_buys >= self.max_golden_buys_per_sync:
                    if golden_signal_kind(row) is None:
                        self._note_filtered(row, "not a golden signal")
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
            "max_golden_buys_per_sync": self.max_golden_buys_per_sync,
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
