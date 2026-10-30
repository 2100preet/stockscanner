"""Cancel/replace rules for unfilled Sniper and Diamond paper buys.

The first limit is the ask at submit. When the live ask runs more than 5%
through that limit, replace it with a marketable limit (ask plus a small
capped cushion). Stop replacing once the ask is 25% above the original
signal ask. Cancel a buy that is still working after 8 minutes or at the
10:30 CT entry window.
"""

from __future__ import annotations

CHASE_TRIGGER = 0.05
CHASE_CAP = 0.25
CHASE_CUSHION_PCT = 0.01
CHASE_CUSHION_CAP = 0.05
CHASE_MAX_AGE_SEC = 8 * 60


def marketable_buy_limit(ask: float) -> float:
    """Ask plus about 1%, and never more than five cents."""
    ask_f = float(ask)
    pct = round(ask_f * CHASE_CUSHION_PCT + 1e-9, 2)
    cushion = min(CHASE_CUSHION_CAP, max(0.01, pct))
    return round(ask_f + cushion, 2)


def reprice_limit(ask: float, signal_ask: float | None, limit: float | None) -> float | None:
    """Next limit, capped at 25% above the original signal ask."""
    baseline = float(signal_ask) if signal_ask and signal_ask > 0 else float(limit or 0)
    if baseline <= 0:
        return None
    ceiling = round(baseline * (1.0 + CHASE_CAP), 2)
    new_limit = min(marketable_buy_limit(ask), ceiling)
    if limit is not None and new_limit <= float(limit) + 0.009:
        return None
    return new_limit


def chase_action(
    *,
    limit: float | None,
    ask: float | None,
    signal_ask: float | None,
    age_sec: float | None,
    past_window: bool,
) -> str:
    """``cancel``, ``cap``, ``reprice``, or ``hold``."""
    if past_window or (age_sec is not None and age_sec >= CHASE_MAX_AGE_SEC):
        return "cancel"
    if ask is None or limit is None or ask <= 0 or limit <= 0:
        return "hold"
    baseline = float(signal_ask) if signal_ask and signal_ask > 0 else float(limit)
    if ask > baseline * (1.0 + CHASE_CAP) + 1e-9:
        return "cap"
    if ask > float(limit) * (1.0 + CHASE_TRIGGER) + 1e-9:
        if reprice_limit(ask, baseline, limit) is not None:
            return "reprice"
    return "hold"
