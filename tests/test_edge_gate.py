"""The edge panel keeps the bar. It does not mute Telegram or paper buys."""

from __future__ import annotations

from pathlib import Path

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.edge.gate import MIN_SAMPLES, decide_sources, edge_gate_enabled
from odte_scanner.edge.metrics import bad_spot_reasons, clears_bar, summarize_pairs
from odte_scanner.edge.view import build_edge_report
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker
from tests.session_clock import iso_day, session_et


def _pairs(start: str, values: list[float]) -> list[tuple[str, float]]:
    return [(f"{start}-{index:02d}", value) for index, value in enumerate(values, start=1)]


def test_bar_requires_sample_size_interval_and_later_half():
    winning = _pairs("2026-01", [10.0] * 20) + _pairs("2026-02", [5.0] * 20)
    summary = summarize_pairs(winning)
    assert summary["n"] == 40
    assert clears_bar(summary, min_samples=MIN_SAMPLES)

    small = summarize_pairs(_pairs("2026-03", [10.0] * 39))
    assert small["lo"] is not None and small["lo"] > 0
    assert not clears_bar(small, min_samples=MIN_SAMPLES)

    faded = summarize_pairs(_pairs("2026-04", [10.0] * 20) + _pairs("2026-05", [-1.0] * 20))
    assert faded["expectancy"] > 0
    assert faded["h2_expectancy"] < 0
    assert not clears_bar(faded, min_samples=MIN_SAMPLES)


def test_bad_spot_rows_are_excluded_and_underlying_does_not_route():
    dirty = {
        "ticker": "AMAT",
        "contract": "AMAT261121C00150000",
        "reference_entry": 64.0,
    }
    assert "named_bad_spot" in bad_spot_reasons(dirty)
    assert "spot_vs_strike" in bad_spot_reasons(dirty)
    assert bad_spot_reasons(
        {"ticker": "SPY", "contract": "SPY261030C00580000", "reference_entry": 575.0}
    ) == []

    rows = []
    for index in range(40):
        rows.append(
            {
                "signal_type": "Gold Setup",
                "ticker": "SPY",
                "direction": "long",
                "contract": "SPY261030C00580000",
                "reference_entry": 575.0,
                "signaled_at": f"2026-09-{(index % 28) + 1:02d}T15:00:00+00:00",
                "outcomes": {"eod": {"status": "settled", "return_pct": 2.0}},
            }
        )
    rows.append(
        {
            "signal_type": "Golden Flow",
            "ticker": "AMAT",
            "direction": "long",
            "contract": "AMAT261121C00150000",
            "reference_entry": 64.0,
            "signaled_at": "2026-10-01T15:00:00+00:00",
            "outcomes": {"eod": {"status": "settled", "return_pct": 400.0}},
        }
    )
    report = build_edge_report(scorecard={"rows": rows, "spx_strategies": {}}, samples=[])
    assert report["route"] == []
    assert report["alert_desks"] == []
    assert report["bad_spots_excluded"] == 1
    gold = next(row for row in report["underlying"] if row["slice"] == "Gold Setup")
    assert gold["n"] == 40
    assert gold["routed"] is False


def test_option_dollars_route_only_the_source_that_clears():
    samples = [
        {"source": "Gold Setup", "pnl": 25.0, "at": f"2026-06-{index:02d}"}
        for index in range(1, 21)
    ] + [
        {"source": "Gold Setup", "pnl": 15.0, "at": f"2026-07-{index:02d}"}
        for index in range(1, 21)
    ]
    samples.append({"source": "Challenge", "pnl": -40.0, "at": "2026-07-15"})
    report = build_edge_report(scorecard={"rows": [], "spx_strategies": {}}, samples=samples)
    assert report["route"] == ["Gold Setup"]
    assert report["alert_desks"] == ["Gold Setup"]
    challenge = next(row for row in report["option_sources"] if row["source"] == "Challenge")
    assert challenge["routed"] is False
    assert decide_sources({"Challenge": [("2026-01-01", -10.0)]})[0]["source"]


def test_zero_hold_flat_is_not_a_trade():
    from odte_scanner.edge.view import _trade_samples

    rows = _trade_samples(
        {
            "trades": [
                {
                    "status": "closed",
                    "pnl_usd": 0.0,
                    "contracts": 1,
                    "hold_minutes": 0,
                    "entry_reason": "clock",
                    "exited_at": "2026-10-01T20:00:00+00:00",
                },
                {
                    "status": "closed",
                    "pnl_usd": -30.0,
                    "contracts": 2,
                    "hold_minutes": 40,
                    "entry_reason": "challenge sprint",
                    "exited_at": "2026-10-02T20:00:00+00:00",
                },
            ]
        },
        "Options",
    )
    assert len(rows) == 1
    assert rows[0]["source"] == "Challenge"
    assert rows[0]["pnl"] == -15.0


