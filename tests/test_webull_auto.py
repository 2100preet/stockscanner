"""Webull auto-trader: desk routing + 100% hist-win gate (dry-run)."""
from __future__ import annotations

from pathlib import Path

from odte_scanner.trading.auto_trader import AutoTrader, clears_perfect_hist_win
from odte_scanner.trading.webull import WebullBroker, webull_option_deep_link


def test_deep_link():
    url = webull_option_deep_link("SPCX", expiry="2026-08-07", strike=129, right="C")
    assert "SPCX" in url
    assert "webull.com" in url


def test_perfect_hist_gate():
    ok, _ = clears_perfect_hist_win({"hist_win_pct": 100, "hist_samples": 5})
    assert ok
    bad, reason = clears_perfect_hist_win({"hist_win_pct": None, "hist_samples": 0})
    assert not bad
    assert "hist-win" in reason.lower() or "sample" in reason.lower()
    bad2, _ = clears_perfect_hist_win({"hist_win_pct": 80, "hist_samples": 10})
    assert not bad2


def test_auto_trader_skips_without_perfect_hist(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
    )
    trader = AutoTrader(broker, require_perfect_hist=True, min_hist_win_samples=3)
    out = trader.sync(
        lottery={
            "buy_now": [
                {
                    "symbol": "SPCX",
                    "action": "BUY_NOW",
                    "ask": 0.45,
                    "contract": "SPCX260807C00120000",
                    "expiry": "2026-08-07",
                    "strike": 120,
                    "hist_win_pct": None,
                    "hist_samples": 0,
                }
            ],
            "sell_now": [],
        }
    )
    assert out["skipped_n"] >= 1
    assert out["submitted_n"] == 0
    assert any(o["symbol"] == "SPCX" and o["status"] == "skipped" for o in out["skipped"])


def test_auto_trader_dry_run_submits_perfect(tmp_path: Path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
    )
    trader = AutoTrader(broker, require_perfect_hist=True, min_hist_win_samples=3)
    out = trader.sync(
        challenge={
            "entry": [
                {
                    "symbol": "TOST",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 1.25,
                    "contract": "TOST260918C00040000",
                    "expiry": "2026-09-18",
                    "strike": 40,
                    "hist_win_pct": 100.0,
                    "hist_samples": 6,
                }
            ],
            "exit": [],
            "tickets": [],
        }
    )
    assert out["submitted_n"] == 1
    order = out["submitted"][0]
    assert order["desk"] == "challenge"
    assert order["status"] == "dry_run"
    assert order["deep_link"]
    assert order["hist_win_pct"] == 100.0


def test_routes_lottery_vs_odte(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=True)
    out = trader.sync(
        lottery={
            "buy_now": [
                {
                    "symbol": "QQQ",
                    "ask": 0.55,
                    "hist_win_pct": 100,
                    "hist_samples": 8,
                    "expiry": "2026-08-07",
                    "strike": 480,
                    "contract": "QQQ260807C00480000",
                }
            ],
            "sell_now": [],
        },
        actions={
            "buy_now": [
                {
                    "symbol": "SPY",
                    "ask": 0.8,
                    "dte_bucket": "0dte",
                    "hist_win_pct": 100,
                    "win_samples": 12,
                    "expiry": "2026-08-07",
                    "strike": 500,
                    "contract": "SPY260807C00500000",
                }
            ],
            "sell_now": [],
        },
    )
    desks = {o["desk"] for o in out["submitted"]}
    assert "lottery" in desks
    assert "odte" in desks
