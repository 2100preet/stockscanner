"""Afternoon Puts: Nov 2 Yahoo replay, the HOD-drop gate, and the paper route."""

from __future__ import annotations

import inspect
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.alerts.exits import format_exit_alert
from odte_scanner.fast_desk import in_afternoon_window, in_hot_window, run_afternoon_puts_cycle, run_fast_desk_cycle
from odte_scanner.hot_state import apply_hot_overlay, clear_hot, hot_snapshot, publish_hot
from odte_scanner.live_desk import resolve_webull_live_flags
from odte_scanner.signals.afternoon_puts import (
    HOD_DROP_PCT,
    PRIMARY_RULES,
    Bar,
    load_replay_fixture,
    replay,
)
from odte_scanner.signals.afternoon_puts_board import LOG_KIND, build_afternoon_board, load_afternoon_board
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "afternoon_puts_nov2.json"


def _ct(hour: int, minute: int, day: str = "2026-11-02") -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=CT)


def test_hod_drop_constant_replaced_the_prior_close_gate():
    assert HOD_DROP_PCT == 0.5
    assert PRIMARY_RULES.hod_drop_pct == 0.5
    assert "prior_close" not in PRIMARY_RULES.rules_id()
    assert "hod0.5" in PRIMARY_RULES.rules_id()


def test_nov2_replay_qqq_and_spy_yahoo_puts():
    underlying, puts = load_replay_fixture(str(FIXTURE))
    rows = replay(underlying, puts)
    by_symbol = {row["symbol"]: row for row in rows}
    assert list(by_symbol) == ["QQQ", "SPY"]
    qqq = by_symbol["QQQ"]
    spy = by_symbol["SPY"]
    assert qqq["at_ct"] == "10:25"
    assert qqq["strike"] == 693
    assert qqq["premium"] == 1.52
    assert qqq["reason"] == "trail"
    assert qqq["pnl_usd"] == 137
    assert qqq["priced"] == "yahoo"
    assert qqq["contracts"] == 1
    assert qqq["webull"] is True
    assert spy["at_ct"] == "11:05"
    assert spy["strike"] == 759
    assert spy["premium"] == 0.98
    assert spy["reason"] == "trail"
    assert spy["pnl_usd"] == 92
    assert spy["priced"] == "yahoo"
    assert spy["webull"] is True
    assert "SPX" not in by_symbol


def test_one_signal_per_symbol_and_cap_two():
    underlying, puts = load_replay_fixture(str(FIXTURE))
    rows = replay(underlying, puts, PRIMARY_RULES)
    assert len(rows) <= 2
    assert len({row["symbol"] for row in rows}) == len(rows)


def test_flat_tape_does_not_trigger():
    day = "2026-11-02"
    bars = []
    price = 100.0
    for i, minute in enumerate(range(8 * 60 + 30, 14 * 60 + 15, 5)):
        hour, mm = divmod(minute, 60)
        stamp = _ct(hour, mm, day)
        # A lower high and a tiny dip, well under a 0.5% drop from the high.
        high = price + (0.05 if i == 4 else 0.02)
        low = price - 0.02
        bars.append(Bar(ts=stamp, open=price, high=high, low=low, close=price - 0.01, volume=1000))
        price -= 0.01
    from odte_scanner.signals.afternoon_puts import first_signal
    from odte_scanner.signals.diamond import group_sessions

    session = group_sessions(bars)[datetime.fromisoformat(day).date()]
    assert first_signal(session) is None


def test_board_marks_spy_qqq_paper_and_keeps_spx_off_webull(tmp_path):
    underlying, puts = load_replay_fixture(str(FIXTURE))
    board = build_afternoon_board(
        underlying,
        now=_ct(15, 0),
        store_path=tmp_path / "day.json",
        option_bars=puts,
        chains={
            "QQQ": [{"right": "P", "strike": 693, "expiry": "2026-11-02", "ask": 1.52, "bid": 1.48, "contract": "QQQ261102P00693000"}],
            "SPY": [{"right": "P", "strike": 759, "expiry": "2026-11-02", "ask": 0.98, "bid": 0.94, "contract": "SPY261102P00759000"}],
        },
    )
    assert board["customer"] == "🌅 Afternoon Puts"
    assert "Sure Winner" not in board["note"]
    symbols = [row["symbol"] for row in board["signals"]]
    assert symbols == ["QQQ", "SPY"]
    for card in board["signals"]:
        assert card["webull"] is True
        assert card["desk"] == LOG_KIND
        assert card["contracts"] == 1
        assert "Sure Winner" not in card["detail"]
        assert "Sure Winner" not in card["exit_plan"]


