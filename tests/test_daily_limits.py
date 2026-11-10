"""Daily loss limits and the kill switch block new paper entries only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401
from odte_scanner.trading import risk_controls as rc
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker
from tests.session_clock import iso_day, session_et


def _trip(broker, desk, kind, symbol, entry, exit_px, at, qty=1):
    expiry = iso_day(at)
    contract = f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00100000"
    buy = broker.stage(desk=desk, action="BUY", symbol=symbol, contract=contract, expiry=expiry, strike=100,
                       quantity=qty, limit_price=entry, meta={"golden_kind": kind})
    buy.status, buy.fill_price = "filled", entry
    sell = broker.stage(desk="golden_exit", action="SELL", symbol=symbol, contract=contract, expiry=expiry,
                        strike=100, quantity=qty, limit_price=exit_px, meta={"closes_order_id": buy.id,
                                                                            "entry_price": entry})
    sell.status, sell.fill_price = "filled", exit_px
    sell.updated_at = at.astimezone(timezone.utc).isoformat()
    broker.save()


def _broker(tmp_path):
    return WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", submit_interval_sec=0)


def test_two_sniper_losers_block_sniper_only(tmp_path: Path):
    now = session_et(11, 0)
    b = _broker(tmp_path)
    _trip(b, "sniper", "sniper", "SPY", 2.0, 1.5, now - timedelta(minutes=30))
    _trip(b, "sniper", "sniper", "IWM", 1.0, 0.8, now - timedelta(minutes=20))
    block = rc.entry_block("sniper", orders=b.orders, now=now)
    assert block and "loser limit" in block["reason"]
    assert rc.entry_block("diamond", orders=b.orders, now=now) is None


def test_desk_dollar_and_account_limits(tmp_path: Path):
    now = session_et(11, 0)
    b = _broker(tmp_path)
    _trip(b, "diamond", "diamond", "AVGO", 6.0, 1.8, now - timedelta(minutes=30))        # -420
    assert "Diamond daily loss limit" in rc.entry_block("diamond", orders=b.orders, now=now)["reason"]
    _trip(b, "sure_winner", "sure_winner_gold", "AAPL", 5.0, 1.0, now - timedelta(minutes=10))   # -400
    block = rc.entry_block("sniper", orders=b.orders, now=now)
    assert block["scope"] == "account"
    # Yesterday's losses do not count today.
    tomorrow = now + timedelta(days=1)
    assert rc.entry_block("diamond", orders=b.orders, now=tomorrow) is None


def test_limits_configurable_and_can_be_off(tmp_path: Path):
    now = session_et(11, 0)
    b = _broker(tmp_path)
    _trip(b, "diamond", "diamond", "AVGO", 6.0, 1.8, now - timedelta(minutes=30))
    off = rc.RiskControls(daily_limits_enabled=False)
    assert rc.entry_block("diamond", orders=b.orders, now=now, controls=off) is None
    loose = rc.RiskControls(desk_daily_loss_usd=1000, account_daily_loss_usd=2000)
    assert rc.entry_block("diamond", orders=b.orders, now=now, controls=loose) is None


def test_kill_switch_blocks_entries_and_env_wins(tmp_path: Path, monkeypatch):
    path = tmp_path / "k.json"
    assert rc.kill_switch_state(path)["on"] is False
    rc.set_kill_switch(True, "testing", path)
    block = rc.entry_block("sniper", orders=[], kill_path=path)
    assert block["scope"] == "kill_switch" and "testing" in block["reason"]
    rc.set_kill_switch(False, "", path)
    assert rc.entry_block("sniper", orders=[], kill_path=path) is None
    monkeypatch.setenv("PAPER_ENTRY_KILL", "1")
    assert rc.entry_block("sniper", orders=[], kill_path=path)["scope"] == "kill_switch"


def test_trader_skips_entry_but_keeps_exits(tmp_path: Path, monkeypatch):
    now = session_et(10, 0)
    b = _broker(tmp_path)
    _trip(b, "sniper", "sniper", "SPY", 2.0, 1.5, now - timedelta(minutes=30))
    _trip(b, "sniper", "sniper", "QQQ", 1.0, 0.8, now - timedelta(minutes=20))
    trader = AutoTrader(b, golden_only=True)
    trader._now = now
    sent: list[str] = []
    import odte_scanner.alerts.telegram as tg
    monkeypatch.setattr(tg, "send_telegram_text", lambda text, **k: sent.append(text) or {"ok": True})
    row = {"symbol": "IWM", "right": "C", "strike": 279, "expiry": iso_day(now), "ask": 0.9, "bid": 0.85,
           "golden_kind": "sniper"}
    assert trader._route_buy(desk="sniper", row=row) is None
    assert "loser limit" in row["paper_entry_skipped"]
    assert any("PAPER ENTRIES PAUSED" in s for s in sent)
    # Deduped: the second skip does not alert again.
    trader._route_buy(desk="sniper", row=dict(row))
    assert len([s for s in sent if "PAUSED" in s]) == 1
    # Exits still run: the premium-exit pass does not consult the limit.
    assert trader._entry_limit("sniper") is not None
    assert "limit" not in AutoTrader._premium_exit_rows.__code__.co_names


def test_signal_alert_is_labeled_when_entry_skipped():
    from odte_scanner.alerts.dispatcher import _fmt_alert

    row = {"symbol": "IWM", "right": "C", "strike": 279, "expiry": "2026-11-09", "bid": 0.85, "ask": 0.9,
           "paper_entry_skipped": "Sniper daily loser limit"}
    text = _fmt_alert(row, "BUY", "Sniper")
    assert "Paper entry skipped — Sniper daily loser limit" in text
    assert "Sure Winner" not in text
