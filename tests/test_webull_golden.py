"""Golden-only Webull routing and paper/sandbox targeting."""
from __future__ import annotations

from pathlib import Path

import pytest

from odte_scanner.live_desk import resolve_webull_live_flags
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import (
    PRODUCTION_TRADE_HOST,
    SANDBOX_TRADE_HOST,
    WebullBroker,
    WebullOrderIntent,
    build_option_place_order,
    classify_broker_order,
    webull_trade_host,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _live_broker(tmp_path: Path, clock: _Clock | None = None) -> WebullBroker:
    kwargs: dict = {
        "enabled": True,
        "dry_run": False,
        "app_key": "k",
        "app_secret": "s",
        "account_id": "acct",
        "ledger_path": tmp_path / "wb.json",
        "submit_interval_sec": 1.5,
    }
    if clock is not None:
        kwargs["sleeper"] = clock.sleep
        kwargs["clock"] = clock.now
    return WebullBroker(**kwargs)


def _gold_winner() -> dict:
    return {
        "symbol": "NVDA",
        "right": "C",
        "action": "SURE_WINNER",
        "confidence_tier": "MAX",
        "color": "gold",
        "go_huge": True,
        "ask": 1.4,
        "contract": "NVDA260821C00120000",
        "expiry": "2026-08-21",
        "strike": 120,
        "win_pct": 100,
        "win_samples": 6,
        "headline": "MAX gold",
    }


def _amber_winner() -> dict:
    return {
        "symbol": "AMD",
        "right": "C",
        "confidence_tier": "HIGH",
        "color": "amber",
        "ask": 0.9,
        "contract": "AMD260821C00160000",
        "expiry": "2026-08-21",
        "strike": 160,
        "win_pct": 90,
        "win_samples": 8,
    }


def _golden_flow() -> dict:
    return {
        "symbol": "SPY",
        "right": "C",
        "tier": "golden",
        "flags": ["golden", "large_premium"],
        "ask": 2.1,
        "mid": 2.0,
        "contract": "SPY260807C00500000",
        "expiry": "2026-08-07",
        "strike": 500,
    }


def _unusual_flow() -> dict:
    return {
        "symbol": "QQQ",
        "right": "P",
        "tier": "unusual",
        "flags": ["unusual"],
        "ask": 1.1,
        "contract": "QQQ260807P00480000",
        "expiry": "2026-08-07",
        "strike": 480,
    }


def test_golden_kind_classifier():
    from odte_scanner.trading.golden import sure_winner_card_class
    from odte_scanner.ui import PAGE

    assert golden_signal_kind(_gold_winner()) == "sure_winner_gold"
    assert golden_signal_kind({"confidence_tier": "MAX"}) == "sure_winner_gold"
    assert sure_winner_card_class({"confidence_tier": "MAX"}) == "sure-max"
    # HIGH is the gold sure-high card (DOCU / JPM on 2026-10-14).
    assert golden_signal_kind(_amber_winner()) == "sure_winner_gold"
    assert sure_winner_card_class(_amber_winner()) == "sure-high"
    assert golden_signal_kind({"confidence_tier": "HIGH", "sure_winner": True}) == "sure_winner_gold"
    # SOLID is sure-solid, not a gold card, even with a stray gold color.
    assert golden_signal_kind({"confidence_tier": "SOLID", "sure_winner": True, "color": "gold"}) is None
    assert sure_winner_card_class({"confidence_tier": "SOLID", "sure_winner": True}) == "sure-solid"
    assert golden_signal_kind({"confidence_tier": "SOLID", "go_huge": True}) is None
    assert golden_signal_kind({"confidence_tier": "SOLID", "sure_winner": True, "go_huge": True}) == "sure_winner_gold"
    # Flow-desk Golden stays on the board and does not auto-trade.
    assert golden_signal_kind(_golden_flow()) is None
    assert golden_signal_kind({"flags": ["golden"]}) is None
    assert golden_signal_kind(_unusual_flow()) is None
    assert golden_signal_kind({"tier": "aggressive"}) is None
    assert golden_signal_kind({"golden": True, "pattern": "ASCENDING_TRIANGLE"}) is None
    assert "function sureWinnerCardClass" in PAGE
    assert "sure-high" in PAGE
    assert 'tier === "HIGH"' in PAGE


def test_golden_only_routes_gold_and_drops_the_rest(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=True, golden_only=True)
    out = trader.sync(
        lottery={
            "buy_now": [
                {
                    "symbol": "IWM",
                    "action": "BUY_NOW",
                    "ask": 0.4,
                    "contract": "IWM260807C00220000",
                    "expiry": "2026-08-07",
                    "strike": 220,
                    "hist_win_pct": 100,
                    "hist_samples": 9,
                }
            ],
            "sell_now": [],
        },
        actions={
            "buy_now": [
                {
                    "symbol": "AAPL",
                    "ask": 0.8,
                    "dte_bucket": "0dte",
                    "contract": "AAPL260807C00200000",
                    "expiry": "2026-08-07",
                    "strike": 200,
                    "hist_win_pct": 100,
                    "hist_samples": 5,
                }
            ],
            "sell_now": [],
        },
        sure_winners={"winners": [_gold_winner(), _amber_winner()], "buy_now": []},
        option_flow={"prints": [_golden_flow(), _unusual_flow()]},
    )
    symbols = {o["symbol"] for o in out["submitted"]}
    assert symbols == {"NVDA", "AMD"}
    desks = {o["desk"] for o in out["submitted"]}
    assert desks == {"sure_winner"}
    assert "SPY" not in symbols
    assert all(o["status"] == "dry_run" for o in out["submitted"])
    assert all((o.get("meta") or {}).get("golden_kind") for o in out["submitted"])
    ledger_symbols = {o.symbol for o in broker.orders}
    assert "IWM" not in ledger_symbols
    assert "AAPL" not in ledger_symbols
    assert "QQQ" not in ledger_symbols
    assert "SPY" not in ledger_symbols
    assert out["filtered_n"] >= 4
    assert out["broker"]["dry_run"] is True
    assert out["broker"]["host"] == SANDBOX_TRADE_HOST


def test_high_sure_winner_routes_and_golden_flow_does_not(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=True, min_hist_win_samples=3)
    high = _amber_winner()
    assert "hist_win_pct" not in high
    out = trader.sync(
        sure_winners={"winners": [high]},
        option_flow={"golden": [_golden_flow()]},
    )
    assert out["submitted_n"] == 1
    assert out["submitted"][0]["symbol"] == "AMD"
    assert out["submitted"][0]["desk"] == "sure_winner"
    assert out["submitted"][0]["meta"]["golden_kind"] == "sure_winner_gold"
    assert all(o.symbol != "SPY" for o in broker.orders)


def test_incomplete_golden_flow_is_not_staged(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker)
    out = trader.sync(
        option_flow={
            "prints": [
                {
                    "symbol": "TSLA",
                    "tier": "golden",
                    "right": "C",
                    "ask": 1.0,
                }
            ]
        }
    )
    assert out["submitted_n"] == 0
    assert broker.orders == []
    assert any("not auto-traded" in r["reason"] for r in out["filtered"])


def test_golden_exit_routes_sell_of_open_gold(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    first = trader.sync(sure_winners={"winners": [_gold_winner()]})
    assert first["submitted_n"] == 1
    second = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                {
                    "symbol": "NVDA",
                    "right": "C",
                    "bid": 1.8,
                    "contract": "NVDA260821C00120000",
                    "expiry": "2026-08-21",
                    "strike": 120,
                    "dte_bucket": "weekly",
                }
            ],
        }
    )
    sells = [o for o in second["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "NVDA"
    assert sells[0]["meta"]["golden_kind"] == "golden_exit"


def test_option_order_body_matches_openapi_shape():
    intent = WebullOrderIntent(
        id="wb-abc123",
        desk="golden_flow",
        action="BUY",
        symbol="aapl",
        right="C",
        contract="AAPL260619C00220000",
        expiry="2026-06-19",
        strike=220,
        quantity=1,
        limit_price=11.25,
    )
    body = build_option_place_order(intent)
    assert body["instrument_type"] == "OPTION"
    assert body["option_strategy"] == "SINGLE"
    assert body["order_type"] == "LIMIT"
    assert body["entrust_type"] == "QTY"
    assert body["market"] == "US"
    assert body["symbol"] == "AAPL"
    assert body["side"] == "BUY"
    assert body["position_intent"] == "BUY_TO_OPEN"
    assert body["client_order_id"] == "wb-abc123"
    leg = body["legs"][0]
    assert leg["option_type"] == "CALL"
    assert leg["strike_price"] == "220.00"
    assert leg["option_expire_date"] == "2026-06-19"
    assert leg["instrument_type"] == "OPTION"
    sell = WebullOrderIntent(
        id="wb-sell",
        desk="golden_exit",
        action="SELL",
        symbol="AAPL",
        right="P",
        contract=None,
        expiry="2026-06-19",
        strike=220,
        quantity=1,
        limit_price=1,
        tif="GTC",
    )
    sold = build_option_place_order(sell)
    assert sold["time_in_force"] == "DAY"
    assert sold["legs"][0]["option_type"] == "PUT"
    assert sold["position_intent"] == "SELL_TO_CLOSE"
    naked = WebullOrderIntent(
        id="wb-mkt",
        desk="golden_flow",
        action="BUY",
        symbol="AAPL",
        right="C",
        contract=None,
        expiry="2026-06-19",
        strike=220,
        quantity=1,
        limit_price=None,
    )
    with pytest.raises(ValueError, match="limit"):
        build_option_place_order(naked)


def test_sell_without_open_golden_is_not_staged(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                {
                    "symbol": "NVDA",
                    "right": "C",
                    "confidence_tier": "MAX",
                    "color": "gold",
                    "bid": 1.8,
                    "contract": "NVDA260821C00120000",
                    "expiry": "2026-08-21",
                    "strike": 120,
                }
            ],
        }
    )
    assert out["submitted_n"] == 0
    assert broker.orders == []
    assert any("closes" in r["reason"] for r in out["filtered"])


