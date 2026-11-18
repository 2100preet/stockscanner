"""Paper-ledger desk alerts are never pushed to a later pulse by the board cap."""

from __future__ import annotations

from odte_scanner.alerts.dispatcher import _send_order


def _board(n: int) -> list[dict]:
    return [{"key": f"B{i}", "message": f"BUY X{i}"} for i in range(n)]


def test_paper_desk_alerts_ride_outside_the_cap():
    paper = [
        {"key": "M1", "message": "[MILLION][VWAP] BUY QQQ 11/17 657C ×1 · Filled @ $1.99 (paper ledger)"},
        {"key": "K1", "message": "[KOTEGAWA] BUY NVDA ×10 · Filled @ $155.53 (paper ledger)"},
    ]
    out = _send_order(_board(12) + paper, max_send=8)
    keys = [a["key"] for a in out]
    assert keys[:2] == ["M1", "K1"]
    assert len(out) == 10
    assert keys[2:] == [f"B{i}" for i in range(8)]


def test_board_only_keeps_the_old_cap():
    out = _send_order(_board(12), max_send=8)
    assert [a["key"] for a in out] == [f"B{i}" for i in range(8)]
