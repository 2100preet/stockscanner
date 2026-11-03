"""Cancel or replace an unfilled Sniper, Diamond, or Afternoon Puts buy.

The first limit sits at the mid, rounded up toward the live ask and never
above it. Each fast-loop tick walks a still-working buy up to the live ask,
and stops at a slippage cap above the original ask. A buy that is still
working after the short window, or after the entry window, is cancelled.
"""

from __future__ import annotations

from odte_scanner.trading.ticks import normalize_option_limit

CHASE_SLIPPAGE = 0.10
CHASE_MAX_AGE_SEC = 75


def _positive(value: float | None) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if number is None or number <= 0:
        return None
    return number


def repair_signal_ask(
    signal_ask: float | None,
    ask: float | None,
    bid: float | None,
    *,
    live: bool,
) -> tuple[float | None, bool]:
    """Adopt the live ask once when the stored ask is really the working limit.

    A frozen live baseline is left alone so a later print cannot lift the
    slippage cap. ``2.49`` with a ``2.78`` bid is that limit, not the market.
    """
    ask_f = _positive(ask)
    bid_f = _positive(bid)
    baseline = _positive(signal_ask)
    if live and baseline is not None:
        return baseline, True
    if baseline is not None and bid_f is not None and baseline + 0.009 < bid_f and ask_f is not None:
        return ask_f, True
    if baseline is not None:
        return baseline, live
    if ask_f is not None:
        return ask_f, True
    return signal_ask, live


def reprice_limit(
    ask: float,
    signal_ask: float | None,
    limit: float | None,
    bid: float | None = None,
) -> float | None:
    """Next limit at the live ask, capped by slippage above the original ask.

    Returns None when the working limit is already at that target. A limit
    that is still below the live bid is not in the market, so the target is
    the ask itself when the stored signal ask was that limit rather than a
    live quote. Callers mark a repaired baseline before they call this.
    """
    ask_f = _positive(ask)
    if ask_f is None:
        return None
    at_ask = normalize_option_limit(ask_f, side="BUY", cap=ask_f)
    baseline = _positive(signal_ask) or ask_f
    ceiling_raw = baseline * (1.0 + CHASE_SLIPPAGE)
    capped = min(ask_f, ceiling_raw)
    target = normalize_option_limit(capped, side="BUY", cap=capped)
    # Keep the tick-legal ask when it is inside the ceiling. The ceiling
    # path above already applied the cap. ``at_ask`` wins only when the
    # cap math rounded the target off the ask while the ask itself is legal
    # and still under the raw ceiling.
    if at_ask <= ceiling_raw + 1e-9 and at_ask >= target:
        target = at_ask
    if limit is not None and target <= float(limit) + 0.001:
        return None
    return target


def chase_action(
    *,
    limit: float | None,
    ask: float | None,
    signal_ask: float | None,
    age_sec: float | None,
    past_window: bool,
    bid: float | None = None,
) -> str:
    """``cancel``, ``cap``, ``reprice``, or ``hold``."""
    if past_window or (age_sec is not None and age_sec >= CHASE_MAX_AGE_SEC):
        return "cancel"
    if ask is None or limit is None or ask <= 0 or limit <= 0:
        return "hold"
    if reprice_limit(float(ask), signal_ask, limit, bid=bid) is not None:
        return "reprice"
    baseline = _positive(signal_ask) or float(limit)
    if float(ask) > baseline * (1.0 + CHASE_SLIPPAGE) + 1e-9:
        return "cap"
    return "hold"