def test_sell_does_not_close_a_different_contract(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    assert trader.sync(sure_winners={"winners": [_gold_winner()]})["submitted_n"] == 1
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                {
                    "symbol": "NVDA",
                    "right": "C",
                    "bid": 0.4,
                    "contract": "NVDA260821C00130000",
                    "expiry": "2026-08-21",
                    "strike": 130,
                }
            ],
        }
    )
    assert [o for o in out["submitted"] if o["action"] == "SELL"] == []
    assert all(o.action != "SELL" for o in broker.orders)


def test_duplicate_golden_sell_is_not_restaged(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    trader.sync(sure_winners={"winners": [_gold_winner()]})
    sell = {
        "symbol": "NVDA",
        "right": "C",
        "bid": 1.8,
        "contract": "NVDA260821C00120000",
        "expiry": "2026-08-21",
        "strike": 120,
    }
    first = trader.sync(actions={"buy_now": [], "sell_now": [sell]})
    second = trader.sync(actions={"buy_now": [], "sell_now": [sell]})
    assert len([o for o in first["submitted"] if o["action"] == "SELL"]) == 1
    assert [o for o in second["submitted"] if o["action"] == "SELL"] == []
    assert len([o for o in broker.orders if o.action == "SELL"]) == 1


def test_broker_refuses_production_host(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        sandbox=False,
        paper=False,
        app_key="k",
        app_secret="s",
        account_id="acct",
        ledger_path=tmp_path / "wb.json",
    )
    assert broker.sandbox is True
    assert broker.paper is True
    assert broker.status()["host"] == SANDBOX_TRADE_HOST
    assert broker.status()["ready_live"] is False


def test_paper_mode_stays_on_sandbox_even_if_webull_live(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "live-acct")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    flags = resolve_webull_live_flags(
        {"enabled": False, "dry_run": True, "paper": True, "golden_only": True}
    )
    assert flags["paper"] is True
    assert flags["sandbox"] is True
    assert flags["dry_run"] is True
    assert flags["enabled"] is False
    assert flags["host"] == SANDBOX_TRADE_HOST
    assert flags["golden_only"] is True
    assert webull_trade_host(sandbox=True) == SANDBOX_TRADE_HOST
    assert webull_trade_host(sandbox=False) == PRODUCTION_TRADE_HOST


def test_paper_submit_env_arms_sandbox_only(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "sandbox-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "sandbox-secret")
    monkeypatch.setenv("WEBULL_PAPER_ACCOUNT_ID", "paper-acct")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "live-acct")
    monkeypatch.setenv("WEBULL_PAPER_SUBMIT", "1")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True, "paper": True})
    assert flags["enabled"] is True
    assert flags["dry_run"] is False
    assert flags["sandbox"] is True
    assert flags["paper"] is True
    assert flags["account_id"] == "paper-acct"
    assert flags["host"] == SANDBOX_TRADE_HOST


