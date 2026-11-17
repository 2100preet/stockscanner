"""Portal feed for the Kotegawa and Million paper desks.

The shaper only reads books the desks already wrote. It does not place orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.desks.portal_feed import shape_portal_feed, snapshot_fields
from odte_scanner.desks.service import portal_view
from odte_scanner.desks.sp100 import sp100_universe
from odte_scanner.scorecard.page import render_scorecard_page
from odte_scanner.scorecard.service import DISCLAIMER
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")
ENTRY = datetime(2026, 11, 16, 14, 50, tzinfo=CT)
QUIET = datetime(2026, 11, 16, 14, 58, tzinfo=CT)
NEXT_DAY = datetime(2026, 11, 17, 10, 0, tzinfo=CT)
VWAP_AT = datetime(2026, 11, 16, 9, 40, tzinfo=CT)
MARKED = datetime(2026, 11, 16, 9, 50, tzinfo=CT)


def _names(n: int = 1) -> list[str]:
    return list(sp100_universe())[:n]


def _dropped(close: float, *, day: str = "2026-11-16", n: int = 30) -> list[dict]:
    bars = []
    for i in range(n - 1):
        bars.append({"date": f"2026-10-{i + 1:02d}" if i < 28 else day, "open": 100, "high": 100, "low": 100, "close": 100})
    bars.append({"date": day, "open": close, "high": close, "low": close, "close": close})
    return bars


def _kot_section(path: Path) -> dict:
    return {"enabled": True, "paper_only": True, "starting_equity": 100_000, "state_path": str(path)}


def _mil_section(path: Path) -> dict:
    return {
        "enabled": True,
        "paper_only": True,
        "starting_equity": 1_000,
        "state_path": str(path),
        "signals": ["vwap_reclaim", "challenge_quality"],
    }


def _quality(symbol: str = "AMD") -> dict:
    return {
        "symbol": symbol,
        "action": "ENTRY",
        "certainty_tier": "perfect",
        "hist_win_pct": 100,
        "hist_samples": 5,
        "right": "C",
        "contract": f"{symbol}261120C00100000",
        "expiry": "2026-11-20",
        "dte": 4,
        "strike": 100,
        "bid": 1.9,
        "ask": 2.0,
        "spot": 100,
    }


def _vwap_tape() -> list[dict]:
    start = datetime(2026, 11, 16, 9, 35, tzinfo=CT)
    nxt = datetime(2026, 11, 16, 9, 36, tzinfo=CT)
    return [
        {"ts": start, "high": 100.0, "low": 99.8, "close": 99.9, "volume": 100, "interval": "1m", "closed": True},
        {"ts": nxt, "high": 101.0, "low": 100.2, "close": 100.6, "volume": 100, "interval": "1m", "closed": True},
    ]


def _vwap_chain() -> list[dict]:
    spot = 100.0
    return [
        {"right": "P", "strike": spot, "dte": 0, "expiry": "2026-11-16", "bid": 1.0, "ask": 1.1, "contract": "PUT"},
        {"right": "C", "strike": spot + 5, "dte": 0, "expiry": "2026-11-16", "bid": 0.40, "ask": 0.50, "contract": "FAR0"},
        {"right": "C", "strike": spot, "dte": 0, "expiry": "2026-11-16", "bid": 1.20, "ask": 1.30, "contract": "DTE0"},
        {"right": "C", "strike": spot, "dte": 1, "expiry": "2026-11-17", "bid": 1.80, "ask": 2.00, "contract": "DTE1"},
    ]


def _open_books(tmp_path: Path):
    from odte_scanner.desks.kotegawa import run_kotegawa
    from odte_scanner.desks.million import run_million

    kot = tmp_path / "kotegawa_state.json"
    mil = tmp_path / "million_challenge_state.json"
    sym = _names(1)[0]
    kot_view = run_kotegawa(_kot_section(kot), bars={sym: _dropped(80)}, now=ENTRY, path=kot)
    mil_view = run_million(
        _mil_section(mil),
        challenge={"entry": [_quality()]},
        chains={"SPY": _vwap_chain()},
        intraday={"SPY": _vwap_tape()},
        now=VWAP_AT,
        path=mil,
        vix={"level": 18.4, "prior_close": 17.1},
    )
    return sym, kot, mil, kot_view, mil_view


def test_fill_alerts_are_included_and_log_only_is_excluded(tmp_path: Path):
    sym, _kot, _mil, kot_view, mil_view = _open_books(tmp_path)
    poisoned = dict(mil_view)
    poisoned["alerts"] = list(mil_view["alerts"]) + [
        {
            "key": "MILLION|BUY|oversold",
            "side": "BUY",
            "symbol": "QQQ",
            "source": "oversold_bounce",
            "message": "[MILLION] BUY QQQ oversold",
            "session": "2026-11-16",
            "log_only": True,
        }
    ]
    poisoned["log_only"] = list(mil_view["log_only"]) + [
        {
            "symbol": "QQQ",
            "source": "oversold_bounce",
            "contract": "BOUNCE",
            "status": "open",
            "entry": 1.2,
            "entry_session": "2026-11-16",
            "log_only": True,
        }
    ]
    feed = shape_portal_feed(kot_view, poisoned, now=QUIET)
    labels = {row["label"] for row in feed["alerts"]}
    assert labels == {"[KOTEGAWA]", "[MILLION]"}
    assert all(row["paper"] is True and row["log_only"] is False for row in feed["alerts"])
    assert all(row["side"] in {"BUY", "SELL"} for row in feed["alerts"])
    messages = [row["message"] for row in feed["alerts"]]
    assert any(text.startswith("[KOTEGAWA] BUY") and sym in text for text in messages)
    assert any(text.startswith("[MILLION]") and "BUY SPY" in text for text in messages)
    sources = {row["source"] for row in feed["log_only"]}
    assert {"challenge_quality", "vwap_reclaim_1dte", "oversold_bounce"} <= sources
    assert all(row["log_only"] is True for row in feed["log_only"])
    blob = " ".join(messages)
    assert "oversold" not in blob
    assert "AMD" not in blob
    assert "DTE1" not in blob
    assert "QQQ" not in blob


def test_fill_alerts_persist_for_the_session_and_drop_the_next_day(tmp_path: Path):
    from odte_scanner.desks.kotegawa import run_kotegawa

    sym, kot, _mil, kot_view, mil_view = _open_books(tmp_path)
    buy_key = kot_view["alerts"][0]["key"]
    assert kot_view["alerts"][0]["session"] == "2026-11-16"
    quiet = run_kotegawa(
        _kot_section(kot),
        bars={sym: _dropped(80)},
        quotes={sym: {"last": 80, "bid": 80, "ask": 80}},
        now=QUIET,
        path=kot,
    )
    assert quiet["cycle_alerts"] == []
    assert any(row["key"] == buy_key for row in quiet["alerts"])
    same_day = shape_portal_feed(quiet, mil_view, now=QUIET)
    assert any(row["key"] == buy_key and row["label"] == "[KOTEGAWA]" for row in same_day["alerts"])
    nxt = shape_portal_feed(quiet, mil_view, now=NEXT_DAY)
    assert nxt["alerts"] == []

    entry = kot_view["open"][0]["entry"]
    closed = run_kotegawa(
        _kot_section(kot),
        quotes={sym: {"last": entry * 1.05, "bid": entry * 1.05}},
        now=datetime(2026, 11, 16, 14, 59, tzinfo=CT),
        path=kot,
    )
    assert closed["cycle_alerts"][0]["side"] == "SELL"
    assert closed["realized_today"] > 0
    sold = shape_portal_feed(closed, {"alerts": [], "log_only": []}, now=QUIET)
    sides = {row["side"] for row in sold["alerts"] if row["label"] == "[KOTEGAWA]"}
    assert sides == {"BUY", "SELL"}
    card = sold["cards"][0]
    assert card["trades"] == 1
    assert card["win_rate"] == 1
    assert card["realized_today"] > 0
    assert card["open"] == []


def test_cards_populate_equity_open_mark_and_doublings(tmp_path: Path):
    from odte_scanner.desks.kotegawa import run_kotegawa
    from odte_scanner.desks.million import run_million

    sym, kot, mil, kot_view, mil_view = _open_books(tmp_path)
    entry = kot_view["open"][0]["entry"]
    marked_kot = run_kotegawa(
        _kot_section(kot),
        bars={sym: _dropped(80)},
        quotes={sym: {"last": entry * 1.02, "bid": entry * 1.02, "ask": entry * 1.02}},
        now=QUIET,
        path=kot,
    )
    mil_entry = mil_view["open"][0]["entry"]
    contract = mil_view["open"][0]["contract"]
    marked_mil = run_million(
        _mil_section(mil),
        quotes={
            contract: {"bid": mil_entry * 1.2, "ask": mil_entry * 1.25, "last": mil_entry * 1.2},
            "SPY": {"last": 100, "bid": 100, "ask": 100},
        },
        now=MARKED,
        path=mil,
    )
    feed = shape_portal_feed(marked_kot, marked_mil, now=QUIET)
    kot_card, mil_card = feed["cards"]
    assert kot_card["label"] == "[KOTEGAWA]" and kot_card["paper"] is True
    assert kot_card["equity"] == pytest.approx(100_000, abs=1)
    assert kot_card["realized_today"] == 0
    assert kot_card["trades"] == 0 and kot_card["win_rate"] is None
    opened = kot_card["open"][0]
    assert opened["contract"].endswith("sh")
    assert opened["entry"] == pytest.approx(entry)
    assert opened["mark"] == pytest.approx(entry * 1.02)
    assert opened["unrealized_pnl"] > 0
    assert "doublings" not in kot_card

    assert mil_card["label"] == "[MILLION]" and mil_card["paper"] is True
    assert mil_card["equity"] == pytest.approx(1_000, abs=1)
    assert mil_card["realized_today"] == 0
    assert mil_card["trades"] == 0
    assert mil_card["doublings"] == pytest.approx(0, abs=0.05)
    assert mil_card["target_doublings"] == pytest.approx(9.97, abs=0.05)
    position = mil_card["open"][0]
    assert position["contract"] == contract
    assert position["entry"] == pytest.approx(mil_entry)
    assert position["mark"] == pytest.approx(mil_entry * 1.2)
    assert position["unrealized_pnl"] == pytest.approx((mil_entry * 0.2) * 100, abs=0.05)

    view = portal_view(
        {"kotegawa": _kot_section(kot), "million_challenge": _mil_section(mil)},
        cycle=False,
        now=QUIET,
    )
    fields = snapshot_fields(view)
    assert fields["paper_desks"][0]["desk"] == "Kotegawa"
    assert fields["paper_desks"][1]["desk"] == "Million"
    assert fields["paper_portal"]["cards"][1]["open"][0]["contract"] == contract
    messages = [row["message"] for row in fields["paper_portal"]["alerts"]]
    assert any(text.startswith("[KOTEGAWA]") for text in messages)
    assert any(text.startswith("[MILLION]") for text in messages)
    assert all("AMD" not in text and "oversold" not in text for text in messages)
    assert any(row["source"] == "challenge_quality" for row in fields["paper_portal"]["log_only"])


def test_main_page_wires_the_feed_and_scorecard_paints_from_embedded_data():
    board = PAGE[PAGE.index("function collectNowBoard"): PAGE.index("function nowBoardCard")]
    alerts = PAGE[PAGE.index("function collectTradeAlerts"): PAGE.index("function pushToast")]
    assert "paperPortal.alerts" in board
    assert "paper.log_only" not in board
    assert "r.log_only" in board
    assert "paperPortal.alerts" in alerts
    assert 'id="paperDeskCards"' in PAGE
    assert 'id="paperLogOnly"' in PAGE
    assert "Log-only · not alerts" in PAGE
    assert "function renderPaperDeskCards" in PAGE
    assert "[KOTEGAWA]" in PAGE and "[MILLION]" in PAGE
    assert '<span class="tag">paper</span>' in PAGE
    ui_source = Path(__file__).resolve().parents[1].joinpath("odte_scanner/ui.py").read_text(encoding="utf-8")
    assert '"paper_portal": paper_fields["paper_portal"]' in ui_source

    html = render_scorecard_page(
        live=True,
        initial={
            "rows": [],
            "paper_desks": [
                {
                    "desk": "Kotegawa",
                    "trades": 2,
                    "win_rate": 0.5,
                    "realized_pnl": 12.5,
                    "open_positions": 1,
                    "equity": 100012.5,
                }
            ],
            "summary": {},
            "chain": {"ok": True, "entries": 0, "head": None},
            "disclaimer": DISCLAIMER,
        },
    )
    assert '"desk": "Kotegawa"' in html
    assert "renderScorecard();\n    loadScorecard();" in html
    assert "function renderPaperDeskTable" in html


def test_alerts_saved_before_session_field_use_the_day_in_the_trade_id():
    from odte_scanner.desks.portal_feed import shape_portal_feed

    old = {
        "key": "KOTEGAWA|BUY|kot-SNDK-2026-11-16-059f8c81",
        "side": "BUY",
        "symbol": "SNDK",
        "message": "[KOTEGAWA] BUY SNDK 24 sh @ 814.01 · paper",
        "row": {"id": "kot-SNDK-2026-11-16-059f8c81", "symbol": "SNDK"},
    }
    kot = {"alerts": [old], "open": []}
    same_day = shape_portal_feed(kot, {}, now=datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc))
    next_day = shape_portal_feed(kot, {}, now=datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))
    assert [row["symbol"] for row in same_day["alerts"]] == ["SNDK"]
    assert next_day["alerts"] == []


def test_paper_fill_rows_render_as_filled_not_buy_now():
    assert '"PAPER BUY · FILLED"' in PAGE
    assert 'display_price_label: filled == null ? "" : "Filled @"' in PAGE
