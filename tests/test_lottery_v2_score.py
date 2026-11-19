"""Lottery v2 log scoring is display-only and cannot break the snapshot."""

import inspect
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery_v2 import (
    append_lottery_v2_log,
    build_lottery_v2_board,
)
from odte_scanner.signals.lottery_v2_score import (
    NOTE,
    build_block,
    default_fetch_bars,
    public_block,
    score_for_snapshot,
)

ET = ZoneInfo("America/New_York")
NOW_OPEN = datetime(2026, 8, 5, 12, 30, tzinfo=ET)
NOW_CLOSE = datetime(2026, 8, 5, 16, 0, tzinfo=ET)


def _row(**overrides):
    base = {
        "action": "LOG",
        "symbol": "SPY",
        "right": "C",
        "side": "call",
        "contract": "SPY260805C00101000",
        "strike": 101,
        "expiry": "2026-08-05",
        "ask": 0.20,
        "contracts": 2,
        "size_usd": 40.0,
        "session_date": "2026-08-05",
        "logged_at": "2026-08-05T11:30:00-04:00",
        "log_only": True,
        "routed": False,
    }
    base.update(overrides)
    return base


def _bar(day, hour, minute, close, high=None):
    return {
        "ts": datetime(day[0], day[1], day[2], hour, minute, tzinfo=ET).isoformat(),
        "close": close,
        "high": close if high is None else high,
    }


class Fetch:
    def __init__(self, bars=None, by_contract=None):
        self.bars = list(bars or [])
        self.by_contract = by_contract or {}
        self.calls = []

    def __call__(self, ticket, session_date):
        self.calls.append((ticket.get("contract"), session_date))
        contract = ticket.get("contract")
        if contract in self.by_contract:
            produced = self.by_contract[contract]
            if isinstance(produced, Exception):
                raise produced
            return list(produced)
        return list(self.bars)


def _write_log(path, rows):
    path.write_text(json.dumps(rows), encoding="utf-8")


def _paths(tmp_path):
    return tmp_path / "lottery_v2_log.json", tmp_path / "lottery_v2_scores.json"


def _score(tmp_path, rows, fetch, now, **kwargs):
    log_path, score_path = _paths(tmp_path)
    _write_log(log_path, rows)
    before = log_path.read_text(encoding="utf-8")
    block = build_block(
        log_path=log_path,
        score_path=score_path,
        now=now,
        fetch_bars=fetch,
        **kwargs,
    )
    assert log_path.read_text(encoding="utf-8") == before
    return block, score_path


def test_ten_x_finalizes_at_size_and_does_not_change_later(tmp_path):
    day = (2026, 8, 5)
    bars = [_bar(day, 11, 40, 1.10), _bar(day, 11, 50, 2.20, 2.40)]
    fetch = Fetch(bars)
    block, score_path = _score(tmp_path, [_row()], fetch, NOW_OPEN)
    ticket = block["today"]["tickets"][0]
    assert block["ok"] is True
    assert block["log_only"] is True
    assert block["routed"] is False
    assert block["not_traded"] is True
    assert block["note"] == NOTE
    assert ticket["status"] == "final"
    assert ticket["reason"] == "10x"
    assert ticket["reason_label"] == "10×"
    assert ticket["entry"] == 0.20
    assert ticket["exit"] == 1.65
    assert ticket["entry_ct"] == "10:30 AM CT"
    assert ticket["exit_ct"] == "10:50 AM CT"
    assert ticket["pnl_per_contract"] == 145.0
    assert ticket["contracts"] == 2
    assert ticket["size_usd"] == 40.0
    assert ticket["size_budget"] == 50.0
    assert ticket["pnl_at_size"] == 290.0
    assert ticket["hit_5x"] is True
    assert ticket["log_only"] is True
    assert ticket["routed"] is False
    assert block["today"]["pnl_per_contract"] == 145.0
    assert block["today"]["pnl_at_size"] == 290.0
    assert block["since_start"]["trades"] == 1
    assert block["since_start"]["wins"] == 1
    assert block["since_start"]["win_rate_pct"] == 100.0
    assert block["since_start"]["hit_5x"] == 1
    stored = json.loads(score_path.read_text(encoding="utf-8"))
    assert stored["scores"]["2026-08-05|SPY260805C00101000"]["final"] is True

    later = Fetch([_bar(day, 15, 50, 0.01)])
    again, _ = _score(tmp_path, [_row()], later, NOW_CLOSE)
    assert later.calls == []
    assert again["today"]["tickets"][0]["pnl_per_contract"] == 145.0
    assert again["today"]["tickets"][0]["reason"] == "10x"


