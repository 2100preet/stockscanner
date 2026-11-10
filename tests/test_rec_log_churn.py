"""A board card for a contract already closed or lapsed today must not reopen.

Before this, a stale SELL NOW (or an expired contract still on the board) made a
new open+close pair on every snapshot rebuild: ~50 rows per rebuild, which pushed
real history out past the row cap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from odte_scanner.trading.rec_log import RecommendationLog

CT = timezone(timedelta(hours=-6))  # America/Chicago in November (CST)
C1 = "NVDA261109C00200000"
C2 = "NVDA261109C00205000"


def _at(hour, minute=0, day=9):
    return datetime(2026, 11, day, hour, minute, tzinfo=CT).isoformat()


def _open(log, at, contract=C1, source="board", section="odte"):
    return log.note_entry(
        section=section, symbol="NVDA", action="BUY_NOW", right="C", price=1.10,
        contract=contract, at=at, source=source,
    )


def _close(log, at, section="odte"):
    return log.note_exit(section=section, symbol="NVDA", action="SELL_NOW", right="C", price=1.50, at=at)


def _count(log, contract=C1):
    return sum(1 for r in log.book.recommendations if r.contract == contract)


def test_closed_card_does_not_reopen_same_ct_day(tmp_path):
    log = RecommendationLog(tmp_path / "rl.json")
    assert _open(log, _at(9, 0)) is not None
    rec = _close(log, _at(9, 30))
    assert rec is not None and rec.status == "closed"
    for minute in range(0, 50, 10):  # several rebuilds still showing the BUY card
        assert _open(log, _at(10, minute)) is None
    assert _count(log) == 1


def test_lapsed_card_does_not_reopen_same_ct_day(tmp_path):
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(9, 0))
    lapsed = log.note_lapse(section="odte", symbol="NVDA", right="C", reason="test lapse")
    assert lapsed is not None and lapsed.status == "lapsed"
    assert _open(log, _at(13, 0)) is None
    assert _count(log) == 1


def test_stale_sell_with_old_closed_at_still_blocks_today(tmp_path):
    """A stale SELL NOW can stamp yesterday's board time as closed_at."""
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(9, 0))
    rec = _close(log, _at(9, 30))
    rec.closed_at = _at(15, 0, day=8)  # stale board time from the day before
    assert _open(log, _at(11, 0)) is None
    assert _count(log) == 1


def test_next_ct_day_reopens(tmp_path):
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(9, 0))
    _close(log, _at(9, 30))
    assert _open(log, _at(8, 45, day=10)) is not None
    assert _count(log) == 2


def test_ct_day_boundary_uses_chicago_not_utc(tmp_path):
    """Closed 11:30 PM CT Nov 9 is 05:30 UTC Nov 10; a 12:10 AM CT Nov 10 card is a new day."""
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(23, 0))
    _close(log, _at(23, 30))
    assert _open(log, _at(23, 50)) is None  # still Nov 9 in CT (Nov 10 in UTC)
    assert _open(log, _at(0, 10, day=10)) is not None


def test_other_contract_or_section_still_opens(tmp_path):
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(9, 0))
    _close(log, _at(9, 30))
    assert _open(log, _at(10, 0), contract=C2) is not None
    assert _open(log, _at(10, 0), section="weekly") is not None


def test_journal_source_is_never_blocked(tmp_path):
    """Real paper trades come from the journal; the board rule must not drop them."""
    log = RecommendationLog(tmp_path / "rl.json")
    _open(log, _at(9, 0))
    _close(log, _at(9, 30))
    assert _open(log, _at(10, 0), source="journal") is not None


def _trade(i):
    return {
        "id": f"jt-{i}", "symbol": "AAPL", "right": "C", "status": "closed", "dte_bucket": "0dte",
        "entry_ask": 1.00 + i / 100, "exit_bid": 1.50 + i / 100,
        "contract": f"AAPL261109C00{270 + i:03d}000",
        "entered_at": f"2026-10-{1 + i % 20:02d}T14:31:00+00:00",
        "exited_at": f"2026-10-{1 + i % 20:02d}T15:00:00+00:00",
        "exit_reason": "target hit",
    }


def test_journal_rows_survive_cap_and_row_count_is_stable(tmp_path, monkeypatch):
    """Capped or pruned journal rows were mirrored back every sync (a loop)."""
    monkeypatch.setenv("REC_LOG_MAX_ROWS", "5")
    monkeypatch.setenv("REC_LOG_LAPSED_PRUNE_ABOVE", "1")
    path = tmp_path / "rl.json"
    journal = {"trades": [_trade(i) for i in range(12)]}
    counts = []
    for _ in range(4):  # each rebuild loads, syncs, compacts on save
        log = RecommendationLog(path)
        log.sync_from_journal(journal)
        # board churn on top: closed board rows past the cap get evicted
        for k in range(8):
            log.note_entry(section="odte", symbol=f"S{k}", action="BUY_NOW", price=1.0,
                           contract=f"S{k}261109C00010000", at=_at(9, k))
            log.note_exit(section="odte", symbol=f"S{k}", action="SELL_NOW", price=1.2, at=_at(10, k))
        log.compact(now=datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc))
        log.save()
        counts.append(len(json.loads(path.read_text())["recommendations"]))
    rows = json.loads(path.read_text())["recommendations"]
    journal_rows = [r for r in rows if r.get("source") == "journal"]
    assert len(journal_rows) == 12
    assert counts[1:] == [counts[1]] * (len(counts) - 1), counts  # no growth after the first rebuild