def test_webull_openapi_sdk_is_importable():
    """Production submit imports these modules. A missing package rejects every order."""
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

    assert ApiClient is not None
    assert TradeClient is not None


def _buy_gold(tmp_path: Path, winner: dict | None = None) -> tuple[WebullBroker, AutoTrader, dict]:
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    row = dict(winner or _gold_winner())
    opened = trader.sync(sure_winners={"winners": [row]})
    assert opened["submitted_n"] == 1
    assert opened["submitted"][0]["action"] == "BUY"
    assert opened["submitted"][0]["contract"] == row["contract"]
    return broker, trader, row


def _sell_row(winner: dict, **overrides) -> dict:
    row = {
        "symbol": winner["symbol"],
        "right": winner["right"],
        "contract": winner["contract"],
        "expiry": winner["expiry"],
        "strike": winner["strike"],
        "dte_bucket": "0dte",
    }
    row.update(overrides)
    return row


def test_take_profit_sells_the_open_contract(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(winner, bid=2.6, detail="take profit +86% ≥ +80%")],
        },
        sure_winners={"winners": []},
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["contract"] == winner["contract"]
    assert sells[0]["limit_price"] == 2.6
    assert sells[0]["desk"] == "golden_exit"
    assert sells[0]["meta"]["golden_kind"] == "golden_exit"
    body = build_option_place_order(
        next(o for o in trader.broker.orders if o.action == "SELL")
    )
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["legs"][0]["strike_price"] == "120.00"
    assert body["legs"][0]["option_expire_date"] == winner["expiry"]


def test_stop_and_reclaim_sell_the_same_contract(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    stop = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(winner, bid=0.4, detail="stop loss -71% ≤ −50%")],
        }
    )
    sells = [o for o in stop["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["contract"] == winner["contract"]
    assert sells[0]["limit_price"] == 0.4

    broker2 = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb2.json")
    trader2 = AutoTrader(broker2, golden_only=True, max_orders_per_sync=4)
    again = dict(winner)
    assert trader2.sync(sure_winners={"winners": [again]})["submitted_n"] == 1
    reclaim = trader2.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                _sell_row(
                    again,
                    bid=1.1,
                    detail="5m dump -0.40% — exit 0DTE call risk",
                )
            ],
        },
        sure_winners={"winners": []},
    )
    sold = [o for o in reclaim["submitted"] if o["action"] == "SELL"]
    assert len(sold) == 1
    assert sold[0]["contract"] == again["contract"]
    assert sold[0]["limit_price"] == 1.1


def test_flatten_missing_bid_still_sells(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                _sell_row(
                    winner,
                    detail="0DTE time-stop — flatten by 15:45 ET (no gamma into the close)",
                )
            ],
        }
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["contract"] == winner["contract"]
    assert sells[0]["limit_price"] == 0.01
    assert sells[0]["meta"]["limit_source"] == "flatten_floor"
    assert sells[0]["status"] == "dry_run"


def test_flatten_stale_entry_bid_is_marketable(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                _sell_row(
                    winner,
                    bid=winner["ask"],
                    detail="0DTE time-stop — flatten by 15:45 ET; live ask $0.26 vs entry $1.40",
                )
            ],
        }
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    # The reason names a live print, so that price wins over the stale entry echo.
    assert sells[0]["limit_price"] == 0.26
    assert sells[0]["meta"]["limit_source"] == "live_reason"
    assert sells[0]["contract"] == winner["contract"]


def test_flatten_stale_bid_without_live_print_uses_floor(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                _sell_row(
                    winner,
                    bid=winner["ask"],
                    detail="0DTE time-stop — flatten by 15:45 ET",
                )
            ],
        }
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["limit_price"] == 0.01
    assert sells[0]["meta"]["limit_source"] == "flatten_floor"
    assert sells[0]["contract"] == winner["contract"]


def test_priced_flatten_keeps_the_live_bid(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [
                _sell_row(winner, bid=0.55, detail="0DTE time-stop — flatten by 15:45 ET")
            ],
        }
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["limit_price"] == 0.55
    assert sells[0]["meta"]["limit_source"] == "bid"


def test_take_profit_without_a_price_is_not_a_blind_flatten(tmp_path: Path):
    _broker, trader, winner = _buy_gold(tmp_path)
    out = trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(winner, detail="take profit +86% ≥ +80%")],
        }
    )
    assert [o for o in out["submitted"] if o["action"] == "SELL"] == []
    assert any("limit price" in r["reason"] for r in out["filtered"])


def test_clock_flatten_when_golden_signal_leaves_the_board(tmp_path: Path):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 50)
    expiry = iso_day(late)
    winner = _gold_winner()
    winner["expiry"] = expiry
    winner["contract"] = f"NVDA{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00120000"
    _broker, trader, row = _buy_gold(tmp_path, winner)
    out = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        option_flow={"prints": []},
        now=late,
    )
    sells = [o for o in out["submitted"] if o["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["contract"] == row["contract"]
    assert sells[0]["expiry"] == expiry
    assert sells[0]["strike"] == 120
    assert sells[0]["limit_price"] == 0.01
    assert sells[0]["meta"]["limit_source"] == "flatten_floor"
    assert "left the board" in sells[0]["reason"]


def test_open_golden_is_not_flattened_before_1545(tmp_path: Path):
    from tests.session_clock import iso_day, session_et

    morning = session_et(11, 0)
    expiry = iso_day(morning)
    winner = _gold_winner()
    winner["expiry"] = expiry
    winner["contract"] = f"NVDA{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00120000"
    _broker, trader, _row = _buy_gold(tmp_path, winner)
    out = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning,
    )
    assert [o for o in out["submitted"] if o["action"] == "SELL"] == []


def test_weekly_golden_is_not_flattened_at_the_close(tmp_path: Path):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 50)
    weekly = session_et(11, 0, weekday_offset=2)
    expiry = iso_day(weekly)
    winner = _gold_winner()
    winner["expiry"] = expiry
    winner["contract"] = f"NVDA{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00120000"
    _broker, trader, _row = _buy_gold(tmp_path, winner)
    out = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=late,
    )
    assert [o for o in out["submitted"] if o["action"] == "SELL"] == []