def test_entry_alert_matches_sniper_shape_and_logs_the_desk_name():
    card = {
        "symbol": "QQQ",
        "send": True,
        "action": "AFTERNOON_PUTS",
        "desk": "AfternoonPuts",
        "afternoon_puts": True,
        "right": "P",
        "strike": 693,
        "expiry": "2026-11-02",
        "dte": 0,
        "ask": 1.52,
        "bid": 1.48,
        "spot": 693.66,
        "contract": "QQQ261102P00693000",
        "at_ct": "10:25",
        "detected_at": "2026-11-02T16:30:10+00:00",
        "detail": "🌅 Afternoon Puts · session high in.",
    }
    alerts = collect_trade_alerts({"afternoon_puts": {"signals": [card]}})
    text = alerts[0]["message"]
    assert text.splitlines()[0].startswith("🌅 AFTERNOON PUTS QQQ 693P 11/2")
    assert "Bid $1.48 / Ask $1.52" in text
    assert "Limit $" not in text
    assert "Signal 10:25 bar" in text
    assert "Sure Winner" not in text
    assert alerts[0]["desk"] == "AfternoonPuts"


def test_exit_line_carries_entry_exit_dollars_and_contracts():
    text = format_exit_alert(
        tier="afternoon",
        symbol="SPY",
        strike=759,
        right="P",
        expiry="2026-11-02",
        contracts=1,
        entry=0.98,
        entry_at="11:05",
        exit_price=1.84,
        exit_at="11:50",
        reason="trail gave back half the peak gain",
        tally="Today 1W/0L · +$92",
    )
    assert text.startswith("🌅 AFTERNOON PUTS EXIT — SPY 759P 11/2 · 1 contract")
    assert "Entry Filled @ $0.98 @ 11:05 CT" in text
    assert "Exit Filled @ $1.84 @ 11:50 CT" in text
    assert "Sure Winner" not in text


def test_fast_cycle_stays_quiet_at_11_and_afternoon_cycle_runs(monkeypatch):
    assert in_hot_window(_ct(11, 5)) is False
    assert in_afternoon_window(_ct(11, 5)) is True
    skipped = run_fast_desk_cycle(
        now=_ct(11, 5),
        boards=({"signals": []}, {"signals": []}),
        route=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("morning route")),
    )
    assert skipped["skipped"] is True

    def route(board, _now):
        assert board["signals"][0]["symbol"] == "QQQ"
        return {"submitted_n": 1, "submitted": [], "recent": []}

    monkeypatch.setattr(
        "odte_scanner.scorecard.service.observe_snapshot",
        lambda **_k: {"rows": []},
    )
    out = run_afternoon_puts_cycle(
        now=_ct(11, 5),
        board={"ok": True, "signals": [{"symbol": "QQQ", "send": False}], "phase": "scanning"},
        route=route,
        dispatch=lambda _snapshot: {"ok": True, "sent": 0},
    )
    assert out["skipped"] is False
    assert out["scan"] == "not_used"


def test_paper_route_buys_qqq_and_refuses_spx(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="paper-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    card = {
        "symbol": "QQQ",
        "send": True,
        "send_order": True,
        "action": "AFTERNOON_PUTS",
        "desk": "AfternoonPuts",
        "afternoon_puts": True,
        "right": "P",
        "strike": 693,
        "expiry": "2026-11-02",
        "dte": 0,
        "ask": 1.52,
        "bid": 1.48,
        "spot": 693.66,
        "contract": "QQQ261102P00693000",
        "webull": True,
    }
    spx = {**card, "symbol": "SPX", "contract": "SPX261102P07615000", "strike": 7615, "webull": False, "send_order": True}
    result = trader.sync_hot(afternoon={"signals": [card, spx]}, now=_ct(10, 30))
    buys = [order for order in broker.orders if order.action == "BUY"]
    assert [order.symbol for order in buys] == ["QQQ"]
    assert buys[0].quantity == 1
    assert buys[0].desk == "afternoon_puts"
    assert buys[0].meta.get("golden_kind") == "afternoon_puts"
    assert result["submitted_n"] == 1
    flags = resolve_webull_live_flags({"enabled": True, "dry_run": False, "paper": False})
    assert flags["sandbox"] is True
    assert flags["paper"] is True
    assert flags["host"] == "api.sandbox.webull.com"
    assert broker.status()["ready_live"] is False
    assert broker.status()["host"] == "api.sandbox.webull.com"


