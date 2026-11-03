"""Option limit ticks so Webull accepts the price.

Premiums under $3.00 use $0.01. Premiums at or above $3.00 use $0.05.
A buy is rounded onto that grid and never above the live ask. A rejected
price is re-rounded once, one tick more conservative.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

NICKEL_CENTS = 300


def _cents(price: float) -> int:
    """Nearest cent, half up. Avoids binary float drift on nickel prices."""
    try:
        number = float(price)
    except (TypeError, ValueError):
        return 0
    if number != number or number <= 0:  # NaN
        return 0
    quantized = (Decimal(str(number)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(quantized)


def tick_cents(cents: int) -> int:
    """$0.01 below $3.00, $0.05 at and above $3.00."""
    return 1 if int(cents) < NICKEL_CENTS else 5


def _floor_tick(cents: int) -> int:
    tick = tick_cents(cents)
    snapped = (int(cents) // tick) * tick
    if snapped >= NICKEL_CENTS and snapped % 5 != 0:
        snapped = (snapped // 5) * 5
    return snapped


def _ceil_tick(cents: int) -> int:
    tick = tick_cents(cents)
    if cents > 0 and cents % tick == 0 and (cents < NICKEL_CENTS or cents % 5 == 0):
        return int(cents)
    snapped = ((int(cents) + tick - 1) // tick) * tick
    if snapped >= NICKEL_CENTS and snapped % 5 != 0:
        snapped = ((snapped + 4) // 5) * 5
    return snapped


def normalize_option_limit(price: float, *, side: str = "BUY", cap: float | None = None) -> float:
    """Snap ``price`` onto the legal tick.

    Buys round up toward the market, then down to the tick at or below ``cap``
    (the live ask) so the limit is never through the ask and never off-tick.
    Sells round down so a stop still rests on a legal price.
    """
    cents = _cents(price)
    if cents <= 0:
        return 0.0
    side_u = str(side or "BUY").upper()
    if side_u == "BUY":
        target = _ceil_tick(cents)
        if cap is not None and float(cap) > 0:
            ceiling = _floor_tick(_cents(cap))
            if ceiling <= 0:
                ceiling = tick_cents(_cents(cap))
            target = min(target, ceiling)
            if target % tick_cents(target) != 0:
                target = _floor_tick(target)
        elif target >= NICKEL_CENTS and target % 5 != 0:
            target = _ceil_tick(target)
        if target <= 0:
            target = tick_cents(max(cents, 1))
        return target / 100.0
    target = _floor_tick(cents)
    if target <= 0:
        target = tick_cents(max(cents, 1))
    return target / 100.0


def initial_buy_limit(bid: float | None, ask: float) -> float:
    """Mid rounded up toward the ask, and never above the ask.

    A missing bid uses the ask itself. The result is on the legal tick.
    """
    ask_f = float(ask)
    if ask_f <= 0:
        return 0.0
    bid_f: float | None
    try:
        bid_f = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        bid_f = None
    if bid_f is None or bid_f <= 0 or bid_f >= ask_f:
        raw = ask_f
    else:
        mid = (bid_f + ask_f) / 2.0
        raw = min(ask_f, _cents(mid) / 100.0)
        # Round the mid up one cent when it was not already on a cent, still under the ask.
        if raw + 1e-9 < mid:
            raw = min(ask_f, ( _cents(mid) + 1) / 100.0)
        if raw < bid_f:
            raw = min(ask_f, bid_f)
    return normalize_option_limit(raw, side="BUY", cap=ask_f)


def rereound_after_reject(price: float, *, side: str = "BUY") -> float | None:
    """One more legal price after the broker rejects the increment.

    An off-tick price floors onto the grid. A price that was already legal
    steps one tick down so the retry is not the same order.
    """
    cents = _cents(price)
    if cents <= 0:
        return None
    floored = _floor_tick(cents)
    if floored != cents and floored > 0:
        return floored / 100.0
    tick = tick_cents(cents)
    stepped = cents - tick
    if stepped <= 0:
        return None
    stepped = _floor_tick(stepped)
    if stepped <= 0 or stepped == cents:
        return None
    return normalize_option_limit(stepped / 100.0, side=side)


def is_tick_reject(exc: BaseException) -> bool:
    """True when the broker text is about the price increment."""
    text = str(exc or "").lower()
    hints = (
        "tick",
        "increment",
        "multiple of",
        "nickel",
        "price precision",
        "invalid price",
        "price is invalid",
        "min tick",
        "tick size",
        "0.05",
    )
    return any(hint in text for hint in hints)
