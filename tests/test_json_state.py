"""Torn or trailing-comma state files must not wipe a desk or fail a snapshot."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from odte_scanner.json_util import atomic_write_text, load_json_state, salvage_json
from odte_scanner.trading.journal import SignalJournal
from odte_scanner.ui import create_app


def _journal_text() -> str:
    return json.dumps(
        {
            "starting_cash": 5000,
            "cash": 4700,
            "trades": [
                {
                    "id": "t1",
                    "symbol": "NVDA",
                    "contract": "NVDA261016C00100000",
                    "expiry": "2026-10-16",
                    "strike": 100,
                    "dte_bucket": "weekly",
                    "entered_at": "2026-10-16T14:00:00+00:00",
                    "entry_ask": 1.2,
                    "entry_score": 80,
                    "entry_reason": "scale-out state",
                    "status": "open",
                    "contracts": 3,
                },
                {
                    "id": "t2",
                    "symbol": "AMD",
                    "contract": "AMD261016C00200000",
                    "expiry": "2026-10-16",
                    "strike": 200,
                    "dte_bucket": "weekly",
                    "entered_at": "2026-10-16T14:05:00+00:00",
                    "entry_ask": 0.8,
                    "entry_score": 77,
                    "entry_reason": "second lot",
                    "status": "open",
                    "contracts": 1,
                },
            ],
        },
        indent=2,
    )


def test_trailing_comma_matches_the_live_decode_error():
    broken = _journal_text().rstrip()[:-1].rstrip() + ",\n}\n"
    with pytest.raises(json.JSONDecodeError, match="property name enclosed in double quotes"):
        json.loads(broken)
    salvaged = salvage_json(broken)
    assert [row["symbol"] for row in salvaged["trades"]] == ["NVDA", "AMD"]


def test_truncated_journal_keeps_the_complete_trade():
    text = _journal_text()
    cut = text.split('"id": "t2"', 1)[0] + '"id": "t2", "symbol": "AM'
    salvaged = salvage_json(cut)
    kept = [row for row in salvaged["trades"] if row.get("symbol")]
    assert [row["symbol"] for row in kept] == ["NVDA"]
    assert kept[0]["contracts"] == 3


def test_load_backs_up_and_rewrites_a_corrupt_journal(tmp_path: Path):
    path = tmp_path / "signal_journal.json"
    broken = _journal_text().rstrip()[:-1].rstrip() + ",\n}\n"
    path.write_text(broken, encoding="utf-8")
    journal = SignalJournal(path, starting_cash=5000)
    symbols = [trade.symbol for trade in journal.book.trades]
    assert symbols == ["NVDA", "AMD"]
    assert json.loads(path.read_text(encoding="utf-8"))["trades"][0]["symbol"] == "NVDA"
    backups = list(tmp_path.glob("signal_journal.json.corrupt-*"))
    assert len(backups) == 1
    assert "NVDA" in backups[0].read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_unrepairable_state_falls_back_to_bak(tmp_path: Path):
    path = tmp_path / "webull_orders.json"
    path.write_text("this is not json {", encoding="utf-8")
    bak = tmp_path / "webull_orders.json.bak"
    bak.write_text(json.dumps({"orders": [{"contract": "NVDA"}]}), encoding="utf-8")
    loaded = load_json_state(path)
    assert loaded["orders"][0]["contract"] == "NVDA"
    assert json.loads(path.read_text(encoding="utf-8"))["orders"][0]["contract"] == "NVDA"
    assert list(tmp_path.glob("webull_orders.json.corrupt-*"))


def test_atomic_write_keeps_the_previous_file_when_replace_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "signal_journal.json"
    atomic_write_text(path, '{"ok": 1}')

    def fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write_text(path, '{"ok": 2}')
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": 1}
    assert not list(tmp_path.glob(".*.tmp"))


def test_snapshot_paints_scores_when_the_journal_is_corrupt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCORECARD_MIRROR_GIT", "0")
    monkeypatch.setenv("SCORECARD_EVAL", "0")
    monkeypatch.setenv("SCORECARD_LEDGER", str(tmp_path / "scorecard.jsonl"))
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    cfg["journal"]["path"] = str(tmp_path / "signal_journal.json")
    cfg["paper_trading"]["ledger_path"] = str(tmp_path / "paper.json")
    actions = cfg.setdefault("actions", {})
    actions["challenge_ledger_path"] = str(tmp_path / "challenge.json")
    actions["rec_log_path"] = str(tmp_path / "rec.json")
    actions["odte_1k_ledger_path"] = str(tmp_path / "o1k.json")
    actions["odte_1k_enabled"] = False
    actions["sure_winners_store_path"] = str(tmp_path / "sure.json")
    actions["signal_times_path"] = str(tmp_path / "signals.json")
    live = cfg.setdefault("live_trading", {})
    live["ledger_path"] = str(tmp_path / "webull.json")
    live["auto_sync"] = False
    live["enabled"] = False
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    broken = _journal_text().rstrip()[:-1].rstrip() + ",\n}\n"
    (tmp_path / "signal_journal.json").write_text(broken, encoding="utf-8")

    def read(path):
        name = str(path)
        if name.endswith("latest_scan.json"):
            return {
                "scores": [{"symbol": "NVDA", "ensemble_score": 88}],
                "session_weekday": "Friday",
                "universe_mode": "focus",
                "universe_size": 12,
                "sure_winners": {"winners": [], "counts": {}},
            }
        return None

    monkeypatch.setattr("odte_scanner.ui._read_json", read)
    monkeypatch.setattr("odte_scanner.ui._snapshot_offline", lambda: True)
    app = create_app(str(tmp_path / "config.yaml"))
    res = app.test_client().get("/api/snapshot?fresh=1")
    assert res.status_code == 200
    body = res.get_json()
    assert "snapshot_error" not in body
    assert body["scores"][0]["symbol"] == "NVDA"
    assert not body.get("snapshot_degraded")
    assert not body.get("snapshot_error")
