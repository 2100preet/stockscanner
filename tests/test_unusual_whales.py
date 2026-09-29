"""Unit tests for Unusual Whales flow client (no live network)."""

from __future__ import annotations

from odte_scanner.challenge.million import build_challenge_board
from odte_scanner.signals import unusual_whales as uw


def test_api_key_from_env(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    assert uw.api_key_from_env() is None
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "  abc-123  ")
    assert uw.api_key_from_env() == "abc-123"


def test_build_uw_flow_board_skipped_without_key(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    board = uw.build_uw_flow_board()
    assert board["ok"] is False
    assert board["skipped"] is True
    assert board["leaders"] == []


def test_build_uw_flow_board_aggregates(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")

    def fake_fetch(*, api_key=None, is_call=None, is_put=None, **kwargs):
        if is_call:
            return {
                "ok": True,
                "data": [
                    {"ticker": "NVDA", "total_premium": "500000"},
                    {"ticker": "TSLA", "total_premium": "200000"},
                ],
            }
        if is_put:
            return {
                "ok": True,
                "data": [
                    {"ticker": "NVDA", "total_premium": "10000"},
                    {"ticker": "AMD", "total_premium": "400000"},
                ],
            }
        return {"ok": False, "data": []}

    monkeypatch.setattr(uw, "fetch_flow_alerts", fake_fetch)
    board = uw.build_uw_flow_board(min_premium=50_000)
    assert board["ok"] is True
    assert "NVDA" in board["bullish_calls"]
    assert "AMD" in board["bearish_puts"]
    leaders = {r["symbol"]: r for r in board["leaders"]}
    assert leaders["NVDA"]["sentiment"] == "bullish"
    assert leaders["AMD"]["sentiment"] == "bearish"


def test_challenge_board_demotes_call_on_bearish_uw():
    win_table = {
        "symbols": {
            "JPM": {
                "swing": {"win_pct": 100.0, "trades": 20, "wins": 20, "hit_1pct": 0.7, "hit_2pct": 0.5},
                "weekly": {"win_pct": 100.0, "trades": 12, "wins": 12},
            },
        }
    }
    uw_flow = {
        "ok": True,
        "configured": True,
        "bullish_calls": [],
        "bearish_puts": ["JPM"],
        "by_symbol": {
            "JPM": {
                "symbol": "JPM",
                "sentiment": "bearish",
                "call_premium": 0,
                "put_premium": 900000,
                "net_flow_score": -900,
            }
        },
        "alerts_n": 1,
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {
                "symbol": "JPM",
                "horizon": "swing",
                "ensemble_score": 80,
                "last_price": 200,
                "quality": True,
            }
        ],
        quotes={
            "JPM": {
                "last": 200.0,
                "session": "live",
                "mom_5m_pct": 0.1,
                "change_pct": 0.5,
            }
        },
        fetch_contracts=False,
        fetch_earnings=False,
        fetch_walls=False,
        max_tickets=4,
        uw_flow=uw_flow,
    )
    tickets = board.get("tickets") or []
    assert tickets
    jpm = next(t for t in tickets if t["symbol"] == "JPM")
    assert jpm["right"] == "C"
    assert jpm["action"] == "WAIT"
    assert "UW flow bearish" in (jpm.get("status_detail") or "")
