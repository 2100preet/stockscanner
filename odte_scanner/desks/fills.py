"""Paper fills for the isolated desks.

Buys fill at the mid when both bid and ask are present, otherwise at the ask
(or the last/close when that is all the tape has). Sells fill at the bid
(or the last/close). Both sides then take 0.05% of adverse slippage.
"""

from __future__ import annotations

SLIPPAGE = 0.0005  # 0.05%


def _positive(value: float | None) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def buy_fill(
    bid: float | None = None,
    ask: float | None = None,
    last: float | None = None,
    *,
    slippage: float = SLIPPAGE,
) -> float | None:
    """Fill a buy at mid, else ask, else last, then add slippage."""
    bid_px = _positive(bid)
    ask_px = _positive(ask)
    last_px = _positive(last)
    if bid_px is not None and ask_px is not None:
        base = (bid_px + ask_px) / 2.0
    elif ask_px is not None:
        base = ask_px
    elif last_px is not None:
        base = last_px
    else:
        return None
    return base * (1.0 + float(slippage))


def sell_fill(
    bid: float | None = None,
    ask: float | None = None,
    last: float | None = None,
    *,
    slippage: float = SLIPPAGE,
) -> float | None:
    """Fill a sell at the bid, else last, else ask, then subtract slippage."""
    bid_px = _positive(bid)
    ask_px = _positive(ask)
    last_px = _positive(last)
    if bid_px is not None:
        base = bid_px
    elif last_px is not None:
        base = last_px
    elif ask_px is not None:
        base = ask_px
    else:
        return None
    return base * (1.0 - float(slippage))
