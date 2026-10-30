"""Diamond is its own book: a capped morning call, not a Gold Setup card."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.edge.diamond_study import (
    HELD_TO_1430,
    ROUTED,
    SAMPLE_TRADES,
    SUMMARY,
    TODAY,
    TODAY_SENT,
)
from odte_scanner.edge.page import render_edge_static
from odte_scanner.edge.view import build_edge_report
from odte_scanner.scorecard.signals import extract_signals
from odte_scanner.signals.diamond import (
    PRIMARY_RULES,
    Bar,
    apply_daily_cap,
    first_signal,
    index_market,
    run_symbol,
    summarize,
)
from odte_scanner.signals.diamond_board import build_diamond_board, choose_call
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import WebullBroker
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")


def _bar(day: str, hh: int, mm: int, price: float, *, high=None, low=None, volume: float = 1000.0) -> Bar:
    ts = datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=CT)
    return Bar(
        ts=ts,
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price,
        volume=volume,
    )


def _trend_day(day: str = "2026-10-27") -> list[Bar]:
    """Opening range stays under 100. The 9:00 CT bar breaks it and holds VWAP."""
    bars = [
        _bar(day, 8, 30, 99.6, high=99.8, low=99.4),
        _bar(day, 8, 35, 99.7, high=99.9, low=99.5),
        _bar(day, 8, 40, 99.7, high=100.0, low=99.5),
        _bar(day, 8, 45, 99.8, high=99.95, low=99.6),
        _bar(day, 8, 50, 99.8, high=99.9, low=99.6),
        _bar(day, 8, 55, 99.85, high=99.95, low=99.7),
        _bar(day, 9, 0, 100.2, high=100.25, low=99.9),
    ]
    price = 100.25
    cursor = datetime.fromisoformat(f"{day}T09:05:00").replace(tzinfo=CT)
    end = datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=CT)
    while cursor <= end:
        price += 0.01
        bars.append(
            Bar(ts=cursor, open=price, high=price + 0.02, low=price - 0.01, close=price, volume=1000)
        )
        cursor += timedelta(minutes=5)
    return bars


def test_breakout_fires_once_and_a_chase_or_a_red_market_does_not():
    day = _trend_day()
    market = index_market(day)
    assert first_signal("SPY", day, market, PRIMARY_RULES) == 6
    chase = day[:7]
    chase[6] = _bar("2026-10-27", 9, 0, 101.0, high=101.2, low=100.5)
    assert first_signal("SPY", chase, index_market(chase), PRIMARY_RULES) is None
    quiet = day[:7]
    # SPY itself is the market. A flat open fails the "market is up" rule.
    quiet[0] = _bar("2026-10-27", 8, 30, 100.2, high=100.3, low=100.0)
    assert first_signal("SPY", quiet, index_market(quiet), PRIMARY_RULES) is None


def test_daily_cap_keeps_the_earliest_three():
    trades = [
        {"symbol": "MU", "at": "2026-10-27T14:45:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "IWM", "at": "2026-10-27T14:45:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "AMZN", "at": "2026-10-27T14:45:00+00:00", "day": "2026-10-27", "option_pct": 1},
        {"symbol": "SPY", "at": "2026-10-27T14:55:00+00:00", "day": "2026-10-27", "option_pct": 1},
    ]
    picked = [row["symbol"] for row in apply_daily_cap(trades)]
    assert picked == ["IWM", "AMZN", "MU"]


def test_recorded_book_is_non_negative_and_the_afternoon_hold_is_not_the_live_rule():
    assert ROUTED is True
    assert SUMMARY["n"] >= 60
    assert SUMMARY["expectancy"] >= 0
    assert SUMMARY["lo"] > 0
    assert SUMMARY["h1"] > 0
    assert SUMMARY["h2"] > 0
    again = summarize(SAMPLE_TRADES)
    assert again["n"] == SUMMARY["n"]
    assert abs(again["expectancy"] - SUMMARY["expectancy"]) < 1e-9
    # Same entries held to 2:30 PM CT. The first half is negative, so that
    # clock is not the live exit.
    assert HELD_TO_1430["h1"] < 0
    assert "flat1030" in PRIMARY_RULES.rules_id()
    sent = {row["symbol"] for row in TODAY_SENT}
    qualified = {row["symbol"] for row in TODAY}
    assert {"IWM", "AMZN", "GOOGL"} <= sent
    assert {"SPY", "QQQ", "SPX", "TSLA"} <= qualified
    assert "NVDA" not in qualified
    assert "AVGO" not in qualified


def test_call_is_atm_or_in_the_money_and_index_dte_stays_inside_two_days():
    chain = [
        {"right": "C", "strike": 99, "expiry": "2026-10-29", "ask": 2.0, "bid": 1.8},
        {"right": "C", "strike": 101, "expiry": "2026-10-27", "ask": 0.4, "bid": 0.3},
        {"right": "C", "strike": 100, "expiry": "2026-11-06", "ask": 3.0, "bid": 2.8},
    ]
    picked = choose_call("SPY", 100.2, chain, today=date(2026, 10, 27))
    assert picked is not None
    assert picked["strike"] == 99
    assert picked["expiry"] == "2026-10-29"
    assert picked["dte"] == 2
    named = choose_call("NVDA", 100.2, chain, today=date(2026, 10, 27))
    assert named is not None
    assert named["strike"] == 99
    assert named["expiry"] == "2026-10-29"


def test_board_sends_inside_the_window_and_not_after_it(tmp_path):
    bars = {"SPY": _trend_day(), "QQQ": _trend_day()}
    morning = datetime(2026, 10, 27, 14, 10, tzinfo=timezone.utc)  # 9:10 CT
    chain = {
        "SPY": [{"right": "C", "strike": 100, "expiry": "2026-10-29", "ask": 1.4, "bid": 1.3}],
        "QQQ": [{"right": "C", "strike": 100, "expiry": "2026-10-29", "ask": 1.5, "bid": 1.4}],
    }
    board = build_diamond_board(
        bars, now=morning, store_path=tmp_path / "diamond_day.json", chains=chain
    )
    symbols = [row["symbol"] for row in board["signals"]]
    assert symbols == ["QQQ", "SPY"] or symbols == ["SPY", "QQQ"]
    assert all(row["send"] and row["ask"] for row in board["signals"])
    assert all(row["action"] == "DIAMOND" for row in board["signals"])
    assert all(row["detected_at_ct"] == "9:10:00" for row in board["signals"])
    again = build_diamond_board(
        bars,
        now=datetime(2026, 10, 27, 14, 20, tzinfo=timezone.utc),
        store_path=tmp_path / "diamond_day.json",
        chains=chain,
    )
    assert [row["detected_at"] for row in again["signals"]] == [row["detected_at"] for row in board["signals"]]
    late = build_diamond_board(
        bars, now=datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc), store_path=tmp_path / "late.json", chains=chain
    )
    assert late["signals"]
    assert all(row["send"] is False for row in late["signals"])


def test_telegram_uses_the_diamond_header_and_the_cap():
    def row(symbol: str) -> dict:
        return {
            "symbol": symbol,
            "send": True,
            "action": "DIAMOND",
            "diamond": True,
            "right": "C",
            "strike": 100,
            "expiry": "2026-10-29",
            "dte": 2,
            "ask": 1.4,
            "bid": 1.2,
            "spot": 100.2,
            "contract": f"{symbol}261029C00100000",
        }

    alerts = collect_trade_alerts(
        {"diamond": {"signals": [row("SPY"), row("QQQ"), row("IWM"), row("NVDA")]}}
    )
    assert [item["symbol"] for item in alerts] == ["SPY", "QQQ", "IWM"]
    assert alerts[0]["desk"] == "Diamond"
    assert alerts[0]["message"].splitlines()[0].startswith("💎 DIAMOND SPY")
    silent = collect_trade_alerts({"diamond": {"signals": [{**row("SPY"), "send": False}]}})
    assert silent == []


def test_paper_buys_a_diamond_call_and_skips_spx(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=6, max_golden_buys_per_sync=2)
    now = datetime(2026, 10, 27, 15, 0, tzinfo=ET)
    card = {
        "symbol": "QQQ",
        "send_order": True,
        "diamond": True,
        "action": "DIAMOND",
        "desk": "Diamond",
        "right": "C",
        "strike": 100,
        "expiry": "2026-10-29",
        "dte": 2,
        "ask": 1.4,
        "bid": 1.2,
        "spot": 100.2,
        "contract": "QQQ261029C00100000",
        "webull": True,
        "detail": "Diamond test",
        "exit_plan": "flat 10:30 AM CT",
    }
    spx = {**card, "symbol": "SPX", "contract": "SPXW261029C01000000", "webull": False}
    out = trader.sync(diamond={"signals": [card, spx]}, now=now)
    buys = [row for row in out["submitted"] if row["action"] == "BUY"]
    assert [row["symbol"] for row in buys] == ["QQQ"]
    assert buys[0]["desk"] == "diamond"
    assert buys[0]["meta"]["golden_kind"] == "diamond"
    assert "10:30" in buys[0]["meta"]["exit_plan"]
    assert golden_signal_kind(card) == "diamond"
    assert golden_signal_kind({"confidence_tier": "MAX", "sure_winner": True}) == "sure_winner_gold"


def test_diamond_exit_sells_the_whole_lot_at_plus_30_and_flats_at_1030(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True)
    trader._now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    full = trader._gold_decision(
        order=None,
        entry=1.0,
        mark=1.30,
        held=3,
        original=3,
        expiry="2026-10-30",
        peak_mark=None,
        force_short=True,
    )
    half = trader._gold_decision(
        order=None,
        entry=1.0,
        mark=1.30,
        held=3,
        original=3,
        expiry="2026-10-30",
        peak_mark=None,
        force_short=False,
    )
    assert full is not None and full.quantity == 3
    assert half is not None and half.quantity == 1
    weekly = "2026-10-30"
    at_flat = datetime(2026, 10, 27, 15, 31, tzinfo=timezone.utc)  # 10:31 CT
    assert trader._lot_flattens("diamond", weekly, at_flat) is True
    assert trader._lot_flattens("sure_winner", weekly, at_flat) is False


def test_scorecard_records_a_sent_diamond_and_the_desk_has_its_own_tile():
    row = {
        "symbol": "QQQ",
        "send": True,
        "action": "DIAMOND",
        "right": "C",
        "strike": 100,
        "expiry": "2026-10-29",
        "dte": 2,
        "ask": 1.4,
        "spot": 100.2,
        "contract": "QQQ261029C00100000",
        "signaled_at": "2026-10-27T14:50:00+00:00",
    }
    found = extract_signals(diamond={"signals": [row, {**row, "symbol": "SPY", "send": False}]})
    assert [item["ticker"] for item in found] == ["QQQ"]
    assert found[0]["signal_type"] == "Diamond"
    assert "Sure Winner" not in found[0]["signal_type"]
    report = build_edge_report(scorecard={"rows": []}, samples=[])
    assert report["diamond"]["routed"] is True
    assert report["diamond"]["n"] == SUMMARY["n"]
    html = render_edge_static(report)
    assert "pulse-card diamond" in html
    assert "💎" in html
    assert 'id="diamondBanner"' in PAGE
    assert "pulse-card.diamond" in PAGE


def test_underlying_exit_is_flat_when_neither_stop_nor_target_hits():
    day = _trend_day()
    trades = run_symbol("SPY", day, index_market(day), PRIMARY_RULES)
    assert len(trades) == 1
    assert trades[0]["at_ct"] == "09:00"
    assert trades[0]["reason"] == "flat"
    assert trades[0]["option_pct"] > 0
