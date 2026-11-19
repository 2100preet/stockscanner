"""Option-print replay. Bars are trade prices, not bids or asks."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.backtest.option_tape import (
    Bar,
    Flags,
    arithmetic_pnl,
    path_status,
    price_inside,
    replay_exit_from_fill,
)
from odte_scanner.backtest.rule_replay import Spec

ET = ZoneInfo("America/New_York")


def _bar(hour: int, minute: int, low: float, high: float, close: float) -> Bar:
    return Bar(
        ts=datetime(2026, 11, 6, hour, minute, tzinfo=ET),
        open=close,
        high=high,
        low=low,
        close=close,
    )


def _spec() -> Spec:
    return Spec(0.40, 0.25, 0.25, __import__("datetime").time(10, 30), __import__("datetime").time(10, 30), True, True)


def test_arithmetic_matches_one_contract():
    assert arithmetic_pnl(2.0, 1.5, 1) == -50.0


def test_gap_wider_than_two_minutes_is_not_a_path():
    bars = [
        _bar(10, 0, 1, 1, 1),
        _bar(10, 1, 1, 1, 1),
        _bar(10, 5, 1, 1, 1),
    ]
    status = path_status(
        bars,
        datetime(2026, 11, 6, 10, 0, tzinfo=ET),
        datetime(2026, 11, 6, 10, 5, tzinfo=ET),
    )
    assert status == "gap"


def test_same_minute_stop_and_target_takes_the_stop():
    bars = [
        _bar(10, 1, 0.5, 3.0, 1.2),
    ]
    trade = replay_exit_from_fill(
        bars,
        entry=2.0,
        entry_ts=datetime(2026, 11, 6, 10, 0, tzinfo=ET),
        qty=1,
        spec=_spec(),
        flags=Flags("broker_stop", broker_exits=True),
        desk="Sniper",
        symbol="AMD",
        day="2026-11-06",
        contract="X",
        actual_pnl=-80.0,
    )
    assert trade.reason == "stop_ambiguous_bar"
    assert trade.exit is not None and trade.exit <= 1.2


def test_size_cap_does_not_increase_quantity():
    bars = [_bar(10, 1, 1.9, 2.1, 2.0), _bar(11, 30, 1.9, 2.1, 2.0)]
    trade = replay_exit_from_fill(
        bars,
        entry=1.0,
        entry_ts=datetime(2026, 11, 6, 10, 0, tzinfo=ET),
        qty=1,
        spec=_spec(),
        flags=Flags("size_175", size_175=True),
        desk="Sniper",
        symbol="QQQ",
        day="2026-11-06",
        contract="X",
        actual_pnl=10.0,
    )
    assert trade.filled
    assert trade.qty == 1


def test_expensive_lot_is_skipped_not_resized():
    bars = [_bar(10, 1, 9, 11, 10)]
    trade = replay_exit_from_fill(
        bars,
        entry=10.0,
        entry_ts=datetime(2026, 11, 6, 10, 0, tzinfo=ET),
        qty=1,
        spec=_spec(),
        flags=Flags("size_175", size_175=True),
        desk="Sniper",
        symbol="MU",
        day="2026-11-06",
        contract="X",
        actual_pnl=-400.0,
    )
    assert trade.reason == "risk_over_175"
    assert trade.qty == 0


def test_print_contains_price():
    bar = _bar(10, 0, 1.1, 1.4, 1.2)
    assert price_inside(bar, 1.2)
    assert not price_inside(bar, 2.0)
