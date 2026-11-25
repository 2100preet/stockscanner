"""Mega-cap Morning Sweep shadow desk. Hermetic: fake quotes, fake sender, fixed clock."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from odte_scanner.ext_hours.common import CT
from odte_scanner.signals import morning_sweep_shadow as ms

MOD = Path(ms.__file__)
ROOT = MOD.parents[2]


def ct(h, m, s=0, d=25):
    return datetime(2026, 11, d, h, m, s, tzinfo=CT)


def raw(**kw):
    base = {
        "id": "2026-11-25:1", "ticker": "TSLA", "symbol": "O:TSLA261202C00400000", "optionType": "C",
        "expirationDate": "2026-12-02", "strikePrice": 400, "tradeType": "SWEEP", "side": "Ask",
        "premium": 612_000, "tradePrice": 6.30, "currentVolume": 12_317, "openInterest": 2_404,
        "sigScore": 0.71, "timestamp": int(ct(8, 52).timestamp()),
    }
    base.update(kw)
    return base


class FakeQuotes:
    def __init__(self):
        self.book = {}
        self.calls = 0

    def quote(self, occ):
        self.calls += 1
        return self.book.get(occ)


class Sent(list):
    def __call__(self, text):
        self.append(text)
        return {"ok": True}


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    monkeypatch.delenv("MORNING_SWEEP_SHADOW_OFF", raising=False)
    q, sent = FakeQuotes(), Sent()
    d = ms.MorningSweepShadow(quotes=q, sender=sent, path=tmp_path / "msw.json", regime=lambda: "negative")
    return d, q, sent


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize("kw,why", [
    ({"ticker": "MU", "symbol": "O:MU261202C00400000"}, "universe"),
    ({"optionType": "P"}, "not_call"),
    ({"tradeType": "BLOCK"}, "not_sweep"),
    ({"side": "Bid"}, "not_ask_side"),
    ({"currentVolume": 100, "openInterest": 100}, "not_opening"),
    ({"premium": 499_999}, "premium"),
    ({"sigScore": 0.59}, "sig_score"),
    ({"expirationDate": "2026-11-26"}, "dte"),
    ({"expirationDate": "2026-12-03"}, "dte"),
    ({"timestamp": int(ct(8, 29, 59).timestamp())}, "time_window"),
    ({"timestamp": int(ct(9, 30, 0).timestamp())}, "time_window"),
    ({"isCancelled": True}, "cancelled"),
])
def test_rule_rejections(kw, why):
    assert ms.qualifies(raw(**kw)) == (False, why)


def test_rule_accepts_exact_spec():
    assert ms.qualifies(raw()) == (True, "ok")
    assert ms.qualifies(raw(side="Above Ask", expirationDate="2026-11-27", timestamp=int(ct(9, 29, 59).timestamp())))[0]
    assert set(ms.UNIVERSE) == set("AAPL AMD AMZN AVGO GOOGL META MSFT NFLX NVDA PLTR TSLA".split())


# ----------------------------------------------------------- full cycle
def test_signal_entry_alert_exit_result(desk):
    d, q, sent = desk
    assert d.on_print(raw()) is True
    assert d.on_print(raw(id="x2", premium=900_000)) is False  # first per ticker per day only
    d.tick(ct(8, 52, 30))
    assert q.calls == 0 and sent == []  # waits 60 s
    q.book["TSLA261202C00400000"] = {"bid": 6.20, "ask": 6.36}
    d.tick(ct(8, 53, 5))
    assert len(sent) == 1
    text = sent[0]
    assert "SHADOW (alerts only, no order placed)" in text and "Limit $6.36" in text
    assert "Sure Winner" not in text and "Prior-day SPY GEX: negative" in text
    t = d.status()["trades"][0]
    assert t["status"] == "open" and t["exit_due"].startswith("2026-11-25T09:53:05")
    q.book["TSLA261202C00400000"] = {"bid": 6.95, "ask": 7.05}
    d.tick(ct(9, 30))
    assert len(sent) == 1
    d.tick(ct(9, 53, 10))
    st = d.status(today=ct(9, 54).date())
    t = st["trades"][0]
    assert t["status"] == "closed"
    assert t["ret"] == pytest.approx((6.95 - 6.36 - 0.013) / 6.36)
    assert "Limit exit $6.95 (bid)" in sent[1] and "Sure Winner" not in sent[1]
    assert st["shadow_only"] is True and st["orders_placed"] == 0
    assert st["tally"]["trades"] == 1 and st["tally"]["per_ticker"]["TSLA"]["share"] == 1.0
    assert st["gate"]["verdict"] == "collecting" and st["gate"]["auto_promote"] is False


def test_wide_spread_and_missing_quotes(desk):
    d, q, sent = desk
    d.on_print(raw())
    d.on_print(raw(id="n", ticker="NVDA", symbol="O:NVDA261202C00200000"))
    q.book["TSLA261202C00400000"] = {"bid": 5.0, "ask": 6.0}
    d.tick(ct(8, 53, 5))
    d.tick(ct(9, 3))  # NVDA never quoted -> gives up after 10 min
    s = {t["ticker"]: t["status"] for t in d.status()["trades"]}
    assert s == {"TSLA": "skipped_spread", "NVDA": "no_entry_quote"}
    assert sent == []


def test_backfill_prints_never_reach_desk(desk, monkeypatch):
    from odte_scanner.signals import bullflow

    monkeypatch.setattr(bullflow.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    d, q, sent = desk
    tape = bullflow.BullflowTape()
    tape.add_listener(d.on_print)
    tape.apply_backfill_rows([raw(id="bf")])
    assert d.status()["trades"] == []
    tape.handle_sse_event({"event": "trade", "data": raw(id="live")})
    tape.handle_sse_event({"event": "trade", "data": raw(id="live")})  # duplicate ignored
    assert [t["bullflow_id"] for t in d.status()["trades"]] == ["live"]
    assert tape._thread is None  # listening never starts a stream


def test_listener_errors_do_not_break_tape():
    from odte_scanner.signals import bullflow

    tape = bullflow.BullflowTape()
    tape.add_listener(lambda *_: 1 / 0)
    assert tape.ingest_trade(raw(id="z")) is not None


def test_kill_switch(desk, monkeypatch):
    d, q, sent = desk
    monkeypatch.setenv("MORNING_SWEEP_SHADOW_OFF", "1")
    assert d.on_print(raw()) is False
    d.tick(ct(8, 54))
    assert q.calls == 0 and sent == [] and d.status()["enabled"] is False
    assert ms.start_morning_sweep_shadow(register=lambda fn: None) is False


def test_gate_math():
    trades = [{"ticker": "AAPL" if i % 2 else "TSLA", "status": "closed", "ret": 0.1 if i % 3 else -0.05} for i in range(30)]
    tl = ms.tally(trades)
    assert tl["trades"] == 30 and tl["profit_factor"] > 1.3 and tl["t_stat"] > 2
    g = ms.gate(tl, datetime(2027, 1, 20).date())
    assert g["trading_days_elapsed"] >= 30
    assert g["checks"]["concentration"] is False and g["verdict"] == "fail"  # each ticker ~50% of P&L


# ---------------------------------------------------- no orders / no SSE
def test_module_imports_no_broker_router_or_order_path():
    banned = ("odte_scanner.trading", "router", "webull", "sniper", "auto_trader", "paper")
    tree = ast.parse(MOD.read_text())
    for node in ast.walk(tree):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for m in mods:
            assert not any(b in m.lower() for b in banned), m
    src = MOD.read_text()
    for bad in ("place_order", "/trading/orders", "/accounts/", "streaming", "text/event-stream", "requests.", ".post("):
        assert bad not in src, bad


def test_full_cycle_no_orders_no_broker_module_no_sse(tmp_path):
    code = f"""
