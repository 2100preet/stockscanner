"""Tests for free dealer/vol feeds."""

from __future__ import annotations

from odte_scanner.signals.free_feeds import (
    build_free_dealer_cockpit,
    fetch_cboe_vol_term,
    fetch_squeezemetrics_dix,
)


def test_squeezemetrics_dix():
    d = fetch_squeezemetrics_dix()
    assert d["ok"] is True
    assert d["dix"] is not None
    assert d["gex"] is not None


def test_cboe_vol_term():
    v = fetch_cboe_vol_term()
    assert v["ok"] is True
    assert "VIX" in v["levels"]
    assert (v["levels"]["VIX"].get("last") is not None) or ("error" in v["levels"]["VIX"])


def test_free_cockpit():
    c = build_free_dealer_cockpit()
    assert c["ok"] is True
    assert "available_free" in c
    assert len(c["available_free"]) >= 4