def test_open_ticket_stays_live_until_the_session_closes(tmp_path):
    day = (2026, 8, 5)
    live_bars = [_bar(day, 12, 0, 0.30)]
    fetch = Fetch(live_bars)
    block, score_path = _score(tmp_path, [_row()], fetch, NOW_OPEN)
    ticket = block["today"]["tickets"][0]
    assert ticket["status"] == "live"
    assert ticket["reason"] == "live"
    assert ticket["exit"] is None
    assert ticket["mark"] == 0.30
    assert ticket["mark_ct"] == "11:00 AM CT"
    assert ticket["pnl_per_contract"] == 10.0
    assert ticket["pnl_at_size"] == 20.0
    assert block["since_start"]["trades"] == 0
    assert block["since_start"]["open"] == 1
    assert block["today"]["pnl_at_size"] == 20.0
    stored = json.loads(score_path.read_text(encoding="utf-8"))
    assert stored["scores"]["2026-08-05|SPY260805C00101000"]["final"] is False

    closed = Fetch([_bar(day, 15, 50, 0.02)])
    final, _ = _score(tmp_path, [_row()], closed, NOW_CLOSE)
    done = final["today"]["tickets"][0]
    assert done["status"] == "final"
    assert done["reason"] == "time_exit"
    assert done["reason_label"] == "time exit 15:45 ET"
    assert done["exit_ct"] == "2:50 PM CT"
    assert done["went_to_zero"] is True
    assert done["pnl_per_contract"] == -18.0
    assert final["since_start"]["trades"] == 1
    assert final["since_start"]["went_to_zero"] == 1
    assert final["since_start"]["wins"] == 0
    assert final["since_start"]["win_rate_pct"] == 0.0

    revised = Fetch([_bar(day, 14, 0, 2.50)])
    frozen, _ = _score(tmp_path, [_row()], revised, NOW_CLOSE)
    assert revised.calls == []
    assert frozen["today"]["tickets"][0]["pnl_per_contract"] == -18.0
    assert frozen["today"]["tickets"][0]["went_to_zero"] is True


def test_giveback_and_rich_budget(tmp_path):
    day = (2026, 8, 5)
    # Ask $0.30 is the $25 budget. 5× is $1.50, so the rest gives back from that peak.
    bars = [
        _bar(day, 11, 40, 1.60),
        _bar(day, 11, 45, 1.80),
        _bar(day, 11, 55, 0.80),
    ]
    row = _row(ask=0.30, bid=0.22, contracts=1, size_usd=30.0, right="P", side="put")
    block, _ = _score(tmp_path, [row], Fetch(bars), NOW_OPEN)
    ticket = block["today"]["tickets"][0]
    assert ticket["status"] == "final"
    assert ticket["reason"] == "giveback"
    assert ticket["reason_label"] == "50% giveback from peak"
    assert ticket["half_exit"] == 1.60
    assert ticket["rest_exit"] == 0.80
    assert ticket["pnl_per_contract"] == 90.0
    assert ticket["pnl_at_size"] == 90.0
    assert ticket["size_budget"] == 25.0
    assert ticket["right"] == "P"