@pytest.fixture(autouse=True)
def _positions_follow_the_ledger(monkeypatch):
    """Don't hit the sandbox positions endpoint from unit tests."""
    monkeypatch.setattr(WebullBroker, "list_option_positions", lambda self, account_id=None: None)


def _sure_card(symbol: str, strike: float, *, right: str = "C", expiry: str = "2026-10-16", tier: str = "HIGH") -> dict:
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    occ = f"{symbol}{yymmdd}{right}{int(round(strike * 1000)):08d}"
    return {
        "symbol": symbol,
        "right": right,
        "confidence_tier": tier,
        "sure_winner": True,
        "desk": "Sure Winner",
        "ask": 1.0,
        "contract": occ,
        "expiry": expiry,
        "strike": strike,
    }


def _flow(symbol: str, strike: float) -> dict:
    expiry = "2026-10-16"
    yymmdd = "261016"
    occ = f"{symbol}{yymmdd}C{int(round(strike * 1000)):08d}"
    return {
        "symbol": symbol,
        "right": "C",
        "tier": "golden",
        "ask": 1.0,
        "contract": occ,
        "expiry": expiry,
        "strike": strike,
    }


def test_account_denial_stops_the_sync(tmp_path: Path, monkeypatch):
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        account_id="acct",
        ledger_path=tmp_path / "wb.json",
    )
    calls: list[str] = []

    def deny(intent):
        calls.append(intent.symbol)
        raise RuntimeError(
            "HTTP Status: 403, Code: ACCOUNT_ACCESS_DENIED, Msg: Invalid account or insufficient permissions."
        )

    monkeypatch.setattr(broker, "_place_via_openapi", deny)
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    out = trader.sync(
        sure_winners={"winners": [_sure_card("SPY", 500), _sure_card("QQQ", 480), _sure_card("IWM", 220)]}
    )
    assert calls == ["SPY"]
    assert out["skipped_n"] == 1
    assert out["submitted_n"] == 0
    assert broker.orders[0].status == "rejected"
    assert "ACCOUNT_ACCESS_DENIED" in (broker.orders[0].error or "")


def test_rejected_orders_still_honor_the_sync_cap(tmp_path: Path, monkeypatch):
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        account_id="acct",
        ledger_path=tmp_path / "wb.json",
    )
    calls: list[str] = []

    def deny(intent):
        calls.append(intent.symbol)
        raise RuntimeError("HTTP Status: 400, Code: INVALID_ORDER, Msg: bad limit")

    monkeypatch.setattr(broker, "_place_via_openapi", deny)
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=2)
    trader.sync(
        sure_winners={
            "winners": [
                _sure_card("SPY", 500),
                _sure_card("QQQ", 480),
                _sure_card("IWM", 220),
                _sure_card("AAPL", 200),
            ]
        }
    )
    assert calls == ["SPY", "QQQ"]


def test_live_submits_are_spaced(tmp_path: Path, monkeypatch):
    clock = _Clock()
    broker = _live_broker(tmp_path, clock)
    monkeypatch.setattr(broker, "_post_option", lambda intent: intent.id)

    def _stage(symbol: str, strike: float) -> WebullOrderIntent:
        return broker.stage(
            desk="golden_flow",
            action="BUY",
            symbol=symbol,
            right="C",
            contract=f"{symbol}261016C{int(strike * 1000):08d}",
            expiry="2026-10-16",
            strike=strike,
            quantity=1,
            limit_price=1.0,
        )

    broker.submit(_stage("SPY", 500))
    assert clock.sleeps == []
    broker.submit(_stage("QQQ", 480))
    assert clock.sleeps == [1.5]


def test_one_webull_client_is_reused(tmp_path: Path, monkeypatch):
    from webull.core.http.initializer.client_initializer import ClientInitializer

    monkeypatch.setattr(ClientInitializer, "initializer", staticmethod(lambda _client: None))
    broker = _live_broker(tmp_path)
    first = broker._trade_api()
    second = broker._trade_api()
    assert first is second
    log = Path("webull_trade_sdk.log")
    if log.exists():
        log.unlink()


def test_rate_limit_retries_the_same_client_order(tmp_path: Path, monkeypatch):
    clock = _Clock()
    broker = _live_broker(tmp_path, clock)
    seen: list[str] = []

    def post(intent: WebullOrderIntent) -> str:
        seen.append(intent.id)
        if len(seen) == 1:
            raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")
        return "WB-1"

    monkeypatch.setattr(broker, "_post_option", post)
    intent = broker.stage(
        desk="golden_flow",
        action="BUY",
        symbol="SPY",
        right="C",
        contract="SPY261016C00500000",
        expiry="2026-10-16",
        strike=500,
        quantity=1,
        limit_price=1.0,
    )
    broker.submit(intent)
    assert seen == [intent.id, intent.id]
    assert intent.status == "submitted"
    assert intent.broker_order_id == "WB-1"
    assert len([o for o in broker.orders if o.action == "BUY"]) == 1
    assert clock.sleeps == [1.5]


def test_exhausted_rate_limit_retries_the_same_row(tmp_path: Path, monkeypatch):
    clock = _Clock()
    broker = _live_broker(tmp_path, clock)
    seen: list[str] = []

    def post(intent: WebullOrderIntent) -> str:
        seen.append(intent.id)
        raise RuntimeError("HTTP 429 TOO_MANY_REQUESTS")

    monkeypatch.setattr(broker, "_post_option", post)
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=5, max_golden_buys_per_sync=2)
    card = _sure_card("SPY", 500)
    trader.sync(sure_winners={"winners": [card]})
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert len(buys) == 1
    assert buys[0].status == "deferred"
    assert seen == [buys[0].id, buys[0].id, buys[0].id]

    def post_ok(intent: WebullOrderIntent) -> str:
        seen.append(intent.id)
        return "WB-OK"

    monkeypatch.setattr(broker, "_post_option", post_ok)
    trader.sync(sure_winners={"winners": [card]})
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert len(buys) == 1
    assert buys[0].status == "submitted"
    assert buys[0].broker_order_id == "WB-OK"
    assert seen[-1] == buys[0].id
    assert seen.count(buys[0].id) == 4