import sys, json, requests
calls = []
def boom(*a, **k):
    calls.append(a[0] if a else k.get('url'))
    raise AssertionError('network')
requests.post = requests.get = requests.request = boom
requests.Session.request = lambda self, *a, **k: boom(*a, **k)
sys.path.insert(0, {str(ROOT)!r})
from tests.test_morning_sweep_shadow import FakeQuotes, Sent, raw, ct
from odte_scanner.signals import morning_sweep_shadow as ms
from odte_scanner.signals import bullflow
from pathlib import Path
q, sent = FakeQuotes(), Sent()
d = ms.MorningSweepShadow(quotes=q, sender=sent, path=Path({str(tmp_path)!r}) / "m.json", regime=lambda: "positive")
d.on_print(raw(id="a", ticker="AAPL", symbol="O:AAPL261202C00230000"))
q.book["AAPL261202C00230000"] = {{"bid": 2.0, "ask": 2.1}}
d.tick(ct(8, 53, 5)); d.tick(ct(9, 54)); d.status()
# The desk's whole alert -> settle path loads no broker/router module at all.
bad = sorted(m for m in sys.modules if m.startswith('odte_scanner.trading') or 'router' in m or m.startswith('webull'))
# Through the real tape (Golden Flow's pre-existing OCC parser lives in trading.webull;
# that is a pure parser): still zero network calls, zero orders, no SSE thread.
d2 = ms.MorningSweepShadow(quotes=q, sender=sent, path=Path({str(tmp_path)!r}) / "m2.json", regime=lambda: "positive")
tape = bullflow.BullflowTape()
tape.add_listener(d2.on_print)
tape.handle_sse_event({{"event": "trade", "data": raw(id="b", ticker="AAPL", symbol="O:AAPL261202C00230000")}})
d2.tick(ct(8, 53, 5)); d2.tick(ct(9, 54))
ms.start_morning_sweep_shadow(register=tape.add_listener); ms.reset_for_tests()
st, st2 = d.status(), d2.status()
print(json.dumps({{"bad": bad, "net": calls, "sent": len(sent), "orders": st["orders_placed"] + st2["orders_placed"], "thread": tape._thread is None, "status": [st["trades"][0]["status"], st2["trades"][0]["status"]]}}))
"""
    env = {**os.environ, "MORNING_SWEEP_SHADOW_OFF": "", "SIGNAL_DESK_OFFLINE": "", "BULLFLOW_API_KEY": ""}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"bad": [], "net": [], "sent": 4, "orders": 0, "thread": True, "status": ["closed", "closed"]}


# ------------------------------------------------------------------- API
def test_api_endpoint(monkeypatch, tmp_path):
    import odte_scanner.trading.auto_trader as at
    from odte_scanner.ui import create_app

    monkeypatch.setenv("MORNING_SWEEP_SHADOW_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: {})
    monkeypatch.setattr(at, "_maybe_close_expired", lambda *a, **k: {})
    c = create_app().test_client()
    r = c.get("/api/morning_sweep_shadow")
    assert r.status_code == 200
    b = r.get_json()
    assert b["shadow_only"] is True and b["orders_placed"] == 0 and b["tally"]["trades"] == 0
    assert b["gate"]["min_trades"] == 25 and b["gate"]["max_ticker_share"] == 0.35
    assert b["kill_switch"] == "MORNING_SWEEP_SHADOW_OFF"
