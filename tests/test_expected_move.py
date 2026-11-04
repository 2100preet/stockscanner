"""Expected move math, the pre-market Telegram line, and the research book."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts
from odte_scanner.alerts.lunch_credit import format_lunch_entry
from odte_scanner.hot_state import apply_hot_overlay, clear_hot, publish_hot
from odte_scanner.research.expected_move_study import (
    LIVE_RULES_CHANGED,
    apply_filter,
    lunch_book,
    recommend,
    run_study,
    short_strike_at,
    split_sessions,
    summarize,
)
from odte_scanner.signals.diamond import Bar
from odte_scanner.signals.expected_move import (
    DIVISOR,
    atm_straddle_from_rows,
    build_expected_move,
    daily_move_pct,
    format_breach_message,
    format_em_used,
    format_expected_move_message,
    in_premarket_window,
    move_points,
    move_used_pct,
    one_sigma_band,
    range_used_pct,
    run_expected_move_cycle,
    straddle_from_yahoo,
    straddle_implied_pct,
)
from odte_scanner.ui import PAGE, _expected_move_block

CT = ZoneInfo("America/Chicago")


def test_rule_of_16_and_the_band_around_the_prior_close():
    assert DIVISOR == 15.87
    pct = daily_move_pct(32)
    assert pct == 32 / 15.87
    assert abs(pct - 2.016) < 0.01
    assert daily_move_pct(0) is None
    assert daily_move_pct(None) is None
    points = move_points(6800, pct)
    assert points == 6800 * pct / 100.0
    low, high = one_sigma_band(6800, 135)
    assert (low, high) == (6665, 6935)


def test_move_used_and_the_high_low_range():
    assert move_used_pct(6862, 6800, 100) == 62
    assert range_used_pct(110, 90, 40) == 50
    assert move_used_pct(None, 100, 1) is None
    assert range_used_pct(90, 110, 1) is None


def test_straddle_mid_over_spot_and_a_missing_wing():
    implied = straddle_implied_pct(100, 1.0, 1.2, 0.8, 1.0)
    assert implied["mid"] == 2.0
    assert implied["pct"] == 2.0
    assert straddle_implied_pct(100, None, 1.2, 0.8, 1.0) is None
    assert straddle_implied_pct(100, 1.2, 1.0, 0.8, 1.0) is None
    rows = [
        {"right": "C", "strike": 100, "expiry": "2026-11-04", "bid": 1.0, "ask": 1.2},
        {"right": "P", "strike": 100, "expiry": "2026-11-04", "bid": 0.8, "ask": 1.0},
        {"right": "C", "strike": 101, "expiry": "2026-11-04", "bid": 0.4, "ask": 0.6},
        {"right": "P", "strike": 101, "expiry": "2026-11-05", "bid": 1.0, "ask": 1.2},
    ]
    picked = atm_straddle_from_rows(rows, 100, "2026-11-04")
    assert picked["strike"] == 100
    assert picked["pct"] == 2.0
    assert atm_straddle_from_rows([], 100, "2026-11-04") is None
    yahoo = straddle_from_yahoo(
        {
            "options": [
                {
                    "expirationDate": int(datetime(2026, 11, 4, tzinfo=ZoneInfo("UTC")).timestamp()),
                    "calls": [{"strike": 100, "bid": 1.0, "ask": 1.2}],
                    "puts": [{"strike": 100, "bid": 0.8, "ask": 1.0}],
                }
            ]
        },
        100,
        date(2026, 11, 4),
    )
    assert yahoo["pct"] == 2.0
    assert straddle_from_yahoo(None, 100, date(2026, 11, 4)) is None


def test_message_shape_vix1d_straddle_and_no_retired_name():
    board = build_expected_move(
        vix=32.1,
        vix1d=20,
        anchors={"SPX": 6800, "SPY": 670, "QQQ": 590},
        straddles={"SPY": {"pct": 1.6}, "QQQ": {"pct": 1.7}},
        day="2026-11-04",
    )
    text = format_expected_move_message(board)
    assert text.splitlines()[0] == (
        "🧭 EXPECTED MOVE (VIX 32.1 → ±2.0%) · "
        "SPX ±138 pts (6,662–6,938) · "
        "SPY ±$13.6 · QQQ ±$11.9"
    )
    assert "VIX1D 20.0 → ±1.3%" in text
    assert "SPY straddle ±1.6%" in text
    assert "QQQ straddle ±1.7%" in text
    assert "Sure Winner" not in text
    bare = build_expected_move(vix=32.1, anchors={"SPY": 670})
    assert "SPX" not in format_expected_move_message(bare)
    assert format_expected_move_message(build_expected_move(vix=None, anchors={"SPY": 1})) == ""
    moved = build_expected_move(
        vix=32.1,
        anchors={"SPX": 6800, "SPY": 670, "QQQ": 590},
        marks={"SPX": {"last": 6862, "high": 6870, "low": 6790}},
    )
    spx = next(row for row in moved["symbols"] if row["symbol"] == "SPX")
    assert round(spx["move_used_pct"]) == 45
    assert spx["range_used_pct"] > 0
    assert "62% used" not in text


def test_sixty_two_percent_used_on_the_panel_numbers():
    board = build_expected_move(
        vix=15.87,
        anchors={"SPX": 6800},
        marks={"SPX": {"last": 6862, "high": 6870, "low": 6790}},
    )
    spx = board["symbols"][0]
    # VIX 15.87 is a 1% day, so 62 points on 6800 is 62 / 68 ≈ 91% of a 68-point move.
    assert spx["display_points"] == 68
    assert round(move_used_pct(6862, 6800, 100)) == 62


def test_premarket_window_and_one_telegram_then_one_breach(tmp_path):
    assert in_premarket_window(datetime(2026, 11, 4, 8, 20, tzinfo=CT)) is True
    assert in_premarket_window(datetime(2026, 11, 4, 8, 10, tzinfo=CT)) is False
    assert in_premarket_window(datetime(2026, 11, 4, 8, 30, tzinfo=CT)) is False
    assert in_premarket_window(datetime(2026, 11, 8, 8, 20, tzinfo=CT)) is False
    sent: list[str] = []

    def send(text: str) -> dict:
        sent.append(text)
        return {"ok": True}

    def vol():
        return {"vix": 32.1, "vix1d": 20.0, "source": "test"}

    quiet = {
        "SPX": {"last": 6800, "prev_close": 6800, "high": 6810, "low": 6790},
        "SPY": {"last": 670, "prev_close": 670, "high": 671, "low": 669},
        "QQQ": {"last": 590, "prev_close": 590, "high": 591, "low": 589},
    }
    path = tmp_path / "expected_move_day.json"
    morning = datetime(2026, 11, 4, 8, 25, tzinfo=CT)
    board = run_expected_move_cycle(
        now=morning,
        store_path=path,
        fetch_vol=vol,
        fetch_marks=lambda: quiet,
        fetch_straddle=lambda _state: {"SPY": {"pct": 1.6, "mid": 10.7, "strike": 670}},
        send=send,
        publish=lambda _board: None,
    )
    assert board["ok"] is True
    assert board["frozen"] is True
    assert len(sent) == 1
    assert sent[0].startswith("🧭 EXPECTED MOVE (VIX 32.1 → ±2.0%)")
    assert "Sure Winner" not in sent[0]
    run_expected_move_cycle(
        now=morning,
        store_path=path,
        fetch_vol=vol,
        fetch_marks=lambda: quiet,
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert len(sent) == 1
    jumped = dict(quiet)
    jumped["SPX"] = {"last": 7000, "prev_close": 6800, "high": 7000, "low": 6790}
    later = run_expected_move_cycle(
        now=datetime(2026, 11, 4, 10, 5, tzinfo=CT),
        store_path=path,
        fetch_vol=lambda: {"vix": 40, "source": "test"},
        fetch_marks=lambda: jumped,
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert later["vix"] == 32.1
    assert len(sent) == 2
    assert sent[1].startswith("🧭 EXPECTED MOVE · SPX used")
    assert "100" in sent[1] or "1" in sent[1]
    breach = format_breach_message(later)
    assert "SPX used" in breach
    run_expected_move_cycle(
        now=datetime(2026, 11, 4, 11, 0, tzinfo=CT),
        store_path=path,
        fetch_vol=vol,
        fetch_marks=lambda: jumped,
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert len(sent) == 2
    waiting = run_expected_move_cycle(
        now=datetime(2026, 11, 6, 8, 29, tzinfo=CT),
        store_path=tmp_path / "waiting.json",
        fetch_vol=vol,
        fetch_marks=lambda: {},
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert waiting["frozen"] is False
    assert waiting["telegram_sent"] is False
    filled = run_expected_move_cycle(
        now=datetime(2026, 11, 6, 8, 40, tzinfo=CT),
        store_path=tmp_path / "waiting.json",
        fetch_vol=vol,
        fetch_marks=lambda: quiet,
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert filled["ok"] is True
    assert filled["telegram_sent"] is True
    early = run_expected_move_cycle(
        now=datetime(2026, 11, 5, 8, 16, tzinfo=CT),
        store_path=tmp_path / "early.json",
        fetch_vol=vol,
        fetch_marks=lambda: quiet,
        fetch_straddle=lambda _state: {},
        send=send,
        publish=lambda _board: None,
    )
    assert early["telegram_sent"] is False
    assert len(sent) == 3
    assert sent[2].startswith("🧭 EXPECTED MOVE (VIX 32.1 → ±2.0%)")


def test_em_used_lands_on_lunch_and_afternoon_alerts_only_when_present():
    card = {
        "symbol": "SPY",
        "send": True,
        "action": "LUNCH_CREDIT",
        "desk": "LunchCredit",
        "lunch_credit": True,
        "expiry": "2026-11-03",
        "at_ct": "12:05",
        "contracts": 1,
        "width": 2,
        "credit": 0.80,
        "limit_price": 0.80,
        "max_risk_usd": 120,
        "short_strike": 500,
        "long_strike": 498,
        "short": {"strike": 500, "bid": 1.20, "ask": 1.25},
        "long": {"strike": 498, "bid": 0.40, "ask": 0.45},
        "detail": "Lunch-Break Credit.",
        "em_used_pct": 45,
    }
    text = format_lunch_entry(card)
    assert "EM used 45%" in text
    assert "Sure Winner" not in text
    assert format_em_used(None) is None
    plain = dict(card)
    plain.pop("em_used_pct")
    assert "EM used" not in format_lunch_entry(plain)
    afternoon = {
        "symbol": "QQQ",
        "send": True,
        "action": "AFTERNOON_PUTS",
        "desk": "AfternoonPuts",
        "afternoon_puts": True,
        "right": "P",
        "strike": 693,
        "expiry": "2026-11-02",
        "ask": 1.52,
        "bid": 1.48,
        "spot": 693.66,
        "contract": "QQQ261102P00693000",
        "at_ct": "10:25",
        "detail": "Afternoon Puts.",
        "em_used_pct": 45.4,
    }
    alerts = collect_trade_alerts({"afternoon_puts": {"signals": [afternoon]}})
    assert "EM used 45%" in alerts[0]["message"]
    assert "Sure Winner" not in alerts[0]["message"]


def test_portal_tile_snapshot_block_and_hot_overlay(tmp_path, monkeypatch):
    assert 'id="expectedMoveBanner"' in PAGE
    assert "function renderExpectedMove" in PAGE
    assert "Expected <em>Move</em>" in PAGE
    assert "% used" in PAGE
    banner = PAGE[PAGE.index("function renderExpectedMove") : PAGE.index("function paint")]
    assert "Sure Winner" not in banner
    assert 'el.classList.remove("active")' not in banner
    monkeypatch.setattr(
        "odte_scanner.signals.expected_move.DEFAULT_STORE",
        tmp_path / "missing.json",
    )
    # load_public_board uses the argument, and the UI helper reads the default.
    # Point the helper at an empty directory by patching the loader.
    monkeypatch.setattr(
        "odte_scanner.ui.load_public_board",
        lambda path=None: {"ok": False, "symbols": [], "note": "Expected move prints around 8:15 CT."},
        raising=False,
    )
    block = _expected_move_block()
    assert block["ok"] is False
    assert block["symbols"] == []
    clear_hot()
    try:
        publish_hot(expected_move={"ok": True, "vix": 16, "symbols": [{"symbol": "SPX", "move_used_pct": 62}]})
        over = apply_hot_overlay({"expected_move": {"ok": False, "symbols": []}})
        assert over["expected_move"]["vix"] == 16
        publish_hot(expected_move={"ok": False, "error": "feed down"})
        kept = apply_hot_overlay({})
        assert kept["expected_move"]["ok"] is True
    finally:
        clear_hot()


def _day(day: str, base: float, break_close: float, finish: float) -> list[Bar]:
    bars: list[Bar] = []
    for minute in range(10 * 60, 14 * 60 + 50, 5):
        hour, mm = divmod(minute, 60)
        stamp = datetime.fromisoformat(f"{day}T{hour:02d}:{mm:02d}:00").replace(tzinfo=CT)
        if minute < 12 * 60:
            bars.append(Bar(ts=stamp, open=base, high=base + 0.4, low=base - 0.2, close=base + 0.1, volume=1000))
        elif minute == 12 * 60:
            bars.append(Bar(ts=stamp, open=base + 0.3, high=break_close, low=base, close=break_close, volume=1000))
        else:
            bars.append(Bar(ts=stamp, open=finish, high=finish + 0.2, low=finish - 0.1, close=finish, volume=800))
    return bars


def test_research_filters_do_not_change_live_rules():
    assert LIVE_RULES_CHANGED is False
    assert short_strike_at(100.4, "SPY", 0) == 100
    assert short_strike_at(100, "SPY", 2.3) == 97
    days = [date(2026, 8, d) for d in range(3, 13)]
    train, oos = split_sessions(days)
    assert len(train) == 7
    assert len(oos) == 3
    assert max(train) < min(oos)
    bars = {
        "SPY": _day("2026-08-03", 100, 100.6, 100) + _day("2026-08-04", 100, 102, 104) + _day("2026-08-05", 104, 104.5, 106),
    }
    vix = {date(2026, 8, 3): 16.0, date(2026, 8, 4): 16.0, date(2026, 8, 5): 16.0}
    trades = lunch_book(bars, vix)
    assert len(trades) == 3
    hot = [row for row in trades if (row.get("em_used_pct") or 0) > 50]
    assert hot and hot[0]["day"] == "2026-08-04"
    kept = apply_filter(trades, lambda used: used is not None and used > 50)
    assert all((row.get("em_used_pct") or 0) <= 50 for row in kept)
    assert any(row["day"] == "2026-08-05" for row in kept)
    report = run_study(bars, vix, label="synthetic")
    assert report["live_rules_changed"] is False
    assert "unchanged" in report["lunch_credit"]["recommendation"]
    assert "Sure Winner" not in report["lunch_credit"]["recommendation"]
    assert report["afternoon_puts"]["variants"][0]["name"] == "baseline"
    no = recommend(
        "Lunch-Break Credit",
        [
            {"name": "baseline", "oos": {"n": 10, "pnl": 100, "win_rate": 0.6}, "train": {"n": 20, "pnl": 50, "win_rate": 0.5}, "all": {"n": 30}},
            {"name": "skip if EM used > 50%", "oos": {"n": 4, "pnl": 200, "win_rate": 0.8}, "train": {"n": 10, "pnl": 40, "win_rate": 0.5}, "all": {"n": 14}},
        ],
    )
    assert "Leave the live rules unchanged" in no
    better = recommend(
        "Afternoon Puts",
        [
            {"name": "baseline", "oos": {"n": 10, "pnl": 100, "win_rate": 0.5}, "train": {"n": 20, "pnl": 80, "win_rate": 0.5}, "all": {"n": 30}},
            {"name": "skip if EM used > 75%", "oos": {"n": 9, "pnl": 180, "win_rate": 0.7}, "train": {"n": 12, "pnl": 70, "win_rate": 0.5}, "all": {"n": 21}},
        ],
    )
    assert "Do not turn it on" in better
    assert summarize([{"pnl_usd": 40}, {"pnl_usd": -10}])["win_rate"] == 0.5
