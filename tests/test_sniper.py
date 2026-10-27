"""Sniper is its own book: a capped 15-minute retest, calls and puts."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.edge.page import render_edge_static
from odte_scanner.edge.sniper_study import ROUTED, SAMPLE_TRADES, SUMMARY, TODAY_SENT
from odte_scanner.edge.view import build_edge_report
from odte_scanner.scorecard.signals import extract_signals
from odte_scanner.signals.sniper import (
    PRIMARY_RULES,
    Bar,
    SniperRules,
    _continuous,
    apply_daily_cap,
    first_signal,
    group_sessions,
    run_symbol,
    sniper_routed,
    summarize,
)
from odte_scanner.signals.sniper_board import build_sniper_board, choose_contract
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import WebullBroker
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")


def _bar(day: str, hh: int, mm: int, price: float, *, high=None, low=None) -> Bar:
    ts = datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=CT)
    return Bar(
        ts=ts,
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price,
        volume=1000.0,
    )


def _fill(day: str, bars: list[Bar], start_h: int, start_m: int, price: float) -> list[Bar]:
    cursor = datetime.fromisoformat(f"{day}T{start_h:02d}:{start_m:02d}:00").replace(tzinfo=CT)
    end = datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=CT)
    while cursor <= end:
        bars.append(_bar(day, cursor.hour, cursor.minute, price, high=price + 0.04, low=price - 0.04))
        price += 0.01
        cursor += timedelta(minutes=5)
    return bars


def _call_day(day: str = "2026-10-27") -> list[Bar]:
    """15-minute range tops at 100.30. 8:45 breaks it. 8:50 retests and holds."""
    bars = [
        _bar(day, 8, 30, 100.0, high=100.20, low=99.80),
        _bar(day, 8, 35, 100.1, high=100.30, low=99.90),
        _bar(day, 8, 40, 100.0, high=100.20, low=99.70),
        _bar(day, 8, 45, 100.50, high=100.55, low=100.42),
        _bar(day, 8, 50, 100.45, high=100.55, low=100.32),
    ]
    return _fill(day, bars, 8, 55, 100.50)


def _put_day(day: str = "2026-10-27") -> list[Bar]:
    """Range bottoms at 99.85. 8:45 breaks it. 8:50 retests and closes back under."""
    bars = [
        _bar(day, 8, 30, 100.0, high=100.40, low=99.90),
        _bar(day, 8, 35, 100.1, high=100.30, low=99.95),
        _bar(day, 8, 40, 100.0, high=100.20, low=99.85),
        _bar(day, 8, 45, 99.60, high=99.70, low=99.50),
        _bar(day, 8, 50, 99.70, high=99.86, low=99.60),
    ]
    price = 99.65
    cursor = datetime.fromisoformat(f"{day}T08:55:00").replace(tzinfo=CT)
    end = datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=CT)
    while cursor <= end:
        bars.append(_bar(day, cursor.hour, cursor.minute, price, high=price + 0.03, low=price - 0.05))
        price -= 0.01
        cursor += timedelta(minutes=5)
    return bars


def _found(bars: list[Bar]):
    sessions = group_sessions(bars)
    day = next(iter(sessions))
    session = sessions[day]
    index, ema9, ema21 = _continuous(sessions)
    return first_signal(session, ema9, ema21, index[(day, 0)], PRIMARY_RULES), session


def test_retest_fires_once_and_a_chase_does_not():
    found, session = _found(_call_day())
    assert found == (4, 1)
    assert session[found[0]].ct().strftime("%H:%M") == "08:50"
    chase = _call_day()
    chase[4] = _bar("2026-10-27", 8, 50, 101.20, high=101.30, low=100.35)
    assert _found(chase)[0] is None
    no_retest = _call_day()
    no_retest[4] = _bar("2026-10-27", 8, 50, 100.70, high=100.80, low=100.60)
    assert _found(no_retest)[0] is None


def test_put_retest_fires_on_the_downside():
    found, _session = _found(_put_day())
    assert found == (4, -1)
    trades = run_symbol("QQQ", _put_day(), PRIMARY_RULES)
    assert len(trades) == 1
    assert trades[0]["side"] == "P"
    assert trades[0]["at_ct"] == "08:50"


def test_call_day_flats_at_1030_and_does_not_use_the_inside_range_stop():
    trades = run_symbol("SPY", _call_day(), PRIMARY_RULES)
    assert len(trades) == 1
    assert trades[0]["side"] == "C"
    assert trades[0]["reason"] == "flat"
    assert trades[0]["option_pct"] > 0
    dipped = _call_day()
    for i, bar in enumerate(dipped):
        if bar.ct().strftime("%H:%M") == "08:55":
            dipped[i] = _bar("2026-10-27", 8, 55, 100.40, high=100.50, low=100.20)
            break
    stopped = run_symbol("SPY", dipped, SniperRules(or_stop=True))
    assert stopped[0]["reason"] == "or"
    assert "orstop0" in PRIMARY_RULES.rules_id()


def test_daily_cap_keeps_the_earliest_three_not_alphabetical():
    trades = [
        {"symbol": "MU", "at": "2026-10-27T14:00:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "IWM", "at": "2026-10-27T13:55:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "AAPL", "at": "2026-10-27T14:35:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "MSFT", "at": "2026-10-27T13:50:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "META", "at": "2026-10-27T14:15:00+00:00", "day": "2026-10-27", "option_pct": 1},
    ]
    picked = [row["symbol"] for row in apply_daily_cap(trades)]
    assert picked == ["MSFT", "IWM", "MU"]


def test_recorded_book_is_non_negative_and_puts_are_in_it():
    assert sniper_routed() is True
    assert ROUTED is True
    assert SUMMARY["n"] == 175
    assert SUMMARY["n"] >= 60
    assert SUMMARY["expectancy"] >= 0
    assert SUMMARY["lo"] > 0
    assert SUMMARY["h1"] > 0
    assert SUMMARY["h2"] > 0
    again = summarize(SAMPLE_TRADES)
    assert again["n"] == SUMMARY["n"]
    assert abs(again["expectancy"] - SUMMARY["expectancy"]) < 1e-9
    assert abs(SUMMARY["expectancy"] - 5.8629) < 1e-3
    assert "full1" in PRIMARY_RULES.rules_id()
    sent = [(row["symbol"], row["side"]) for row in TODAY_SENT]
    assert sent[:3] == [("MSFT", "P"), ("META", "P"), ("AAPL", "C")]


def test_contract_is_atm_or_in_the_money_for_calls_and_puts():
    chain = [
        {"right": "C", "strike": 99, "expiry": "2026-10-29", "ask": 2.0, "bid": 1.8},
        {"right": "C", "strike": 101, "expiry": "2026-10-27", "ask": 0.4, "bid": 0.3},
        {"right": "P", "strike": 101, "expiry": "2026-10-29", "ask": 1.6, "bid": 1.4},
        {"right": "P", "strike": 99, "expiry": "2026-10-27", "ask": 0.3, "bid": 0.2},
    ]
    call = choose_contract("SPY", 100.2, chain, right="C", today=date(2026, 10, 27))
    put = choose_contract("QQQ", 100.2, chain, right="P", today=date(2026, 10, 27))
    assert call is not None and call["strike"] == 99 and call["dte"] == 2
    assert put is not None and put["strike"] == 101 and put["right"] == "P"


def test_board_sends_inside_the_window_and_not_after_it(tmp_path):
    bars = {"SPY": _call_day(), "QQQ": _put_day()}
    morning = datetime(2026, 10, 27, 14, 10, tzinfo=timezone.utc)
    chain = {
        "SPY": [{"right": "C", "strike": 100, "expiry": "2026-10-29", "ask": 1.4, "bid": 1.3}],
        "QQQ": [{"right": "P", "strike": 101, "expiry": "2026-10-29", "ask": 1.2, "bid": 1.1}],
    }
    board = build_sniper_board(
        bars, now=morning, store_path=tmp_path / "sniper_day.json", chains=chain
    )
    by_symbol = {row["symbol"]: row for row in board["signals"]}
    assert set(by_symbol) == {"SPY", "QQQ"}
    assert by_symbol["SPY"]["right"] == "C"
    assert by_symbol["QQQ"]["right"] == "P"
    assert all(row["send"] and row["send_order"] and row["action"] == "SNIPER" for row in board["signals"])
    late = build_sniper_board(
        bars,
        now=datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc),
        store_path=tmp_path / "late.json",
        chains=chain,
    )
    assert late["signals"]
    assert all(row["send"] is False and row["send_order"] is False for row in late["signals"])
    spx = build_sniper_board(
        {"SPX": _call_day()},
        now=morning,
        store_path=tmp_path / "spx.json",
        chains={"SPX": [{"right": "C", "strike": 100, "expiry": "2026-10-29", "ask": 12.0, "bid": 11.0}]},
    )
    assert spx["signals"][0]["send"] is True
    assert spx["signals"][0]["send_order"] is False
    assert spx["signals"][0]["webull"] is False


def test_telegram_uses_the_sniper_header_and_the_cap():
    def row(symbol: str, right: str = "C") -> dict:
        return {
            "symbol": symbol,
            "send": True,
            "action": "SNIPER",
            "sniper": True,
            "right": right,
            "strike": 100,
            "expiry": "2026-10-29",
            "dte": 2,
            "ask": 1.4,
            "bid": 1.2,
            "spot": 100.2,
            "contract": f"{symbol}261029{right}00100000",
        }

    alerts = collect_trade_alerts(
        {"sniper": {"signals": [row("MSFT", "P"), row("META", "P"), row("AAPL"), row("SPY")]}}
    )
    assert [item["symbol"] for item in alerts] == ["MSFT", "META", "AAPL"]
    assert alerts[0]["desk"] == "Sniper"
    assert alerts[0]["message"].splitlines()[0].startswith("🎯 SNIPER MSFT")
    silent = collect_trade_alerts({"sniper": {"signals": [{**row("MSFT", "P"), "send": False}]}})
    assert silent == []


def test_paper_buys_a_sniper_put_and_skips_spx(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(
        broker,
        golden_only=True,
        max_orders_per_sync=6,
        max_golden_buys_per_sync=2,
        max_risk_per_trade_usd=250,
    )
    now = datetime(2026, 10, 27, 14, 10, tzinfo=timezone.utc)
    card = {
        "symbol": "QQQ",
        "send_order": True,
        "sniper": True,
        "action": "SNIPER",
        "desk": "Sniper",
        "right": "P",
        "strike": 101,
        "expiry": "2026-10-29",
        "dte": 2,
        "ask": 1.0,
        "bid": 0.9,
        "spot": 100.2,
        "contract": "QQQ261029P00101000",
        "webull": True,
        "detail": "Sniper test",
        "exit_plan": "flat 10:30 AM CT",
    }
    spx = {**card, "symbol": "SPX", "contract": "SPXW261029P01000000", "webull": False, "send_order": False}
    out = trader.sync(sniper={"signals": [card, {**spx, "send_order": True}]}, now=now)
    buys = [row for row in out["submitted"] if row["action"] == "BUY"]
    assert [row["symbol"] for row in buys] == ["QQQ"]
    assert buys[0]["desk"] == "sniper"
    assert buys[0]["right"] == "P"
    assert buys[0]["quantity"] == 1
    assert buys[0]["meta"]["golden_kind"] == "sniper"
    assert "10:30" in buys[0]["meta"]["exit_plan"]
    assert golden_signal_kind(card) == "sniper"
    assert golden_signal_kind({"diamond": True, "action": "DIAMOND"}) == "diamond"
    assert golden_signal_kind({"confidence_tier": "MAX", "sure_winner": True}) == "sure_winner_gold"
    assert trader._paper_entry_qty(card, "sniper", False, 2.0) == 1


def test_sniper_exit_sells_the_whole_lot_at_plus_25_and_flats_at_1030(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True)
    trader._now = datetime(2026, 10, 27, 14, 10, tzinfo=timezone.utc)
    order = SimpleNamespace(desk="sniper", meta={"golden_kind": "sniper"})
    whole = trader._gold_decision(
        order=order,
        entry=1.0,
        mark=1.25,
        held=2,
        original=2,
        expiry="2026-10-30",
        peak_mark=None,
    )
    one_lot = trader._gold_decision(
        order=order,
        entry=1.0,
        mark=1.25,
        held=1,
        original=1,
        expiry="2026-10-30",
        peak_mark=None,
    )
    stopped = trader._gold_decision(
        order=order,
        entry=1.0,
        mark=0.60,
        held=2,
        original=2,
        expiry="2026-10-30",
        peak_mark=None,
    )
    gold_order = SimpleNamespace(desk="sure_winner", meta={"golden_kind": "sure_winner_gold"})
    gold_early = trader._gold_decision(
        order=gold_order,
        entry=1.0,
        mark=1.25,
        held=2,
        original=2,
        expiry="2026-10-30",
        peak_mark=None,
        force_short=False,
    )
    gold = trader._gold_decision(
        order=gold_order,
        entry=1.0,
        mark=1.30,
        held=2,
        original=2,
        expiry="2026-10-30",
        peak_mark=None,
        force_short=False,
    )
    assert whole is not None and whole.quantity == 2
    assert "25" in whole.detail
    assert one_lot is not None and one_lot.quantity == 1
    assert stopped is not None and stopped.quantity == 2
    assert "40" in stopped.detail
    # A weekly gold lot waits for +30%. Sniper sells the whole lot at +25%.
    assert gold_early is not None and gold_early.quantity == 0
    assert gold is not None and gold.quantity == 1
    assert "30" in gold.detail
    weekly = "2026-10-30"
    at_flat = datetime(2026, 10, 27, 15, 31, tzinfo=timezone.utc)  # 10:31 CT
    at_gold = datetime(2026, 10, 27, 19, 31, tzinfo=timezone.utc)  # 14:31 CT
    assert trader._lot_flattens("sniper", weekly, at_flat) is True
    assert trader._lot_flattens("diamond", weekly, at_flat) is True
    assert trader._lot_flattens("sure_winner", weekly, at_flat) is False
    assert trader._lot_flattens("sure_winner", "2026-10-27", at_gold) is True


def test_scorecard_records_a_sent_sniper_and_the_desk_has_its_own_tile():
    row = {
        "symbol": "QQQ",
        "send": True,
        "action": "SNIPER",
        "right": "P",
        "strike": 101,
        "expiry": "2026-10-29",
        "dte": 2,
        "ask": 1.2,
        "spot": 100.2,
        "contract": "QQQ261029P00101000",
        "signaled_at": "2026-10-27T13:50:00+00:00",
    }
    found = extract_signals(sniper={"signals": [row, {**row, "symbol": "SPY", "send": False}]})
    assert [item["ticker"] for item in found] == ["QQQ"]
    assert found[0]["signal_type"] == "Sniper"
    assert found[0]["direction"] == "short"
    assert "Sure Winner" not in found[0]["signal_type"]
    report = build_edge_report(scorecard={"rows": []}, samples=[])
    assert report["sniper"]["routed"] is True
    assert report["sniper"]["n"] == SUMMARY["n"]
    assert report["diamond"]["routed"] is True
    html = render_edge_static(report)
    assert "pulse-card sniper" in html
    assert "🎯" in html
    assert 'id="sniperBanner"' in PAGE
    assert "pulse-card.sniper" in PAGE
    assert "Clint" not in html
    assert "BDSniper" not in html
