"""Golden Flow skips, Gold Setup rank, and paper round trips."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.edge.filters import golden_flow_skip_reason, gold_setup_preferred
from odte_scanner.edge.round_trips import load_round_trips, record_sell_fill
from odte_scanner.signals.sure_winners import collect_candidates, select_sure_winners
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent
from tests.test_sure_winners import _buy

CT = ZoneInfo("America/Chicago")
AFTERNOON = "2026-10-26T18:30:00+00:00"  # 13:30 CT


def _call(**extra):
    row = {
        "right": "C",
        "moneyness_pct": 0.4,
        "dte": 3,
        "signaled_at": AFTERNOON,
    }
    row.update(extra)
    return row


def test_golden_flow_skips_name_the_losing_slices():
    assert "puts/shorts" in golden_flow_skip_reason(_call(right="P"))
    assert "puts/shorts" in golden_flow_skip_reason(_call(direction="short", right="C"))
    assert "1–3%" in golden_flow_skip_reason(_call(moneyness_pct=2.0))
    assert "1–3%" in golden_flow_skip_reason(_call(moneyness_pct=None, spot=100, strike=102))
    assert golden_flow_skip_reason(_call(moneyness_pct=0.4)) is None
    assert golden_flow_skip_reason(_call(moneyness_pct=4.0)) is None
    assert "8–21" in golden_flow_skip_reason(_call(dte=10))
    assert "8–21" in golden_flow_skip_reason(_call(dte=21))
    assert golden_flow_skip_reason(_call(dte=7)) is None
    assert "before 8:00" in golden_flow_skip_reason(_call(signaled_at="2026-10-26T12:30:00+00:00"))
    # 8:40 CT is the open, not the measured before-8 bucket.
    assert golden_flow_skip_reason(_call(signaled_at="2026-10-26T13:40:00+00:00")) is None
    assert "10–12" in golden_flow_skip_reason(_call(signaled_at="2026-10-26T16:30:00+00:00"))
    assert golden_flow_skip_reason(_call(signaled_at="2026-10-26T19:30:00+00:00")) is None


def test_skipped_golden_flow_is_not_telegram_and_the_card_keeps_the_reason(monkeypatch, tmp_path):
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
    put = {
        "symbol": "TSLA",
        "right": "P",
        "tier": "golden",
        "flags": ["golden"],
        "strike": 400,
        "spot": 410,
        "moneyness_pct": -2.4,
        "dte": 3,
        "ask": 2.0,
        "contract": "TSLA261030P00400000",
        "signaled_at": AFTERNOON,
        "premium_notional": 300000,
    }
    call = {
        "symbol": "NVDA",
        "right": "C",
        "tier": "golden",
        "flags": ["golden"],
        "strike": 180,
        "spot": 180.2,
        "moneyness_pct": 0.1,
        "dte": 3,
        "ask": 2.4,
        "contract": "NVDA261030C00180000",
        "signaled_at": AFTERNOON,
        "premium_notional": 400000,
        "edge_skip": None,
    }
    from odte_scanner.edge.filters import stamp_flow_prints

    stamp_flow_prints([put, call])
    assert "puts/shorts" in put["edge_skip"]
    assert "edge_skip" not in call
    result = disp.dispatch_snapshot_alerts(
        {"echo": {"option_flow": {"golden": [put, call]}}},
        seen_path=seen,
        edge_gate=True,
    )
    assert result["sent"] == 1
    assert sent and "NVDA" in sent[0]
    assert all("TSLA" not in message for message in sent)


def test_gold_setup_preference_does_not_override_tier():
    favored = _buy("AAA", win=100, n=6, score=70, strike=100)
    favored["dte"] = 1
    other = _buy("BBB", win=100, n=6, score=95, strike=110)
    other["dte"] = 5
    winners, _store = select_sure_winners(
        collect_candidates(actions={"buy_now": [other, favored]}),
        max_per_day=1,
        day="2026-10-12",
        store={},
    )
    assert winners[0].symbol == "AAA"

    high = _buy("CCC", win=86, n=6, score=70, strike=50)
    high["dte"] = 1
    high["certainty_tier"] = "elite"
    high["hit_1pct"] = 40
    maxed = _buy("DDD", win=100, n=6, score=68, strike=60)
    maxed["dte"] = 5
    picked, _store = select_sure_winners(
        collect_candidates(actions={"buy_now": [high, maxed]}),
        max_per_day=1,
        day="2026-10-12",
        store={},
    )
    assert picked[0].symbol == "DDD"
    assert picked[0].confidence_tier == "MAX"

    early = _buy("EEE", win=100, n=6, score=99, strike=70)
    early["signaled_at"] = "2026-10-12T15:00:00+00:00"
    late = _buy("FFF", win=100, n=6, score=70, strike=80)
    late["signaled_at"] = "2026-10-12T18:00:00+00:00"
    assert gold_setup_preferred(late, datetime(2026, 10, 12, 9, 0, tzinfo=CT))
    clocked, _store = select_sure_winners(
        collect_candidates(actions={"buy_now": [early, late]}),
        max_per_day=1,
        day="2026-10-12",
        store={},
    )
    assert clocked[0].symbol == "FFF"


def test_sell_fill_records_one_gold_setup_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "trips.jsonl"
    monkeypatch.setattr("odte_scanner.edge.round_trips.PATH", path)
    sell = WebullOrderIntent(
        id="wb-sell",
        desk="golden_exit",
        action="SELL",
        symbol="QQQ",
        right="C",
        contract="QQQ261030C00500000",
        expiry="2026-10-30",
        strike=500,
        quantity=1,
        limit_price=1.3,
        reason="exit",
        status="filled",
        fill_price=1.3,
        created_at="2026-10-27T15:00:00+00:00",
        updated_at="2026-10-27T16:00:00+00:00",
        meta={"entry_price": 1.0, "filled_qty": 1},
    )
    first = record_sell_fill(sell, [], path=path)
    again = record_sell_fill(sell, [], path=path)
    assert first is not None and first["source"] == "Gold Setup"
    assert first["pnl"] == 30.0
    assert again is None
    rows = load_round_trips(path)
    assert len(rows) == 1

    from odte_scanner.edge.view import load_option_samples

    monkeypatch.setattr("odte_scanner.edge.view._read_json", lambda _path: {
        "orders": [
            {
                "status": "filled",
                "action": "BUY",
                "fill_price": 1.0,
                "quantity": 1,
                "contract": "QQQ261030C00500000",
                "desk": "sure_winner",
                "updated_at": "2026-10-27T15:00:00+00:00",
            },
            {
                "status": "filled",
                "action": "SELL",
                "fill_price": 1.3,
                "quantity": 1,
                "contract": "QQQ261030C00500000",
                "desk": "golden_exit",
                "updated_at": "2026-10-27T16:00:00+00:00",
            },
        ]
    })
    samples = [row for row in load_option_samples({}) if row["source"] == "Gold Setup"]
    assert len(samples) == 1
    assert samples[0]["pnl"] == 30.0


def test_live_broker_still_refuses_a_new_buy_when_the_route_is_empty():
    class _Broker:
        paper = False
        orders: list = []

    trader = AutoTrader(_Broker(), edge_gate=True, edge_route=set())
    assert trader._edge_allows_buy("sure_winner") is False
    paper = AutoTrader(WebullBroker(enabled=False, dry_run=True), edge_gate=True, edge_route=set())
    assert paper._edge_allows_buy("sure_winner") is True


def test_edge_copy_does_not_say_alerts_are_off():
    from odte_scanner.edge.view import build_edge_report

    report = build_edge_report(scorecard={"rows": [], "spx_strategies": {}}, samples=[])
    assert "stay off" not in report["verdict"]
    assert "Information only" in report["verdict"]
    assert len(report["filters"]) >= 5
