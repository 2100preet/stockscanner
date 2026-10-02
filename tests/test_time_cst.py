"""Tests for CST signal timestamps."""

from __future__ import annotations

from odte_scanner.time_cst import (
    append_asked_cst,
    merge_first_signal_time,
    resolve_first_signal_time,
    signal_timestamps,
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
    from odte_scanner.time_cst import stamp_buy_sell_times

    store: dict = {}
    row1, store = stamp_buy_sell_times(
        {"symbol": "AMD", "action": "BUY_RIP", "detail": "rip"},
        store,
    )
    row2, store2 = stamp_buy_sell_times(
        {"symbol": "AMD", "action": "BUY_RIP", "detail": "rip again"},
        store,
    )
    assert row1["signaled_at"]
    assert row1["signaled_at_cst"]
    assert "asked to buy" in row1["detail"]
    assert row2["signaled_at"] == row1["signaled_at"]
    assert store2["AMD:BUY_RIP"]["signaled_at"] == row1["signaled_at"]
