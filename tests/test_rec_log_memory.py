"""Recommendation log stays bounded: no journal re-mirroring, retention, cheap saves."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from odte_scanner import json_util
from odte_scanner.trading.rec_log import Recommendation, RecommendationLog

NOW = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)


def _closed_trade(**over):
    row = {
        "id": "jt-1",
        "symbol": "AAPL",
        "right": "C",
        "status": "closed",
        "dte_bucket": "0dte",
        "entry_ask": 1.21,
        "exit_bid": 0.80,
        "contract": "AAPL261109C00270000",
        "entered_at": "2026-11-09T14:31:00+00:00",
        "exited_at": "2026-11-09T14:44:55+00:00",
        "exit_reason": "session +1.24% against long · asked to sell Nov 9, 2026, 8:42:19 AM CST",
    }
    row.update(over)
    return row


def _rows(path: Path) -> list[dict]:
    return json.loads(path.read_text())["recommendations"]


def test_closed_journal_trade_is_mirrored_once_across_rebuilds(tmp_path):
    path = tmp_path / "rl.json"
    journal = {"trades": [_closed_trade()]}
    for _ in range(5):  # each snapshot rebuild loads, syncs, saves
        log = RecommendationLog(path)
        log.sync_from_journal(journal)
        log.save()
    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["contract"] == "AAPL261109C00270000"  # the stub keeps its contract
    assert rows[0]["status"] == "closed"


def test_clock_exit_scrubbed_to_lapsed_is_not_mirrored_again(tmp_path):
    """exit == entry on a clock flatten: the load-time scrub lapses it; the next sync must not re-add it."""
    path = tmp_path / "rl.json"
    trade = _closed_trade(exit_bid=1.21, exit_reason="0DTE time-stop — flatten by 15:45 ET · asked to sell 2:45 PM")
    for _ in range(4):
        log = RecommendationLog(path)
        log.sync_from_journal({"trades": [trade]})
        log.save()
    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["status"] == "lapsed"


def test_without_exit_time_the_reason_stamp_dedupes(tmp_path):
    path = tmp_path / "rl.json"
    trade = _closed_trade(exited_at=None)
    for _ in range(3):
        log = RecommendationLog(path)
        log.sync_from_journal({"trades": [trade]})
        log.save()
    assert len(_rows(path)) == 1


def test_two_real_trades_on_the_same_ticker_both_kept(tmp_path):
    path = tmp_path / "rl.json"
    journal = {
        "trades": [
            _closed_trade(),
            _closed_trade(id="jt-2", exited_at="2026-11-09T15:10:00+00:00", exit_reason="target hit · asked to sell 9:10"),
            _closed_trade(id="jt-3", entry_ask=1.50, exit_bid=2.0, exited_at="2026-11-09T16:00:00+00:00"),
        ]
    }
    for _ in range(3):
        log = RecommendationLog(path)
        log.sync_from_journal(journal)
        log.save()
    assert len(_rows(path)) == 3


def _rec(i: int, *, status: str, source: str = "board", age_days: float = 0.0, **over) -> Recommendation:
    at = (NOW - timedelta(days=age_days)).isoformat()
    data = dict(
        id=f"rec-{i}",
        section="odte",
        symbol=f"S{i}",
        status=status,
        source=source,
        recommended_at=at,
        last_recommended_at=at,
        closed_at=None if status == "open" else at,
        entry_price=1.0,
    )
    data.update(over)
    return Recommendation(**data)


def _log_with(tmp_path, recs) -> RecommendationLog:
    log = RecommendationLog(tmp_path / "none.json")
    log.book.recommendations = list(recs)
    return log


def test_compact_drops_duplicate_journal_rows_keeps_the_first():
    dup = dict(source="journal", symbol="AAPL", recommended_at="2026-11-09T14:44:55+00:00",
               closed_at="2026-11-09T14:44:55+00:00", exit_price=0.8)
    recs = [_rec(i, status="closed", **dup) for i in range(50)]
    log = RecommendationLog.__new__(RecommendationLog)
    log.book = type("B", (), {})()
    log.book.recommendations = recs
    removed = RecommendationLog.compact(log, now=NOW)
    assert removed == 49
    assert [r.id for r in log.book.recommendations] == ["rec-0"]


def test_small_log_keeps_old_lapses(tmp_path, monkeypatch):
    monkeypatch.delenv("REC_LOG_LAPSED_PRUNE_ABOVE", raising=False)
    log = _log_with(tmp_path, [_rec(i, status="lapsed", age_days=60) for i in range(20)])
    assert log.compact(now=NOW) == 0


def test_compact_retention_keeps_open_and_recent_lapsed(tmp_path, monkeypatch):
    monkeypatch.delenv("REC_LOG_LAPSED_KEEP_DAYS", raising=False)
    monkeypatch.delenv("REC_LOG_MAX_ROWS", raising=False)
    monkeypatch.setenv("REC_LOG_LAPSED_PRUNE_ABOVE", "2")
    recs = [
        _rec(1, status="open", age_days=60),  # open: always kept
        _rec(2, status="lapsed", age_days=30),  # old lapse: dropped
        _rec(3, status="lapsed", age_days=3),  # recent lapse: kept
        _rec(4, status="closed", age_days=40, exit_price=1.5),  # priced history: kept
        _rec(5, status="lapsed", recommended_at="", last_recommended_at="", closed_at=None),  # no time: kept
    ]
    log = _log_with(tmp_path, recs)
    assert log.compact(now=NOW) == 1
    assert {r.id for r in log.book.recommendations} == {"rec-1", "rec-3", "rec-4", "rec-5"}


def test_compact_cap_keeps_newest_done_rows_and_every_open(tmp_path, monkeypatch):
    monkeypatch.setenv("REC_LOG_MAX_ROWS", "3")
    recs = [_rec(i, status="closed", age_days=i, exit_price=1.2) for i in range(6)]
    recs.append(_rec(99, status="open", age_days=100))
    log = _log_with(tmp_path, recs)
    log.compact(now=NOW)
    assert [r.id for r in log.book.recommendations] == ["rec-0", "rec-1", "rec-2", "rec-99"]


def test_compact_env_zero_disables_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("REC_LOG_MAX_ROWS", "0")
    monkeypatch.setenv("REC_LOG_LAPSED_KEEP_DAYS", "0")
    recs = [_rec(i, status="lapsed", age_days=100 + i) for i in range(5)]
    log = _log_with(tmp_path, recs)
    assert log.compact(now=NOW) == 0


def test_load_compacts_a_bloated_file_and_saves_compact_json(tmp_path):
    path = tmp_path / "rl.json"
    dup = _rec(0, status="closed", source="journal", exit_price=0.8).to_dict()
    rows = []
    for i in range(200):
        row = dict(dup)
        row["id"] = f"rec-{i}"
        rows.append(row)
    rows.append(_rec(500, status="open").to_dict())
    path.write_text(json.dumps({"updated_at": "", "recommendations": rows}, indent=2))
    before = path.stat().st_size
    log = RecommendationLog(path)
    assert len(log.book.recommendations) == 2
    text = path.read_text()
    assert path.stat().st_size < before / 20
    assert "\n  " not in text  # compact, not indent=2
    assert len(json.loads(text)["recommendations"]) == 2


def test_atomic_write_does_not_read_a_large_previous_file(tmp_path, monkeypatch):
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"x": "a" * (json_util._BAK_MAX_BYTES + 10)}))
    real_read = Path.read_text

    def guarded(self, *a, **k):
        if self == big:
            raise AssertionError("large previous file was read")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    json_util.atomic_write_text(big, '{"x": 1}')
    monkeypatch.setattr(Path, "read_text", real_read)
    assert json.loads(big.read_text()) == {"x": 1}
    assert not (tmp_path / "big.json.bak").exists()


def test_atomic_write_still_keeps_a_bak_for_small_files(tmp_path):
    small = tmp_path / "s.json"
    small.write_text('{"v": 1}')
    json_util.atomic_write_text(small, '{"v": 2}')
    assert json.loads((tmp_path / "s.json.bak").read_text()) == {"v": 1}
    assert json.loads(small.read_text()) == {"v": 2}
