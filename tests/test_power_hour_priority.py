"""Power Hour: recent listings get a live quote ahead of the 48-name cap. Display only."""

from __future__ import annotations

from odte_scanner.signals import power_hour as ph


def _board():
    filler = [f"N{i:03d}" for i in range(80)]
    return ph.resolve_power_hour_symbols(filler[:60] + ["SPCX"] + filler[60:], config={})


def test_spcx_is_quoted_even_when_listed_past_the_cap():
    board = _board()
    assert board.index("SPCX") > 48
    order = ph.power_hour_quote_order(board, priority=["SPCX", "CRWV"], cap=48)
    assert len(order) == 48
    assert order[0] == "QQQ"
    assert "SPCX" in order
    # CRWV is not on this board, so it is not added.
    assert "CRWV" not in order


def test_quote_order_keeps_cap_and_no_duplicates():
    board = _board()
    order = ph.power_hour_quote_order(board, priority=["SPCX", "SPCX", "NVDA"], cap=20)
    assert len(order) == 20 == len(set(order))


def test_without_priority_matches_old_order_after_specials():
    board = _board()
    order = ph.power_hour_quote_order(board, priority=[], cap=48)
    assert order == (["QQQ"] + board)[:48]


def test_priority_from_config():
    cfg = {"actions": {"power_hour_priority_symbols": "spcx, crwv"}}
    assert ph.resolve_priority_symbols(cfg) == ["SPCX", "CRWV"]
    assert ph.resolve_priority_symbols({})[:1] == ["SPCX"]


def test_priority_bar_slots_are_capped(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(ph, "fetch_intraday_1m", lambda s, yahoo_symbol=None: fetched.append(s) or None)
    board = _board()
    ph.build_power_hour_board(quotes={}, symbols=board, config={}, fetch_bars=True, max_bar_fetch=22,
                              priority=["SPCX", "N000", "N001", "N002", "N003", "N004", "N005", "N006"],
                              priority_bar_slots=6)
    # QQQ + 8 specials, then exactly 6 priority names, then the ranked rest.
    assert fetched[0] == "QQQ"
    assert fetched[9:15] == ["SPCX", "N000", "N001", "N002", "N003", "N004"]
    assert len(fetched) == 22


def test_config_priority_fits_inside_the_quote_cap():
    from odte_scanner.config import load_config

    cfg = load_config()
    pri = ph.resolve_priority_symbols(cfg)
    assert pri[0] == "SPCX" and "AVGO" in pri
    assert pri == list(ph.DEFAULT_PRIORITY_SYMBOLS)
    actions = cfg.get("actions") or {}
    cap = int(actions.get("power_hour_max_quote_fetch", 48))
    assert cap == 48
    assert 1 + len(ph.SPECIAL_TICKERS) + len(pri) < cap
    assert int(actions.get("power_hour_max_bar_fetch", 16)) <= 22
    board = ph.with_priority_names(ph.resolve_power_hour_symbols("focus", config=cfg), pri)
    assert {"MARA", "RIOT", "LUNR"} <= set(board)
    order = ph.power_hour_quote_order(board, priority=pri, cap=cap)
    # Every priority name gets a live quote, inside the unchanged cap.
    assert set(pri) <= set(order) and len(order) == cap


def test_with_priority_names_appends_without_duplicates():
    assert ph.with_priority_names(["SPY", "SPCX"], ["spcx", "MARA"]) == ["SPY", "SPCX", "MARA"]
