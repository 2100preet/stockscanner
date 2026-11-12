"""Webull auto-trader: desk routing + 100% hist-win gate (dry-run)."""
from __future__ import annotations

from pathlib import Path

from odte_scanner.trading.auto_trader import AutoTrader, board_option_desk, clears_perfect_hist_win
from odte_scanner.trading.webull import WebullBroker, webull_option_deep_link


def test_board_option_desk_keeps_0dte_and_names_swing():
    assert board_option_desk("0dte") == "odte"
    assert board_option_desk("weekly") == "weekly"
    assert board_option_desk("swing") == "swing"
    assert board_option_desk("monthly") == "swing"
    assert board_option_desk("leap") == "swing"


def test_golden_only_still_does_not_buy_a_monthly_swing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.options.contract_quote.refresh_traded_quote",
        lambda row: row,
    )
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, golden_only=True)
    out = trader.sync(
        actions={
            "buy_now": [
                {
                    "symbol": "AVGO",
                    "action": "BUY_NOW",
                    "dte_bucket": "monthly",
                    "right": "C",
                    "strike": 370,
                    "expiry": "2026-12-18",
                    "ask": 23.1,
                    "bid": 22.65,
                    "contract": "AVGO261218C00370000",
                }
            ],
            "sell_now": [],
        }
    )
    assert all(row.get("desk") != "swing" for row in out["submitted"])
    assert any("not a golden signal" in row["reason"] for row in out["filtered"])


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
    trader = AutoTrader(
        broker, require_perfect_hist=True, min_hist_win_samples=3, golden_only=False
    )
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
    trader = AutoTrader(
        broker,
        require_perfect_hist=True,
        min_hist_win_samples=3,
        golden_only=False,
        challenge_paper=True,
    )
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
    trader = AutoTrader(broker, require_perfect_hist=True, golden_only=False)
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


def test_preview_logs_when_bridge_disabled(tmp_path: Path):
    """enabled=false + dry_run=true still writes BUY/SELL history for verification."""
    broker = WebullBroker(enabled=False, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(
        broker, require_perfect_hist=True, min_hist_win_samples=3, golden_only=False
    )
    out = trader.sync(
        actions={
            "buy_now": [
                {
                    "symbol": "MSFT",
                    "ask": 1.1,
                    "dte_bucket": "0dte",
                    "hist_win_pct": 100,
                    "hist_samples": 5,
                    "expiry": "2026-08-12",
                    "strike": 420,
                    "contract": "MSFT260812C00420000",
                }
            ],
            "sell_now": [
                {
                    "symbol": "AAPL",
                    "bid": 0.9,
                    "dte_bucket": "0dte",
                    "expiry": "2026-08-12",
                    "strike": 200,
                    "contract": "AAPL260812C00200000",
                    "right": "C",
                }
            ],
        }
    )
    assert out["submitted_n"] >= 2
    statuses = {o["status"] for o in out["submitted"]}
    assert "dry_run" in statuses
    act = out["activity"]
    assert act["counts"]["buys"] >= 1
    assert act["counts"]["sells"] >= 1
    assert any("verify" in s.lower() or "Webull" in s for s in act["how_to_verify"])
    # Ledger file persists
    assert (tmp_path / "wb.json").exists()
    recent = broker.recent(10)
    assert len(recent) >= 2


def test_put_buy_routes_with_right(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json")
    trader = AutoTrader(broker, require_perfect_hist=False, golden_only=False)
    out = trader.sync(
        actions={
            "buy_now": [
                {
                    "symbol": "IWM",
                    "ask": 1.0,
                    "dte_bucket": "0dte",
                    "right": "P",
                    "expiry": "2026-08-12",
                    "strike": 220,
                    "contract": "IWM260812P00220000",
                }
            ],
            "sell_now": [],
        }
    )
    assert out["submitted_n"] == 1
    assert out["submitted"][0]["right"] == "P"
    assert out["submitted"][0]["action"] == "BUY"
