"""Kotegawa and Million tiles on the main desk page."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import odte_scanner.desks.service as service
from odte_scanner.desks import kotegawa, million
from odte_scanner.desks.service import portal_view
from odte_scanner.ui import PAGE

KOT_BOOK = {
    "desk": "kotegawa",
    "paper_only": True,
    "starting_equity": 100_000,
    "cash": 79_000,
    "trades": [
        {
            "id": "kot-INTC-2026-11-13-a",
            "symbol": "INTC",
            "shares": 400,
            "entry": 50.0,
            "cost": 20_000.0,
            "status": "open",
            "entry_session": "2026-11-13",  # Friday: day 3 is Tuesday
            "entry_at": "2026-11-13T14:51:00-06:00",
            "last_mark": 51.0,
        },
        {
            "id": "kot-DELL-2026-11-10-b",
            "symbol": "DELL",
            "shares": 100,
            "entry": 100.0,
            "cost": 10_000.0,
            "status": "closed",
            "entry_session": "2026-11-10",
            "entry_at": "2026-11-10T14:52:00-06:00",
            "exit": 105.0,
            "exit_at": "2026-11-11T10:05:00-06:00",
            "exit_reason": "take_profit",
            "pnl": 500.0,
        },
    ],
    "recent_alerts": [],
}

MIL_BOOK = {
    "desk": "million_challenge",
    "paper_only": True,
    "starting_equity": 1000,
    "cash": 880,
    "day_start_equity": 1000,
    "ledger_epoch": 1,
    "trades": [
        {
            "id": "mil-1",
            "symbol": "QQQ",
            "contract": "QQQ261117C00600000",
            "contracts": 1,
            "right": "C",
            "strike": 600,
            "expiry": "2026-11-17",
            "entry": 1.20,
            "cost": 120.0,
            "status": "open",
            "source": "vwap_reclaim",
            "entry_session": "2026-11-17",
            "entry_at": "2026-11-17T09:40:00-06:00",
            "target_pct": 50,
            "stop_pct": 40,
            "time_stop_minutes": 60,
            "flatten_ct": "14:55",
            "last_mark": 1.5,
        },
        {
            "id": "mil-0",
            "symbol": "SPY",
            "contract": "SPY261116C00590000",
            "contracts": 2,
            "right": "C",
            "strike": 590,
            "expiry": "2026-11-16",
            "entry": 1.0,
            "cost": 200.0,
            "status": "closed",
            "source": "vwap_reclaim",
            "entry_at": "2026-11-16T09:35:00-06:00",
            "exit": 0.6,
            "exit_at": "2026-11-16T10:01:00-06:00",
            "exit_reason": "stop_loss",
            "pnl": -80.0,
        },
    ],
    "log_only": [],
    "recent_alerts": [],
}


def test_kotegawa_view_carries_entry_time_exit_plan_and_closed_trades():
    view = kotegawa.public_view(KOT_BOOK)
    row = view["open"][0]
    assert row["entry_at"] == "2026-11-13T14:51:00-06:00"
    assert row["target"] == 52.5 and row["stop"] == 42.5
    assert row["time_exit"] == "2026-11-17"  # Fri, Mon, Tue
    closed = view["closed"][0]
    assert closed["exit"] == 105.0 and closed["pnl"] == 500.0 and closed["pnl_pct"] == 5.0
    assert closed["shares"] == 100


def test_million_view_prices_the_target_and_stop_on_the_option():
    view = million.public_view(MIL_BOOK)
    row = view["open"][0]
    assert row["target"] == 1.8 and row["stop"] == 0.72
    assert row["time_stop_at"].startswith("2026-11-17T10:40")
    assert row["flatten_ct"] == "14:55"
    assert row["unrealized_pnl"] == 30.0
    closed = view["closed"][0]
    assert closed["pnl_pct"] == -40.0 and closed["contracts"] == 2


def _cfg(tmp_path: Path) -> dict:
    kot = tmp_path / "kotegawa_state.json"
    mil = tmp_path / "million_challenge_state.json"
    kot.write_text(json.dumps(KOT_BOOK), encoding="utf-8")
    mil.write_text(json.dumps(MIL_BOOK), encoding="utf-8")
    return {
        "kotegawa": {"enabled": True, "paper_only": True, "starting_equity": 100_000, "state_path": str(kot)},
        "million_challenge": {
            "enabled": True,
            "paper_only": True,
            "starting_equity": 1000,
            "state_path": str(mil),
            "ledger_epoch": 1,
            "vwap_reclaim": {"target_pct": 50, "stop_pct": 40, "flatten_ct": "14:55", "entry_cutoff_ct": "14:30"},
        },
    }


def test_cards_carry_strategy_cash_running_pnl_and_closed(tmp_path: Path):
    view = portal_view(_cfg(tmp_path), cycle=False)
    kot, mil = view["portal"]["cards"]
    assert kot["cash"] == 79_000 and kot["equity"] == 79_000 + 400 * 51.0
    assert kot["unrealized_pnl"] == 400.0
    assert kot["total_pnl"] == pytest.approx(kot["equity"] - 100_000)
    assert kot["closed"][0]["symbol"] == "DELL"
    assert "2:50–3:00 PM CT" in kot["strategy"]["window"]
    assert "+5%" in kot["strategy"]["exit"] and "−15%" in kot["strategy"]["exit"]
    assert "S&P 100" in kot["strategy"]["universe"]
    assert "2:55 PM CT" in mil["strategy"]["window"] and "2:30 PM CT" in mil["strategy"]["window"]
    assert "SPY and QQQ" in mil["strategy"]["universe"]
    assert mil["open"][0]["target"] == 1.8
    assert mil["closed"][0]["pnl"] == -80.0
    text = json.dumps([kot["strategy"], mil["strategy"]]).lower()
    assert "sure" not in text


def test_tiles_are_on_the_main_tabs_and_never_buttons():
    assert 'id="paperDeskCards"' in PAGE and 'id="paperDeskCardsOverview"' in PAGE
    start = PAGE.index("function renderPaperDeskCards()")
    body = PAGE[start : PAGE.index("function renderPaperLogOnly()")]
    assert "paper ledger · internal, not Webull" in body
    assert "Filled @ $" in body
    for banned in ("BUY NOW", "ENTER NOW", "SELL NOW", "<button", "Sure"):
        assert banned not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_tiles_render_positions_and_closed_rows(tmp_path: Path):
    view = portal_view(_cfg(tmp_path), cycle=False)
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"paper_portal": view["portal"]}, default=str), encoding="utf-8")
    script = re.findall(r"<script>(.*?)</script>", PAGE, re.S)[0]
    fn = script[script.index("    function renderPaperDeskCards() {") : script.index("    function renderPaperLogOnly() {")]
    cst = script[script.index("    function fmtCST(") : script.index("    /** Asked / entry / exit time")]
    js = tmp_path / "render.js"
    js.write_text(
        "const hosts={paperDeskCards:{innerHTML:''},paperDeskCardsOverview:{innerHTML:''}};\n"
        "const document={getElementById:(id)=>hosts[id]||null};\n"
        f"const DATA=JSON.parse(require('fs').readFileSync({json.dumps(str(data))},'utf8'));\n"
        "const fmt=(n,d=2)=>(n==null||Number.isNaN(Number(n)))?'—':Number(n).toFixed(d);\n"
        f"{cst}\n{fn}\nrenderPaperDeskCards();\n"
        "process.stdout.write(JSON.stringify([hosts.paperDeskCards.innerHTML, hosts.paperDeskCardsOverview.innerHTML]));\n",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=30, check=True)
    main, overview = json.loads(out.stdout)
    assert main == overview
    assert "INTC · 400 sh" in main and "Filled @ $50.00" in main and "$51.00" in main
    assert "Nov 13, 2026, 2:51 PM CST" in main
    assert "target $52.50" in main and "stop $42.50" in main
    assert "QQQ 11/17 600C ×1" in main and "target $1.80" in main and "flat by 14:55 CT" in main
    assert "DELL · 100 sh" in main and "Filled @ $105.00" in main and "take profit" in main
    assert "SPY 11/16 590C ×2" in main and "stop loss" in main
    assert "BUY NOW" not in main


def test_github_actions_shows_fly_books_and_never_cycles(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def no_cycle(*args, **kwargs):
        raise AssertionError("a CI export must not advance the paper books")

    monkeypatch.setattr(service, "run_kotegawa", no_cycle)
    monkeypatch.setattr(service, "run_million", no_cycle)
    live = {
        "kotegawa": {**kotegawa.public_view(KOT_BOOK), "alerts": [{"key": "k", "message": "[KOTEGAWA] BUY"}]},
        "million_challenge": million.public_view(MIL_BOOK),
    }
    monkeypatch.setattr(service, "_fetch_remote_books", lambda url: live)
    view = portal_view(cfg)
    assert view["kotegawa"]["alerts"] == []  # Fly already sent these
    assert view["portal"]["cards"][0]["source"] == "live desk (Fly)"
    assert view["portal"]["cards"][0]["open"][0]["symbol"] == "INTC"

    def down(url):
        raise OSError("offline")

    monkeypatch.setattr(service, "_fetch_remote_books", down)
    fallback = portal_view(cfg)
    assert fallback["kotegawa"]["open_positions"] == 1  # read-only local books


def test_paper_desks_endpoint_hides_state_paths(monkeypatch):
    from odte_scanner.ui import create_app

    app = create_app()
    res = app.test_client().get("/api/paper_desks")
    assert res.status_code == 200
    body = res.get_json()
    assert set(body) >= {"kotegawa", "million_challenge", "generated_at"}
    assert "state_path" not in body["kotegawa"] and "state_path" not in body["million_challenge"]
    assert body["kotegawa"]["paper_only"] is True
