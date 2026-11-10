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


@pytest.fixture(autouse=True)
def _no_webull_history_network(monkeypatch):
    """Fake sandbox keys must not reach api.sandbox.webull.com from a test."""
    monkeypatch.setenv("WEBULL_HISTORY_RECONCILE", "0")
    monkeypatch.setenv("WEBULL_EXPIRY_CLOSEOUT", "0")


@pytest.fixture(autouse=True)
def _paper_limit_files_in_tmp(monkeypatch, tmp_path):
    """Kill switch and limit-alert dedupe never touch the repo outputs/ folder."""
    from odte_scanner.trading import risk_controls

    monkeypatch.setattr(risk_controls, "KILL_PATH", tmp_path / "paper_kill_switch.json")
    monkeypatch.setattr(risk_controls, "LIMIT_ALERTS_PATH", tmp_path / "daily_limit_alerts.json")
    monkeypatch.delenv("PAPER_ENTRY_KILL", raising=False)


@pytest.fixture(autouse=True)
def _fresh_webull_budget():
    """Each test starts with an empty shared Webull call budget."""
    from odte_scanner.trading import webull

    webull.WEBULL_BUDGET.reset()
    webull._DETAIL_LAST.clear()
    yield
    webull.WEBULL_BUDGET.reset()
    webull._DETAIL_LAST.clear()
