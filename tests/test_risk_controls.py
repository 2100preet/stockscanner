"""Per-trade $ stop and risk sizing: both OFF unless config turns them on."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import odte_scanner.edge  # noqa: F401
import odte_scanner.trading.auto_trader as at
from odte_scanner.config import load_config
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.risk_controls import RiskControls, load_risk_controls, risk_sized_qty
from odte_scanner.trading.webull import WebullBroker
from tests.session_clock import iso_day, session_et


def _occ(symbol, expiry, right, strike):
    return f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{right}{int(round(strike * 1000)):08d}"


def test_repo_config_keeps_both_experiments_off():
    rc = load_risk_controls(load_config(None))
    assert rc.dollar_stop_enabled is False and rc.risk_sizing_enabled is False
    assert rc.dollar_stop_usd == 175 and rc.risk_per_trade_usd == 175


def test_risk_sizing_math():
    on = RiskControls(risk_sizing_enabled=True, risk_per_trade_usd=175, risk_sizing_max_contracts=3)
    assert risk_sized_qty(on, premium=1.00, stop_pct=40) == 3     # $40 per lot -> 4, capped 3
    assert risk_sized_qty(on, premium=2.76, stop_pct=40) == 1     # $110 per lot -> 1
    assert risk_sized_qty(on, premium=0.70, stop_pct=50) == 3
    assert risk_sized_qty(on, premium=9.00, stop_pct=40) == 1     # never 0
    assert risk_sized_qty(RiskControls(), premium=1.0, stop_pct=40) is None


def _gold_lot(tmp_path: Path, monkeypatch, mark: float):
    now = session_et(10, 30)
    expiry = iso_day(now + timedelta(days=7))
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id="a",
                          ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    broker._fetch_order_detail = lambda intent: None  # type: ignore[method-assign]
    contract = _occ("AAPL", expiry, "C", 350)
    buy = broker.stage(desk="sure_winner", action="BUY", symbol="AAPL", contract=contract, expiry=expiry,
                       strike=350, quantity=2, limit_price=3.0, meta={"golden_kind": "sure_winner_gold", "gold_exit": True})
    buy.status = "filled"
    buy.fill_price = 3.0
    buy.meta["filled_qty"] = 2
    buy.broker_order_id = "B"
    buy.created_at = (now - timedelta(hours=2)).isoformat()
    broker.save()
    pos = {"contract": contract, "symbol": "AAPL", "right": "C", "expiry": expiry, "strike": 350, "quantity": 2,
           "cost": 3.0, "bid": mark, "mark": mark}
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [dict(pos)])
    sent: list[tuple[int, str]] = []

    def place(intent):
        sent.append((int(intent.quantity), str(intent.reason)))
        return "X"

    broker._place_via_openapi = place  # type: ignore[method-assign]
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    return now, trader, sent


def test_dollar_stop_off_holds_a_30pct_loser(tmp_path: Path, monkeypatch):
    now, trader, sent = _gold_lot(tmp_path, monkeypatch, mark=2.10)
    trader.sync(now=now)
    assert [s for s in sent if "stop" in s[1]] == []


def test_dollar_stop_on_sells_the_lot_at_the_cap(tmp_path: Path, monkeypatch):
    now, trader, sent = _gold_lot(tmp_path, monkeypatch, mark=2.10)   # -$180 on 2 lots
    trader.risk_controls = RiskControls(dollar_stop_enabled=True, dollar_stop_usd=175)
    trader.sync(now=now)
    assert sent and sent[0][0] == 2 and "per-trade cap" in sent[0][1]


def test_planned_stop_pct_per_desk(tmp_path: Path):
    broker = WebullBroker(enabled=False, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True)
    trader._now = session_et(10, 0)
    assert trader.planned_stop_pct("sniper", None) == 40
    assert trader.planned_stop_pct("diamond", None) == 40
    assert trader.planned_stop_pct("afternoon_puts", None) == 40
    assert trader.planned_stop_pct("sure_winner", iso_day(session_et(10, 0) + timedelta(days=7))) == 50