def test_golden_buy_cap_limits_one_burst(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=5, max_golden_buys_per_sync=2)
    trader.sync(
        sure_winners={
            "winners": [
                _sure_card("SPY", 500),
                _sure_card("QQQ", 480),
                _sure_card("IWM", 220),
                _sure_card("AAPL", 200),
            ]
        }
    )
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert len(buys) == 2
    assert {o.symbol for o in buys} == {"SPY", "QQQ"}


def test_golden_cap_does_not_block_an_exit(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=5, max_golden_buys_per_sync=1)
    assert trader.sync(sure_winners={"winners": [_gold_winner()]})["submitted_n"] == 1
    out = trader.sync(
        actions={"buy_now": [], "sell_now": [_sell_row(_gold_winner(), bid=2.0, detail="take profit")]},
        sure_winners={"winners": [_amber_winner()]},
    )
    assert [o for o in out["submitted"] if o["action"] == "SELL" and o["symbol"] == "NVDA"]
    assert [o for o in out["submitted"] if o["action"] == "BUY" and o["symbol"] == "AMD"]


def _arm_live_buy(broker: WebullBroker, row: dict, *, quantity: int = 1) -> WebullOrderIntent:
    intent = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol=row["symbol"],
        right=row["right"],
        contract=row["contract"],
        expiry=row["expiry"],
        strike=row["strike"],
        quantity=quantity,
        limit_price=1.0,
        meta={"golden_kind": "sure_winner_gold", "fill_state": "working"},
    )
    intent.status = "submitted"
    broker.save()
    return intent


def test_unfilled_limit_is_cancelled_not_sold(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    flow = _flow("SPY", 500)
    intent = _arm_live_buy(broker, flow)
    cancelled: list[str] = []
    placed: list[str] = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )

    def cancel(order: WebullOrderIntent) -> None:
        cancelled.append(order.id)

    monkeypatch.setattr(broker, "_cancel_via_openapi", cancel)
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order.action) or order.id)
    trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(flow, bid=0.4, detail="stop loss -71% ≤ −50%")],
        }
    )
    assert cancelled == [intent.id]
    assert placed == []
    assert intent.status == "cancelled"
    assert [o for o in broker.orders if o.action == "SELL"] == []


def test_filled_limit_is_sold(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    flow = _flow("SPY", 500)
    intent = _arm_live_buy(broker, flow)
    placed: list[WebullOrderIntent] = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "FILLED", "filled_quantity": "1", "quantity": "1"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: (_ for _ in ()).throw(AssertionError("cancel")))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(flow, bid=1.8, detail="take profit +86% ≥ +80%")],
        }
    )
    assert intent.status == "filled"
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].contract == flow["contract"]
    body = build_option_place_order(placed[0])
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["client_order_id"] == placed[0].id


def test_partial_fill_sells_the_filled_contracts_and_cancels_the_rest(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    flow = _flow("SPY", 500)
    intent = _arm_live_buy(broker, flow, quantity=2)
    cancelled: list[str] = []
    placed: list[WebullOrderIntent] = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "PARTIAL FILLED", "filled_quantity": "1", "quantity": "2"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    trader.sync(
        actions={"buy_now": [], "sell_now": [_sell_row(flow, bid=1.2, detail="take profit")]},
    )
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].quantity == 1
    assert placed[0].contract == flow["contract"]
    assert cancelled == [intent.id]


def test_flatten_cancels_a_working_0dte_and_sells_a_fill(tmp_path: Path, monkeypatch):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 50)
    expiry = iso_day(late)
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)

    def _occ(symbol: str, strike: float) -> dict:
        yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
        return {
            "symbol": symbol,
            "right": "C",
            "contract": f"{symbol}{yymmdd}C{int(strike * 1000):08d}",
            "expiry": expiry,
            "strike": strike,
        }

    working_row = _occ("SPY", 500)
    filled_row = _occ("QQQ", 480)
    working = _arm_live_buy(broker, working_row)
    filled = _arm_live_buy(broker, filled_row)
    cancelled: list[str] = []
    placed: list[WebullOrderIntent] = []

    def detail(order: WebullOrderIntent) -> dict:
        if order.id == filled.id:
            return {"order_status": "FILLED", "filled_quantity": "1", "quantity": "1"}
        return {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"}

    monkeypatch.setattr(broker, "_fetch_order_detail", detail)
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=late)
    assert working.status == "cancelled"
    assert cancelled == [working.id]
    assert filled.status == "filled"
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].contract == filled.contract
    assert placed[0].limit_price == 0.01