def test_edge_gate_does_not_mute_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr("odte_scanner.edge.view.alert_desk_allowlist", lambda: set())
    monkeypatch.setattr(
        disp,
        "telegram_configured",
        lambda: {"ok": True, "token_set": True, "chat_set": True, "enabled": True},
    )
    monkeypatch.setattr(
        disp,
        "whatsapp_configured",
        lambda: {"ok": False, "callmebot": False, "twilio": False, "meta": False},
    )
    sent: list[str] = []
    monkeypatch.setattr(disp, "send_telegram_text", lambda message: sent.append(message) or {"ok": True})
    seen = tmp_path / "seen.json"
    seen.write_text('{"keys":["seed"],"strengths":{}}', encoding="utf-8")
    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "NVDA",
                    "action": "BUY_NOW",
                    "contract": "NVDA261030C00180000",
                    "ask": 1.2,
                    "signaled_at": "2026-10-27T15:00:00+00:00",
                }
            ]
        }
    }
    result = disp.dispatch_snapshot_alerts(snap, seen_path=seen, edge_gate=True)
    assert result["sent"] == 1
    assert result["log_only"] == 0
    assert sent
    assert "NVDA" in sent[0]


def test_open_route_still_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "odte_scanner.edge.view.alert_desk_allowlist",
        lambda: {"Options"},
    )
    monkeypatch.setattr(
        disp,
        "telegram_configured",
        lambda: {"ok": True, "token_set": True, "chat_set": True, "enabled": True},
    )
    monkeypatch.setattr(
        disp,
        "whatsapp_configured",
        lambda: {"ok": False, "callmebot": False, "twilio": False, "meta": False},
    )
    sent: list[str] = []
    monkeypatch.setattr(disp, "send_telegram_text", lambda message: sent.append(message) or {"ok": True})
    seen = tmp_path / "seen.json"
    seen.write_text('{"keys":["seed"],"strengths":{}}', encoding="utf-8")
    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "NVDA",
                    "action": "BUY_NOW",
                    "contract": "NVDA261030C00180000",
                    "ask": 1.2,
                    "signaled_at": "2026-10-27T15:00:00+00:00",
                }
            ]
        }
    }
    result = disp.dispatch_snapshot_alerts(snap, seen_path=seen, edge_gate=True)
    assert result["sent"] == 1
    assert sent


def test_webull_paper_buy_stays_on_and_the_exit_still_sells(tmp_path, monkeypatch):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(
        broker,
        golden_only=True,
        max_orders_per_sync=4,
        edge_gate=True,
        edge_route=set(),
    )
    morning = session_et(11, 0)
    out = trader.sync(
        sure_winners={
            "winners": [
                {
                    "symbol": "NVDA",
                    "right": "C",
                    "confidence_tier": "MAX",
                    "ask": 1.5,
                    "contract": "NVDA260821C00120000",
                    "expiry": "2026-08-21",
                    "strike": 120,
                }
            ]
        },
        now=morning,
    )
    assert out["edge_gate"] is True
    assert out["edge_route"] == []
    buys = [row for row in out["submitted"] if row["action"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["symbol"] == "NVDA"
    assert not any("edge gate" in str(row.get("reason") or "") for row in out["filtered"])

    today = iso_day(morning)
    yymmdd = f"{today[2:4]}{today[5:7]}{today[8:10]}"
    contract = f"QQQ{yymmdd}C00500000"
    pos = {
        "contract": contract,
        "symbol": "QQQ",
        "right": "C",
        "expiry": today,
        "strike": 500,
        "quantity": 1,
        "cost": 1.0,
        "mark": 1.30,
    }
    buy = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol="QQQ",
        right="C",
        contract=contract,
        expiry=today,
        strike=500,
        quantity=1,
        limit_price=1.0,
        reason="held",
        meta={"original_qty": 1, "golden_kind": "sure_winner_gold"},
    )
    buy.status = "filled"
    buy.fill_price = 1.0
    broker.save()
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [] if account_id == "cash-2" else [pos])
    sold = trader.sync(sure_winners={"winners": []}, now=morning)
    sells = [row for row in sold["submitted"] if row["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "QQQ"


def test_night_pulse_does_not_cancel_a_submitted_buy(tmp_path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, edge_gate=True, edge_route=set())
    staged = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol="NVDA",
        right="C",
        contract="NVDA260821C00120000",
        expiry="2026-08-21",
        strike=120,
        quantity=1,
        limit_price=1.0,
        reason="staged",
    )
    resting = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol="SPY",
        right="C",
        contract="SPY260821C00580000",
        expiry="2026-08-21",
        strike=580,
        quantity=1,
        limit_price=1.0,
        reason="resting",
    )
    resting.status = "submitted"
    resting.broker_order_id = "broker-1"
    broker.save()
    trader.sync(now=session_et(20, 0))
    by_symbol = {order.symbol: order.status for order in broker.orders}
    assert by_symbol["NVDA"] == "staged"
    assert by_symbol["SPY"] == "submitted"
    assert staged.id


def test_desk_page_has_an_edge_tab():
    from odte_scanner.ui import PAGE

    assert 'data-tab="edge"' in PAGE
    assert 'id="tab-edge"' in PAGE
    assert "function renderEdge" in PAGE
    assert edge_gate_enabled({"edge_gate": {"enabled": True}}) is True
    assert edge_gate_enabled({}) is True
