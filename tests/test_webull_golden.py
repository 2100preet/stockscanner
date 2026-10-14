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
    webull_trade_host,
)


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
    assert golden_signal_kind(_gold_winner()) == "sure_winner_gold"
    assert golden_signal_kind({"confidence_tier": "MAX"}) == "sure_winner_gold"
    assert golden_signal_kind({"color": "gold"}) == "sure_winner_gold"
    assert golden_signal_kind(_amber_winner()) is None
    assert golden_signal_kind({"confidence_tier": "HIGH", "color": "gold"}) is None
    assert golden_signal_kind({"confidence_tier": "SOLID", "go_huge": True}) is None
    assert golden_signal_kind({"confidence_tier": "SOLID", "color": "soft-amber"}) is None
    assert golden_signal_kind(_golden_flow()) == "golden_flow"
    assert golden_signal_kind({"flags": ["golden"]}) == "golden_flow"
    assert golden_signal_kind(_unusual_flow()) is None
    assert golden_signal_kind({"tier": "aggressive"}) is None
    # Chart-pattern golden triangle is not the flow tape.
    assert golden_signal_kind({"golden": True, "pattern": "ASCENDING_TRIANGLE"}) is None


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
    assert symbols == {"NVDA", "SPY"}
    desks = {o["desk"] for o in out["submitted"]}
    assert desks == {"sure_winner", "golden_flow"}
    assert all(o["status"] == "dry_run" for o in out["submitted"])
    assert all((o.get("meta") or {}).get("golden_kind") for o in out["submitted"])
    ledger_symbols = {o.symbol for o in broker.orders}
    assert "IWM" not in ledger_symbols
    assert "AAPL" not in ledger_symbols
    assert "AMD" not in ledger_symbols
    assert "QQQ" not in ledger_symbols
    assert out["filtered_n"] >= 4
    assert out["broker"]["dry_run"] is True
    assert out["broker"]["host"] == SANDBOX_TRADE_HOST


def test_golden_flow_ignores_hist_gate(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=True, min_hist_win_samples=3)
    row = _golden_flow()
    assert "hist_win_pct" not in row
    out = trader.sync(option_flow={"golden": [row]})
    assert out["submitted_n"] == 1
    assert out["submitted"][0]["symbol"] == "SPY"
    assert out["submitted"][0]["desk"] == "golden_flow"


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
    assert any("expiry" in r["reason"] or "strike" in r["reason"] for r in out["filtered"])


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
