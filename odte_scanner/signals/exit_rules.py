"""Per-desk exit rules from the October 2026 exit study.

The study replayed logged entries on Yahoo 1-minute option bars. These defaults
are what that replay found, and ``config.yaml`` can override any desk.

``premium`` sells on the take-profit / stop-loss percents.
``eod`` does not take those premium exits. The 15:45 ET clock for a same-session
bucket still runs in ``decide_exit`` — the study's gain on that bucket came from
not stopping at −50%, not from holding through the close.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExitRule:
    mode: str  # premium | eod
    take_profit_pct: float
    stop_loss_pct: float


# Best same-sample rule per desk. Desks with no option path keep the prior
# +80% / −50% premium rule. See docs/exit_study.md.
DEFAULT_EXIT_RULES: dict[str, ExitRule] = {
    "sure_winner": ExitRule("premium", 80.0, 50.0),
    "odte": ExitRule("eod", 80.0, 50.0),
    "weekly": ExitRule("premium", 100.0, 50.0),
    "swing": ExitRule("premium", 80.0, 50.0),
    "lottery": ExitRule("premium", 80.0, 50.0),
    "challenge": ExitRule("premium", 100.0, 50.0),
    "rip": ExitRule("premium", 30.0, 50.0),
}


def load_exit_rules(raw: dict[str, Any] | None) -> dict[str, ExitRule]:
    """Overlay a config ``exits:`` map on the study defaults."""
    out = dict(DEFAULT_EXIT_RULES)
    for key, val in (raw or {}).items():
        if not isinstance(val, dict):
            continue
        name = str(key).strip().lower()
        base = out.get(name, ExitRule("premium", 80.0, 50.0))
        mode = str(val.get("mode") or base.mode).strip().lower()
        if mode not in {"premium", "eod"}:
            mode = base.mode
        out[name] = ExitRule(
            mode=mode,
            take_profit_pct=float(val.get("take_profit_pct", base.take_profit_pct)),
            stop_loss_pct=float(val.get("stop_loss_pct", base.stop_loss_pct)),
        )
    return out


def rule_for_bucket(rules: dict[str, ExitRule] | None, bucket: Any) -> ExitRule:
    """Map an action-board DTE bucket onto a desk rule."""
    table = rules or DEFAULT_EXIT_RULES
    text = str(bucket or "odte").lower()
    if "week" in text:
        key = "weekly"
    elif "swing" in text or "leap" in text:
        key = "swing"
    elif "lot" in text:
        key = "lottery"
    elif "challenge" in text:
        key = "challenge"
    else:
        key = "odte"
    return table.get(key, DEFAULT_EXIT_RULES["odte"])


def challenge_uses_tp100(rules: dict[str, ExitRule] | None) -> bool:
    rule = (rules or DEFAULT_EXIT_RULES).get("challenge", DEFAULT_EXIT_RULES["challenge"])
    return rule.mode == "premium" and float(rule.take_profit_pct) >= 100.0
