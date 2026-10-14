"""Keep session rules off the wall clock.

Entry and flatten decisions take an explicit ``now``. When a test omits it,
the desk clock is 11:00 ET on a weekday so the suite does not start failing
after 15:45.
"""
from __future__ import annotations

import pytest

from tests.session_clock import session_et


@pytest.fixture(autouse=True)
def _weekday_morning_when_now_is_omitted(monkeypatch):
    from odte_scanner.signals import hold_rules

    real = hold_rules._et_now
    morning = session_et(11, 0)

    def frozen(now=None):
        if now is None:
            return morning
        return real(now)

    monkeypatch.setattr(hold_rules, "_et_now", frozen)
