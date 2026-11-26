"""Heavy-volume Reversal 7-14 DTE shadow desk. Hermetic: fake market, fake sender, fixed clock."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from odte_scanner.ext_hours.common import CT
from odte_scanner.signals import reversal_shadow as rs

MOD = Path(rs.__file__)
ROOT = MOD.parents[2]
DAY = date(2026, 12, 1)  # Tuesday


def ct(h, m, s=0, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=CT)


def session(side="C", climax_at=(9, 30), vol_x=4.0, close_pos=0.6, confirm=True, ext=2.0, day=DAY):
    """Synthetic 5-min session: drift to an extension of `ext` (ATR=2.0) then a climax bar."""
    bars, px = [], 100.0
    t0 = ct(8, 30, d=day)
    sgn = -1 if side == "C" else 1
    ci = ((climax_at[0] * 60 + climax_at[1]) - (8 * 60 + 30)) // 5
    step = ext * 2.0 / ci  # move `ext` ATRs by the bar before the climax
    for i in range(78):
        t = t0 + timedelta(minutes=5 * i)
        if i < ci:
            o, c = px, px + sgn * step
            hi, lo = max(o, c) + 0.01, min(o, c) - 0.01
            v = 1000.0
        elif i == ci:  # climax: new extreme, heavy volume, closes off the extreme
            o = px
            ext_px = px + sgn * 1.0
            hi, lo = (max(o, ext_px), min(o, ext_px))
            rng = hi - lo
            c = (lo + close_pos * rng) if side == "C" else (hi - close_pos * rng)
            v = 1000.0 * vol_x
            climax = (hi, lo)
        elif i == ci + 1:
            o = px
            c = (climax[0] + 0.2) if (side == "C") == confirm else px
            if not confirm:
                c = px
            hi, lo = max(o, c) + 0.01, min(o, c) - 0.01
            if side == "C":
                lo = max(lo, climax[1] + 0.05)
            else:
                hi = min(hi, climax[0] - 0.05)
            if side == "P" and confirm:
                c = climax[1] - 0.2
                lo = c - 0.01
            v = 1000.0
        else:
            o = c = px
            hi, lo = px + 0.01, px - 0.01
            if side == "C":
                lo = max(lo, climax[1] + 0.05)
            else:
                hi = min(hi, climax[0] - 0.05)
            v = 1000.0
        bars.append({"t": t, "o": o, "h": hi, "l": lo, "c": c, "v": v})
        px = c
    return bars


def flat_hist(day=DAY, n=22):
    out, d = [], day
    k = 0
    while k < n:
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        k += 1
        for i in range(78):
            t = datetime(d.year, d.month, d.day, 8, 30, tzinfo=CT) + timedelta(minutes=5 * i)
            out.append({"t": t, "o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 1000.0})
    return out


def daily_hist(day=DAY, n=30, rng=2.0):
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "open": 100, "high": 100 + rng / 2, "low": 100 - rng / 2, "close": 100})
    return out[::-1]


class FakeMarket:
    def __init__(self):
        self.today_bars = {}
        self.book = {}
        self.calls = []

    def daily(self, ticker, start, end):
        self.calls.append(("daily", ticker))
        return daily_hist()

    def bars5(self, ticker, start, end):
        self.calls.append(("bars5", ticker))
        if start == end:
            return list(self.today_bars.get(ticker, []))
        return flat_hist()

    def expirations(self, ticker):
        return ["2026-12-02", "2026-12-07", "2026-12-08", "2026-12-09", "2026-12-11", "2026-12-18"]

    def strikes(self, ticker, expiry, side):
        return [float(k) for k in range(80, 121)]

    def quotes(self, occs):
        self.calls.append(("quotes", tuple(occs)))
        return {o: self.book[o] for o in occs if o in self.book}


class Sent(list):
    def __call__(self, text):
        self.append(text)
        return {"ok": True}


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    monkeypatch.delenv("REVERSAL_SHADOW_OFF", raising=False)
    m, sent = FakeMarket(), Sent()
    return rs.ReversalShadow(market=m, sender=sent, path=tmp_path / "rv.json"), m, sent


# ------------------------------------------------------------------ rule
def test_atr_and_slot_volume():
    assert rs.atr20(daily_hist(), DAY) == pytest.approx(2.0)
    assert rs.atr20(daily_hist(n=10), DAY) is None
    tv = rs.tod_avg(flat_hist(), DAY)
    assert tv["09:30"] == pytest.approx(1000.0) and len(tv) == 78


@pytest.mark.parametrize("side", ["C", "P"])
def test_detects_climax_reversal_both_sides(side):
    sig = rs.detect([b for b in session(side) if b["t"] + rs.BAR <= ct(9, 45)], 2.0, rs.tod_avg(flat_hist(), DAY))
    assert list(sig) == [side]
    assert sig[side]["climax_bar"] == ct(9, 30).isoformat() and sig[side]["signal_bar"] == ct(9, 35).isoformat()
    assert sig[side]["climax_vol_x"] == 4.0 and sig[side]["extension_atr"] >= 0.5


@pytest.mark.parametrize("kw,atr", [
    ({"vol_x": 2.9}, 2.0),             # volume < 3x slot average
    ({"close_pos": 0.3}, 2.0),         # closed too near the extreme
    ({"confirm": False}, 2.0),         # next bar did not break the climax bar
    ({"ext": 0.0}, 2.5),               # only 0.4 ATR from the open (< 0.5)
    ({"climax_at": (13, 35)}, 2.0),    # signal bar after 1:30 PM CT
    ({"climax_at": (8, 45)}, 2.0),     # signal bar before 8:55 AM CT
])
def test_rejections(kw, atr):
    tv = rs.tod_avg(flat_hist(), DAY)
    for side in ("C", "P"):
        assert rs.detect(session(side, **kw), atr, tv) == {}
        assert rs.detect(session(side), atr, tv) != {}  # control: the base session does fire


def test_contract_selection():
    exps = ["2026-12-02", "2026-12-07", "2026-12-08", "2026-12-09", "2026-12-11", "2026-12-18"]
    assert rs.pick_expiry(exps, DAY, "SPY") == "2026-12-08"
    assert rs.pick_expiry(exps, DAY, "NVDA") == "2026-12-11"
    # Thanksgiving week: Friday 11/27 is a trading day (half day) -> Friday
    assert rs.pick_expiry(["2026-11-27", "2026-12-04"], date(2026, 11, 20), "TSLA") == "2026-11-27"
    ks = [95, 96, 97, 98, 99, 100, 101, 102, 103]
    assert rs.pick_strike(ks, 100.2, 2.0, "C") == 102 and rs.pick_strike(ks, 100.2, 2.0, "P") == 99
    assert rs.occ("SPY", "2026-12-08", "P", 599.5) == "SPY261208P00599500"
    assert rs.exit_due(date(2026, 11, 25)) == datetime(2026, 11, 27, 11, 58, tzinfo=CT)  # half day
    assert rs.exit_due(DAY) == datetime(2026, 12, 2, 14, 58, tzinfo=CT)


# ------------------------------------------------------------- lifecycle
def test_signal_alert_target_exit(desk):
    d, m, sent = desk
    m.today_bars["NVDA"] = session("C")
    d.tick(ct(9, 41))
    tr = d.ledger.load()["trades"]
    assert len(tr) == 1 and tr[0]["status"] == "signal" and tr[0]["contract"] == "NVDA261211C00098000"
    m.book["NVDA261211C00098000"] = {"bid": 1.90, "ask": 2.00}
    d.tick(ct(9, 41, 30))
    t = d.ledger.load()["trades"][0]
    assert t["status"] == "open" and t["entry_ask"] == 2.00 and t["target_bid"] == 4.01
    assert "Shadow entry Limit $2.00 (ask)" in sent[0] and rs.SHADOW_LABEL in sent[0]
    assert "30-trading-day shadow test" in sent[0] and "never auto-promotes" in sent[0]
    assert "Failed the pre-registered holdout" in sent[0]
    d.tick(ct(9, 45))  # same side not re-signalled
    assert len(d.ledger.load()["trades"]) == 1
    m.book["NVDA261211C00098000"] = {"bid": 4.05, "ask": 4.20}
    d.tick(ct(11, 0))
    t = d.ledger.load()["trades"][0]
    assert t["status"] == "closed" and t["exit_reason"] == "target" and t["ret"] > 1.0
    assert "target +100% hit" in sent[-1] and "Limit exit $4.05 (bid)" in sent[-1]
    assert all("sure winner" not in s.lower() for s in sent)
    st = d.status(DAY)
    assert st["orders_enabled"] is False and st["orders_placed"] == 0 and st["tally"]["trades"] == 1


def test_time_exit_next_trading_day(desk):
    d, m, sent = desk
    m.today_bars["SPY"] = session("P")
    d.tick(ct(9, 41))
    occ = d.ledger.load()["trades"][0]["contract"]
    assert occ.startswith("SPY261208P")
    m.book[occ] = {"bid": 1.0, "ask": 1.05}
    d.tick(ct(9, 42))
    m.book[occ] = {"bid": 0.80, "ask": 0.85}
    d.tick(ct(14, 59))  # same day: hold
    assert d.ledger.load()["trades"][0]["status"] == "open"
    d.tick(ct(14, 58, d=date(2026, 12, 2)))
    t = d.ledger.load()["trades"][0]
    assert t["status"] == "closed" and t["exit_reason"] == "time"
    assert t["ret"] == pytest.approx((0.80 - 1.05 - 0.013) / 1.05)


def test_missing_quotes_and_no_entry(desk):
    d, m, sent = desk
    m.today_bars["TSLA"] = session("C")
    d.tick(ct(9, 41))
    d.tick(ct(9, 52))
    assert d.ledger.load()["trades"][0]["status"] == "no_entry_quote" and sent == []


def test_kill_switch_and_start_day(desk, monkeypatch):
    d, m, sent = desk
    m.today_bars["QQQ"] = session("C", day=date(2026, 11, 25))
    d.tick(ct(9, 41, d=date(2026, 11, 25)))  # before the 30-day window starts
    assert d.ledger.load()["trades"] == []
    monkeypatch.setenv("REVERSAL_SHADOW_OFF", "1")
    m.today_bars["QQQ"] = session("C")
    d.tick(ct(9, 41))
    assert d.ledger.load()["trades"] == [] and m.calls == []
    assert rs.start_reversal_shadow() is False
    assert d.status(DAY)["enabled"] is False


def test_bar_fetch_throttle(desk):
    d, m, _ = desk
    d.tick(ct(9, 41)); d.tick(ct(9, 41, 20)); d.tick(ct(9, 41, 40))
    assert sum(1 for c in m.calls if c[0] == "bars5") == 2 * len(rs.UNIVERSE)  # 1 prep + 1 scan each
    d.tick(ct(9, 42, 5))
    assert sum(1 for c in m.calls if c[0] == "daily") == len(rs.UNIVERSE)  # prep cached for the day


def test_gate_math():
    trades = [{"ticker": "SPY" if i % 2 else "NVDA", "status": "closed", "ret": 0.3 if i % 3 else -0.1} for i in range(30)]
    tl = rs.tally(trades)
    g = rs.gate(tl, date(2027, 1, 20))
    assert g["min_trades"] == 30 and g["min_t"] == 2.0 and g["max_ticker_share"] == 0.5
    assert g["auto_promote"] is False and g["trading_days_elapsed"] >= 30
    assert rs.gate(tl, DAY)["verdict"] == "collecting"


# ---------------------------------------------------- no orders / no SSE
def test_module_imports_no_broker_router_order_or_stream():
    banned = ("odte_scanner.trading", "router", "webull", "sniper", "auto_trader", "paper", "bullflow", "polygon")
    tree = ast.parse(MOD.read_text())
    for node in ast.walk(tree):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for mname in mods:
            assert not any(b in mname.lower() for b in banned), mname
    src = MOD.read_text()
    for bad in ("place_order", "/trading/orders", "/accounts/", "streaming", "text/event-stream", "requests.",
                ".post(", "event-stream", "EventSource", "Sure Winner"):
        assert bad not in src, bad


def test_full_cycle_no_orders_no_network_no_sse(tmp_path):
    code = f"""
