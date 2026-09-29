"""Tradier client unit tests (no live network)."""

from __future__ import annotations

from odte_scanner.data import tradier as tr
from odte_scanner.options.live_chain import fetch_live_option_quote, LiveOptionQuote


def test_status_without_token(monkeypatch):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    st = tr.status()
    assert st["configured"] is False
    assert st["token_len"] == 0
    assert "quotes" in st["endpoints"]
    assert "timesales" in st["endpoints"]
    assert "clock" in st["endpoints"]


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


def test_probe_aggregates_clock_and_quotes(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(
        tr,
        "fetch_clock",
        lambda **k: {"ok": True, "configured": True, "state": "open", "description": "Market is open"},
    )
    monkeypatch.setattr(
        tr,
        "fetch_quotes",
        lambda *a, **k: {"ok": True, "n": 1, "quotes": {"SPY": {"last": 500}}},
    )
    out = tr.probe()
    assert out["ok"] is True
    assert out["clock"]["state"] == "open"
    assert out["confidence_boost"]


def test_pick_option_contract(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(
        tr,
        "fetch_expirations",
        lambda *a, **k: {"ok": True, "dates": ["2099-01-02", "2099-01-09"]},
    )
    monkeypatch.setattr(
        tr,
        "fetch_quotes",
        lambda *a, **k: {"ok": True, "quotes": {"NVDA": {"last": 100.0, "symbol": "NVDA"}}},
    )

    def fake_chain(symbol, expiry, **kwargs):
        return {
            "ok": True,
            "options": [
                {
                    "symbol": "NVDA990102C00103000",
                    "option_type": "call",
                    "strike": 103.0,
                    "bid": 1.0,
                    "ask": 1.1,
                    "last": 1.05,
                    "volume": 200,
                    "open_interest": 500,
                }
            ],
        }

    monkeypatch.setattr(tr, "fetch_option_chain", fake_chain)
    picked = tr.pick_option_contract("NVDA", 100.0, right="C", min_dte=0, max_dte=40000, prefer_dte=3)
    assert picked is not None
    assert picked["mark_source"] == "ask"
    assert picked["source"] == "tradier"
    assert picked["ask"] == 1.1


def test_timesales_to_dataframe():
    res = {
        "bars": [
            {"time": "2026-09-29 09:30:00", "open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 100},
            {"time": "2026-09-29 09:31:00", "open": 10.5, "high": 11.2, "low": 10.4, "close": 11.0, "volume": 80},
        ]
    }
    df = tr.timesales_to_dataframe(res)
    assert df is not None
    assert len(df) == 2
    assert "Close" in df.columns


def test_live_quote_prefers_tradier(monkeypatch):
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "tok")

    def fake_quotes(symbols, **kwargs):
        return {
            "ok": True,
            "quotes": {
                "AAPL": {
                    "symbol": "AAPL",
                    "last": 200.0,
                    "prevclose": 198.0,
                    "change": 2.0,
                    "open": 199.0,
                    "high": 201.0,
                    "low": 197.5,
                    "source": "tradier",
                }
            },
        }

    monkeypatch.setattr("odte_scanner.data.tradier.fetch_quotes", fake_quotes)
    from odte_scanner.data.live_quotes import fetch_live_quote

    q = fetch_live_quote("AAPL")
    assert q is not None
    assert q.last == 200.0
    assert abs(q.change_pct - (2.0 / 198.0 * 100)) < 0.01


def test_quote_to_live_dict():
    d = tr.quote_to_live_dict(
        {"symbol": "MSFT", "last": 420, "prevclose": 400, "open": 405, "high": 425, "low": 399},
        symbol="MSFT",
    )
    assert d["last"] == 420
    assert d["source"] == "tradier"
    assert d["mark_source"] == "tradier"
