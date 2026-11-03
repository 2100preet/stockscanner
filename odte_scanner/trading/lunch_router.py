"""Paper route for Lunch-Break Credit. Sandbox only. SPX is never an order.

The primary order is one Webull vertical combo (option_strategy VERTICAL):
sell the ATM put and buy the put $2 lower, limit at the mid credit. If the
sandbox rejects that body as an unsupported multi-leg strategy, the desk
buys the long put first and then sells the short put, and both legs share
one spread id. Other desks are not touched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from odte_scanner.signals.lunch_credit import (
    ORDER_PATH,
    WEBULL_SYMBOLS,
    credit_chase_action,
    exit_reason,
    natural_credit,
    natural_debit,
    next_credit_limit,
    opening_limit,
    spread_mid,
)
from odte_scanner.time_cst import CT
from odte_scanner.trading.ticks import normalize_option_limit
from odte_scanner.trading.webull import (
    WebullBroker,
    WebullOrderIntent,
    build_vertical_spread_order,
    multi_leg_rejected,
)

logger = logging.getLogger(__name__)

DESK = "lunch_credit"


def spread_contract(symbol: str, expiry: str, short_strike: float, long_strike: float) -> str:
    return f"{str(symbol).upper()}|{str(expiry)[:10]}|{float(short_strike):g}/{float(long_strike):g}|P"


def fallback_leg_orders(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Buy the long put first, then sell the short put. One spread, two orders."""
    long = card.get("long") if isinstance(card.get("long"), dict) else {}
    short = card.get("short") if isinstance(card.get("short"), dict) else {}
    return [
        {
            "action": "BUY",
            "leg": "long",
            "symbol": card.get("symbol"),
            "strike": long.get("strike") if long.get("strike") is not None else card.get("long_strike"),
            "expiry": long.get("expiry") or card.get("expiry"),
            "contract": long.get("contract"),
            "bid": long.get("bid"),
            "ask": long.get("ask"),
            "limit_price": long.get("ask"),
        },
        {
            "action": "SELL",
            "leg": "short",
            "symbol": card.get("symbol"),
            "strike": short.get("strike") if short.get("strike") is not None else card.get("short_strike"),
            "expiry": short.get("expiry") or card.get("expiry"),
            "contract": short.get("contract"),
            "bid": short.get("bid"),
            "ask": short.get("ask"),
            "limit_price": short.get("bid"),
        },
    ]


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _leg(card: dict[str, Any], name: str) -> dict[str, Any]:
    row = card.get(name)
    return row if isinstance(row, dict) else {}


def _working(order: WebullOrderIntent) -> bool:
    if order.status in {"dry_run", "submitted", "staged", "deferred"}:
        return str((order.meta or {}).get("fill_state") or "working") not in {"filled", "partial"}
    return False


def _filled(order: WebullOrderIntent) -> bool:
    if order.status in {"filled", "partial"}:
        return True
    return str((order.meta or {}).get("fill_state") or "") in {"filled", "partial"}


def _is_lunch(order: WebullOrderIntent) -> bool:
    if str(order.desk or "") == DESK:
        return True
    return str((order.meta or {}).get("golden_kind") or "") == DESK


def _age_sec(order: WebullOrderIntent, now: datetime) -> float:
    raw = str((order.meta or {}).get("chase_started_at") or order.created_at or "")
    if not raw:
        return 0.0
    try:
        started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()


