"""Public scorecard: hash chain, Gold Setup / Golden Flow capture, and outcomes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from odte_scanner.scorecard.evaluate import evaluate_outcome
from odte_scanner.scorecard.ledger import verify_chain
from odte_scanner.scorecard.page import render_scorecard_page
from odte_scanner.scorecard.service import DISCLAIMER, observe_snapshot, public_view
from odte_scanner.trading.golden import golden_signal_kind

BANNED = ("Sure Winner", "sure_winner", "sure-max", "sure-high")
NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
SIGNAL_AT = "2026-10-12T15:00:00+00:00"


def _bars() -> list[dict]:
    # Monday 2026-10-12 through the fifth later session (2026-10-19).
    rows = [
        ("2026-10-12", 101.0, 102.0, 99.0),
        ("2026-10-13", 103.0, 104.0, 100.0),
        ("2026-10-14", 100.0, 101.0, 97.0),
        ("2026-10-15", 104.0, 105.0, 100.0),
        ("2026-10-16", 98.0, 104.0, 97.0),
        ("2026-10-19", 110.0, 112.0, 99.0),
    ]
    return [
        {"date": day, "open": 100.0, "close": close, "high": high, "low": low}
        for day, close, high, low in rows
    ]


def _gold(symbol: str = "NVDA", tier: str = "MAX", right: str = "C", spot: float = 100.0) -> dict:
    return {
        "symbol": symbol,
        "right": right,
        "confidence_tier": tier,
        "sure_winner": True,
        "ask": 1.25,
        "spot": spot,
        "contract": f"{symbol}261016C00100000",
        "expiry": "2026-10-16",
        "strike": 100,
        "signaled_at": SIGNAL_AT,
        "day_cst": "2026-10-12",
        "pick_key": f"{symbol}|{right}|100",
    }


def _flow(symbol: str = "SPY", right: str = "P", tier: str = "golden") -> dict:
    return {
        "symbol": symbol,
        "right": right,
        "tier": tier,
        "flags": [tier],
        "mid": 2.0,
        "spot": 100.0,
        "sentiment": "bearish" if right == "P" else "bullish",
        "contract": f"{symbol}261016{right}00500000",
        "expiry": "2026-10-16",
        "strike": 500,
        "signaled_at": SIGNAL_AT,
        "day_cst": "2026-10-12",
    }


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("SCORECARD_LEDGER", str(path))
    monkeypatch.setenv("SCORECARD_EVAL_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setenv("SCORECARD_EVAL", "1")
    return path


def _observe(ledger: Path, **kwargs):
    del ledger
    return observe_snapshot(
        now=kwargs.pop("now", NOW),
        fetch_bars=kwargs.pop("fetch_bars", lambda _symbol: _bars()),
        **kwargs,
    )


def test_logs_gold_setup_and_golden_flow_and_skips_the_rest(ledger: Path):
    solid = _gold("AMD", tier="SOLID")
    unusual = _flow("QQQ", right="C", tier="unusual")
    triangle = {"symbol": "AAPL", "golden": True, "pattern": "ASCENDING_TRIANGLE", "spot": 100}
    view = _observe(
        ledger,
        sure_winners={"winners": [_gold(), solid], "buy_now": [_gold()]},
        option_flow={"prints": [_flow(), unusual, triangle], "golden": [_flow()]},
    )
    types = sorted(row["signal_type"] for row in view["rows"])
    assert types == ["Gold Setup", "Golden Flow"]
    tickers = {row["ticker"] for row in view["rows"]}
    assert tickers == {"NVDA", "SPY"}
    assert golden_signal_kind(_flow()) is None
    blob = ledger.read_text(encoding="utf-8") + json.dumps(view)
    for phrase in BANNED:
        assert phrase not in blob
    assert view["chain"]["ok"] is True
    assert view["disclaimer"] == DISCLAIMER


def test_chain_is_append_only_and_links(ledger: Path):
    first = _observe(ledger, sure_winners={"winners": [_gold()]})
    original = ledger.read_text(encoding="utf-8")
    first_line = original.splitlines()[0]
    again = _observe(
        ledger,
        sure_winners={"winners": [_gold(), _gold("META", tier="HIGH")]},
        option_flow={"golden": [_flow()]},
    )
    updated = ledger.read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.splitlines()[0] == first_line
    events = [json.loads(line) for line in updated.splitlines() if line.strip()]
    status = verify_chain(events)
    assert status.ok
    assert events[1]["prev_hash"] == events[0]["entry_hash"]
    assert len(again["rows"]) == len(first["rows"]) + 2
    # A second pass with the same signals does not grow the file.
    frozen = ledger.read_text(encoding="utf-8")
    _observe(
        ledger,
        sure_winners={"winners": [_gold(), _gold("META", tier="HIGH")]},
        option_flow={"golden": [_flow()]},
    )
    assert ledger.read_text(encoding="utf-8") == frozen


def test_tamper_is_visible_and_blocks_appends(ledger: Path):
    _observe(ledger, sure_winners={"winners": [_gold()]})
    lines = ledger.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["payload"]["ticker"] = "HACK"
    ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    before = ledger.read_text(encoding="utf-8")
    view = _observe(ledger, sure_winners={"winners": [_gold("META")]})
    assert view["chain"]["ok"] is False
    assert view["chain"]["broken_at"] == 1
    assert ledger.read_text(encoding="utf-8") == before
    assert "Tamper" not in before  # the file itself is the evidence; the page warns


def test_outcomes_count_wins_and_losses(ledger: Path):
    view = _observe(
        ledger,
        sure_winners={"winners": [_gold(spot=100.0)]},
        option_flow={"golden": [_flow()]},
    )
    by_type = {row["signal_type"]: row for row in view["rows"]}
    gold = by_type["Gold Setup"]
    flow = by_type["Golden Flow"]
    assert gold["direction"] == "long"
    assert gold["reference_entry"] == 100.0
    assert gold["signaled_at_ct"]
    assert "CDT" in gold["signaled_at_ct"] or "CST" in gold["signaled_at_ct"]
    assert gold["outcomes"]["eod"]["win"] is True
    assert gold["outcomes"]["eod"]["return_pct"] == 1.0
    assert gold["outcomes"]["d1"]["return_pct"] == 3.0
    assert gold["outcomes"]["d5"]["return_pct"] == 10.0
    assert gold["outcomes"]["mfe_pct"] == 12.0
    assert gold["outcomes"]["mae_pct"] == -3.0
    # Premium is recorded, but the scored mark is the underlying close.
    assert gold["premium_reference"] == 1.25
    assert gold["outcomes"]["eod"]["mark"] == 101.0
    assert flow["direction"] == "short"
    assert flow["outcomes"]["eod"]["win"] is False
    assert flow["outcomes"]["eod"]["return_pct"] == -1.0
    eod = view["summary"]["horizons"]["eod"]
    assert eod["wins"] == 1
    assert eod["losses"] == 1
    assert eod["win_rate"] == 0.5
    assert eod["avg_move_pct"] == 0.0
    assert view["summary"]["count"] == 2
    assert view["summary"]["by_type"]["Gold Setup"] == 1
    assert view["summary"]["by_type"]["Golden Flow"] == 1


def test_open_session_stays_pending(ledger: Path):
    during = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)  # 11:00 CT, before the cash close
    only_today = [_bars()[0]]
    view = _observe(
        ledger,
        now=during,
        fetch_bars=lambda _symbol: only_today,
        sure_winners={"winners": [_gold()]},
    )
    eod = view["rows"][0]["outcomes"]["eod"]
    assert eod["status"] == "pending"
    assert eod["win"] is None
    assert view["summary"]["horizons"]["eod"]["win_rate"] is None
    assert view["summary"]["horizons"]["eod"]["settled"] == 0


def test_reference_price_can_come_from_the_scan(ledger: Path):
    row = _gold()
    row.pop("spot")
    view = _observe(
        ledger,
        sure_winners={"winners": [row]},
        scores=[{"symbol": "NVDA", "last_price": 100.0}],
    )
    assert view["rows"][0]["reference_entry"] == 100.0


def test_evaluate_outcome_direct_short_favorable():
    signal = {
        "reference_entry": 100.0,
        "direction": "short",
        "signaled_at": SIGNAL_AT,
    }
    bars = [
        {"date": "2026-10-12", "close": 90.0, "high": 101.0, "low": 89.0},
        {"date": "2026-10-13", "close": 95.0, "high": 96.0, "low": 88.0},
    ]
    outcome = evaluate_outcome(signal, bars, now=NOW)
    assert outcome["eod"]["win"] is True
    assert outcome["eod"]["return_pct"] == 10.0
    assert outcome["d1"]["return_pct"] == 5.0
    assert outcome["d5"]["status"] == "pending"
    assert outcome["mfe_status"] == "partial"
    assert outcome["mfe_pct"] == 12.0  # low 88
    assert outcome["mae_pct"] == -1.0  # high 101


def test_public_page_and_nav(ledger: Path):
    view = _observe(ledger, sure_winners={"winners": [_gold()]}, option_flow={"golden": [_flow()]})
    page = render_scorecard_page(live=False, initial=view)
    for phrase in BANNED:
        assert phrase not in page
    assert DISCLAIMER in page
    assert "Gold Setup" in page
    assert "Golden Flow" in page
    assert "not investment advice" in page
    assert "not real-money returns" in page
    assert 'id="scType"' in page
    assert "Max favorable" in page
    from odte_scanner.ui import PAGE

    assert 'data-tab="scorecard"' in PAGE
    assert "Scorecard" in PAGE
    start = PAGE.index('id="tab-scorecard"')
    section = PAGE[start : PAGE.index("<footer>", start)]
    assert DISCLAIMER in section
    for phrase in BANNED:
        assert phrase not in section
    assert 'href="scorecard.html"' in section


def test_export_writes_scorecard_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from odte_scanner.pages_export import export_pages

    payload = {
        "generated_at": "2026-10-15T18:00:00+00:00",
        "scores": [],
        "actions": {},
        "insights": {},
        "scorecard": {
            "disclaimer": DISCLAIMER,
            "move_note": "underlying move",
            "rows": [],
            "summary": {"count": 0, "horizons": {}, "by_type": {}},
            "chain": {"ok": True, "entries": 0, "head": None},
            "audit": {"head_short": None},
        },
    }
    monkeypatch.setattr("odte_scanner.alert_pulse.build_offline_snapshot", lambda *_a, **_k: payload)
    monkeypatch.setattr(
        "odte_scanner.alert_pulse.pulse_desk_alerts",
        lambda *_a, **_k: {"ok": False, "skipped": True},
    )
    out = export_pages(out_dir=tmp_path / "site")
    html = (out / "scorecard.html").read_text(encoding="utf-8")
    assert DISCLAIMER in html
    assert "Sure Winner" not in html
    saved = json.loads((out / "data" / "scorecard.json").read_text(encoding="utf-8"))
    assert saved["chain"]["ok"] is True
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="scorecard"' in index


def test_flask_scorecard_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCORECARD_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SCORECARD_EVAL", "0")
    from odte_scanner.ui import create_app

    app = create_app()
    client = app.test_client()
    page = client.get("/scorecard")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert DISCLAIMER in body
    assert "Sure Winner" not in body
    api = client.get("/api/scorecard")
    assert api.status_code == 200
    assert api.get_json()["chain"]["ok"] is True
    assert client.get("/scorecard.html").status_code == 200
    assert client.get("/scorecard/ledger.jsonl").status_code == 200


def test_public_view_reads_the_same_ledger(ledger: Path):
    _observe(ledger, sure_winners={"winners": [_gold()]})
    again = public_view()
    assert again["summary"]["count"] == 1
    assert again["rows"][0]["signal_type"] == "Gold Setup"


def test_git_mirror_appends_and_keeps_the_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    git = tmp_path / "git.jsonl"
    run = tmp_path / "run.jsonl"
    monkeypatch.delenv("SCORECARD_LEDGER", raising=False)
    monkeypatch.setenv("SCORECARD_MIRROR_GIT", "1")
    monkeypatch.setenv("SCORECARD_EVAL_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setattr("odte_scanner.scorecard.service.git_ledger_path", lambda: git)
    monkeypatch.setattr("odte_scanner.scorecard.service.runtime_ledger_path", lambda: run)
    observe_snapshot(
        sure_winners={"winners": [_gold()]},
        now=NOW,
        fetch_bars=lambda _symbol: _bars(),
    )
    first = git.read_text(encoding="utf-8")
    assert first
    assert run.read_text(encoding="utf-8") == first
    observe_snapshot(
        sure_winners={"winners": [_gold(), _gold("META", tier="HIGH")]},
        now=NOW,
        fetch_bars=lambda _symbol: _bars(),
    )
    assert git.read_text(encoding="utf-8").startswith(first)
    assert verify_chain([json.loads(line) for line in git.read_text().splitlines() if line.strip()]).ok
