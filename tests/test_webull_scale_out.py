"""Paper scale-out: partial rungs, a trailed runner, and 1-lot full exits."""
from __future__ import annotations

from pathlib import Path

from odte_scanner.signals.exit_rules import load_exit_rules, whole_contracts
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import (
    SANDBOX_TRADE_HOST,
    WebullBroker,
    build_option_place_order,
)
from tests.session_clock import iso_day, session_et


def _broker(tmp_path: Path, name: str = "wb.json") -> WebullBroker:
    return WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / name,
        account_id="margin-1",
        account_id_2="cash-2",
    )


def _trader(broker: WebullBroker, **kwargs) -> AutoTrader:
    params = dict(
        golden_only=True,
        max_orders_per_sync=6,
        max_golden_buys_per_sync=2,
        second_desk="challenge",
    )
    params.update(kwargs)
    return AutoTrader(broker, **params)


def _pos(
    symbol: str,
    strike: float,
    expiry: str,
    *,
    cost: float,
    mark: float,
    quantity: int = 1,
    right: str = "C",
) -> dict:
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    return {
        "contract": f"{symbol}{yymmdd}{right}{int(round(strike * 1000)):08d}",
        "symbol": symbol,
        "right": right,
        "expiry": expiry,
        "strike": strike,
        "quantity": quantity,
        "cost": cost,
        "mark": mark,
    }


def _arm(broker: WebullBroker, pos: dict, *, qty: int, desk: str = "sure_winner", exit_plan: str | None = None) -> None:
    meta: dict = {"original_qty": qty, "gate": "passed"}
    if desk == "sure_winner":
        meta["golden_kind"] = "sure_winner_gold"
    if desk == "challenge":
        meta["account_id"] = "cash-2"
    if exit_plan:
        meta["exit_plan"] = exit_plan
    intent = broker.stage(
        desk=desk,
        action="BUY",
        symbol=pos["symbol"],
        right=pos["right"],
        contract=pos["contract"],
        expiry=pos["expiry"],
        strike=pos["strike"],
        quantity=qty,
        limit_price=pos["cost"],
        reason="scale fixture",
        meta=meta,
    )
    intent.status = "filled"
    intent.fill_price = pos["cost"]
    broker.save()


def _install(monkeypatch, broker: WebullBroker, rows_for) -> None:
    monkeypatch.setattr(broker, "list_option_positions", rows_for)


def _sync(trader: AutoTrader, now, *, challenge: bool = False) -> dict:
    payload = {
        "actions": {"buy_now": [], "sell_now": []},
        "sure_winners": {"winners": []},
        "now": now,
    }
    if challenge:
        payload["challenge"] = {"entry": [], "exit": [], "tickets": []}
    return trader.sync(**payload)


def _sells(out: dict) -> list[dict]:
    return [row for row in out["submitted"] if row["action"] == "SELL"]


def _settle(broker: WebullBroker) -> None:
    for order in broker.orders:
        if order.action == "SELL" and order.status in {"dry_run", "staged", "submitted"}:
            order.status = "filled"
    broker.save()


def test_rung_fractions_round_down_to_whole_contracts():
    assert whole_contracts(3, 40) == 1
    assert whole_contracts(2, 50) == 1
    assert whole_contracts(2, 30) == 0
    assert whole_contracts(1, 40) == 0


def test_config_ladder_parses_and_can_be_turned_off():
    rules = load_exit_rules(
        {
            "sure_winner": {
                "scale_out": {
                    "min_contracts": 3,
                    "steps": [{"at_pct": 40, "sell_pct": 40}, {"at_pct": 80, "sell_pct": 40}],
                    "trail_giveback_pct": 25,
                    "trail_after_pct": 80,
                }
            },
            "challenge": {"scale_out": False},
        }
    )
    assert rules["sure_winner"].scale_out is not None
    assert rules["sure_winner"].scale_out.min_contracts == 3
    assert rules["challenge"].scale_out is None
    assert rules["lottery"].scale_out is None