class LunchRouter:
    """Stage, chase, and close Lunch-Break Credit spreads on the paper broker."""

    def __init__(self, broker: WebullBroker, *, enabled: bool = True):
        self.broker = broker
        self.enabled = bool(enabled)

    def sync(self, board: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self.broker._session_now = moment
        book = board if isinstance(board, dict) else {}
        notices: list[dict[str, Any]] = []
        self._chase(book, moment, notices)
        self._close(book, moment, notices)
        if self.enabled:
            self._open(book, moment)
        book["notices"] = notices
        book["order_path"] = ORDER_PATH
        return {
            "ok": True,
            "order_path": ORDER_PATH,
            "submitted": [order.to_dict() for order in self.broker.orders if _is_lunch(order)],
            "notices": notices,
        }

    def _cards(self, board: dict[str, Any]) -> list[dict[str, Any]]:
        return [row for row in (board.get("signals") or []) if isinstance(row, dict)]

    def _card_for(self, board: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        for row in self._cards(board):
            if str(row.get("symbol") or "").upper() == symbol:
                return row
        return None

    def _open(self, board: dict[str, Any], now: datetime) -> None:
        placed = 0
        for card in self._cards(board):
            if not card.get("send_order"):
                continue
            symbol = str(card.get("symbol") or "").upper()
            if symbol not in WEBULL_SYMBOLS or symbol == "SPX":
                continue
            if self._already(symbol):
                continue
            if placed >= 2:
                break
            self._place_combo(card, now)
            placed += 1

    def _already(self, symbol: str) -> bool:
        for order in self.broker.orders:
            if not _is_lunch(order) or str(order.symbol or "").upper() != symbol:
                continue
            if (order.meta or {}).get("leg"):
                if order.status in {"rejected", "cancelled", "skipped"}:
                    continue
                return True
            if order.status in {"rejected", "cancelled", "skipped"} and (order.meta or {}).get("order_path") == "combo_rejected":
                continue
            if order.status in {"dry_run", "submitted", "staged", "deferred", "filled", "partial"}:
                return True
            if order.status == "cancelled" and (order.meta or {}).get("chase_abandoned"):
                return True
        return False

    def _place_combo(self, card: dict[str, Any], now: datetime) -> WebullOrderIntent | None:
        symbol = str(card.get("symbol") or "").upper()
        short = _leg(card, "short")
        long = _leg(card, "long")
        short_k = _num(short.get("strike") if short else None)
        if short_k is None:
            short_k = _num(card.get("short_strike"))
        long_k = _num(long.get("strike") if long else None)
        if long_k is None:
            long_k = _num(card.get("long_strike"))
        expiry = str(card.get("expiry") or (short.get("expiry") if short else "") or "")[:10]
        limit = _num(card.get("limit_price"))
        if limit is None:
            limit = opening_limit(_num(card.get("credit_mid")), _num(card.get("credit_natural")))
        if symbol == "SPX" or short_k is None or long_k is None or not expiry or limit is None or limit <= 0:
            return None
        contract = spread_contract(symbol, expiry, short_k, long_k)
        spread_id = contract
        try:
            body = build_vertical_spread_order(
                client_order_id="pending",
                symbol=symbol,
                expiry=expiry,
                short_strike=short_k,
                long_strike=long_k,
                quantity=int(card.get("contracts") or 1),
                limit_price=limit,
                opening=True,
                right="P",
            )
        except ValueError as exc:
            logger.info("lunch credit order skipped %s: %s", symbol, exc)
            return None
        meta = {
            "golden_kind": DESK,
            "exit_tier": "lunch",
            "role": "open",
            "order_path": ORDER_PATH,
            "spread_id": spread_id,
            "short_strike": short_k,
            "long_strike": long_k,
            "short_contract": short.get("contract"),
            "long_contract": long.get("contract"),
            "width": card.get("width"),
            "max_risk_usd": card.get("max_risk_usd"),
            "signal_credit": _num(card.get("credit_mid")) or limit,
            "chase_started_at": now.astimezone(timezone.utc).isoformat(),
            "openapi_body": body,
            "exit_plan": card.get("exit_plan") or "",
        }
        intent = self.broker.stage(
            desk=DESK,
            action="SELL",
            symbol=symbol,
            right="P",
            contract=contract,
            expiry=expiry,
            strike=short_k,
            quantity=int(card.get("contracts") or 1),
            limit_price=limit,
            reason=str(card.get("detail") or "")[:320],
            meta=meta,
        )
        body["client_order_id"] = str(intent.id)[:32]
        intent.meta["openapi_body"] = body
        self.broker.submit(intent)
        if intent.status == "rejected" and multi_leg_rejected(intent.error):
            intent.meta["order_path"] = "combo_rejected"
            intent.meta["exclude_pnl"] = True
            self.broker.save()
            self._place_legs(card, spread_id, now)
        return intent

    def _place_legs(self, card: dict[str, Any], spread_id: str, now: datetime) -> None:
        """Long put first, then the short put. Both carry the same spread id."""
        for leg in fallback_leg_orders(card):
            strike = _num(leg.get("strike"))
            limit = _num(leg.get("limit_price"))
            if strike is None or limit is None or limit <= 0:
                continue
            side = "BUY" if leg["action"] == "BUY" else "SELL"
            limit = normalize_option_limit(limit, side=side)
            self.broker.stage(
                desk=DESK,
                action=side,
                symbol=str(leg.get("symbol") or card.get("symbol") or ""),
                right="P",
                contract=str(leg.get("contract") or "") or None,
                expiry=str(leg.get("expiry") or card.get("expiry") or "")[:10] or None,
                strike=strike,
                quantity=int(card.get("contracts") or 1),
                limit_price=limit,
                reason="lunch credit two-leg fallback",
                meta={
                    "golden_kind": DESK,
                    "exit_tier": "lunch",
                    "role": "open",
                    "order_path": "two_leg",
                    "leg": leg["leg"],
                    "spread_id": spread_id,
                    "exclude_pnl": True,
                    "chase_started_at": now.astimezone(timezone.utc).isoformat(),
                },
            )
            self.broker.submit(self.broker.orders[0])

    def _chase(self, board: dict[str, Any], now: datetime, notices: list[dict[str, Any]]) -> None:
        from odte_scanner.signals.hold_rules import past_gold_flatten

        past = past_gold_flatten(now, "14:15")
        for order in list(self.broker.orders):
            if not _is_lunch(order) or not _working(order):
                continue
            if str(order.symbol or "").upper() == "SPX":
                order.meta["chase_abandoned"] = True
                order.meta["cancel_reason"] = "SPX is not a Webull order"
                self.broker.cancel(order)
                continue
            if (order.meta or {}).get("role") == "close":
                continue
            if (order.meta or {}).get("leg"):
                if past or _age_sec(order, now) >= 75:
                    order.meta["chase_abandoned"] = True
                    order.meta["cancel_reason"] = "not filled — cancelled"
                    self.broker.cancel(order)
                    if order.status == "cancelled":
                        notices.append(
                            {
                                "symbol": order.symbol,
                                "kind": "LunchCredit",
                                "side": "CANCEL",
                                "message": _cancel_text(order, "not filled — cancelled"),
                            }
                        )
                continue
            card = self._card_for(board, str(order.symbol or "").upper()) or {}
            short = _leg(card, "short")
            long = _leg(card, "long")
            natural = _num(card.get("credit_natural"))
            if natural is None:
                natural = natural_credit(short.get("bid") or card.get("bid"), long.get("ask"))
            baseline = _num((order.meta or {}).get("signal_credit")) or _num(order.limit_price)
            action = credit_chase_action(
                limit=_num(order.limit_price),
                natural=natural,
                baseline=baseline,
                age_sec=_age_sec(order, now),
                past_window=past,
            )
            if action == "hold":
                continue
            if action == "cap":
                order.meta["chase_capped"] = True
                self.broker.save()
                continue
            if action == "cancel":
                reason = "past 14:15 CT entry window · not filled — cancelled" if past else "not filled — cancelled"
                order.meta["chase_abandoned"] = True
                order.meta["cancel_reason"] = reason
                self.broker.cancel(order)
                if str(order.symbol or "").upper() != "SPX" and order.status == "cancelled":
                    notices.append(
                        {
                            "symbol": order.symbol,
                            "kind": "LunchCredit",
                            "side": "CANCEL",
                            "message": _cancel_text(order, reason),
                        }
                    )
                continue
            new_limit = next_credit_limit(baseline, natural, _num(order.limit_price))
            if new_limit is None:
                continue
            short_k = _num((order.meta or {}).get("short_strike")) or _num(order.strike)
            long_k = _num((order.meta or {}).get("long_strike"))
            if short_k is None or long_k is None or not order.expiry:
                continue
            previous = float(order.limit_price or 0)
            order.meta["cancel_reason"] = f"reprice {previous:.2f} -> {new_limit:.2f}"
            self.broker.cancel(order)
            if order.status != "cancelled":
                continue
            try:
                body = build_vertical_spread_order(
                    client_order_id="pending",
                    symbol=order.symbol,
                    expiry=str(order.expiry)[:10],
                    short_strike=short_k,
                    long_strike=long_k,
                    quantity=int(order.quantity or 1),
                    limit_price=new_limit,
                    opening=True,
                    right="P",
                )
            except ValueError:
                continue
            fresh = self.broker.stage(
                desk=DESK,
                action="SELL",
                symbol=order.symbol,
                right="P",
                contract=order.contract,
                expiry=order.expiry,
                strike=short_k,
                quantity=int(order.quantity or 1),
                limit_price=new_limit,
                reason=order.reason or "lunch credit reprice",
                meta={
                    "golden_kind": DESK,
                    "exit_tier": "lunch",
                    "role": "open",
                    "order_path": ORDER_PATH,
                    "spread_id": (order.meta or {}).get("spread_id") or order.contract,
                    "short_strike": short_k,
                    "long_strike": long_k,
                    "short_contract": (order.meta or {}).get("short_contract"),
                    "long_contract": (order.meta or {}).get("long_contract"),
                    "width": (order.meta or {}).get("width"),
                    "max_risk_usd": (order.meta or {}).get("max_risk_usd"),
                    "signal_credit": baseline,
                    "chase_started_at": (order.meta or {}).get("chase_started_at") or order.created_at,
                    "reprice_count": int((order.meta or {}).get("reprice_count") or 0) + 1,
                    "replaces": order.id,
                    "openapi_body": body,
                    "exit_plan": (order.meta or {}).get("exit_plan") or "",
                },
            )
            body["client_order_id"] = str(fresh.id)[:32]
            fresh.meta["openapi_body"] = body
            self.broker.submit(fresh)

    def _close(self, board: dict[str, Any], now: datetime, notices: list[dict[str, Any]]) -> None:
        clock = now.astimezone(CT).time()
        from datetime import time as clock_time

        flat = clock >= clock_time(14, 45)
        for order in list(self.broker.orders):
            if not _is_lunch(order) or not _filled(order):
                continue
            if (order.meta or {}).get("role") != "open" or (order.meta or {}).get("leg"):
                continue
            if (order.meta or {}).get("exclude_pnl"):
                continue
            if str(order.symbol or "").upper() == "SPX":
                continue
            spread_id = str((order.meta or {}).get("spread_id") or order.contract or "")
            if self._close_working(spread_id):
                continue
            credit = _num(order.fill_price) or _num((order.meta or {}).get("signal_credit"))
            if credit is None or credit <= 0:
                continue
            card = self._card_for(board, str(order.symbol or "").upper()) or {}
            short = _leg(card, "short")
            long = _leg(card, "long")
            mark = spread_mid(
                short.get("bid") if short else card.get("bid"),
                short.get("ask") if short else card.get("ask"),
                long.get("bid"),
                long.get("ask"),
            )
            if mark is None:
                mark = _num(card.get("mark"))
            if mark is None and not flat:
                continue
            reason = exit_reason(credit, float(mark or credit), flat=flat)
            if reason is None:
                continue
            debit = float(mark if mark is not None else credit)
            if reason == "target":
                debit = min(debit, credit * 0.5)
            elif reason == "stop":
                debit = max(debit, credit * 2.0)
            limit = normalize_option_limit(max(debit, 0.01), side="BUY")
            short_k = _num((order.meta or {}).get("short_strike")) or _num(order.strike)
            long_k = _num((order.meta or {}).get("long_strike"))
            if short_k is None or long_k is None:
                continue
            path = str((order.meta or {}).get("order_path") or ORDER_PATH)
            if path == "two_leg":
                self._close_legs(order, card, limit, credit, reason, now, notices)
                continue
            try:
                body = build_vertical_spread_order(
                    client_order_id="pending",
                    symbol=order.symbol,
                    expiry=str(order.expiry or "")[:10],
                    short_strike=short_k,
                    long_strike=long_k,
                    quantity=int(order.quantity or 1),
                    limit_price=limit,
                    opening=False,
                    right="P",
                )
            except ValueError:
                continue
            close = self.broker.stage(
                desk=DESK,
                action="BUY",
                symbol=order.symbol,
                right="P",
                contract=order.contract,
                expiry=order.expiry,
                strike=short_k,
                quantity=int(order.quantity or 1),
                limit_price=limit,
                reason=f"lunch credit {reason}",
                meta={
                    "golden_kind": DESK,
                    "exit_tier": "lunch",
                    "role": "close",
                    "order_path": ORDER_PATH,
                    "spread_id": spread_id,
                    "entry_price": credit,
                    "short_strike": short_k,
                    "long_strike": long_k,
                    "exit_reason": reason,
                    "openapi_body": body,
                },
            )
            body["client_order_id"] = str(close.id)[:32]
            close.meta["openapi_body"] = body
            self.broker.submit(close)
        self._announce_exits(now, notices)

    def _close_working(self, spread_id: str) -> bool:
        for order in self.broker.orders:
            if not _is_lunch(order):
                continue
            if str((order.meta or {}).get("spread_id") or "") != spread_id:
                continue
            if (order.meta or {}).get("role") != "close":
                continue
            if order.status in {"dry_run", "submitted", "staged", "deferred", "filled", "partial"}:
                return True
        return False

    def _close_legs(
        self,
        order: WebullOrderIntent,
        card: dict[str, Any],
        limit: float,
        credit: float,
        reason: str,
        now: datetime,
        notices: list[dict[str, Any]],
    ) -> None:
        """Close the short put first, then the long put."""
        short = _leg(card, "short")
        long = _leg(card, "long")
        short_k = _num((order.meta or {}).get("short_strike")) or _num(short.get("strike"))
        long_k = _num((order.meta or {}).get("long_strike")) or _num(long.get("strike"))
        spread_id = str((order.meta or {}).get("spread_id") or "")
        debit = natural_debit(short.get("ask"), long.get("bid"))
        short_limit = normalize_option_limit(float(short.get("ask") or limit), side="BUY")
        long_limit = normalize_option_limit(float(long.get("bid") or 0.01), side="SELL")
        for leg_name, strike, action, px, contract in (
            ("short", short_k, "BUY", short_limit, short.get("contract")),
            ("long", long_k, "SELL", long_limit, long.get("contract")),
        ):
            if strike is None or px is None or px <= 0:
                continue
            self.broker.stage(
                desk=DESK,
                action=action,
                symbol=order.symbol,
                right="P",
                contract=str(contract or "") or None,
                expiry=order.expiry,
                strike=float(strike),
                quantity=int(order.quantity or 1),
                limit_price=float(px),
                reason=f"lunch credit {reason} {leg_name}",
                meta={
                    "golden_kind": DESK,
                    "exit_tier": "lunch",
                    "role": "close",
                    "order_path": "two_leg",
                    "leg": leg_name,
                    "spread_id": spread_id,
                    "exclude_pnl": True,
                    "entry_price": credit,
                    "exit_reason": reason,
                },
            )
            self.broker.submit(self.broker.orders[0])
        # One scorecard row for the spread, not one per leg.
        summary = self.broker.stage(
            desk=DESK,
            action="BUY",
            symbol=order.symbol,
            right="P",
            contract=order.contract,
            expiry=order.expiry,
            strike=_num(order.strike),
            quantity=int(order.quantity or 1),
            limit_price=limit,
            reason=f"lunch credit {reason}",
            meta={
                "golden_kind": DESK,
                "exit_tier": "lunch",
                "role": "close",
                "order_path": "two_leg",
                "spread_id": spread_id,
                "entry_price": credit,
                "short_strike": short_k,
                "long_strike": long_k,
                "exit_reason": reason,
                "exit_debit": debit if debit is not None else limit,
            },
        )
        self.broker.submit(summary)
        self.broker.save()


    def _announce_exits(self, now: datetime, notices: list[dict[str, Any]]) -> None:
        """One exit note per filled close. A working close is not an exit yet."""
        from odte_scanner.alerts.lunch_credit import format_lunch_exit

        for order in self.broker.orders:
            if not _is_lunch(order) or not _filled(order):
                continue
            if (order.meta or {}).get("role") != "close" or (order.meta or {}).get("leg"):
                continue
            if (order.meta or {}).get("exclude_pnl") or (order.meta or {}).get("exit_alerted"):
                continue
            credit = _num((order.meta or {}).get("entry_price"))
            debit = _num(order.fill_price) or _num(order.limit_price)
            if credit is None or debit is None:
                continue
            order.meta["exit_alerted"] = True
            self.broker.save()
            card = {
                "symbol": order.symbol,
                "expiry": order.expiry,
                "short_strike": (order.meta or {}).get("short_strike") or order.strike,
                "long_strike": (order.meta or {}).get("long_strike"),
                "contracts": int(order.quantity or 1),
                "credit": credit,
                "exit_debit": debit,
                "reason": (order.meta or {}).get("exit_reason") or "flat",
                "entry_at": (order.meta or {}).get("entry_at_ct") or "",
                "exit_at": now.astimezone(CT).strftime("%H:%M"),
                "order_status": "filled",
                "fill_price": credit,
            }
            notices.append(
                {
                    "symbol": order.symbol,
                    "kind": "LunchCredit",
                    "side": "EXIT",
                    "message": format_lunch_exit(card),
                }
            )


def _cancel_text(order: WebullOrderIntent, reason: str) -> str:
    from odte_scanner.alerts.lunch_credit import format_lunch_cancel

    return format_lunch_cancel(order.to_dict(), reason)