import sys, json, threading, requests
calls = []
def boom(*a, **k):
    calls.append(str(a[0] if a else k.get('url')))
    raise AssertionError('network')
requests.post = requests.get = requests.request = boom
requests.Session.request = lambda self, *a, **k: boom(*a, **k)
sys.path.insert(0, {str(ROOT)!r})
from tests.test_reversal_shadow import FakeMarket, Sent, session, ct
from odte_scanner.signals import reversal_shadow as rs
from pathlib import Path
from datetime import date
m, sent = FakeMarket(), Sent()
d = rs.ReversalShadow(market=m, sender=sent, path=Path({str(tmp_path)!r}) / "r.json")
m.today_bars["AMD"] = session("C"); m.today_bars["IWM"] = session("P")
d.tick(ct(9, 41))
for t in d.ledger.load()["trades"]:
    m.book[t["contract"]] = {{"bid": 1.0, "ask": 1.1}}
d.tick(ct(9, 42))
for k in list(m.book): m.book[k] = {{"bid": 1.2, "ask": 1.3}}
d.tick(ct(14, 58, d=date(2026, 12, 2)))
bad = sorted(x for x in sys.modules if x.startswith('odte_scanner.trading') or 'router' in x or x.startswith('webull')
             or x.endswith('signals.bullflow') or x.endswith('data.polygon'))
