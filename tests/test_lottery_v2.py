"""Lottery v2 is log-only: direction of the break, no Webull route."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery_v2 import (
    LotteryV2Rules,
    build_lottery_v2_board,
    decide_lottery_v2,
    lottery_v2_universe,
    replay_ladder,
    rules_from_config,
    scan_session_break,
    ticket_budget,
)

ET = ZoneInfo("America/New_York")
SESSION_NOW = datetime(2026, 8, 5, 11, 30, tzinfo=ET)


def _tape(**overrides):
    base = {
        "last": 101.0,
        "orb_high": 100.0,
        "orb_low": 99.0,
        "vwap": 100.2,
        "rel_volume": 1.4,
    }
    base.update(overrides)
    return base


def _candidate(**overrides):
    base = {
        "symbol": "SPY",
        "right": "C",
        "contract": "SPY260805C00101000",
        "strike": 101,
        "expiry": "2026-08-05",
        "dte": 0,
        "ask": 0.15,
        "bid": 0.12,
        "volume": 200,
        "open_interest": 400,
    }
    base.update(overrides)
    return base


def test_call_break_logs_and_does_not_buy():
    sig = decide_lottery_v2(_candidate(), tape=_tape(), now=SESSION_NOW)
    assert sig.action == "LOG"
    assert sig.action != "BUY_NOW"
    assert sig.side == "call"
    assert sig.log_only is True
    assert sig.routed is False
    assert sig.confirms >= 3
    assert "orb15_hold" in sig.confirm_tags
    assert "vwap_side" in sig.confirm_tags
    assert sig.size_usd == 45.0  # 3 contracts at $0.15
    assert sig.contracts == 3


def test_put_break_logs():
    sig = decide_lottery_v2(
        _candidate(right="P", contract="SPY260805P00099000", strike=99, ask=0.10, bid=0.08),
        tape=_tape(last=98.0, vwap=99.4),
        now=SESSION_NOW,
    )
    assert sig.action == "LOG"
    assert sig.side == "put"
    assert sig.size_usd == 50.0
    assert sig.contracts == 5


def test_rejects_ask_outside_band():
    cheap = decide_lottery_v2(_candidate(ask=0.01, bid=0.01), tape=_tape(), now=SESSION_NOW)
    rich = decide_lottery_v2(_candidate(ask=0.80, bid=0.70), tape=_tape(), now=SESSION_NOW)
    assert cheap.action == "SKIP"
    assert rich.action == "SKIP"
    assert "outside" in cheap.detail
    assert "outside" in rich.detail


def test_rejects_open_and_last_half_hour():
    early = decide_lottery_v2(
        _candidate(),
        tape=_tape(),
        now=datetime(2026, 8, 5, 9, 32, tzinfo=ET),
    )
    late = decide_lottery_v2(
        _candidate(),
        tape=_tape(),
        now=datetime(2026, 8, 5, 15, 40, tzinfo=ET),
    )
    assert early.action == "SKIP"
    assert late.action == "SKIP"
    assert "entry window" in early.detail
    assert "entry window" in late.detail
    open_ok = decide_lottery_v2(
        _candidate(),
        tape=_tape(),
        now=datetime(2026, 8, 5, 9, 35, tzinfo=ET),
    )
    assert open_ok.action == "LOG"


def test_rejects_dte_over_one_and_wide_spread():
    far = decide_lottery_v2(_candidate(dte=2), tape=_tape(), now=SESSION_NOW)
    wide = decide_lottery_v2(_candidate(ask=0.30, bid=0.05), tape=_tape(), now=SESSION_NOW)
    missing_bid = decide_lottery_v2(_candidate(bid=None), tape=_tape(), now=SESSION_NOW)
    assert far.action == "SKIP"
    assert "DTE" in far.detail
    assert wide.action == "SKIP"
    assert "spread" in wide.detail
    assert missing_bid.action == "LOG"


def test_opposite_flow_vetoes_and_missing_orb_skips():
    veto = decide_lottery_v2(
        _candidate(),
        tape=_tape(),
        now=SESSION_NOW,
        flow_sentiment="bearish",
    )
    assert veto.action == "SKIP"
    assert "opposes" in veto.detail
    missing = decide_lottery_v2(
        _candidate(),
        tape=_tape(orb_high=None, orb_low=None, vwap=None),
        now=SESSION_NOW,
    )
    assert missing.action == "SKIP"
    assert "missing ORB15" in missing.detail


def test_needs_a_third_confirm():
    sig = decide_lottery_v2(
        _candidate(volume=10, open_interest=500),
        tape=_tape(rel_volume=0.4),
        now=SESSION_NOW,
    )
    assert sig.action == "SKIP"
    assert "confirms" in sig.detail
    with_vix = decide_lottery_v2(
        _candidate(volume=10, open_interest=500),
        tape=_tape(rel_volume=0.4),
        now=SESSION_NOW,
        vix=15.0,
    )
    assert with_vix.action == "LOG"
    assert "vix_regime" in with_vix.confirm_tags


def test_max_three_tickets():
    names = ["SPY", "QQQ", "IWM", "DIA"]
    candidates = [
        _candidate(
            symbol=sym,
            ask=ask,
            bid=round(ask - 0.02, 2),
            contract=f"{sym}260805C00001000",
        )
        for sym, ask in zip(names, (0.30, 0.10, 0.20, 0.15), strict=True)
    ]
    tape = {sym: _tape() for sym in names}
    board = build_lottery_v2_board(candidates, tape_by_symbol=tape, now=SESSION_NOW)
    assert board["log_only"] is True
    assert board["routed"] is False
    assert len(board["tickets"]) == 3
    assert [t["symbol"] for t in board["tickets"]] == ["QQQ", "DIA", "IWM"]
    assert any("max tickets" in (t.get("detail") or "") for t in board["skip"])
    assert all(t["action"] == "LOG" for t in board["tickets"])


def test_replay_half_at_5x_rest_at_10x_and_giveback():
    ten = replay_ladder(
        0.20,
        [{"close": 1.10, "high": 1.20}, {"close": 2.20, "high": 2.40}],
    )
    assert ten["hit_5x"] is True
    assert ten["hit_10x"] is True
    assert ten["reason"] == "10x"
    assert ten["half_exit"] == 1.10
    assert ten["rest_exit"] == 2.20
    assert ten["went_to_zero"] is False

    giveback = replay_ladder(
        0.10,
        [
            {"close": 0.60, "high": 0.62},
            {"close": 0.70, "high": 0.75},
            {"close": 0.30, "high": 0.40},
        ],
    )
    assert giveback["reason"] == "giveback"
    assert giveback["half_exit"] == 0.60
    assert giveback["rest_exit"] == 0.30
    assert giveback["hit_10x"] is False

    zero = replay_ladder(
        0.20,
        [{"ts": "2026-08-05T15:50:00-04:00", "close": 0.02, "high": 0.03}],
    )
    assert zero["reason"] == "time_exit"
    assert zero["hit_5x"] is False
    assert zero["went_to_zero"] is True
    assert zero["ev_per_100"] == -90.0


def test_scan_session_break_call_then_put():
    def bars(closes):
        out = []
        for i, close in enumerate(closes):
            minute = 30 + i * 5
            hour = 9 + minute // 60
            minute = minute % 60
            out.append(
                {
                    "ts": datetime(2026, 10, 14, hour, minute, tzinfo=ET).isoformat(),
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1000 * (i + 1),
                }
            )
        return out

    call = scan_session_break(bars([100.0, 100.1, 100.0, 101.5]))
    assert call is not None
    assert call["side"] == "call"
    assert call["orb_high"] > 100
    put = scan_session_break(bars([100.0, 100.1, 100.0, 98.0]))
    assert put is not None
    assert put["side"] == "put"


def test_universe_includes_meta_and_indices():
    names = lottery_v2_universe(["PFE"])
    assert "META" in names
    assert "SPY" in names
    assert "QQQ" in names
    assert "PFE" in names
    assert "AAPL" in names


def test_rules_from_config_and_budget():
    rules = rules_from_config({"lottery_v2_min_ask": 0.08, "lottery_v2_max_tickets": 2, "buy_score": 72})
    assert rules.min_ask == 0.08
    assert rules.max_tickets == 2
    assert ticket_budget(0.40, LotteryV2Rules()) == (1, 40.0)
    assert ticket_budget(0.05, LotteryV2Rules()) == (10, 50.0)


def test_webull_sync_does_not_know_lottery_v2():
    import inspect

    from odte_scanner.trading.auto_trader import AutoTrader
    from odte_scanner.ui import PAGE

    assert "lottery_v2" not in inspect.getsource(AutoTrader.sync)
    assert 'data-tab="explosive">Lottery<' in PAGE
    assert PAGE.count('id="tab-explosive"') == 1
    assert PAGE.count('id="tab-rip"') == 1
    assert PAGE.count('id="tab-patterns"') == 1
    assert 'id="nowBoardPinned"' in PAGE
    assert 'const PINNED_DESKS = ["Lottery", "Lottery v2", "RIP/META", "Patterns"]' in PAGE
    assert "Lottery v2 — LOG ONLY" in PAGE
