"""GEX/OI-wall take-profit alert collector."""

from __future__ import annotations

from odte_scanner.alerts.take_profit import collect_take_profit_alerts


def test_take_profit_on_call_wall_hit():
    snap = {
        "insights": {
            "open_positions": [
                {
                    "symbol": "AAPL",
                    "right": "C",
                    "strike": 250,
                    "contract": "AAPL261009C00250000",
                    "status": "open",
                    "entry_ask": 1.0,
                    "bid": 1.6,
                    "spot": 251.0,
                    "soft_exit": 250.9,
                    "call_wall": 251.0,
                }
            ]
        },
        "walls_by_symbol": {
            "AAPL": {"call_wall": 251.0, "put_wall": 240.0, "soft_exit": 250.9},
        },
        "scores": [{"symbol": "AAPL", "last_price": 251.0}],
    }
    alerts = collect_take_profit_alerts(snap, approach_pct=0.35, take_profit_pct=80.0)
    assert alerts
    assert alerts[0]["action"] == "TAKE_PROFIT"
    assert "TAKE PROFIT" in alerts[0]["message"]
    assert "soft EXIT" in alerts[0]["message"]


def test_approach_wall_ping():
    snap = {
        "insights": {
            "open_positions": [
                {
                    "symbol": "MSFT",
                    "right": "C",
                    "strike": 520,
                    "contract": "MSFT261009C00520000",
                    "status": "open",
                    "entry_ask": 2.0,
                    "bid": 2.1,
                    "spot": 519.0,
                    "soft_exit": 520.0,
                    "call_wall": 520.1,
                }
            ]
        },
        "walls_by_symbol": {},
        "scores": [{"symbol": "MSFT", "last_price": 519.0}],
    }
    # 519 → 520 is ~0.19% away — within 0.35% approach
    alerts = collect_take_profit_alerts(snap, approach_pct=0.35)
    assert alerts
    assert alerts[0]["action"] == "APPROACH_WALL"


def test_put_wall_take_profit():
    snap = {
        "daily_pnl": {
            "open": [
                {
                    "symbol": "HUM",
                    "right": "P",
                    "strike": 430,
                    "contract": "HUM261009P00430000",
                    "status": "open",
                    "category": "journal",
                    "entry_ask": 0.22,
                    "bid": 2.5,
                    "spot": 428.0,
                    "soft_exit": 430.1,
                    "put_wall": 430.0,
                }
            ]
        },
        "walls_by_symbol": {
            "HUM": {"put_wall": 430.0, "call_wall": 460.0, "soft_exit": 430.1},
        },
        "scores": [{"symbol": "HUM", "last_price": 428.0}],
    }
    alerts = collect_take_profit_alerts(snap)
    assert alerts
    assert alerts[0]["action"] == "TAKE_PROFIT"
    assert alerts[0]["symbol"] == "HUM"


def test_premium_tp_without_walls():
    snap = {
        "insights": {
            "open_positions": [
                {
                    "symbol": "TSLA",
                    "right": "C",
                    "strike": 400,
                    "contract": "TSLA261009C00400000",
                    "status": "open",
                    "entry_ask": 1.0,
                    "bid": 2.0,
                    "spot": 405.0,
                }
            ]
        },
        "walls_by_symbol": {},
        "scores": [{"symbol": "TSLA", "last_price": 405.0}],
    }
    alerts = collect_take_profit_alerts(snap, take_profit_pct=80.0)
    assert alerts
    assert alerts[0]["action"] == "TAKE_PROFIT"
    assert "+100%" in alerts[0]["message"] or "100%" in alerts[0]["message"]
