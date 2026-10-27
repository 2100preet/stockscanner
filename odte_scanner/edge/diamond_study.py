"""Recorded Diamond book. Routed only while this sample's average stays non-negative.

Yahoo 5-minute bars, 2026-08-04 through 2026-10-27 (60 sessions). The option
percent is a 0.50 delta model, not a quoted fill. The interval and the
first-half / second-half split are recomputed from the stored trades so a
hand-edited summary cannot drift from the trades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odte_scanner.signals.diamond import PRIMARY_RULES, RULES_TEXT, summarize

_PATH = Path(__file__).with_name("diamond_sample.json")
_BLOB: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))

if _BLOB.get("rules_id") != PRIMARY_RULES.rules_id():
    raise RuntimeError(
        f"Diamond sample {_BLOB.get('rules_id')} does not match live rules {PRIMARY_RULES.rules_id()}"
    )

SAMPLE_TRADES: list[dict[str, Any]] = list(_BLOB["trades"])
SUMMARY: dict[str, Any] = summarize(SAMPLE_TRADES)
RECORDED_AT = str(_BLOB.get("recorded_at") or "")
SOURCE = str(_BLOB.get("source") or "")
RULES = RULES_TEXT
HELD_TO_1430: dict[str, Any] = dict(_BLOB.get("held_to_1430") or {})
TRAIL: dict[str, Any] = dict(_BLOB.get("trail") or {})
UNCAPPED: dict[str, Any] = dict(_BLOB.get("uncapped") or {})
TODAY: list[dict[str, Any]] = list(_BLOB.get("today") or [])
TODAY_SENT: list[dict[str, Any]] = list(_BLOB.get("today_sent") or [])

_mean = SUMMARY.get("expectancy")
_lo = SUMMARY.get("lo")
_h1 = SUMMARY.get("h1")
_h2 = SUMMARY.get("h2")
# The ship bar for this book is a non-negative average. The recorded sample
# also has a 95% interval above zero and both halves positive. If a future
# re-record loses the average, the route turns off by itself.
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
    """Edge-tab payload. Dollars are the model, labeled as such."""
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
        "held_to_1430": HELD_TO_1430.get("expectancy"),
        "trail": TRAIL.get("expectancy"),
        "uncapped": UNCAPPED.get("expectancy"),
        "note": (
            "In-sample 60-session model. The 10:30 CT flat on the first 3 names "
            "is the live book. Holding those same entries to 2:30 PM CT had a "
            "negative first half, and the trail did not clear zero. This is not "
            "a chain fill and it is not a promise."
        ),
    }