def test_since_start_counts_final_tickets_and_keeps_today_live(tmp_path):
    winner = _row(
        contract="QQQ260804C00100000",
        symbol="QQQ",
        session_date="2026-08-04",
        logged_at="2026-08-04T11:00:00-04:00",
        ask=0.10,
        contracts=5,
        size_usd=50.0,
    )
    zero = _row(
        contract="IWM260803P00200000",
        symbol="IWM",
        right="P",
        session_date="2026-08-03",
        logged_at="2026-08-03T11:00:00-04:00",
        ask=0.20,
        contracts=1,
        size_usd=20.0,
    )
    live = _row(ask=0.20, contracts=2, size_usd=40.0)
    bars = {
        winner["contract"]: [
            _bar((2026, 8, 4), 11, 20, 0.55),
            _bar((2026, 8, 4), 11, 40, 1.20),
        ],
        zero["contract"]: [_bar((2026, 8, 3), 15, 50, 0.02)],
        live["contract"]: [_bar((2026, 8, 5), 12, 0, 0.05)],
    }
    block, _ = _score(tmp_path, [zero, winner, live], Fetch(by_contract=bars), NOW_OPEN)
    since = block["since_start"]
    assert since["start_day"] == "2026-08-03"
    assert since["trades"] == 2
    assert since["wins"] == 1
    assert since["win_rate_pct"] == 50.0
    assert since["hit_5x"] == 1
    assert since["went_to_zero"] == 1
    assert since["open"] == 1
    assert since["pnl_per_contract"] == 59.5
    assert since["pnl_at_size"] == 369.5
    assert block["today"]["n"] == 1
    assert block["today"]["live"] == 1
    assert block["today"]["tickets"][0]["status"] == "live"
    assert block["today"]["pnl_per_contract"] == -15.0
    assert block["today"]["pnl_at_size"] == -30.0
    assert block["note"] == "log only · not traded"


def test_bars_before_the_log_time_are_not_the_trade(tmp_path):
    early = [_bar((2026, 8, 5), 10, 0, 2.20)]
    fetch = Fetch(early)
    block, _ = _score(tmp_path, [_row()], fetch, NOW_CLOSE)
    ticket = block["today"]["tickets"][0]
    assert ticket["status"] == "unscored"
    assert ticket["pnl_per_contract"] is None
    assert block["since_start"]["trades"] == 0
    assert block["since_start"]["hit_5x"] == 0
    assert fetch.calls


def test_tradier_time_string_scores_the_session(tmp_path):
    bars = [{"time": "2026-08-05 15:50:00", "close": 0.02, "high": 0.03}]
    block, _ = _score(tmp_path, [_row()], Fetch(bars), NOW_CLOSE)
    ticket = block["today"]["tickets"][0]
    assert ticket["status"] == "final"
    assert ticket["reason"] == "time_exit"
    assert ticket["went_to_zero"] is True
    assert ticket["exit_ct"] == "2:50 PM CT"


def test_one_ticket_fetch_failure_leaves_the_others(tmp_path):
    good = _row()
    bad = _row(symbol="QQQ", contract="QQQ260805C00100000")
    fetch = Fetch(
        by_contract={
            good["contract"]: [_bar((2026, 8, 5), 15, 50, 0.02)],
            bad["contract"]: RuntimeError("quote down"),
        }
    )
    block, _ = _score(tmp_path, [good, bad], fetch, NOW_CLOSE)
    assert block["ok"] is True
    by_symbol = {row["symbol"]: row for row in block["today"]["tickets"]}
    assert by_symbol["SPY"]["status"] == "final"
    assert by_symbol["SPY"]["went_to_zero"] is True
    assert by_symbol["QQQ"]["status"] == "unscored"
    assert by_symbol["QQQ"]["reason"] == "error"
    assert block["since_start"]["trades"] == 1


def test_closed_miss_backs_off_without_a_fake_zero(tmp_path):
    fetch = Fetch([])
    block, score_path = _score(tmp_path, [_row()], fetch, NOW_CLOSE)
    assert block["today"]["tickets"][0]["status"] == "unscored"
    assert block["since_start"]["trades"] == 0
    assert block["today"]["pnl_per_contract"] is None
    stored = json.loads(score_path.read_text(encoding="utf-8"))["scores"]
    assert stored["2026-08-05|SPY260805C00101000"]["final"] is False
    assert "next_try_at" in stored["2026-08-05|SPY260805C00101000"]

    again = Fetch([_bar((2026, 8, 5), 15, 50, 0.01)])
    blocked, _ = _score(tmp_path, [_row()], again, NOW_CLOSE + timedelta(minutes=10))
    assert again.calls == []
    assert blocked["today"]["tickets"][0]["status"] == "unscored"

    later = Fetch([_bar((2026, 8, 5), 15, 50, 0.01)])
    scored, _ = _score(tmp_path, [_row()], later, NOW_CLOSE + timedelta(minutes=31))
    assert later.calls
    assert scored["today"]["tickets"][0]["status"] == "final"
    assert scored["today"]["tickets"][0]["went_to_zero"] is True


