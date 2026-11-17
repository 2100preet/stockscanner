"""Kotegawa open stock positions move with the market."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.desks.kotegawa import public_view, run_kotegawa
from odte_scanner.desks.service import _quotes_for_open_positions

CT = ZoneInfo("America/Chicago")
MONDAY = datetime(2026, 11, 16, 10, 0, tzinfo=CT)  # mid-session, no exits due


def _seed(path: Path, *, last_mark: float | None = None) -> None:
    trade = {
        "id": "kot-INTC-2026-11-16-aaaa",
        "symbol": "INTC",
        "shares": 100,
        "entry": 50.0,
        "cost": 5000.0,
        "status": "open",
        "entry_session": "2026-11-16",
        "entry_at": "2026-11-16T09:00:00-06:00",
    }
    if last_mark is not None:
        trade["last_mark"] = last_mark
    path.write_text(
        json.dumps(
            {
                "desk": "kotegawa",
                "paper_only": True,
                "starting_equity": 100_000,
                "cash": 95_000,
                "trades": [trade],
                "recent_alerts": [],
                "sandbox_orders": [],
            }
        ),
        encoding="utf-8",
    )


def _section(path: Path) -> dict:
    return {"enabled": True, "paper_only": True, "starting_equity": 100_000, "state_path": str(path)}


def test_equity_and_unrealized_follow_the_live_quote(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    _seed(path)
    view = run_kotegawa(_section(path), quotes={"INTC": {"last": 51.0, "bid": 50.98}}, now=MONDAY, path=path)
    assert view["equity"] == 95_000 + 100 * 51.0
    assert view["unrealized_pnl"] == 100.0
    assert view["open"][0]["mark"] == 51.0
    assert view["open"][0]["unrealized_pnl"] == 100.0
    assert view["marked_at"].startswith("2026-11-16T10:00")

    lower = run_kotegawa(_section(path), quotes={"INTC": {"last": 48.0, "bid": 47.99}}, now=MONDAY, path=path)
    assert lower["equity"] == 95_000 + 100 * 48.0
    assert lower["unrealized_pnl"] == -200.0


def test_a_missing_quote_keeps_the_last_mark_not_the_entry(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    _seed(path, last_mark=51.5)
    view = run_kotegawa(_section(path), quotes={}, now=MONDAY, path=path)
    assert view["open"][0]["mark"] == 51.5
    assert view["equity"] == 95_000 + 100 * 51.5


def test_read_only_view_uses_the_saved_mark(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    _seed(path, last_mark=49.0)
    book = json.loads(path.read_text())
    view = public_view(book)
    assert view["equity"] == 95_000 + 100 * 49.0
    assert view["unrealized_pnl"] == -100.0


def test_never_marked_position_is_valued_at_entry(tmp_path: Path):
    path = tmp_path / "kotegawa_state.json"
    _seed(path)
    view = public_view(json.loads(path.read_text()))
    assert view["equity"] == 100_000
    assert view["unrealized_pnl"] == 0


def test_open_names_missing_from_the_scan_quotes_are_fetched_once():
    book = {
        "trades": [
            {"symbol": "INTC", "status": "open"},
            {"symbol": "DELL", "status": "open"},
            {"symbol": "OLD", "status": "closed"},
        ]
    }
    asked: list[list[str]] = []

    def fetch(symbols):
        asked.append(list(symbols))
        return {"DELL": {"last": 120.5, "bid": 120.4}}

    merged = _quotes_for_open_positions(book, {"INTC": {"last": 51.0}}, fetch)
    assert asked == [["DELL"]]
    assert merged["DELL"]["last"] == 120.5
    assert merged["INTC"]["last"] == 51.0


def test_quote_fetch_failure_leaves_the_scan_quotes():
    book = {"trades": [{"symbol": "DELL", "status": "open"}]}

    def boom(symbols):
        raise RuntimeError("network down")

    assert _quotes_for_open_positions(book, {"SPY": {"last": 1}}, boom) == {"SPY": {"last": 1}}
    assert _quotes_for_open_positions({"trades": []}, None, boom) is None
