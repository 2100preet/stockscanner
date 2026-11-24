"""Polymarket unusual-flow & odds-spike monitor (alert only, public no-auth APIs).

Signals are labelled "Unusual Polymarket flow — review". They never route to
Webull paper, Sniper LIVE, or any other order path.
"""
from __future__ import annotations

ALERT_LABEL = "Unusual Polymarket flow — review"
