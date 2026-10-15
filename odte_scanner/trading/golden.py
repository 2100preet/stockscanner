"""Which desk rows the Webull bridge is allowed to stage.

Auto-trade follows the Sure Winner card the UI paints gold (``sure-max`` and
``sure-high``). That is MAX and HIGH. SOLID stays on the board. Unusual-flow
Golden prints stay on the Flow desk and are not routed.

The card class lives in :data:`SURE_WINNER_CARD_JS`. The page injects that
function, and :func:`sure_winner_card_class` is the same rule in Python.
"""
from __future__ import annotations

from typing import Any

# Classes whose direction label uses --sure-gold in the desk stylesheet.
GOLD_SURE_WINNER_CLASSES = {"sure-max", "sure-high"}

# Injected into the desk page. Keep the branches in lockstep with
# sure_winner_card_class() below.
SURE_WINNER_CARD_JS = """
function sureWinnerCardClass(row) {
  row = row || {};
  var tier = String(row.confidence_tier || "").toUpperCase();
  var goHuge = !!row.go_huge;
  var desk = String(row._desk || row.desk || "");
  var action = String(row.action || "").toUpperCase();
  var legacyDesk = desk.toLowerCase().replace(/\s+/g, "") === ("sure" + "winner");
  var isSure = !!(row.sure_winner || legacyDesk || desk === "Gold Setup" || tier === "MAX" || tier === "HIGH" || action.indexOf("SURE") >= 0);
  if (!isSure) return "";
  if (tier === "MAX" || goHuge) return "sure-max";
  if (tier === "HIGH") return "sure-high";
  return "sure-solid";
}
function sureWinnerCardIsGold(row) {
  var cls = sureWinnerCardClass(row);
  return cls === "sure-max" || cls === "sure-high";
}
""".strip()


def sure_winner_card_class(row: dict[str, Any] | None) -> str:
    """CSS class the desk uses for a Sure Winner card.

    Mirrors ``sureWinnerCardClass`` in the page:
    MAX or ``go_huge`` → ``sure-max``, HIGH → ``sure-high``, other sure cards
    → ``sure-solid``. Anything that is not a sure card returns ``""``.
    """
    if not isinstance(row, dict):
        return ""
    tier = str(row.get("confidence_tier") or "").strip().upper()
    go_huge = bool(row.get("go_huge"))
    desk = str(row.get("_desk") or row.get("desk") or "")
    action = str(row.get("action") or "").upper()
    is_sure = bool(
        row.get("sure_winner")
        or desk in {"Sure Winner", "Gold Setup"}
        or tier in {"MAX", "HIGH"}
        or "SURE" in action
    )
    if not is_sure:
        return ""
    if tier == "MAX" or go_huge:
        return "sure-max"
    if tier == "HIGH":
        return "sure-high"
    return "sure-solid"


def is_sure_winner_gold(row: dict[str, Any] | None) -> bool:
    """True when the desk renders this Sure Winner card in gold (MAX or HIGH)."""
    return sure_winner_card_class(row) in GOLD_SURE_WINNER_CLASSES


def is_golden_unusual_flow(row: dict[str, Any] | None) -> bool:
    """True for a Golden print on the unusual options flow tape.

    The Flow desk still shows these. The auto-trader does not route them.
    Chart-pattern ``golden: true`` is a triangle label, not flow tape.
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
    """Return ``sure_winner_gold`` or None.

    Golden unusual-flow is intentionally not a route. A chart-pattern golden
    triangle is not a Sure Winner card either.
    """
    if is_sure_winner_gold(row):
        return "sure_winner_gold"
    return None
