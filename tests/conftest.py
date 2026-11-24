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


@pytest.fixture(autouse=True)
def _no_live_market_or_telegram_keys(monkeypatch):
    """The suite runs like CI: no quote vendor or Telegram credentials.

    A developer shell with TRADIER_ACCESS_TOKEN or POLYGON_API_KEY exported
    made tests price contracts off the live market (a BABA weekly at $3.85
    instead of the card's $0.50), and a Telegram token could send real
    messages. A test that needs a key sets its own fake value.
    """
    for name in (
        "TRADIER_ACCESS_TOKEN",
        "TRADIER_TOKEN",
        "POLYGON_API_KEY",
        "MASSIVE_API_KEY",
        "UW_API_KEY",
        "UNUSUAL_WHALES_API_KEY",
        "BULLFLOW_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _polymarket_monitor_off(monkeypatch, tmp_path):
    """No Polymarket poller thread or state file from the suite; tests opt in."""
    monkeypatch.setenv("POLYMARKET_MONITOR_ENABLED", "0")
    monkeypatch.setenv("POLYMARKET_STATE_PATH", str(tmp_path / "polymarket_monitor_state.json"))


@pytest.fixture(autouse=True)
def _gex_regime_and_lunch_shadow_in_tmp(monkeypatch, tmp_path):
    """The GEX regime cache and the Lunch shadow log never touch repo outputs/."""
    from odte_scanner.signals import gex_regime, lunch_gex_shadow

    monkeypatch.setattr(gex_regime, "DEFAULT_STORE", tmp_path / "gex_regime.json")
    monkeypatch.setattr(lunch_gex_shadow, "DEFAULT_STORE", tmp_path / "lunch_gex_shadow.json")
    monkeypatch.setenv("LUNCH_GEX_SHADOW", "0")
    lunch_gex_shadow._last_chain.clear()
