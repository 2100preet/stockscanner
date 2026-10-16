"""Paper exits from open sandbox longs, not from a gold card still on the board."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.trading.auto_trader import AutoTrader, _pcts_from_exit_plan
from odte_scanner.trading.webull import WebullBroker, build_option_place_order, parse_option_positions
from tests.session_clock import iso_day, session_et

ET = ZoneInfo("America/New_York")


def _broker(tmp_path: Path, name: str = "wb.json") -> WebullBroker:
    return WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / name,
        account_id="margin-1",
        account_id_2="cash-2",
    )


def _trader(broker: WebullBroker) -> AutoTrader:
    return AutoTrader(
        broker,
        golden_only=True,
        max_orders_per_sync=6,
        second_desk="challenge",
    )


def _pos(
    symbol: str,
    strike: float,
    expiry: str,
    *,
    cost: float,
    mark: float,
    right: str = "C",
) -> dict:
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    return {
        "contract": f"{symbol}{yymmdd}{right}{int(round(strike * 1000)):08d}",
        "symbol": symbol,
        "right": right,
        "expiry": expiry,
        "strike": strike,
        "quantity": 1,
        "cost": cost,
        "mark": mark,
    }


def _install_positions(monkeypatch, broker: WebullBroker, rows_for) -> None:
    monkeypatch.setattr(broker, "list_option_positions", rows_for)


def _sells(out: dict) -> list[dict]:
    return [row for row in out["submitted"] if row["action"] == "SELL"]


def test_exit_plan_percents_override_the_desk_default():
    assert _pcts_from_exit_plan("TP +50% / SL −50%") == (50.0, 50.0)
    assert _pcts_from_exit_plan(
        "EXIT when premium ≥$3.00 (+50%) or stop −50%, or max hold 2d"
    ) == (50.0, 50.0)
    assert _pcts_from_exit_plan("hold the winner") is None


def test_position_mark_prefers_bid_over_last():
    rows = parse_option_positions(
        [
            {
                "symbol": "GOOGL",
                "instrument_type": "OPTION",
                "quantity": "1",
                "cost_price": "1.00",
                "bid": "1.80",
                "last_price": "1.40",
                "legs": [
                    {
                        "symbol": "GOOGL",
                        "option_type": "CALL",
                        "option_expire_date": "2026-10-16",
                        "option_exercise_price": "357.5",
                    }
                ],
            }
        ]
    )
    assert rows[0]["mark"] == 1.8
    assert rows[0]["cost"] == 1.0
    assert rows[0]["strike"] == 357.5


def test_take_profit_fires_from_position_mark_without_gold_card(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    state = {"mark": 1.50}
    pos = _pos("NVDA", 120, expiry, cost=1.0, mark=state["mark"])

    def positions(account_id=None):
        pos["mark"] = state["mark"]
        if account_id == "cash-2":
            return []
        return [pos]

    _install_positions(monkeypatch, broker, positions)
    held = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert _sells(held) == []
    assert broker.orders == []

    state["mark"] = 1.80
    sold = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["contract"] == pos["contract"]
    assert sells[0]["limit_price"] == 1.80
    assert "take profit" in sells[0]["reason"]
    assert sells[0]["meta"]["golden_kind"] == "position_exit"
    body = build_option_place_order(next(o for o in broker.orders if o.action == "SELL"))
    assert body["position_intent"] == "SELL_TO_CLOSE"


def test_stop_fires_from_position_mark_without_gold_card(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    state = {"mark": 0.51}
    pos = _pos("AVGO", 180, expiry, cost=1.0, mark=state["mark"])

    def positions(account_id=None):
        pos["mark"] = state["mark"]
        if account_id == "cash-2":
            return []
        return [pos]

    _install_positions(monkeypatch, broker, positions)
    held = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert _sells(held) == []

    state["mark"] = 0.50
    sold = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["symbol"] == "AVGO"
    assert sells[0]["limit_price"] == 0.50
    assert "stop loss" in sells[0]["reason"]
    body = build_option_place_order(next(o for o in broker.orders if o.action == "SELL"))
    assert body["position_intent"] == "SELL_TO_CLOSE"


def test_sell_allowed_without_golden_open_order(tmp_path: Path, monkeypatch):
    """A journal SELL closes a sandbox long that was never a golden order."""
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    gtlb = _pos("GTLB", 45, expiry, cost=2.40, mark=2.20)

    def positions(account_id=None):
        if account_id == "cash-2":
            return []
        return [gtlb]

    _install_positions(monkeypatch, broker, positions)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                {
                    "symbol": "GTLB",
                    "right": "C",
                    "contract": gtlb["contract"],
                    "expiry": expiry,
                    "strike": 45,
                    "bid": 2.20,
                    "detail": "journal SELL GTLB",
                },
                {
                    "symbol": "AVGO",
                    "right": "C",
                    "contract": "AVGO261113C00180000",
                    "expiry": expiry,
                    "strike": 180,
                    "bid": 1.1,
                    "detail": "journal SELL AVGO",
                },
            ],
        },
        sure_winners={"winners": []},
        now=morning,
    )
    sells = _sells(out)
    assert [row["symbol"] for row in sells] == ["GTLB"]
    assert sells[0]["limit_price"] == 2.20
    assert sells[0]["meta"]["golden_kind"] == "position_exit"
    assert any("closes" in row["reason"] for row in out["filtered"])
    body = build_option_place_order(next(o for o in broker.orders if o.action == "SELL"))
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["symbol"] == "GTLB"


def test_rip_exit_plan_takes_profit_at_fifty_without_a_board_card(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    googl = _pos("GOOGL", 357.5, expiry, cost=1.0, mark=1.0)
    entry = {
        "symbol": "GOOGL",
        "right": "C",
        "action": "ENTRY",
        "ask": 1.0,
        "contract": googl["contract"],
        "expiry": expiry,
        "strike": 357.5,
        "exit_plan": "TP +50% / SL −50%",
    }
    opened = trader.sync(
        challenge={"entry": [entry], "exit": [], "tickets": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert [row["symbol"] for row in opened["submitted"] if row["action"] == "BUY"] == ["GOOGL"]
    buy = next(order for order in broker.orders if order.action == "BUY")
    assert buy.meta["exit_plan"] == "TP +50% / SL −50%"
    assert buy.meta["account_id"] == "cash-2"

    def positions(account_id=None):
        if account_id == "cash-2":
            return [googl]
        return []

    _install_positions(monkeypatch, broker, positions)
    googl["mark"] = 1.40
    held = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        challenge={"entry": [], "exit": [], "tickets": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert _sells(held) == []

    googl["mark"] = 1.50
    sold = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        challenge={"entry": [], "exit": [], "tickets": []},
        sure_winners={"winners": []},
        now=morning,
    )
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["symbol"] == "GOOGL"
    assert sells[0]["limit_price"] == 1.50
    assert "take profit" in sells[0]["reason"]
    assert sells[0]["meta"]["account_id"] == "cash-2"
    assert sells[0]["desk"] == "challenge"


def test_challenge_hundred_holds_through_eighty(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    baba = _pos("BABA", 180, expiry, cost=1.0, mark=1.80)

    def positions(account_id=None):
        if account_id == "cash-2":
            return [baba]
        return []

    _install_positions(monkeypatch, broker, positions)
    held = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert _sells(held) == []
    baba["mark"] = 2.0
    sold = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    sells = _sells(sold)
    assert len(sells) == 1
    assert "take profit" in sells[0]["reason"]
    assert "+100%" in sells[0]["reason"]
    assert sells[0]["meta"]["account_id"] == "cash-2"


def test_1545_flattens_same_day_and_challenge_next_session(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    late = session_et(15, 50)
    today = iso_day(late)
    nxt = iso_day(session_et(11, 0, weekday_offset=1))
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    qqq = _pos("QQQ", 725, today, cost=1.0, mark=1.10)
    spy = _pos("SPY", 771, today, cost=1.0, mark=1.10)
    nvda = _pos("NVDA", 120, weekly, cost=1.0, mark=1.10)
    baba = _pos("BABA", 180, nxt, cost=1.0, mark=1.10)

    def positions(account_id=None):
        if account_id == "cash-2":
            return [baba]
        return [qqq, spy, nvda]

    _install_positions(monkeypatch, broker, positions)
    early = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert _sells(early) == []

    flat = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=late,
    )
    sells = _sells(flat)
    assert {row["symbol"] for row in sells} == {"QQQ", "SPY", "BABA"}
    assert all("15:45" in row["reason"] for row in sells)
    baba_sell = next(row for row in sells if row["symbol"] == "BABA")
    assert baba_sell["meta"]["account_id"] == "cash-2"
    assert "NVDA" not in {row["symbol"] for row in sells}
    assert all(
        build_option_place_order(order)["position_intent"] == "SELL_TO_CLOSE"
        for order in broker.orders
        if order.action == "SELL"
    )


def test_friday_challenge_into_monday_flattens_sure_winner_does_not(tmp_path: Path, monkeypatch):
    friday = datetime(2026, 10, 16, 15, 50, tzinfo=ET)
    assert friday.weekday() == 4
    broker = _broker(tmp_path, "fri.json")
    trader = _trader(broker)
    baba = _pos("BABA", 180, "2026-10-19", cost=1.0, mark=1.05)
    nvda = _pos("NVDA", 140, "2026-10-19", cost=1.0, mark=1.05)

    def positions(account_id=None):
        if account_id == "cash-2":
            return [baba]
        return [nvda]

    _install_positions(monkeypatch, broker, positions)
    out = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=friday,
    )
    sells = _sells(out)
    assert [row["symbol"] for row in sells] == ["BABA"]
    assert "15:45" in sells[0]["reason"]
    assert sells[0]["meta"]["account_id"] == "cash-2"
