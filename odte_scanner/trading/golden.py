"""Which desk rows the Webull bridge is allowed to stage.

Golden signals (only these route when ``golden_only`` is on):

* Sure Winner **gold** tier — confidence tier ``MAX`` (desk color ``gold`` / GO HUGE).
  HIGH is amber and SOLID is soft-amber; those stay on the board and do not route.
* Unusual options flow tape rows whose tier is ``golden`` (Flow Desk Golden filter).
  Aggressive / unusual prints and chart-pattern "golden triangle" flags do not match.
"""
from __future__ import annotations

from typing import Any

SURE_WINNER_GOLD_TIERS = {"MAX"}
SURE_WINNER_GOLD_COLORS = {"gold"}


def is_sure_winner_gold(row: dict[str, Any] | None) -> bool:
    """True for the Sure Winner desk's gold tier (MAX), not HIGH/SOLID."""
    if not isinstance(row, dict):
        return False
    tier = str(row.get("confidence_tier") or "").strip().upper()
    color = str(row.get("color") or "").strip().lower()
    # An explicit lower tier wins over a stray gold color or GO HUGE flag.
    if tier and tier not in SURE_WINNER_GOLD_TIERS:
        return False
    if tier in SURE_WINNER_GOLD_TIERS:
        return True
    if color in SURE_WINNER_GOLD_COLORS:
        return True
    if bool(row.get("go_huge")) and color in {"", "gold"}:
        return True
    return False


def is_golden_unusual_flow(row: dict[str, Any] | None) -> bool:
    """True for a Golden print on the unusual options flow tape.

    Matches ``tier == golden`` (or a ``golden`` flag). The chart-pattern
    ``golden: true`` boolean is a triangle label, not flow tape, so it is ignored.
    """
    if not isinstance(row, dict):
        return False
    tier = str(row.get("tier") or row.get("flow_tier") or "").strip().lower()
    if tier == "golden":
        return True
    flags = row.get("flags") or []
    if isinstance(flags, (list, tuple, set)) and any(str(flag).strip().lower() == "golden" for flag in flags):
        return True
    return False


def golden_signal_kind(row: dict[str, Any] | None) -> str | None:
    """Return ``sure_winner_gold``, ``golden_flow``, or None.

    Sure-winner gold wins when a row is both (a MAX ticket can also sit on a
    golden print). Chart-pattern golden triangles return None.
    """
    if is_sure_winner_gold(row):
        return "sure_winner_gold"
    if is_golden_unusual_flow(row):
        return "golden_flow"
    return None
