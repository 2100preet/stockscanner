"""Recorded Sniper book. Routed only while this sample stays non-negative.

Yahoo 5-minute bars, 2026-08-04 through 2026-10-27 (60 sessions). The option
percent is a 0.50 delta model, not a quoted fill. The interval and the
first-half / second-half split are recomputed from the stored trades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odte_scanner.signals.sniper import PRIMARY_RULES, RULES_TEXT, summarize

_PATH = Path(__file__).with_name("sniper_sample.json")
_BLOB: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))

if _BLOB.get("rules_id") != PRIMARY_RULES.rules_id():
    raise RuntimeError(
        f"Sniper sample {_BLOB.get('rules_id')} does not match live rules {PRIMARY_RULES.rules_id()}"
    )

SAMPLE_TRADES: list[dict[str, Any]] = list(_BLOB["trades"])
SUMMARY: dict[str, Any] = summarize(SAMPLE_TRADES)
RECORDED_AT = str(_BLOB.get("recorded_at") or "")
SOURCE = str(_BLOB.get("source") or "")
RULES = RULES_TEXT
UNCAPPED: dict[str, Any] = dict(_BLOB.get("uncapped") or {})
TODAY: list[dict[str, Any]] = list(_BLOB.get("today") or [])
TODAY_SENT: list[dict[str, Any]] = list(_BLOB.get("today_sent") or [])

_mean = SUMMARY.get("expectancy")
_lo = SUMMARY.get("lo")
_h1 = SUMMARY.get("h1")
_h2 = SUMMARY.get("h2")
# Same ship bar as Diamond: average at or above zero, 95% interval above zero,
# and both halves positive. A later re-record that loses the average turns
# Telegram and Webull off by itself.
ROUTED = bool(
    isinstance(_mean, (int, float))
    and _mean >= 0
    and isinstance(_lo, (int, float))
    and _lo > 0
    and isinstance(_h1, (int, float))
    and _h1 > 0
    and isinstance(_h2, (int, float))
    and _h2 > 0
)


def public_summary() -> dict[str, Any]:
    """Edge-tab payload. The percent is the model, labeled as such."""
    return {
        "routed": ROUTED,
        "rules_id": PRIMARY_RULES.rules_id(),
        "rules": RULES,
        "source": SOURCE,
        "recorded_at": RECORDED_AT,
        "n": SUMMARY.get("n"),
        "win_rate": SUMMARY.get("win_rate"),
        "expectancy": SUMMARY.get("expectancy"),
        "lo": SUMMARY.get("lo"),
        "hi": SUMMARY.get("hi"),
        "h1": SUMMARY.get("h1"),
        "h2": SUMMARY.get("h2"),
        "underlying": SUMMARY.get("underlying"),
        "dollars": SUMMARY.get("dollars"),
        "uncapped": UNCAPPED.get("expectancy"),
        "or_stop": _BLOB.get("or_stop_cap3_expectancy"),
        "calls_only": _BLOB.get("calls_only_cap3_expectancy"),
        "puts_only": _BLOB.get("puts_only_cap3_expectancy"),
        "note": (
            "In-sample 60-session model. The live book is a 15-minute opening-range "
            "retest with the 9/21 EMA stack, calls and puts, a full exit at +25%, "
            "a −40% stop, and a 10:30 CT flat, capped at 3 names. Selling half at "
            "+25% and trailing the rest made less on these same entries. The book "
            "without the cap is weaker. A stop back inside the opening range was "
            "tested and dropped. This is not a chain fill and it is not a promise."
        ),
    }
