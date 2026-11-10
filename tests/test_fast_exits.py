"""Fast exit loop: flattens and stops without the focus scan."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import odte_scanner.edge  # noqa: F401
import odte_scanner.trading.auto_trader as at
from odte_scanner import fast_exits
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker
from tests.session_clock import iso_day, session_et


def _occ(symbol, expiry, right, strike):
    return f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{right}{int(round(strike * 1000)):08d}"


def _book(tmp_path: Path, monkeypatch, lots, *, mark_of=None):
    now0 = session_et(9, 50)
    expiry = iso_day(now0)
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id="a",
                          ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    broker._fetch_order_detail = lambda intent: None  # type: ignore[method-assign]
    positions = []
    for symbol, strike, fill in lots:
        contract = _occ(symbol, expiry, "C", strike)
        buy = broker.stage(desk="sniper", action="BUY", symbol=symbol, contract=contract, expiry=expiry,
                           strike=strike, limit_price=fill,
                           meta={"golden_kind": "sniper", "gold_exit": True, "sniper_flat_ct": "10:30"})
        buy.status, buy.fill_price, buy.broker_order_id = "filled", fill, "B" + symbol
        buy.created_at = now0.isoformat()
        mark = (mark_of or {}).get(symbol, fill * 1.1)
        positions.append({"contract": contract, "symbol": symbol, "right": "C", "expiry": expiry, "strike": strike,
                          "quantity": 1, "cost": fill, "bid": mark, "mark": mark})
    broker.save()
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [dict(p) for p in positions])
    sent = []

    def place(intent):
        sent.append((intent.action, intent.symbol, intent.reason))
        return "X" + intent.id

    broker._place_via_openapi = place  # type: ignore[method-assign]
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    return broker, AutoTrader(broker, golden_only=True, max_orders_per_sync=3), sent


def test_flatten_goes_out_on_the_first_tick_after_1030_ct(tmp_path: Path, monkeypatch):
    broker, trader, sent = _book(tmp_path, monkeypatch, [("SPY", 746, 2.76), ("IWM", 278, 1.34), ("SPY", 747, 2.57)])
    before = trader.sync_exits(now=session_et(11, 29) + timedelta(seconds=50))
    assert [s for s in sent if s[0] == "SELL"] == [] and "sent" in before
    trader.sync_exits(now=session_et(11, 30) + timedelta(seconds=5))
    sells = [s for s in sent if s[0] == "SELL"]
    assert len(sells) == 3
    assert all(s[0] != "BUY" for s in sent)


def test_stop_fires_from_the_exit_loop(tmp_path: Path, monkeypatch):
    broker, trader, sent = _book(tmp_path, monkeypatch, [("SPY", 746, 2.50)], mark_of={"SPY": 1.40})  # -44%
    trader.sync_exits(now=session_et(10, 15))
    assert any(s[0] == "SELL" and "stop" in s[2] for s in sent)


def test_busy_lock_skips_the_tick(tmp_path: Path, monkeypatch):
    broker, trader, sent = _book(tmp_path, monkeypatch, [("SPY", 746, 2.76)])
    with at._SYNC_LOCK:
        out = trader.sync_exits(now=session_et(11, 31), lock_timeout=0.01)
    assert out == {"skipped": "sync busy"} and sent == []


def test_tick_outside_session_sends_nothing(tmp_path: Path, monkeypatch):
    broker, trader, sent = _book(tmp_path, monkeypatch, [("SPY", 746, 2.76)])
    out = fast_exits.run_exit_tick(trader, now=session_et(17, 0))
    assert out == {"skipped": "session closed"} and sent == []


def test_pause_is_clamped(monkeypatch):
    monkeypatch.setenv("FAST_EXIT_PAUSE_SEC", "1")
    assert fast_exits.exit_pause_sec() == 5
    monkeypatch.setenv("FAST_EXIT_PAUSE_SEC", "60")
    assert fast_exits.exit_pause_sec() == 10
    monkeypatch.delenv("FAST_EXIT_PAUSE_SEC")
    assert fast_exits.exit_pause_sec() == 7
