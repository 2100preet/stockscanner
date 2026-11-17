"""Squeeze Fire: alert-only Telegram + paper study. Never orders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.signals import squeeze_fire as sf

CT = ZoneInfo("America/Chicago")
FIXTURE = Path(__file__).parent / "fixtures" / "squeeze_fire_spcx_nov16.json"


def _bars() -> list[dict]:
    blob = json.loads(FIXTURE.read_text())
    return [
        {"t": datetime.fromtimestamp(t, tz=timezone.utc).astimezone(CT), "o": o, "h": h, "l": l, "c": c, "v": v}
        for t, o, h, l, c, v in blob["bars"]
    ]


def _at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 11, 16, hh, mm, ss, tzinfo=CT)


def _pick(symbol, spot, today):
    return {"symbol": symbol, "contract": "SPCX261116C00138000", "expiry": "2026-11-16", "strike": 138.0,
            "right": "C", "dte": 0, "bid": 0.80, "ask": 0.95}


def test_spcx_nov16_fires_on_the_1255_bar_after_it_closes():
    sig = sf.detect_fire(_bars(), _at(13, 0, 30))
    assert sig is not None
    assert sig["bar_start_ct"].startswith("2026-11-16T12:55")
    assert sig["close"] == pytest.approx(137.735, abs=0.01)
    assert sig["close"] > sig["vwap"] and sig["close"] > sig["upper_bb"]
    assert sig["vol_ratio"] >= 1.5


def test_forming_bar_never_signals():
    # At 12:59 the 12:55 bar is still forming, even though the data has it.
    assert sf.detect_fire(_bars(), _at(12, 59, 59)) is None


def test_next_bar_does_not_refire():
    # Newest closed bar is 13:00; the squeeze already fired one bar earlier.
    assert sf.detect_fire(_bars(), _at(13, 5, 30)) is None


def test_window_ends_at_1355_bar_start():
    ind = sf.compute_indicators(sf.closed_bars(_bars(), _at(15, 0)))
    late = [i for i, r in enumerate(ind) if r["t"].date().isoformat() == "2026-11-16" and r["t"].hour == 14]
    for i in late:
        fired, fails = sf.evaluate_bar(ind, i)
        assert not fired
        assert "outside 9:00–1:55 CT" in fails


def test_squeeze_needs_six_bars_and_volume(monkeypatch):
    monkeypatch.setattr(sf, "MIN_SQUEEZE_BARS", 400)
    assert sf.detect_fire(_bars(), _at(13, 0, 30)) is None
    monkeypatch.setattr(sf, "MIN_SQUEEZE_BARS", 6)
    monkeypatch.setattr(sf, "VOL_MULT", 50.0)
    assert sf.detect_fire(_bars(), _at(13, 0, 30)) is None


def test_alert_text_is_alert_only_with_labeled_bid_ask():
    sig = sf.detect_fire(_bars(), _at(13, 0, 30))
    text = sf.format_alert("SPCX", sig, _pick("SPCX", 137.7, None))
    assert "ALERT ONLY" in text and "no order placed" in text
    assert "Live Bid $0.80 / Live Ask $0.95" in text
    assert "SPCX 11/16 $138C" in text
    assert "12:55–1:00 PM CT" in text
    assert "Sure Winner" not in text and "sure winner" not in text.lower()


def test_cycle_sends_once_per_day_and_logs_checkpoints(tmp_path):
    store, study = tmp_path / "day.json", tmp_path / "study.jsonl"
    sent: list[str] = []
    quotes = iter([{"bid": 1.1, "ask": 1.2}, {"bid": 1.5, "ask": 1.6}, {"bid": 1.7, "ask": 1.8}, {"bid": 0.4, "ask": 0.5}])
    kw = dict(
        symbols=["SPCX"],
        bars_fn=lambda s, n: _bars(),
        pick_fn=_pick,
        option_quote=lambda row: next(quotes),
        send=lambda t: sent.append(t) or {"ok": True},
        store_path=store,
        study_path=study,
    )
    out = sf.run_cycle(_at(13, 0, 30), **kw)
    assert out["fired"] == ["SPCX"] and len(sent) == 1
    # Same day, later bars: no second alert for SPCX.
    sf.run_cycle(_at(13, 5, 30), **kw)
    assert len(sent) == 1
    state = json.loads(store.read_text())
    row = state["signals"][0]
    assert row["alert_only"] is True and row["orders"] is False
    assert row["signal_bid"] == 0.80 and row["signal_ask"] == 0.95
    # Checkpoints: +15 (1:15), +30 (1:30), +60 (2:00), 2:55 PM.
    for hh, mm in ((13, 15), (13, 30), (14, 0), (14, 55)):
        sf.run_cycle(_at(hh, mm, 20), **kw)
    row = json.loads(store.read_text())["signals"][0]
    assert row["marks"]["+15m"]["bid"] == 1.1
    assert row["marks"]["+30m"]["bid"] == 1.5
    assert row["marks"]["+60m"]["bid"] == 1.7
    assert row["marks"]["2:55 PM"]["bid"] == 0.4
    assert row["status"] == "complete"
    # Next day the row moves to the persistent study file.
    sf.run_cycle(datetime(2026, 11, 17, 8, 56, tzinfo=CT), **kw)
    rows = sf.read_study(study)
    assert len(rows) == 1 and rows[0]["symbol"] == "SPCX"
    stats = sf.study_stats(rows)
    assert stats["n"] == 1 and stats["+60m"]["avg_pct"] == pytest.approx(78.9, abs=0.1)


def test_late_wake_marks_missed_not_a_wrong_time_quote(tmp_path):
    store = tmp_path / "day.json"
    kw = dict(symbols=["SPCX"], bars_fn=lambda s, n: _bars(), pick_fn=_pick,
              option_quote=lambda row: {"bid": 9.0, "ask": 9.5}, send=lambda t: {"ok": True},
              store_path=store, study_path=tmp_path / "s.jsonl")
    sf.run_cycle(_at(13, 0, 30), **kw)
    sf.run_cycle(_at(13, 40, 0), **kw)  # +15 due at 1:15 → 25 min late
    marks = json.loads(store.read_text())["signals"][0]["marks"]
    assert marks["+15m"].get("missed") is True
    assert marks["+30m"]["bid"] == 9.0


def test_telegram_switch_off_still_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUEEZE_FIRE_TELEGRAM", "0")
    sent: list[str] = []
    store = tmp_path / "day.json"
    sf.run_cycle(_at(13, 0, 30), symbols=["SPCX"], bars_fn=lambda s, n: _bars(), pick_fn=_pick,
                 option_quote=lambda r: None, send=lambda t: sent.append(t) or {"ok": True},
                 store_path=store, study_path=tmp_path / "s.jsonl")
    assert sent == []
    assert json.loads(store.read_text())["signals"][0]["symbol"] == "SPCX"


def test_spx_and_index_products_are_excluded():
    syms = sf.resolve_symbols({"actions": {"squeeze_fire_symbols": ["SPX", "spcx", "XSP", "IREN"]}})
    assert syms == ["SPCX", "IREN"]
    assert sf.pick_call("SPX", 6000.0, datetime(2026, 11, 16).date()) is None


def test_module_never_touches_broker_or_auto_trader():
    import ast

    tree = ast.parse(Path(sf.__file__).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    joined = " ".join(names).lower()
    for banned in ("webull", "auto_trader", "place_order", "submit_order", "paper_order"):
        assert banned not in joined


def test_default_config_lists_the_chosen_names():
    from odte_scanner.config import load_config

    cfg = load_config()
    syms = sf.resolve_symbols(cfg)
    assert syms == list(sf.DEFAULT_SYMBOLS)
    assert syms[:7] == ["SPCX", "CRWV", "NBIS", "IREN", "ASTS", "CRCL", "AVGO"]
    assert {"MARA", "RIOT", "WULF", "APLD", "CBRS", "LUNR"} <= set(syms)
    # Kept small: about one 5m-bar request per name every 5 minutes.
    assert 10 <= len(syms) <= 15
    assert not set(syms) & sf.EXCLUDED
    assert (cfg.get("actions") or {}).get("squeeze_fire_enabled") is True


def test_block_reads_disk(tmp_path):
    store = tmp_path / "day.json"
    sf.run_cycle(_at(13, 0, 30), symbols=["SPCX"], bars_fn=lambda s, n: _bars(), pick_fn=_pick,
                 option_quote=lambda r: None, send=lambda t: {"ok": True},
                 store_path=store, study_path=tmp_path / "s.jsonl")
    block = sf.build_block(store_path=store, study_path=tmp_path / "s.jsonl")
    assert block["alert_only"] is True and block["orders"] is False
    assert block["today"][0]["contract"] == "SPCX261116C00138000"