def test_classify_broker_order_states():
    assert classify_broker_order({"order_status": "FILLED", "filled_quantity": "1", "quantity": "1"})[
        "fill_state"
    ] == "filled"
    working = classify_broker_order(
        {"data": {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"}}
    )
    assert working["fill_state"] == "working"
    assert working["filled_qty"] == 0
    partial = classify_broker_order({"order_status": "PARTIAL FILLED", "filled_qty": 1, "quantity": 2})
    assert partial["fill_state"] == "partial"
    assert classify_broker_order({"order_status": "CANCELLED", "filled_quantity": "0"})["fill_state"] == "cancelled"
    covered = classify_broker_order({"order_status": "SUBMITTED", "filled_quantity": "2", "quantity": "2"})
    assert covered["fill_state"] == "filled"
    filled = classify_broker_order(
        {"order_status": "FILLED", "filled_quantity": "1", "quantity": "1", "avg_filled_price": "0.42"}
    )
    assert filled["fill_price"] == 0.42


def test_order_detail_failure_stays_working(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    intent = _arm_live_buy(broker, _flow("SPY", 500))

    def boom(_order: WebullOrderIntent) -> dict:
        raise RuntimeError("network down")

    monkeypatch.setattr(broker, "_fetch_order_detail", boom)
    broker.refresh_fill(intent)
    assert intent.status == "submitted"
    assert intent.meta["fill_state"] == "working"


def test_outgoing_order_matches_the_occ_contract():
    """Puts logged as calls, and 10/14 contracts logged as 10/16, follow the OCC."""
    put = WebullOrderIntent(
        id="wb-iren",
        desk="sure_winner",
        action="BUY",
        symbol="IREN",
        right="C",
        contract="IREN261016P00030000",
        expiry="2026-10-16",
        strike=30,
        quantity=1,
        limit_price=1.2,
    )
    body = build_option_place_order(put)
    assert body["side"] == "BUY"
    assert body["legs"][0]["option_type"] == "PUT"
    assert body["legs"][0]["option_expire_date"] == "2026-10-16"
    assert body["legs"][0]["strike_price"] == "30.00"
    assert put.right == "P"

    uso = WebullOrderIntent(
        id="wb-uso",
        desk="sure_winner",
        action="BUY",
        symbol="USO",
        right="C",
        contract="USO261014C00152000",
        expiry="2026-10-16",
        strike=152,
        quantity=1,
        limit_price=0.4,
    )
    body = build_option_place_order(uso)
    assert body["legs"][0]["option_type"] == "CALL"
    assert body["legs"][0]["option_expire_date"] == "2026-10-14"
    assert body["legs"][0]["strike_price"] == "152.00"
    assert uso.expiry == "2026-10-14"


def test_routed_buy_uses_occ_when_the_row_fields_disagree(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True)
    row = _sure_card("WULF", 11.5, right="C", expiry="2026-10-16")
    row["contract"] = "WULF261023P00011500"
    row["right"] = "C"
    row["expiry"] = "2026-10-16"
    out = trader.sync(sure_winners={"winners": [row]})
    assert out["submitted_n"] == 1
    order = broker.orders[0]
    body = build_option_place_order(order)
    assert order.right == "P"
    assert order.expiry == "2026-10-23"
    assert order.strike == 11.5
    assert body["legs"][0]["option_type"] == "PUT"
    assert body["legs"][0]["option_expire_date"] == "2026-10-23"


def test_no_new_entries_at_or_after_the_close(tmp_path: Path):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 48)
    afternoon = session_et(15, 30)
    expiry_today = iso_day(late)
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    blocked = trader.sync(
        sure_winners={
            "winners": [
                _sure_card("ANET", 200, expiry=weekly),
                _sure_card("USO", 152, expiry=expiry_today),
            ]
        },
        now=late,
    )
    assert blocked["submitted_n"] == 0
    assert any("15:45" in r["reason"] for r in blocked["filtered"])

    broker2 = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb2.json")
    trader2 = AutoTrader(broker2, golden_only=True, max_orders_per_sync=4)
    odte_late = trader2.sync(
        sure_winners={"winners": [_sure_card("USO", 152, expiry=expiry_today, tier="MAX")]},
        now=afternoon,
    )
    assert odte_late["submitted_n"] == 0
    assert any("0DTE" in r["reason"] for r in odte_late["filtered"])
    weekly_ok = trader2.sync(
        sure_winners={"winners": [_sure_card("AMAT", 450, expiry=weekly, tier="HIGH")]},
        now=afternoon,
    )
    assert weekly_ok["submitted_n"] == 1
    assert weekly_ok["submitted"][0]["symbol"] == "AMAT"


def test_weekly_fill_is_not_flattened_but_unfilled_day_order_is_cancelled(tmp_path: Path, monkeypatch):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 50)
    expiry = iso_day(session_et(11, 0, weekday_offset=2))
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    card = _sure_card("AMAT", 450, expiry=expiry)
    filled = _arm_live_buy(broker, card)
    working = _arm_live_buy(broker, _sure_card("ANET", 200, expiry=expiry))
    cancelled: list[str] = []
    placed: list[WebullOrderIntent] = []

    def detail(order: WebullOrderIntent) -> dict:
        if order.id == filled.id:
            return {"order_status": "FILLED", "filled_quantity": "1", "quantity": "1"}
        return {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"}

    monkeypatch.setattr(broker, "_fetch_order_detail", detail)
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=late)
    assert filled.status == "filled"
    assert placed == []
    assert working.status == "cancelled"
    assert cancelled == [working.id]


def test_legacy_flow_position_is_not_closed_without_the_switch(tmp_path: Path, monkeypatch):
    from tests.session_clock import iso_day, session_et

    late = session_et(15, 50)
    expiry = iso_day(late)
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4, close_legacy_positions=False)
    row = _sure_card("NBIS", 185, expiry=expiry)
    intent = _arm_live_buy(broker, row)
    intent.meta["golden_kind"] = "golden_flow"
    intent.desk = "golden_flow"
    broker.save()
    placed: list[str] = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "FILLED", "filled_quantity": "1", "quantity": "1"},
    )
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order.action) or order.id)
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: None)
    trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=late)
    assert placed == []
    assert intent.status == "filled"


def test_legacy_switch_closes_the_open_sandbox_lots(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4, close_legacy_positions=True)
    row = _sure_card("RKLB", 125, right="P", expiry="2026-10-16")
    intent = _arm_live_buy(broker, row)
    intent.meta["golden_kind"] = "golden_flow"
    intent.desk = "golden_flow"
    intent.status = "filled"
    intent.meta["fill_state"] = "filled"
    broker.save()
    placed: list[WebullOrderIntent] = []
    monkeypatch.setattr(broker, "_fetch_order_detail", lambda order: {"order_status": "FILLED"})
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=None)
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].contract == row["contract"]
    assert placed[0].right == "P"


def test_position_snapshot_turns_a_submitted_limit_into_a_sell(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    row = _sure_card("DOCU", 76, expiry="2026-10-16")
    intent = _arm_live_buy(broker, row)
    placed: list[WebullOrderIntent] = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )
    monkeypatch.setattr(
        broker,
        "list_option_positions",
        lambda: [
            {
                "contract": row["contract"],
                "symbol": "DOCU",
                "right": "C",
                "expiry": "2026-10-16",
                "strike": 76,
                "quantity": 1,
            }
        ],
    )
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: (_ for _ in ()).throw(AssertionError("cancel")))
    trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(row, bid=1.5, detail="take profit +80%")],
        }
    )
    assert intent.status == "filled"
    assert intent.meta["position_source"] == "webull"
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].contract == row["contract"]


