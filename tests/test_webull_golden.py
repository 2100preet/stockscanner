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