threads = [t.name for t in threading.enumerate() if 'bullflow' in t.name.lower() or 'sse' in t.name.lower()]
st = d.status(date(2026, 12, 2))
print(json.dumps({{"bad": bad, "net": calls, "sent": len(sent), "orders": st["orders_placed"], "enabled": st["orders_enabled"],
                  "threads": threads, "status": sorted(t["status"] for t in st["closed"])}}))
"""
    env = {**os.environ, "REVERSAL_SHADOW_OFF": "", "SIGNAL_DESK_OFFLINE": "", "BULLFLOW_API_KEY": ""}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"bad": [], "net": [], "sent": 4, "orders": 0, "enabled": False, "threads": [],
                   "status": ["closed", "closed"]}


# ------------------------------------------------------------------- API
def test_api_endpoint(monkeypatch, tmp_path):
    import odte_scanner.trading.auto_trader as at
    from odte_scanner.ui import create_app

    monkeypatch.setenv("REVERSAL_SHADOW_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: {})
    monkeypatch.setattr(at, "_maybe_close_expired", lambda *a, **k: {})
    c = create_app().test_client()
    r = c.get("/api/reversal_shadow")
    assert r.status_code == 200
    b = r.get_json()
    assert b["shadow_only"] is True and b["orders_enabled"] is False and b["orders_placed"] == 0
    assert b["tally"]["trades"] == 0 and b["open_positions"] == [] and b["closed"] == []
    assert b["gate"]["min_trades"] == 30 and b["gate"]["min_t"] == 2.0 and b["gate"]["max_ticker_share"] == 0.5
    assert b["gate"]["auto_promote"] is False and b["kill_switch"] == "REVERSAL_SHADOW_OFF"
    assert "30-trading-day shadow test" in b["test"]
    assert c.get("/api/morning_sweep_shadow").status_code == 200