def test_flow_print_keeps_occ_put_and_expiry():
    from odte_scanner.echo.flow import build_option_flow

    board = build_option_flow(
        [
            {
                "symbol": "IREN",
                "spot": 40,
                "expiry": "2026-10-16",
                "dte": 2,
                "calls": [],
                "puts": [
                    {
                        "strike": 30,
                        "volume": 20000,
                        "open_interest": 100,
                        "mid": 1.5,
                        "contract": "IREN261016P00030000",
                    }
                ],
            }
        ],
        min_volume=100,
        min_premium=1000,
    )
    print_row = board["prints"][0]
    assert print_row["right"] == "P"
    assert print_row["expiry"] == "2026-10-16"
    assert print_row["contract"] == "IREN261016P00030000"


def test_parse_option_positions_reads_occ_and_option_fields():
    from odte_scanner.trading.webull import parse_option_positions

    rows = parse_option_positions(
        {
            "holdings": [
                {"instrument_type": "OPTION", "symbol": "NBIS261016P00175000", "quantity": "1"},
                {
                    "instrument_type": "OPTION",
                    "underlying_symbol": "AMAT",
                    "option_type": "CALL",
                    "option_expire_date": "2026-11-20",
                    "strike_price": "450",
                    "quantity": "1",
                },
                {"instrument_type": "EQUITY", "symbol": "SPY", "quantity": "10"},
            ]
        }
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["NBIS"]["right"] == "P"
    assert by_symbol["NBIS"]["expiry"] == "2026-10-16"
    assert by_symbol["NBIS"]["strike"] == 175
    assert by_symbol["AMAT"]["right"] == "C"
    assert by_symbol["AMAT"]["expiry"] == "2026-11-20"
    assert "SPY" not in by_symbol


def test_parse_option_positions_reads_webull_exercise_price_legs():
    """Official positions list: strike is option_exercise_price on each leg.

    The response is a JSON array. Symbol is the underlying, not an OCC code.
    A strike_price-only parser returns an empty book for this payload.
    """
    from odte_scanner.trading.webull import parse_option_positions

    rows = parse_option_positions(
        [
            {
                "position_id": "POS1",
                "currency": "USD",
                "quantity": "1",
                "symbol": "DOCU",
                "option_strategy": "SINGLE",
                "instrument_type": "OPTION",
                "last_price": "0.55",
                "cost_price": "1.39",
                "unrealized_profit_loss": "-84.00",
                "legs": [
                    {
                        "leg_id": "LEG1",
                        "symbol": "DOCU",
                        "quantity": "1",
                        "option_type": "CALL",
                        "option_expire_date": "2026-10-16",
                        "option_exercise_price": "76",
                        "option_contract_multiplier": "100",
                        "option_contract_deliverable": "100",
                    }
                ],
            },
            {
                "position_id": "POS2",
                "quantity": "2",
                "symbol": "AAPL",
                "instrument_type": "OPTION",
                "option_strategy": "SINGLE",
                "cost_price": "11.12",
                "last_price": "10.0",
                "legs": [
                    {
                        "leg_id": "LEG2",
                        "symbol": "AAPL",
                        "option_type": "CALL",
                        "option_expire_date": "2019-09-20",
                        "option_exercise_price": "11.0",
                    }
                ],
            },
            {
                "symbol": "SPY",
                "instrument_type": "EQUITY",
                "quantity": "10",
                "cost_price": "500",
            },
        ]
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"DOCU", "AAPL"}
    assert by_symbol["DOCU"]["contract"] == "DOCU261016C00076000"
    assert by_symbol["DOCU"]["right"] == "C"
    assert by_symbol["DOCU"]["expiry"] == "2026-10-16"
    assert by_symbol["DOCU"]["strike"] == 76
    assert by_symbol["DOCU"]["quantity"] == 1
    assert by_symbol["DOCU"]["cost"] == 1.39
    assert by_symbol["AAPL"]["contract"] == "AAPL190920C00011000"
    assert by_symbol["AAPL"]["quantity"] == 2
    assert by_symbol["AAPL"]["strike"] == 11
    assert by_symbol["AAPL"]["cost"] == 11.12


def test_exercise_price_position_reconciles_to_the_ledger_contract(tmp_path: Path, monkeypatch):
    from odte_scanner.trading.webull import parse_option_positions

    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    row = _sure_card("DOCU", 76, expiry="2026-10-16")
    intent = _arm_live_buy(broker, row)
    payload = [
        {
            "quantity": "1",
            "symbol": "DOCU",
            "instrument_type": "OPTION",
            "option_strategy": "SINGLE",
            "cost_price": "1.39",
            "legs": [
                {
                    "symbol": "DOCU",
                    "option_type": "CALL",
                    "option_expire_date": "2026-10-16",
                    "option_exercise_price": "76",
                }
            ],
        }
    ]
    monkeypatch.setattr(
        broker,
        "list_option_positions",
        lambda account_id=None: parse_option_positions(payload),
    )
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )
    trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []})
    assert intent.status == "filled"
    assert intent.contract == "DOCU261016C00076000"
    assert intent.fill_price == 1.39
    assert intent.meta.get("position_source") == "webull"


def test_same_symbol_working_golden_buy_does_not_stack(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=5, max_golden_buys_per_sync=2)
    first = trader.sync(
        sure_winners={
            "winners": [_sure_card("DOCU", 75), _sure_card("DOCU", 76), _sure_card("IWM", 272)]
        }
    )
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert first["submitted_n"] == 2
    assert {o.symbol for o in buys} == {"DOCU", "IWM"}
    second = trader.sync(sure_winners={"winners": [_sure_card("DOCU", 77)]})
    assert second["submitted_n"] == 0
    assert any("already open" in r["reason"] for r in second["filtered"])
    assert len([o for o in broker.orders if o.action == "BUY"]) == 2
    for order in list(broker.orders):
        if order.action == "BUY" and order.symbol == "DOCU":
            order.status = "filled"
            order.meta["fill_state"] = "filled"
    broker.save()
    third = trader.sync(sure_winners={"winners": [_sure_card("DOCU", 77)]})
    assert third["submitted_n"] == 1
    assert third["submitted"][0]["strike"] == 77


