"""Rule replay decisions. Bars are underlying prices. No network and no orders."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from odte_scanner.backtest.rule_replay import (
    Bar,
    Flags,
    Idea,
    realized_size_screen,
    run_variant,
    simulate_idea,
)

ET = ZoneInfo("America/New_York")


def _bars(start: datetime, spots: list[float]) -> list[Bar]:
    rows = []
    for offset, spot in enumerate(spots):
        rows.append(Bar(ts=start + timedelta(minutes=offset), open=spot, high=spot, low=spot, close=spot))
    return rows


def _idea(**overrides) -> Idea:
    base = dict(
        desk="Sniper",
        symbol="SPY",
        right="C",
        day="2026-11-04",
        signaled_at=datetime(2026, 11, 4, 11, 7, tzinfo=ET),
        seen_at=datetime(2026, 11, 4, 11, 7, tzinfo=ET),
        signal_ask=2.0,
        spot=100.0,
        contract="SPY261104C00100000",
        quote_kind="ask",
        expiry=datetime(2026, 11, 4).date(),
        strike=100.0,
        actual_entry=2.0,
        actual_entry_at=datetime(2026, 11, 4, 11, 7, tzinfo=ET),
        actual_pnl=-50.0,
        source="test",
    )
    base.update(overrides)
    return Idea(**base)


def test_broker_stop_fills_at_the_cheap_print_when_price_gaps_through():
    # 0.50 delta: spot 96 -> premium 2 + 0.5*(96-100) = 0.01 floor, well under the 1.20 stop.
    start = datetime(2026, 11, 4, 11, 7, tzinfo=ET)
    spots = [100.0, 100.0, 96.0]
    trade = simulate_idea(
        _idea(),
        _bars(start, spots),
        Flags(name="broker_stop", broker_exits=True, use_actual_fill=True),
    )
    assert trade.filled
    assert trade.reason == "stop"
    assert trade.exit is not None and trade.exit < 1.20
    assert trade.pnl < -80


def test_flat_market_mid_is_not_filled_and_the_ask_is():
    start = datetime(2026, 11, 4, 11, 0, tzinfo=ET)
    bars = _bars(start, [100.0] * 5)
    idea = _idea(signaled_at=start, seen_at=start, actual_entry=None, actual_entry_at=None)
    missed = simulate_idea(idea, bars, Flags(name="baseline"))
    paid = simulate_idea(idea, bars, Flags(name="ask_entry", ask_entry=True))
    assert missed.reason == "no_fill"
    assert paid.filled
    assert paid.entry == 2.0 * 1.04


def test_mid_fills_when_the_underlying_dips_inside_75_seconds():
    start = datetime(2026, 11, 4, 11, 0, tzinfo=ET)
    # Spot 99.6 -> call premium 1.80, under the 1.92 mid.
    bars = _bars(start, [100.0, 99.6])
    idea = _idea(signaled_at=start, seen_at=start, actual_entry=None, actual_entry_at=None)
    trade = simulate_idea(idea, bars, Flags(name="baseline"))
    assert trade.filled
    assert trade.entry == 1.92


def test_one_contract_over_175_dollars_of_risk_is_skipped():
    start = datetime(2026, 11, 4, 11, 7, tzinfo=ET)
    bars = _bars(start, [100.0] * 40)
    expensive = simulate_idea(
        _idea(actual_entry=10.0),
        bars,
        Flags(name="size_175", size_175=True, use_actual_fill=True),
    )
    cheap = simulate_idea(
        _idea(actual_entry=1.0, signal_ask=1.0),
        bars,
        Flags(name="size_175", size_175=True, use_actual_fill=True),
    )
    assert expensive.reason == "risk_over_175"
    assert cheap.filled
    assert cheap.qty == 4


def test_clock_on_time_flattens_at_1030_and_the_scan_grid_is_later():
    start = datetime(2026, 11, 4, 11, 7, tzinfo=ET)  # 10:07 CT
    spots = [100.0] * 50
    bars = _bars(start, spots)
    idea = _idea()
    on_time = simulate_idea(idea, bars, Flags(name="clock_ontime", clock_on_time=True, use_actual_fill=True))
    late = simulate_idea(idea, bars, Flags(name="baseline", use_actual_fill=True))
    assert on_time.reason == "flat"
    assert late.reason == "flat"
    assert on_time.exit_at is not None and late.exit_at is not None
    assert on_time.exit_at < late.exit_at
    assert "11:30" in on_time.exit_at  # 10:30 CT == 11:30 ET


def test_two_losers_halt_the_third_entry():
    start = datetime(2026, 11, 4, 10, 31, tzinfo=ET)  # 09:31 CT
    spots = [100.0] * 3 + [96.0] * 7 + [100.0] * 3 + [96.0] * 60
    bars = _bars(start, spots)
    ideas = [
        _idea(
            contract=f"SPY261104C0010000{index}",
            signaled_at=start + timedelta(minutes=offset),
            seen_at=start + timedelta(minutes=offset),
            actual_entry_at=start + timedelta(minutes=offset),
        )
        for index, offset in enumerate((0, 10, 30))
    ]
    flags = Flags(name="halt", broker_exits=True, daily_halt=True, clock_on_time=True, use_actual_fill=True)
    trades = run_variant(ideas, {"SPY": bars}, flags)
    assert [trade.reason for trade in trades] == ["stop", "stop", "halted"]
    assert trades[2].pnl == 0


def test_realized_size_screen_skips_a_large_premium_and_scales_a_small_one():
    stamp = "2026-11-06T15:00:00+00:00"
    screen = realized_size_screen(
        [
            {
                "desk": "Sniper",
                "entry": 10,
                "pnl": -400,
                "expiry": "2026-11-06",
                "entered_at": stamp,
                "exited_at": stamp,
            },
            {
                "desk": "Sniper",
                "entry": 1,
                "pnl": 40,
                "expiry": "2026-11-06",
                "entered_at": stamp,
                "exited_at": stamp,
            },
        ]
    )
    assert screen["skipped"] == 1
    assert screen["skipped_pnl"] == -400
    assert screen["kept_pnl"] == 40
    assert screen["scaled_pnl"] == 160