def test_sure_winner_scales_then_trails_the_runner(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    pos = _pos("NVDA", 120, expiry, cost=1.0, mark=1.30, quantity=3)
    _arm(broker, pos, qty=3)

    def positions(account_id=None):
        if account_id == "cash-2":
            return []
        return [pos]

    _install(monkeypatch, broker, positions)
    first = _sync(trader, morning)
    sells = _sells(first)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "scale out +30%" in sells[0]["reason"]
    assert "sell 1 of 3" in sells[0]["reason"]
    assert "flatten" not in sells[0]["reason"].lower()
    assert sells[0]["meta"]["host"] == SANDBOX_TRADE_HOST
    assert sells[0]["meta"]["sandbox"] is True
    body = build_option_place_order(next(o for o in broker.orders if o.action == "SELL"))
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["legs"][0]["position_intent"] == "SELL_TO_CLOSE"
    assert body["quantity"] == "1"
    buy = next(o for o in broker.orders if o.action == "BUY")
    assert buy.meta["scale_rungs_done"] == [0]
    assert buy.meta["trail_armed"] is True

    _settle(broker)
    pos["quantity"] = 2
    pos["mark"] = 1.80
    second = _sync(trader, morning)
    sells = _sells(second)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 2
    assert "take profit" in sells[0]["reason"]
    assert "+80%" in sells[0]["reason"]

    runner = _broker(tmp_path, "trail.json")
    trail = _trader(runner)
    left = _pos("AMD", 140, expiry, cost=1.0, mark=1.30, quantity=1)
    _arm(runner, left, qty=1)
    _install(
        monkeypatch,
        runner,
        lambda account_id=None: [] if account_id == "cash-2" else [left],
    )
    assert _sells(_sync(trail, morning)) == []
    left["mark"] = 1.15
    trailed = _sync(trail, morning)
    sells = _sells(trailed)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "half the peak gain" in sells[0]["reason"]
    assert sells[0]["limit_price"] == 1.15


def test_gap_through_both_rungs_sells_them_together(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "gap.json")
    trader = _trader(broker)
    pos = _pos("AMD", 160, expiry, cost=1.0, mark=1.80, quantity=3)
    _arm(broker, pos, qty=3)
    _install(
        monkeypatch,
        broker,
        lambda account_id=None: [] if account_id == "cash-2" else [pos],
    )
    sold = _sync(trader, morning)
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 3
    assert "take profit" in sells[0]["reason"]
    assert "+80%" in sells[0]["reason"]


def test_one_contract_still_full_exits_and_small_lots_skip_the_ladder(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "one.json")
    trader = _trader(broker)
    single = _pos("AVGO", 180, expiry, cost=1.0, mark=1.40, quantity=1)
    _arm(broker, single, qty=1)
    _install(
        monkeypatch,
        broker,
        lambda account_id=None: [] if account_id == "cash-2" else [single],
    )
    held = _sync(trader, morning)
    assert _sells(held) == []
    single["mark"] = 1.80
    sold = _sync(trader, morning)
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "take profit" in sells[0]["reason"]
    assert "scale" not in sells[0]["reason"]

    pair = _pos("META", 500, expiry, cost=1.0, mark=1.40, quantity=2)
    _arm(broker, pair, qty=2)
    book = [pair]

    def positions(account_id=None):
        if account_id == "cash-2":
            return []
        return book

    _install(monkeypatch, broker, positions)
    # The filled 1-lot sell above is already settled by being a take-profit
    # dry-run. Mark it filled so it does not block the next name.
    _settle(broker)
    quiet = _sync(trader, morning)
    meta = [row for row in _sells(quiet) if row["symbol"] == "META"]
    assert len(meta) == 1
    assert meta[0]["quantity"] == 1
    assert "scale out +30%" in meta[0]["reason"]


def test_never_scales_into_a_loser(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "lose.json")
    trader = _trader(broker)
    pos = _pos("TSLA", 250, expiry, cost=1.0, mark=0.90, quantity=3)
    _arm(broker, pos, qty=3)
    _install(
        monkeypatch,
        broker,
        lambda account_id=None: [] if account_id == "cash-2" else [pos],
    )
    dipped = _sync(trader, morning)
    assert _sells(dipped) == []

    pos["mark"] = 0.50
    stopped = _sync(trader, morning)
    sells = _sells(stopped)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 3
    assert "stop loss" in sells[0]["reason"]
    assert "−50%" in sells[0]["reason"]
    assert "scale" not in sells[0]["reason"]
    assert sells[0]["limit_price"] == 0.50


def test_same_day_stays_all_in_and_clock_flattens_the_lot(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(morning)
    broker = _broker(tmp_path, "odte.json")
    trader = _trader(broker)
    pos = _pos("QQQ", 500, expiry, cost=1.0, mark=1.40, quantity=3)
    _arm(broker, pos, qty=3)
    _install(
        monkeypatch,
        broker,
        lambda account_id=None: [] if account_id == "cash-2" else [pos],
    )
    sold = _sync(trader, morning)
    sells = _sells(sold)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 3
    assert "take profit" in sells[0]["reason"]
    assert "+30%" in sells[0]["reason"]

    flat_broker = _broker(tmp_path, "flat.json")
    flat_trader = _trader(flat_broker)
    flat = _pos("SPY", 560, expiry, cost=1.0, mark=1.10, quantity=3)
    _arm(flat_broker, flat, qty=3)
    _install(
        monkeypatch,
        flat_broker,
        lambda account_id=None: [] if account_id == "cash-2" else [flat],
    )
    closed = _sync(flat_trader, session_et(15, 30))
    sells = _sells(closed)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 3
    assert "2:30 PM CT" in sells[0]["reason"]


def test_challenge_ladder_trails_after_the_second_rung_floors_to_zero(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path, "ch.json")
    trader = _trader(broker)
    pos = _pos("BABA", 90, expiry, cost=1.0, mark=1.40, quantity=2)
    _arm(broker, pos, qty=2, desk="challenge")

    def positions(account_id=None):
        if account_id == "cash-2":
            return [pos]
        return []

    _install(monkeypatch, broker, positions)
    first = _sync(trader, morning, challenge=True)
    sells = _sells(first)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert sells[0]["desk"] == "challenge"
    assert "scale out +40%" in sells[0]["reason"]
    assert "sell 1 of 2" in sells[0]["reason"]

    _settle(broker)
    pos["quantity"] = 1
    pos["mark"] = 2.0
    at_target = _sync(trader, morning, challenge=True)
    assert _sells(at_target) == []

    pos["mark"] = 1.60
    trailed = _sync(trader, morning, challenge=True)
    sells = _sells(trailed)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "trail" in sells[0]["reason"]
    assert "20" in sells[0]["reason"]


def test_rip_fifty_plan_scales_and_a_one_lot_still_takes_the_full_target(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "rip.json")
    trader = _trader(broker)
    pos = _pos("GOOGL", 180, expiry, cost=1.0, mark=1.30, quantity=2)
    _arm(broker, pos, qty=2, desk="challenge", exit_plan="TP +50% / SL −50%")

    def positions(account_id=None):
        if account_id == "cash-2":
            return [pos]
        return []

    _install(monkeypatch, broker, positions)
    first = _sync(trader, morning, challenge=True)
    sells = _sells(first)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "scale out +30%" in sells[0]["reason"]

    _settle(broker)
    pos["quantity"] = 1
    pos["mark"] = 1.50
    held = _sync(trader, morning, challenge=True)
    assert _sells(held) == []

    pos["mark"] = 0.50
    stopped = _sync(trader, morning, challenge=True)
    sells = _sells(stopped)
    assert len(sells) == 1
    assert sells[0]["quantity"] == 1
    assert "stop loss" in sells[0]["reason"]

    one = _pos("GOOG", 175, expiry, cost=1.0, mark=1.50, quantity=1)
    _arm(broker, one, qty=1, desk="challenge", exit_plan="TP +50% / SL −50%")
    book = {"pos": pos, "one": one}

    def both(account_id=None):
        if account_id == "cash-2":
            return [book["one"]]
        return []

    _install(monkeypatch, broker, both)
    _settle(broker)
    # The 2-lot is flat on the book; the 1-lot is at +50% and must leave in full.
    sold = _sync(trader, morning, challenge=True)
    goog = [row for row in _sells(sold) if row["symbol"] == "GOOG"]
    assert len(goog) == 1
    assert goog[0]["quantity"] == 1
    assert "take profit" in goog[0]["reason"]
    assert "scale" not in goog[0]["reason"]


def test_paper_size_respects_risk_and_leaves_lottery_at_one_lot(tmp_path: Path):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    today = iso_day(morning)
    broker = _broker(tmp_path, "size.json")
    trader = _trader(broker, max_risk_per_trade_usd=250)
    assert trader.close_legacy_positions is False
    assert trader.max_golden_buys_per_sync == 2

    def card(symbol: str, strike: float, ask: float, expiry: str) -> dict:
        yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
        return {
            "symbol": symbol,
            "right": "C",
            "action": "SURE_WINNER",
            "confidence_tier": "MAX",
            "color": "gold",
            "go_huge": True,
            "ask": ask,
            "contract": f"{symbol}{yymmdd}C{int(round(strike * 1000)):08d}",
            "expiry": expiry,
            "strike": strike,
            "headline": "MAX gold",
        }

    # Two buys fit the golden cap. A third name is left for the next pass.
    opened = trader.sync(
        sure_winners={
            "winners": [
                card("AAA", 100, 0.50, weekly),
                card("BBB", 100, 1.00, weekly),
                card("CCC", 100, 0.50, weekly),
            ]
        },
        now=morning,
    )
    buys = [row for row in opened["submitted"] if row["action"] == "BUY"]
    assert sorted(row["symbol"] for row in buys) == ["AAA", "BBB"]
    by_symbol = {row["symbol"]: row["quantity"] for row in buys}
    assert by_symbol["AAA"] == 3
    assert by_symbol["BBB"] == 2

    pricey = _broker(tmp_path, "pricey.json")
    pricey_trader = _trader(pricey, max_risk_per_trade_usd=250)
    out = pricey_trader.sync(
        sure_winners={"winners": [card("DDD", 100, 2.00, weekly), card("EEE", 100, 0.50, today)]},
        now=morning,
    )
    qty = {row["symbol"]: row["quantity"] for row in out["submitted"] if row["action"] == "BUY"}
    assert qty["DDD"] == 1
    assert qty["EEE"] == 1

    challenge_broker = _broker(tmp_path, "ch-size.json")
    challenge = _trader(challenge_broker, max_risk_per_trade_usd=250)
    challenged = challenge.sync(
        challenge={
            "entry": [
                {
                    "symbol": "BABA",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 0.50,
                    "contract": f"BABA{weekly[2:4]}{weekly[5:7]}{weekly[8:10]}C{int(round(90 * 1000)):08d}",
                    "expiry": weekly,
                    "strike": 90,
                }
            ],
            "exit": [],
            "tickets": [],
        },
        sure_winners={"winners": []},
        now=morning,
    )
    ch_buys = [row for row in challenged["submitted"] if row["action"] == "BUY"]
    assert len(ch_buys) == 1
    assert ch_buys[0]["quantity"] == 2
    assert ch_buys[0]["desk"] == "challenge"

    lottery_broker = _broker(tmp_path, "lot.json")
    lottery = AutoTrader(
        lottery_broker,
        golden_only=False,
        max_contracts=1,
        max_risk_per_trade_usd=250,
        require_perfect_hist=True,
    )
    lotto = lottery.sync(
        lottery={
            "buy_now": [
                {
                    "symbol": "QQQ",
                    "ask": 0.50,
                    "hist_win_pct": 100,
                    "hist_samples": 8,
                    "expiry": weekly,
                    "strike": 480,
                    "contract": f"QQQ{weekly[2:4]}{weekly[5:7]}{weekly[8:10]}C{int(round(480 * 1000)):08d}",
                }
            ],
            "sell_now": [],
        },
        now=morning,
    )
    lotto_buys = [row for row in lotto["submitted"] if row["action"] == "BUY"]
    assert len(lotto_buys) == 1
    assert lotto_buys[0]["desk"] == "lottery"
    assert lotto_buys[0]["quantity"] == 1


def test_scale_sell_stays_on_the_sandbox_host(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "host.json",
        sandbox=False,
        paper=False,
    )
    assert broker.sandbox is True
    assert broker.paper is True
    assert broker.status()["host"] == SANDBOX_TRADE_HOST
