"""BUY/SELL NOW board must surface asked/entry time (AMD RIP regression)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = (ROOT / "odte_scanner" / "ui.py").read_text()


def test_now_board_uses_row_asked_at_helper():
    assert "function rowAskedAt(r)" in UI_SRC
    assert "function settledBuyContractSet()" in UI_SRC
    assert "function openPositionTimeIndex()" in UI_SRC
    assert "withOpenEntryTime(row, openIdx)" in UI_SRC
    # Card + table should prefer the shared helper (not signaled_at-only).
    assert "rowAskedAt(r)" in UI_SRC
    assert "entered ${entryWhen}" in UI_SRC


def test_ui_page_embeds_entered_fallback_keys():
    # Defensive: helper falls back through entered_at when signaled_at missing.
    assert "entered_at_cst" in UI_SRC
    assert "r.signaled_at || r.entered_at || r.recommended_at" in UI_SRC


def test_rip_amd_buy_time_matches_live_shape(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from odte_scanner.signals.rip_radar import build_rip_board

    morning = datetime(2026, 9, 18, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    board = build_rip_board(
        candidates=[
            {
                "symbol": "AMD",
                "ask": 2.19,
                "bid": 2.10,
                "strike": 630,
                "expiry": "2026-10-02",
                "contract": "AMD261002C00630000",
                "dte": 1,
                "dte_bucket": "0dte",
                "moneyness_pct": 0.4,
                "volume": 1500,
                "open_interest": 5000,
                "score": 72,
                "right": "C",
            }
        ],
        scores=[{"symbol": "AMD", "ensemble_score": 74}],
        quotes={
            "AMD": {
                "last": 632.0,
                "session_change_pct": 2.1,
                "mom_5m_pct": 0.18,
                "mom_15m_pct": 0.35,
            }
        },
        now=morning,
        signal_times_path=str(tmp_path / "rip.json"),
    )
    buys = board.get("buy_rip") or board.get("buy_now") or []
    assert buys and buys[0]["symbol"] == "AMD"
    assert buys[0]["signaled_at_cst"]
    # Simulate what nowBoardCard reads
    when = buys[0].get("signaled_at_cst") or buys[0].get("signaled_at")
    assert when and when != "—"