def test_history_fetch_cap_scores_oldest_first_then_the_rest(tmp_path):
    rows = [
        _row(session_date="2026-08-03", logged_at="2026-08-03T11:00:00-04:00", contract="C3", symbol="IWM"),
        _row(session_date="2026-08-04", logged_at="2026-08-04T11:00:00-04:00", contract="C4", symbol="QQQ"),
        _row(session_date="2026-08-05", logged_at="2026-08-05T11:00:00-04:00", contract="C5", symbol="SPY"),
    ]
    # Today is 2026-08-06, so every row is history. Cap 1 per pass.
    bars = {
        "C3": [{"time": "2026-08-03 15:50:00", "close": 0.02, "high": 0.03}],
        "C4": [{"time": "2026-08-04 15:50:00", "close": 0.02, "high": 0.03}],
        "C5": [{"time": "2026-08-05 15:50:00", "close": 0.02, "high": 0.03}],
    }
    fetch = Fetch(by_contract=bars)
    now = datetime(2026, 8, 6, 10, 0, tzinfo=ET)
    block, _ = _score(tmp_path, rows, fetch, now, max_history_fetches=1)
    assert [call[0] for call in fetch.calls] == ["C3"]
    assert block["since_start"]["trades"] == 1
    assert block["since_start"]["unscored"] == 2

    nxt = Fetch(by_contract=bars)
    again, _ = _score(tmp_path, rows, nxt, now, max_history_fetches=1)
    assert [call[0] for call in nxt.calls] == ["C4"]
    assert again["since_start"]["trades"] == 2


def test_rows_already_in_the_log_are_scored_without_rewriting_it(tmp_path):
    log_path, score_path = _paths(tmp_path)
    now = datetime(2026, 8, 5, 11, 30, tzinfo=ET)
    board = build_lottery_v2_board(
        [
            {
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
        ],
        tape_by_symbol={
            "SPY": {"last": 101.0, "orb_high": 100.0, "orb_low": 99.0, "vwap": 100.2, "rel_volume": 1.4}
        },
        now=now,
    )
    assert board["tickets"] and board["log_only"] is True and board["routed"] is False
    append_lottery_v2_log(board, log_path)
    original = log_path.read_text(encoding="utf-8")
    saved = json.loads(original)
    assert saved[0]["session_date"] == "2026-08-05"
    assert saved[0]["action"] == "LOG"
    assert saved[0]["contracts"] == 3
    assert saved[0]["size_usd"] == 45.0

    fetch = Fetch([{"time": "2026-08-05 15:50:00", "close": 0.05, "high": 0.05}])
    block = build_block(
        log_path=log_path,
        score_path=score_path,
        now=NOW_CLOSE,
        fetch_bars=fetch,
    )
    assert log_path.read_text(encoding="utf-8") == original
    ticket = block["today"]["tickets"][0]
    assert ticket["status"] == "final"
    assert ticket["entry"] == 0.15
    assert ticket["contracts"] == 3
    assert ticket["size_usd"] == 45.0
    assert ticket["size_budget"] == 50.0
    assert ticket["pnl_per_contract"] == -10.0
    assert ticket["pnl_at_size"] == -30.0
    assert ticket["routed"] is False
    assert block["since_start"]["trades"] == 1


def test_skip_rows_are_not_scored(tmp_path):
    fetch = Fetch([_bar((2026, 8, 5), 15, 50, 0.02)])
    block, _ = _score(tmp_path, [_row(action="SKIP")], fetch, NOW_CLOSE)
    assert fetch.calls == []
    assert block["today"]["n"] == 0
    assert block["since_start"]["trades"] == 0


def test_public_block_never_raises(monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score.build_block",
        lambda **k: (_ for _ in ()).throw(RuntimeError("score down")),
    )
    out = public_block()
    assert out["ok"] is False
    assert out["log_only"] is True
    assert out["routed"] is False
    assert out["not_traded"] is True
    assert out["today"]["tickets"] == []
    assert "unavailable" in out["note"]


def test_score_for_snapshot_keeps_the_board_when_scoring_raises(monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score.public_block",
        lambda **k: (_ for _ in ()).throw(RuntimeError("score down")),
    )
    board = {
        "tickets": [{"symbol": "SPY", "action": "LOG"}],
        "skip": [{"symbol": "QQQ"}],
        "log_only": True,
        "routed": False,
    }
    out = score_for_snapshot(board, log_path="unused")
    assert out["tickets"] == board["tickets"]
    assert out["skip"] == board["skip"]
    assert out["log_only"] is True
    assert out["routed"] is False
    assert out["score"]["ok"] is False
    assert out["score"]["routed"] is False


def test_attach_failure_keeps_log_only_board(monkeypatch):
    from odte_scanner import ui

    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score.score_for_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("score down")),
    )
    board = {"tickets": [{"symbol": "SPY"}], "log_only": True, "routed": False, "counts": {"log": 1}}
    out = ui._attach_lottery_v2_score(board, {})
    assert out["tickets"] == board["tickets"]
    assert out["log_only"] is True
    assert out["routed"] is False
    assert out["counts"]["log"] == 1
    assert out["score"]["ok"] is False
    assert out["score"]["log_only"] is True
    assert out["score"]["routed"] is False