def test_portal_tile_states_are_on_the_page():
    assert 'id="afternoonBanner"' in PAGE
    assert "function renderAfternoonBanner" in PAGE
    assert "Waiting — window opens 10:00 CT" in PAGE
    assert "Scanning SPY QQQ SPX — no trigger yet" in PAGE
    assert "No Afternoon Puts today" in PAGE
    assert "🌅 Afternoon" in PAGE
    banner = PAGE[PAGE.index("function renderAfternoonBanner") : PAGE.index("function paint")]
    assert "Sure Winner" not in banner
    assert "setInterval(loadAll, window.SIGNAL_DESK_STATIC ? 180000 : 5000);" in PAGE


def test_cycle_uses_the_real_loader_signature_and_publishes(monkeypatch, tmp_path):
    """The live loader is keyword-only. A positional moment crashes every cycle."""
    params = inspect.signature(load_afternoon_board).parameters
    assert list(params) == ["now", "store_path", "bars"]
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in params.values())

    underlying, _puts = load_replay_fixture(str(FIXTURE))
    import odte_scanner.signals.afternoon_puts_board as board_mod

    monkeypatch.setattr(board_mod, "load_intraday_bars", lambda symbols=None: underlying)
    monkeypatch.setattr(board_mod, "DEFAULT_STORE", tmp_path / "afternoon_puts_day.json")
    monkeypatch.setattr(board_mod, "_yahoo_chain", lambda symbol, side, *, now=None: [])
    monkeypatch.setattr("odte_scanner.scorecard.service.observe_snapshot", lambda **_k: {"rows": []})
    clear_hot()
    try:
        out = run_afternoon_puts_cycle(
            now=_ct(11, 5),
            route=lambda board, _now: {"submitted": [], "recent": [], "symbols": [row["symbol"] for row in board["signals"]]},
            dispatch=lambda _snapshot: {"ok": True, "sent": 0},
        )
        assert out["ok"] is True
        assert out["skipped"] is False
        assert out["scan"] == "not_used"
        assert "QQQ" in (out.get("route") or {}).get("symbols", [])
        hot = hot_snapshot()["afternoon_puts"]
        assert isinstance(hot, dict)
        assert hot.get("ok") is True
        assert any(row.get("symbol") == "QQQ" for row in hot.get("signals") or [])
        snap = apply_hot_overlay({"afternoon_puts": None})
        board = snap["afternoon_puts"]
        assert isinstance(board, dict)
        assert board.get("ok") is True
        assert board is not None
        assert any(row.get("symbol") == "QQQ" for row in board.get("signals") or [])
        assert board.get("error") in (None, "")
    finally:
        clear_hot()


def test_failed_cycle_publishes_an_error_board_not_null():
    """A crash is {ok: false, error: ...}. Null is reserved for no board yet."""

    def explode(*, now=None):
        raise RuntimeError("afternoon board exploded")

    clear_hot()
    try:
        publish_hot(sniper={"ok": True, "signals": [{"symbol": "SPY"}]})
        publish_hot(diamond={"ok": True, "signals": [{"symbol": "IWM"}]})
        out = run_afternoon_puts_cycle(
            now=_ct(11, 5),
            load_board=explode,
            route=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("route ran after a crash")),
            dispatch=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("alert ran after a crash")),
        )
        assert out["ok"] is False
        assert "afternoon board exploded" in out["error"]
        snap = apply_hot_overlay({"afternoon_puts": None, "sniper": None, "diamond": None})
        board = snap["afternoon_puts"]
        assert board["ok"] is False
        assert "afternoon board exploded" in board["error"]
        assert board.get("signals") == []
        assert snap["sniper"]["signals"][0]["symbol"] == "SPY"
        assert snap["diamond"]["signals"][0]["symbol"] == "IWM"
        # A soft miss (ok false, no error) must not wipe a good afternoon board.
        publish_hot(afternoon={"ok": True, "signals": [{"symbol": "QQQ"}], "phase": "scanning"})
        publish_hot(afternoon={"ok": False, "signals": []})
        assert hot_snapshot()["afternoon_puts"]["ok"] is True
        # Sniper and Diamond still keep the last good board.
        publish_hot(sniper={"ok": False, "error": "bars down"})
        publish_hot(diamond={"ok": False, "error": "bars down"})
        kept = hot_snapshot()
        assert kept["sniper"]["ok"] is True
        assert kept["diamond"]["ok"] is True
    finally:
        clear_hot()
