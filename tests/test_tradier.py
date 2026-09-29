"""Tradier client unit tests (no live network)."""

from __future__ import annotations

from odte_scanner.data import tradier as tr
from odte_scanner.options.live_chain import fetch_live_option_quote, LiveOptionQuote


def test_status_without_token(monkeypatch):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    st = tr.status()
    assert st["configured"] is False
    assert st["token_len"] == 0


def test_fetch_quotes_skipped(monkeypatch):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    out = tr.fetch_quotes(["SPY"])
    assert out["skipped"] is True
    assert out["quotes"] == {}


def test_fetch_option_quote_uses_chain(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "test-token")

    def fake_chain(symbol, expiry, **kwargs):
        return {
            "ok": True,
            "options": [
                {
                    "symbol": "NVDA250103C00100000",
                    "option_type": "call",
                    "strike": 100.0,
                    "bid": 1.2,
                    "ask": 1.35,
                    "last": 1.3,
                    "volume": 10,
                    "open_interest": 100,
                }
            ],
        }

    monkeypatch.setattr(tr, "fetch_option_chain", fake_chain)
    monkeypatch.setattr(tr, "fetch_quotes", lambda *a, **k: {"ok": True, "quotes": {}})
    row = tr.fetch_option_quote(symbol="NVDA", expiry="2025-01-03", strike=100.0, right="call")
    assert row is not None
    assert row["ask"] == 1.35
    assert row["source"] == "tradier"


def test_live_chain_prefers_tradier(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "test-token")

    def fake_tq(**kwargs):
        return {
            "symbol": "TSLA250103C00250000",
            "bid": 2.0,
            "ask": 2.1,
            "last": 2.05,
            "volume": 5,
            "open_interest": 50,
            "strike": 250.0,
            "source": "tradier",
        }

    monkeypatch.setattr("odte_scanner.data.tradier.fetch_option_quote", fake_tq)
    q = fetch_live_option_quote("TSLA", "2025-01-03", 250.0, right="call")
    assert isinstance(q, LiveOptionQuote)
    assert q.ask == 2.1
    assert q.contract.startswith("TSLA")