def test_score_module_does_not_route_orders():
    import odte_scanner.signals.lottery_v2_score as mod

    src = inspect.getsource(mod)
    assert "webull" not in src.lower()
    assert "place_stock" not in src
    assert "AutoTrader" not in src


def test_default_fetch_uses_the_first_source_and_swallows_errors(monkeypatch):
    def boom(contract, day):
        raise RuntimeError(f"down {contract}")

    monkeypatch.setattr("odte_scanner.signals.lottery_v2_score._tradier_option_bars", boom)
    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score._polygon_option_bars",
        lambda contract, day: [{"ts": f"{day}T15:50:00-04:00", "close": 0.02, "high": 0.03}],
    )
    called = {"yahoo": 0, "uw": 0}
    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score._yahoo_option_bars",
        lambda contract, day: called.__setitem__("yahoo", called["yahoo"] + 1) or [],
    )
    monkeypatch.setattr(
        "odte_scanner.signals.lottery_v2_score._uw_option_bars",
        lambda contract, day: called.__setitem__("uw", called["uw"] + 1) or [],
    )
    bars = default_fetch_bars({"contract": "SPY260805C00101000"}, session_date="2026-08-05")
    assert bars[0]["close"] == 0.02
    assert called == {"yahoo": 0, "uw": 0}

    monkeypatch.setattr("odte_scanner.signals.lottery_v2_score._polygon_option_bars", boom)
    monkeypatch.setattr("odte_scanner.signals.lottery_v2_score._yahoo_option_bars", boom)
    monkeypatch.setattr("odte_scanner.signals.lottery_v2_score._uw_option_bars", boom)
    assert default_fetch_bars({"contract": "SPY260805C00101000"}, session_date="2026-08-05") == []
    assert default_fetch_bars({"contract": ""}, session_date="2026-08-05") == []


def test_snapshot_builder_attaches_the_score():
    from odte_scanner import ui

    assert "_attach_lottery_v2_score(lottery_v2, actions_cfg)" in inspect.getsource(ui.create_app)


def test_page_shows_the_log_score_section():
    from odte_scanner.ui import PAGE

    assert 'id="lotteryV2Score"' in PAGE
    assert "function lotteryV2ScoreHtml" in PAGE
    assert "log only · not traded" in PAGE
    assert "P&amp;L at $50/$25 size" in PAGE
    assert "Time (CT)" in PAGE
    assert "Lottery v2 — LOG ONLY" in PAGE


def test_polygon_minute_aggs_become_session_bars(monkeypatch):
    from datetime import timezone

    ms = int(datetime(2026, 8, 5, 19, 50, tzinfo=timezone.utc).timestamp() * 1000)

    def fake_get(path, params=None, timeout=8.0):
        assert "/v2/aggs/ticker/O:SPY260805C00101000/range/1/minute/2026-08-05/2026-08-05" in path
        return {"ok": True, "payload": {"results": [{"t": ms, "o": 0.02, "h": 0.03, "l": 0.01, "c": 0.02, "v": 4}]}}

    monkeypatch.setattr("odte_scanner.data.polygon._get", fake_get)
    from odte_scanner.signals.lottery_v2_score import _polygon_option_bars

    bars = _polygon_option_bars("SPY260805C00101000", "2026-08-05")
    assert bars[0]["close"] == 0.02
    assert bars[0]["ts"].startswith("2026-08-05T15:50:00")
