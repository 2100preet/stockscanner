"""Tests for CST signal timestamps + exit/re-enter sticky clear."""

from __future__ import annotations

from odte_scanner.time_cst import (
    append_asked_cst,
    clear_buy_stamps_on_sell,
    clear_signal_time,
    clear_signal_times_for_contracts,
    merge_first_signal_time,
    prune_signal_store_to_active,
    resolve_first_signal_time,
    signal_store_key,
    signal_timestamps,
    stamp_buy_sell_times,
    to_cst_label,
)


def test_to_cst_label_has_central_zone():
    label = to_cst_label("2026-08-13T18:30:00+00:00")
    assert label is not None
    assert "2026" in label
    assert ("CDT" in label) or ("CST" in label)


def test_signal_timestamps_pair():
    ts = signal_timestamps()
    assert "signaled_at" in ts and "signaled_at_cst" in ts
    assert "T" in ts["signaled_at"]
    assert ("CST" in ts["signaled_at_cst"]) or ("CDT" in ts["signaled_at_cst"])


def test_merge_keeps_first_buy_time():
    store = {}
    store = merge_first_signal_time(
        store,
        symbol="FRMI",
        action="BUY_NOW",
        signaled_at="2026-08-13T15:00:00+00:00",
        signaled_at_cst="Aug 13, 2026, 10:00:00 AM CDT",
    )
    store2 = merge_first_signal_time(
        store,
        symbol="FRMI",
        action="BUY_NOW",
        signaled_at="2026-08-13T16:00:00+00:00",
        signaled_at_cst="Aug 13, 2026, 11:00:00 AM CDT",
    )
    assert store2["FRMI:BUY_NOW"]["signaled_at"] == "2026-08-13T15:00:00+00:00"


def test_resolve_first_signal_time_sticky():
    store: dict = {}
    utc1, cst1, store = resolve_first_signal_time(store, symbol="TSSI", action="BUY_NOW")
    utc2, cst2, store2 = resolve_first_signal_time(store, symbol="TSSI", action="BUY_NOW")
    assert utc1 == utc2
    assert cst1 == cst2
    assert store2["TSSI:BUY_NOW"]["signaled_at"] == utc1


def test_append_asked_cst_once():
    d1 = append_asked_cst("accepted tape", action="BUY_NOW", signaled_at_cst="Aug 13, 2026, 10:00:00 AM CDT")
    assert "asked to buy" in d1 and "CDT" in d1
    d2 = append_asked_cst(d1, action="BUY_NOW", signaled_at_cst="Aug 13, 2026, 11:00:00 AM CDT")
    assert d2 == d1


def test_append_asked_cst_buy_rip_verb():
    d = append_asked_cst("mega rip", action="BUY_RIP", signaled_at_cst="Oct 1, 2026, 6:18:24 PM CDT")
    assert "asked to buy" in d
    assert "asked to sell" not in d


def test_stamp_buy_sell_times_sticky():
    store: dict = {}
    row1, store = stamp_buy_sell_times(
        {"symbol": "AMD", "action": "BUY_RIP", "detail": "rip", "contract": "AMD261002C00630000"},
        store,
    )
    row2, store2 = stamp_buy_sell_times(
        {"symbol": "AMD", "action": "BUY_RIP", "detail": "rip again", "contract": "AMD261002C00630000"},
        store,
    )
    assert row1["signaled_at"]
    assert row1["signaled_at_cst"]
    assert "asked to buy" in row1["detail"]
    assert row2["signaled_at"] == row1["signaled_at"]
    key = signal_store_key("AMD", "BUY_RIP", "AMD261002C00630000")
    assert store2[key]["signaled_at"] == row1["signaled_at"]


def test_exit_clears_buy_stamp_reenter_gets_fresh_time():
    """Same OCC: BUY sticky → SELL clears BUY → re-BUY gets a new asked time."""
    occ = "META261005C00750000"
    store: dict = {}
    buy1, store = stamp_buy_sell_times(
        {"symbol": "META", "action": "BUY_RIP", "contract": occ, "detail": "rip"},
        store,
    )
    first = buy1["signaled_at"]
    assert signal_store_key("META", "BUY_RIP", occ) in store

    store = clear_buy_stamps_on_sell(store, symbol="META", contract=occ)
    assert signal_store_key("META", "BUY_RIP", occ) not in store

    buy2, store2 = stamp_buy_sell_times(
        {"symbol": "META", "action": "BUY_RIP", "contract": occ, "detail": "rip again"},
        store,
    )
    assert buy2["signaled_at"] != first
    assert store2[signal_store_key("META", "BUY_RIP", occ)]["signaled_at"] == buy2["signaled_at"]


def test_prune_drops_stamp_when_pulse_leaves_board():
    occ = "TSLA261005C00380000"
    store: dict = {}
    _, store = stamp_buy_sell_times(
        {"symbol": "TSLA", "action": "BUY_NOW", "contract": occ, "detail": "buy"},
        store,
    )
    key = signal_store_key("TSLA", "BUY_NOW", occ)
    assert key in store
    # Pulse left the board → prune clears sticky so re-enter is fresh.
    store = prune_signal_store_to_active(store, active_keys=set())
    assert key not in store
    buy2, _ = stamp_buy_sell_times(
        {"symbol": "TSLA", "action": "BUY_NOW", "contract": occ, "detail": "reenter"},
        store,
    )
    assert buy2["signaled_at"]


def test_clear_signal_times_for_contracts():
    store = {
        "NVDA:BUY_NOW:NVDA261005C00200000": {
            "symbol": "NVDA",
            "action": "BUY_NOW",
            "contract": "NVDA261005C00200000",
            "signaled_at": "2026-10-01T12:00:00+00:00",
            "signaled_at_cst": "x",
        },
        "AAPL:BUY_NOW:AAPL261005C00250000": {
            "symbol": "AAPL",
            "action": "BUY_NOW",
            "contract": "AAPL261005C00250000",
            "signaled_at": "2026-10-01T12:00:00+00:00",
            "signaled_at_cst": "y",
        },
    }
    out = clear_signal_times_for_contracts(store, {"NVDA261005C00200000"})
    assert "NVDA:BUY_NOW:NVDA261005C00200000" not in out
    assert "AAPL:BUY_NOW:AAPL261005C00250000" in out


def test_clear_signal_time_drops_legacy_symbol_key():
    store = {
        "META:BUY_NOW": {"symbol": "META", "action": "BUY_NOW", "signaled_at": "t0"},
        "META:BUY_NOW:META261005C00750000": {
            "symbol": "META",
            "action": "BUY_NOW",
            "contract": "META261005C00750000",
            "signaled_at": "t1",
        },
    }
    out = clear_signal_time(
        store, symbol="META", action="BUY_NOW", contract="META261005C00750000"
    )
    assert "META:BUY_NOW" not in out
    assert "META:BUY_NOW:META261005C00750000" not in out
