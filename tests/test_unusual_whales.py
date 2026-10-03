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


def test_fetch_greek_exposure_by_expiry(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "expiry": "2026-10-10T00:00:00.000Z",
                        "dte": 7,
                        "call_gex": "1000000",
                        "put_gex": "-400000",
                        "call_delta": "500000",
                        "put_delta": "-200000",
                    }
                ]
            }

    def fake_get(url, **kwargs):
        assert "/api/stock/TSM/greek-exposure/expiry" in url
        return _Resp()

    monkeypatch.setattr(uw.requests, "get", fake_get)
    out = uw.fetch_greek_exposure_by_expiry("TSM")
    assert out["ok"] is True
    assert out["ticker"] == "TSM"
    assert out["summary"]["bias"] == "call_gex"
    assert out["summary"]["net_gex"] == 600000.0


def test_fetch_flow_per_expiry(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "date": "2026-10-02T00:00:00.000Z",
                "data": [
                    {
                        "expiry": "2026-10-03T00:00:00.000Z",
                        "call_premium": "900000",
                        "put_premium": "100000",
                        "call_volume": 1200,
                        "put_volume": 200,
                        "ticker": "AMAT",
                    }
                ],
            }

    def fake_get(url, **kwargs):
        assert "/api/stock/AMAT/flow-per-expiry" in url
        return _Resp()

    monkeypatch.setattr(uw.requests, "get", fake_get)
    out = uw.fetch_flow_per_expiry("AMAT")
    assert out["ok"] is True
    assert out["summary"]["sentiment"] == "bullish"
    assert out["summary"]["net_premium"] == 800000.0


def test_fetch_option_contract_intraday(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "close": "1.08",
                        "volume_ask_side": 80,
                        "volume_bid_side": 20,
                        "volume_mid_side": 2,
                        "premium_ask_side": "500",
                        "premium_bid_side": "100",
                    }
                ]
            }

    def fake_get(url, **kwargs):
        assert "/api/option-contract/TSM261002C00472500/intraday" in url
        return _Resp()

    monkeypatch.setattr(uw.requests, "get", fake_get)
    out = uw.fetch_option_contract_intraday("TSM261002C00472500")
    assert out["ok"] is True
    assert out["summary"]["dominant_side"] == "ask"
    assert out["summary"]["last_close"] == 1.08


def test_build_uw_desk_context_includes_expiry_pack(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(
        uw,
        "build_uw_flow_board",
        lambda **kwargs: {
            "ok": True,
            "configured": True,
            "alerts_n": 1,
            "leaders": [],
            "bullish_calls": ["NVDA"],
            "bearish_puts": [],
            "by_symbol": {},
        },
    )
    monkeypatch.setattr(
        uw,
        "fetch_market_tide",
        lambda **kwargs: {"ok": True, "configured": True, "sentiment": "bullish"},
    )
    monkeypatch.setattr(
        uw,
        "fetch_darkpool_recent",
        lambda **kwargs: {"ok": True, "configured": True, "leaders": [], "symbols": []},
    )
    monkeypatch.setattr(
        uw,
        "build_uw_expiry_pack",
        lambda ticker, **kwargs: {
            "ok": True,
            "configured": True,
            "ticker": ticker,
            "greek_by_expiry": {"headline": f"{ticker} GEX", "net_gex": 1},
            "flow_per_expiry": {"headline": f"{ticker} flow", "sentiment": "bullish"},
        },
    )
    monkeypatch.setattr(
        uw,
        "fetch_option_contract_intraday",
        lambda cid, **kwargs: {
            "ok": True,
            "contract": cid,
            "data": [{"close": 1.0}],
            "summary": {"headline": f"{cid} intraday", "dominant_side": "ask"},
        },
    )
    desk = uw.build_uw_desk_context(
        focus_tickers=["TSM", "AMAT"],
        focus_contracts=["TSM261002C00472500"],
    )
    assert desk["ok"] is True
    assert "TSM" in desk["greek_flow_by_ticker"]
    assert "AMAT" in desk["greek_flow_by_ticker"]
    assert "TSM261002C00472500" in desk["contract_intraday"]
    assert desk["expiry_headlines"]


def test_fetch_endpoints_skip_without_key(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    assert uw.fetch_greek_exposure_by_expiry("TSM")["skipped"] is True
    assert uw.fetch_flow_per_expiry("AMAT")["skipped"] is True
    assert uw.fetch_option_contract_intraday("TSM261002C00472500")["skipped"] is True


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
