"""Polygon / Massive client unit tests (no live network)."""

from __future__ import annotations

from odte_scanner.data import polygon as poly
from odte_scanner.options.live_chain import LiveOptionQuote, fetch_live_option_quote


def test_status_without_key(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    st = poly.status()
    assert st["configured"] is False


def test_massive_alias(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.setenv("MASSIVE_API_KEY", "massive-tok")
    assert poly.api_key_from_env() == "massive-tok"
    assert poly.status()["source"] == "massive"


def test_occ_symbol():
    assert poly.occ_symbol("AAPL", "2024-01-19", 150.0, "call") == "AAPL240119C00150000"
    assert poly.occ_symbol("SPY", "2026-09-30", 570.0, "put") == "SPY260930P00570000"


def test_live_chain_falls_back_to_polygon(monkeypatch):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "poly-tok")

    def fake_poly(**kwargs):
        return {
            "symbol": "TSLA250103C00250000",
            "bid": 3.0,
            "ask": 3.2,
            "last": 3.1,
            "volume": 9,
            "open_interest": 90,
            "strike": 250.0,
            "source": "polygon",
        }

    monkeypatch.setattr("odte_scanner.data.polygon.fetch_option_quote", fake_poly)
    q = fetch_live_option_quote("TSLA", "2025-01-03", 250.0, right="call")
    assert isinstance(q, LiveOptionQuote)
    assert q.ask == 3.2
    assert q.source == "polygon"


def test_probe_ok(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "tok")
    monkeypatch.setattr(
        poly,
        "fetch_equity_quote",
        lambda *a, **k: {"symbol": "SPY", "last": 500.0, "source": "polygon"},
    )
    out = poly.probe()
    assert out["ok"] is True
    assert out["confidence_boost"]
