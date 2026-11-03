"""Lunch-Break Credit: range, spread math, paper combo, breadth is not this desk."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.alerts.lunch_credit import format_lunch_cancel, format_lunch_entry, format_lunch_exit, lunch_log_row
from odte_scanner.live_desk import resolve_webull_live_flags
from odte_scanner.scorecard.daily_pnl import build_scorecard_daily_pnl
from odte_scanner.signals.diamond import Bar
from odte_scanner.signals.lunch_credit import (
    ORDER_PATH,
    PRIMARY_RULES,
    atm_put,
    credit_chase_action,
    exit_reason,
    first_break,
    long_put,
    max_risk_usd,
    natural_credit,
    next_credit_limit,
    opening_limit,
    range_high,
    replay,
    spread_mid,
    spread_pnl,
    walk_marks,
)
from odte_scanner.signals.lunch_credit_board import build_lunch_board
from odte_scanner.trading.lunch_router import LunchRouter, fallback_leg_orders
from odte_scanner.trading.webull import WebullBroker, build_vertical_spread_order, multi_leg_rejected
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")


def _ct(hour: int, minute: int, day: str = "2026-11-03") -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=CT)


def _session() -> list[Bar]:
    """10:00–12:00 range high is 101. A 12:05 close through it is the only break."""
    bars: list[Bar] = []
    for minute in range(10 * 60, 14 * 60 + 45, 5):
        hour, mm = divmod(minute, 60)
        stamp = _ct(hour, mm)
        if minute < 12 * 60:
            high = 101.0 if minute == 10 * 60 + 30 else 100.4
            bars.append(Bar(ts=stamp, open=100.0, high=high, low=99.5, close=100.2, volume=1000))
        elif minute == 12 * 60:
            bars.append(Bar(ts=stamp, open=100.8, high=101.2, low=100.6, close=101.05, volume=1000))
        elif minute == 12 * 60 + 30:
            bars.append(Bar(ts=stamp, open=102.0, high=102.4, low=101.6, close=102.2, volume=1000))
        else:
            bars.append(Bar(ts=stamp, open=101.2, high=101.4, low=100.8, close=101.1, volume=800))
    return bars


def test_range_high_is_only_the_morning_window_and_the_break_is_after_noon():
    bars = _session()
    assert range_high(bars) == 101.0
    assert first_break(bars) == next(i for i, bar in enumerate(bars) if bar.ct().strftime("%H:%M") == "12:00")
    before = [bar for bar in bars if bar.ct().time() < PRIMARY_RULES.entry_start]
    assert first_break(before) is None
    tied = []
    for bar in bars:
        if bar.ct().time() >= PRIMARY_RULES.entry_start:
            tied.append(Bar(ts=bar.ts, open=100.0, high=101.0, low=99.8, close=101.0, volume=1))
        else:
            tied.append(bar)
    assert first_break(tied) is None
    after = []
    for bar in bars:
        if bar.ct().time() >= PRIMARY_RULES.entry_end:
            after.append(Bar(ts=bar.ts, open=102, high=103, low=101.5, close=102.5, volume=1))
        elif bar.ct().time() >= PRIMARY_RULES.entry_start:
            after.append(Bar(ts=bar.ts, open=100, high=100.8, low=99.8, close=100.4, volume=1))
        else:
            after.append(bar)
    assert first_break(after) is None


def test_one_break_per_symbol_and_cap_two():
    bars = _session()
    book = {
        "SPY": bars,
        "QQQ": bars,
        "SPX": bars,
    }
    rows = replay(book)
    assert [row["symbol"] for row in rows] == ["SPY", "QQQ"]
    assert rows[0]["at_ct"] == "12:00"
    assert all(row["symbol"] != "SPX" for row in rows[2:])
    again = replay({"SPY": bars + [Bar(ts=_ct(13, 0), open=103, high=104, low=102, close=103.5, volume=1)]})
    assert len(again) == 1
    assert again[0]["at_ct"] == "12:00"


def test_strike_is_atm_and_two_dollars_lower():
    assert atm_put(500.4, "SPY") == 500
    assert atm_put(500.5, "SPY") == 501
    assert long_put(500) == 498
    assert atm_put(5810.2, "SPX") == 5810
    assert long_put(5810) == 5808
    assert abs(500 - 498) == PRIMARY_RULES.width


def test_credit_take_profit_stop_and_flat_math():
    credit = spread_mid(1.20, 1.30, 0.40, 0.50)
    assert credit == 0.80
    assert natural_credit(1.20, 0.50) == 0.70
    assert max_risk_usd(0.80) == 120.0
    assert spread_pnl(0.80, 0.40) == (40.0, 50.0)
    assert exit_reason(0.80, 0.40, flat=False) == "target"
    assert exit_reason(0.80, 1.60, flat=False) == "stop"
    assert exit_reason(0.80, 1.00, flat=False) is None
    assert exit_reason(0.80, 1.00, flat=True) == "flat"
    walked = walk_marks(
        0.80,
        [
            (_ct(12, 30), 0.90, 1.70, 0.30),
        ],
    )
    assert walked["reason"] == "stop"
    target = walk_marks(0.80, [(_ct(12, 30), 0.70, 0.75, 0.35)])
    assert target["reason"] == "target"
    assert target["pnl_usd"] == 40.0
    flat = walk_marks(0.80, [(_ct(14, 45), 0.55, 1.90, 0.10)])
    assert flat["reason"] == "flat"
    assert flat["exit_debit"] == 0.55
    limit = opening_limit(0.80, 0.70)
    assert limit == 0.80
    assert next_credit_limit(0.80, 0.60, 0.80) == 0.72
    assert credit_chase_action(limit=0.72, natural=0.50, baseline=0.80, age_sec=10, past_window=False) == "cap"
    assert credit_chase_action(limit=0.80, natural=0.70, baseline=0.80, age_sec=75, past_window=False) == "cancel"
    assert credit_chase_action(limit=3.10, natural=2.90, baseline=3.12, age_sec=10, past_window=False) in {"reprice", "hold", "cap"}


def test_flat_at_1445_uses_the_open_and_not_the_stop():
    walked = walk_marks(
        1.00,
        [
            (_ct(13, 0), 0.90, 0.95, 0.80),
            (_ct(14, 45), 0.70, 2.50, 0.20),
        ],
    )
    assert walked["reason"] == "flat"
    assert walked["exit_debit"] == 0.70


def test_board_keeps_spx_off_the_order_and_names_the_combo_path(tmp_path):
    bars = _session()
    board = build_lunch_board(
        {"SPY": bars, "QQQ": bars, "SPX": bars},
        now=_ct(12, 10),
        store_path=tmp_path / "lunch.json",
        chains={
            "SPY": [
                {"right": "P", "strike": 101, "expiry": "2026-11-03", "bid": 1.20, "ask": 1.30, "contract": "SPY261103P00101000"},
                {"right": "P", "strike": 99, "expiry": "2026-11-03", "bid": 0.40, "ask": 0.50, "contract": "SPY261103P00099000"},
            ],
            "QQQ": [
                {"right": "P", "strike": 101, "expiry": "2026-11-03", "bid": 1.10, "ask": 1.20, "contract": "QQQ261103P00101000"},
                {"right": "P", "strike": 99, "expiry": "2026-11-03", "bid": 0.30, "ask": 0.40, "contract": "QQQ261103P00099000"},
            ],
        },
    )
    assert board["customer"] == "🌤️ Lunch-Break Credit"
    assert board["order_path"] == "combo"
    assert "Sure Winner" not in board["note"]
    symbols = [row["symbol"] for row in board["signals"]]
    assert symbols == ["SPY", "QQQ"]
    spy = board["signals"][0]
    assert spy["webull"] is True
    assert spy["send_order"] is True
    assert spy["short_strike"] == 101
    assert spy["long_strike"] == 99
    assert spy["desk"] == "LunchCredit"


def test_entry_and_exit_alerts_and_the_log_kind():
    card = {
        "symbol": "SPY",
        "send": True,
        "action": "LUNCH_CREDIT",
        "desk": "LunchCredit",
        "lunch_credit": True,
        "expiry": "2026-11-03",
        "at_ct": "12:05",
        "contracts": 1,
        "width": 2,
        "credit": 0.80,
        "credit_mid": 0.80,
        "limit_price": 0.80,
        "max_risk_usd": 120,
        "short_strike": 500,
        "long_strike": 498,
        "ask": 1.25,
        "bid": 1.20,
        "contract": "SPY261103P00500000",
        "short": {"strike": 500, "bid": 1.20, "ask": 1.25},
        "long": {"strike": 498, "bid": 0.40, "ask": 0.45},
        "detail": "🌤️ Lunch-Break Credit · first close above the range high.",
    }
    alerts = collect_trade_alerts({"lunch_credit": {"signals": [card]}})
    text = alerts[0]["message"]
    assert alerts[0]["desk"] == "LunchCredit"
    assert text.startswith("🌤️ LUNCH-BREAK CREDIT SPY SELL 500P / BUY 498P 11/3")
    assert "SELL 500P · Bid $1.20 / Ask $1.25" in text
    assert "BUY 498P · Bid $0.40 / Ask $0.45" in text
    assert "Limit $0.80 (working)" in text
    assert "Credit $0.80" in text
    assert "max risk $120.00" in text
    assert "1 contract" in text
    assert "Sure Winner" not in text
    assert lunch_log_row(card, side="BUY")["kind"] == "LunchCredit"
    exit_text = format_lunch_exit(
        {
            **card,
            "credit": 0.80,
            "exit_debit": 0.40,
            "entry_at": "12:05",
            "exit_at": "13:10",
        }
    )
    assert "Entry Filled @ $0.80 @ 12:05 CT" in exit_text
    assert "Exit Filled @ $0.40 @ 13:10 CT" in exit_text
    assert "Debit $0.40" in exit_text
    assert "P&L +$40" in exit_text
    assert "+50.0%" in exit_text
    assert "Sure Winner" not in exit_text
    cancel = format_lunch_cancel(
        {"symbol": "SPY", "limit_price": 0.80, "strike": 500, "meta": {"short_strike": 500, "long_strike": 498}},
        "not filled — cancelled",
    )
    assert "not filled — cancelled" in cancel
    assert "Limit $0.80" in cancel
    assert format_lunch_entry(card)


def test_paper_combo_buys_nothing_on_spx_and_falls_back_long_leg_first(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="paper-1",
        submit_interval_sec=0,
    )
    card = {
        "symbol": "SPY",
        "send": True,
        "send_order": True,
        "expiry": "2026-11-03",
        "contracts": 1,
        "width": 2,
        "credit_mid": 0.80,
        "credit_natural": 0.70,
        "limit_price": 0.80,
        "max_risk_usd": 120,
        "short_strike": 500,
        "long_strike": 498,
        "short": {"strike": 500, "bid": 1.20, "ask": 1.25, "contract": "SPY261103P00500000", "expiry": "2026-11-03"},
        "long": {"strike": 498, "bid": 0.40, "ask": 0.45, "contract": "SPY261103P00498000", "expiry": "2026-11-03"},
        "webull": True,
        "detail": "lunch",
        "exit_plan": "flat 2:45 PM CT",
    }
    spx = {**card, "symbol": "SPX", "send_order": True, "webull": False}
    LunchRouter(broker, enabled=True).sync({"signals": [card, spx]}, now=_ct(12, 10))
    opens = [order for order in broker.orders if order.meta.get("role") == "open" and not order.meta.get("leg")]
    assert [order.symbol for order in opens] == ["SPY"]
    assert opens[0].action == "SELL"
    assert opens[0].desk == "lunch_credit"
    assert opens[0].meta.get("order_path") == ORDER_PATH
    body = opens[0].meta["openapi_body"]
    assert body["option_strategy"] == "VERTICAL"
    assert body["side"] == "SELL"
    assert body["limit_price"] == "0.80"
    assert [(leg["side"], leg["strike_price"]) for leg in body["legs"]] == [("SELL", "500.00"), ("BUY", "498.00")]
    flags = resolve_webull_live_flags({"enabled": True, "dry_run": False, "paper": False})
    assert flags["sandbox"] is True
    assert flags["paper"] is True
    assert broker.status()["ready_live"] is False
    built = build_vertical_spread_order(
        client_order_id="close1",
        symbol="QQQ",
        expiry="2026-11-03",
        short_strike=500,
        long_strike=498,
        limit_price=0.40,
        opening=False,
    )
    assert built["side"] == "BUY"
    assert [(leg["side"], leg["position_intent"]) for leg in built["legs"]] == [
        ("BUY", "BUY_TO_CLOSE"),
        ("SELL", "SELL_TO_CLOSE"),
    ]
    legs = fallback_leg_orders(card)
    assert [leg["action"] for leg in legs] == ["BUY", "SELL"]
    assert legs[0]["leg"] == "long"
    assert legs[0]["strike"] == 498
    assert legs[1]["strike"] == 500
    assert multi_leg_rejected("INVALID_STRATEGY vertical") is True
    assert multi_leg_rejected("tick size") is False


def test_chase_walks_toward_natural_then_cancels(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="paper-1",
        submit_interval_sec=0,
    )
    card = {
        "symbol": "QQQ",
        "send_order": True,
        "expiry": "2026-11-03",
        "contracts": 1,
        "width": 2,
        "credit_mid": 0.80,
        "credit_natural": 0.70,
        "limit_price": 0.80,
        "short_strike": 500,
        "long_strike": 498,
        "short": {"strike": 500, "bid": 1.20, "ask": 1.30, "expiry": "2026-11-03"},
        "long": {"strike": 498, "bid": 0.40, "ask": 0.50, "expiry": "2026-11-03"},
        "webull": True,
        "detail": "lunch",
    }
    router = LunchRouter(broker, enabled=True)
    router.sync({"signals": [card]}, now=_ct(12, 10))
    card["credit_natural"] = 0.60
    router.sync({"signals": [card]}, now=_ct(12, 11))
    working = [order for order in broker.orders if order.status == "dry_run"]
    assert len(working) == 1
    assert working[0].limit_price == 0.72
    assert working[0].meta.get("order_path") == "combo"
    result = router.sync({"signals": [card]}, now=_ct(12, 12))
    assert all(order.status != "dry_run" for order in broker.orders)
    assert any("not filled — cancelled" in str(note.get("message")) for note in result["notices"])


def test_scorecard_attributes_the_lunch_fill_on_its_own_row():
    orders = [
        {
            "id": "open-spy",
            "desk": "lunch_credit",
            "action": "SELL",
            "symbol": "SPY",
            "right": "P",
            "strike": 500,
            "expiry": "2026-11-03",
            "contract": "SPY|2026-11-03|500/498|P",
            "quantity": 1,
            "limit_price": 0.80,
            "fill_price": 0.80,
            "status": "filled",
            "created_at": "2026-11-03T17:05:00+00:00",
            "updated_at": "2026-11-03T17:06:00+00:00",
            "meta": {
                "golden_kind": "lunch_credit",
                "role": "open",
                "spread_id": "SPY|2026-11-03|500/498|P",
                "short_strike": 500,
                "long_strike": 498,
            },
        },
        {
            "id": "close-spy",
            "desk": "lunch_credit",
            "action": "BUY",
            "symbol": "SPY",
            "right": "P",
            "strike": 500,
            "expiry": "2026-11-03",
            "contract": "SPY|2026-11-03|500/498|P",
            "quantity": 1,
            "limit_price": 0.40,
            "fill_price": 0.40,
            "status": "filled",
            "created_at": "2026-11-03T18:10:00+00:00",
            "updated_at": "2026-11-03T18:11:00+00:00",
            "meta": {
                "golden_kind": "lunch_credit",
                "role": "close",
                "spread_id": "SPY|2026-11-03|500/498|P",
                "entry_price": 0.80,
                "short_strike": 500,
                "long_strike": 498,
            },
        },
        {
            "id": "afternoon",
            "desk": "afternoon_puts",
            "action": "BUY",
            "symbol": "QQQ",
            "right": "P",
            "strike": 490,
            "expiry": "2026-11-03",
            "contract": "QQQ261103P00490000",
            "quantity": 1,
            "fill_price": 1.00,
            "status": "filled",
            "created_at": "2026-11-03T16:00:00+00:00",
            "updated_at": "2026-11-03T16:01:00+00:00",
            "meta": {"golden_kind": "afternoon_puts"},
        },
        {
            "id": "afternoon-sell",
            "desk": "afternoon_puts",
            "action": "SELL",
            "symbol": "QQQ",
            "right": "P",
            "strike": 490,
            "expiry": "2026-11-03",
            "contract": "QQQ261103P00490000",
            "quantity": 1,
            "fill_price": 1.50,
            "status": "filled",
            "created_at": "2026-11-03T18:00:00+00:00",
            "updated_at": "2026-11-03T18:01:00+00:00",
            "meta": {"golden_kind": "afternoon_puts", "entry_price": 1.00},
        },
    ]
    board = build_scorecard_daily_pnl(orders, now=_ct(16, 0), load_side=False)
    day = board["days"][0]
    assert day["desks"]["Lunch-Break Credit"]["pnl"] == 40.0
    assert day["desks"]["Lunch-Break Credit"]["trades"] == 1
    assert day["desks"]["Afternoon Puts"]["pnl"] == 50.0
    assert "Lunch-Break Credit" in board["desks"]
    assert "Sure Winner" not in str(board)


def test_quiet_hours_publish_boards_without_routing(monkeypatch):
    from odte_scanner.fast_desk import publish_index_boards_if_missing
    from odte_scanner.hot_state import clear_hot, hot_snapshot

    calls = {"afternoon": 0, "lunch": 0, "route": 0}

    def afternoon(*, now=None):
        calls["afternoon"] += 1
        return {"ok": True, "signals": [{"symbol": "QQQ"}], "qualified": [], "phase": "closed"}

    def lunch(*, now=None):
        calls["lunch"] += 1
        return {"ok": True, "signals": [], "qualified": [], "phase": "closed", "customer": "🌤️ Lunch-Break Credit"}

    monkeypatch.setattr("odte_scanner.signals.afternoon_puts_board.load_afternoon_board", afternoon)
    monkeypatch.setattr("odte_scanner.signals.lunch_credit_board.load_lunch_board", lunch)
    clear_hot()
    try:
        first = publish_index_boards_if_missing(now=_ct(15, 10))
        assert first == {"afternoon_puts": True, "lunch_credit": True}
        hot = hot_snapshot()
        assert hot["afternoon_puts"]["ok"] is True
        assert hot["lunch_credit"]["ok"] is True
        assert hot["lunch_credit"]["customer"] == "🌤️ Lunch-Break Credit"
        again = publish_index_boards_if_missing(now=_ct(15, 11))
        assert again == {}
        assert calls == {"afternoon": 1, "lunch": 1, "route": 0}
    finally:
        clear_hot()


def test_portal_tile_and_edge_line_and_the_config_flag():
    assert 'id="lunchBanner"' in PAGE
    assert "function renderLunchBanner" in PAGE
    assert "Waiting — range opens 10:00 CT" in PAGE
    assert "Tracking the 10:00–12:00 CT range high" in PAGE
    assert "No Lunch-Break Credit today" in PAGE
    assert "🌤️ Lunch-Break" in PAGE
    banner = PAGE[PAGE.index("function renderLunchBanner") : PAGE.index("function paint")]
    assert "Sure Winner" not in banner
    from odte_scanner.edge.page import EDGE_SECTION

    assert "Lunch-Break Credit — its own book" in EDGE_SECTION
    assert 'id="edgeLunch"' in EDGE_SECTION
    from odte_scanner.config import load_config

    desks = (load_config("config.yaml").get("live_trading") or {}).get("desks") or {}
    assert desks.get("lunch_credit") is True
    assert desks.get("afternoon_puts") is True