def test_parse_option_positions_reads_legs():
    """Strike, expiry, and side live on legs; quantity and cost sit on the parent."""
    from odte_scanner.trading.webull import parse_option_positions

    rows = parse_option_positions(
        {
            "positions": [
                {
                    "instrument_type": "OPTION",
                    "symbol": "IWM",
                    "quantity": "1",
                    "cost_price": "0.35",
                    "legs": [
                        {
                            "symbol": "IWM261015C00272000",
                            "side": "BUY",
                            "option_type": "CALL",
                            "strike_price": "272",
                            "option_expire_date": "2026-10-15",
                        }
                    ],
                },
                {"instrument_type": "EQUITY", "symbol": "SPY", "quantity": "10"},
            ]
        }
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["contract"] == "IWM261015C00272000"
    assert row["symbol"] == "IWM"
    assert row["right"] == "C"
    assert row["expiry"] == "2026-10-15"
    assert row["strike"] == 272
    assert row["quantity"] == 1
    assert row["cost"] == 0.35


def test_preopen_signal_is_queued_and_not_submitted(tmp_path: Path, monkeypatch):
    from tests.session_clock import session_et

    early = session_et(8, 15)
    opened = session_et(9, 35)
    broker = _live_broker(tmp_path, _Clock())
    calls: list[str] = []
    monkeypatch.setattr(broker, "_place_via_openapi", lambda intent: calls.append(intent.id) or "WB-1")
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [])
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    card = _sure_card("IWM", 272)
    trader.sync(sure_winners={"winners": [card]}, now=early)
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert calls == []
    assert len(buys) == 1
    assert buys[0].status == "deferred"
    assert buys[0].meta.get("queued_for_open") is True
    trader.sync(sure_winners={"winners": [card]}, now=early)
    assert calls == []
    assert len([o for o in broker.orders if o.action == "BUY"]) == 1
    trader.sync(sure_winners={"winners": [card]}, now=opened)
    assert calls == [buys[0].id]
    assert buys[0].status == "submitted"
    assert buys[0].broker_order_id == "WB-1"


def test_nonretryable_reject_is_not_submitted_again(tmp_path: Path, monkeypatch):
    from tests.session_clock import session_et

    broker = _live_broker(tmp_path, _Clock())
    calls: list[str] = []

    def deny(intent: WebullOrderIntent) -> str:
        calls.append(intent.symbol)
        raise RuntimeError(
            "HTTP Status: 417, Code: OPENAPI_ETF_OPTION_CAN_NOT_TRADING, "
            "Msg: ETF option can not trading, only trading in 09:30-16:15"
        )

    monkeypatch.setattr(broker, "_place_via_openapi", deny)
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [])
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    card = _sure_card("IWM", 272)
    trader.sync(sure_winners={"winners": [card]}, now=session_et(10, 0))
    trader.sync(sure_winners={"winners": [card]}, now=session_et(10, 5))
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert calls == ["IWM"]
    assert len(buys) == 1
    assert buys[0].status == "rejected"
    assert buys[0].meta.get("retryable") is False


def test_order_detail_records_the_fill_price(tmp_path: Path, monkeypatch):
    broker = _live_broker(tmp_path, _Clock())
    intent = _arm_live_buy(broker, _flow("SPY", 500))
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {
            "order_status": "FILLED",
            "filled_quantity": "1",
            "quantity": "1",
            "avg_filled_price": "0.42",
        },
    )
    broker.refresh_fill(intent)
    assert intent.status == "filled"
    assert intent.fill_price == 0.42
    assert intent.limit_price == 1.0


def test_legs_position_cost_prices_the_exit(tmp_path: Path, monkeypatch):
    from tests.session_clock import session_et

    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    row = _sure_card("IWM", 272)
    intent = _arm_live_buy(broker, row)
    payload = {
        "positions": [
            {
                "instrument_type": "OPTION",
                "symbol": "IWM",
                "quantity": "1",
                "cost_price": "0.35",
                "legs": [
                    {
                        "symbol": row["contract"],
                        "side": "BUY",
                        "option_type": "CALL",
                        "strike_price": "272",
                        "option_expire_date": row["expiry"],
                    }
                ],
            }
        ]
    }
    placed: list[WebullOrderIntent] = []
    from odte_scanner.trading.webull import parse_option_positions

    monkeypatch.setattr(
        broker,
        "list_option_positions",
        lambda account_id=None: parse_option_positions(payload),
    )
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: (_ for _ in ()).throw(AssertionError("cancel")))
    trader.sync(
        actions={
            "buy_now": [],
            "sell_now": [_sell_row(row, bid=0.35, detail="0DTE time-stop — flatten by 15:45 ET")],
        },
        now=session_et(11, 0),
    )
    assert intent.status == "filled"
    assert intent.fill_price == 0.35
    assert intent.limit_price == 1.0
    assert len(placed) == 1
    assert placed[0].action == "SELL"
    assert placed[0].limit_price == 0.01
    assert placed[0].contract == row["contract"]


def test_deferred_unfilled_is_cancelled_at_the_close(tmp_path: Path, monkeypatch):
    from tests.session_clock import session_et

    late = session_et(15, 50)
    broker = _live_broker(tmp_path, _Clock())
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    row = _sure_card("IWM", 272)
    intent = _arm_live_buy(broker, row)
    intent.status = "deferred"
    intent.broker_order_id = None
    intent.meta["fill_state"] = "deferred"
    intent.meta["retryable"] = True
    intent.meta["queued_for_open"] = True
    broker.save()
    placed: list[str] = []
    cancelled_api: list[str] = []
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order.action) or order.id)
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled_api.append(order.id))
    monkeypatch.setattr(broker, "_fetch_order_detail", lambda order: {"order_status": "SUBMITTED"})
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [])
    trader.sync(sure_winners={"winners": [row]}, now=late)
    assert intent.status == "cancelled"
    assert placed == []
    assert cancelled_api == []
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert len(buys) == 1


def test_paper_submit_without_keys_stays_dry(monkeypatch):
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    monkeypatch.delenv("WEBULL_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("WEBULL_PAPER_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("WEBULL_LIVE", raising=False)
    monkeypatch.setenv("WEBULL_PAPER_SUBMIT", "1")
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True})
    assert flags["paper"] is True
    assert flags["dry_run"] is True
    assert flags["enabled"] is False
    assert flags["sandbox"] is True
