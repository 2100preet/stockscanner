"""Extended-hours shadow desks: AH earnings drift (S2) + overnight bias (S1).

Hermetic: fake quotes, fake calendar sources, fake Telegram sender, fixed
clock. Nothing touches the network or any broker.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path

import pytest

from odte_scanner.ext_hours import SHADOW_LABEL, ah_earnings, overnight_bias, quotes as quotes_mod
from odte_scanner.ext_hours.common import CT, ET
from odte_scanner.ext_hours.earnings_calendar import S2_UNIVERSE, EarningsCalendar

PKG = Path(__file__).resolve().parents[1] / "odte_scanner" / "ext_hours"


def ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


class FakeQuotes:
    def __init__(self):
        self.live: dict[str, dict] = {}
        self.ah: dict[tuple[str, str], dict] = {}
        self.rng: dict[tuple[str, str], dict] = {}
        self.calls = 0

    def quotes(self, symbols):
        self.calls += 1
        return {s: dict(self.live[s]) for s in symbols if s in self.live}

    def ah_prices(self, symbol, day):
        return self.ah.get((symbol, day.isoformat()), {})

    def ah_range(self, symbol, day):
        return self.rng.get((symbol, day.isoformat()), {"high": None, "low": None})


class Sent(list):
    def __call__(self, text):
        self.append(text)
        return {"ok": True}


def make_calendar(tmp_path, tags=None, yahoo=None, nasdaq_exc=None):
    calls = {"nasdaq": 0, "yahoo": 0}

    def nasdaq(day):
        calls["nasdaq"] += 1
        if nasdaq_exc:
            raise nasdaq_exc
        return dict(tags or {})

    def yh(symbols, day):
        calls["yahoo"] += 1
        if isinstance(yahoo, Exception):
            raise yahoo
        return set(yahoo or set()) & set(symbols)

    cal = EarningsCalendar(path=tmp_path / "cal.json", nasdaq=nasdaq, yahoo=yh)
    return cal, calls


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.delenv("AH_EARNINGS_SHADOW_OFF", raising=False)
    monkeypatch.delenv("OVERNIGHT_BIAS_OFF", raising=False)


def dell_desk(tmp_path, *, P4=452.0, P5=455.10, C=424.60, bid=455.00, ask=455.20, tags=None):
    q = FakeQuotes()
    q.ah[("DELL", "2026-11-24")] = {"C": C, "P4": P4, "P5": P5}
    q.live["DELL"] = {"bid": bid, "ask": ask, "last": P5}
    cal, calls = make_calendar(tmp_path, tags or {"DELL": "time-after-hours", "ADI": "time-pre-market"})
    sent = Sent()
    desk = ah_earnings.AhEarningsShadow(quotes=q, calendar=cal, sender=sent, ledger_path=tmp_path / "ah.json",
                                        clock=lambda: ct(2026, 11, 24, 17, 0))
    return desk, q, sent, calls


# ----------------------------------------------------------------- S2 alerts
def test_alert_fires_at_5pm_with_shadow_copy_and_gold_label(tmp_path, on):
    desk, q, sent, _ = dell_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 16, 59))
    assert sent == []
    desk.tick(ct(2026, 11, 24, 17, 0))
    assert len(sent) == 1
    text = sent[0]
    assert text.startswith("🌙 AH EARNINGS DRIFT — Gold Setup (shadow)")
    assert SHADOW_LABEL in text and "SHADOW (alerts only, no order placed)" in text
    assert "DELL reported after the close: +7.2% vs 3:00 PM close ($424.60 → $455.10 at 5:00 PM CT)" in text
    assert "Setup: LONG shares, hold to tomorrow's open" in text
    assert "Entry Limit: $455.56 (good until 7:00 PM CT, extended hours)" in text  # mid 455.10 +0.1%
    assert "Exit: tomorrow 8:30 AM CT open" in text
    assert "Last earnings season was negative (−0.86%/trade over 15 trades)" in text
    assert "sure winner" not in text.lower()
    led = json.loads((tmp_path / "ah.json").read_text())
    t = led["trades"][0]
    assert t["status"] == "pending" and t["gold"] and t["orders_placed"] == 0 and t["shadow"] is True
    # runs once per day
    desk.tick(ct(2026, 11, 24, 17, 5))
    assert len(sent) == 1


def test_regular_setup_uses_shadow_header(tmp_path, on):
    desk, q, sent, _ = dell_desk(tmp_path, P5=440.0, P4=439.0, bid=439.9, ask=440.1)  # +3.6%
    desk.tick(ct(2026, 11, 24, 17, 1))
    assert sent[0].splitlines()[0] == "🌙 AH EARNINGS DRIFT — SHADOW (alerts only, no order placed)"
    assert "Gold Setup" not in sent[0]


@pytest.mark.parametrize(
    "kw,decision",
    [
        ({"P5": 430.0, "P4": 429.0, "bid": 429.9, "ask": 430.1}, "NO_SIGNAL"),           # +1.3%
        ({"P4": 435.0}, "SKIP_PRINT_CHECK"),                                              # 5PM vs 4PM +4.6%
        ({"bid": 452.0, "ask": 456.0}, "SKIP_SPREAD"),                                    # 0.88% spread
        ({"P5": None}, "SKIP_NO_PRICE"),
    ],
)
def test_skips(tmp_path, on, kw, decision):
    desk, q, sent, _ = dell_desk(tmp_path, **kw)
    desk.tick(ct(2026, 11, 24, 17, 0))
    assert sent == []
    led = json.loads((tmp_path / "ah.json").read_text())
    assert led["candidates"][-1]["decision"] == decision
    assert led["trades"] == []


def test_short_side_and_friday_exit_phrase(tmp_path, on):
    q = FakeQuotes()
    q.ah[("NKE", "2026-12-18")] = {"C": 100.0, "P4": 90.5, "P5": 90.0}
    q.live["NKE"] = {"bid": 89.99, "ask": 90.01}
    cal, _ = make_calendar(tmp_path, {"NKE": "time-after-hours"})
    sent = Sent()
    desk = ah_earnings.AhEarningsShadow(quotes=q, calendar=cal, sender=sent, ledger_path=tmp_path / "a.json")
    desk.tick(ct(2026, 12, 18, 17, 0))  # Friday
    text = sent[0]
    assert "Setup: SHORT shares, hold to Monday's open" in text
    assert "Entry Limit: $89.91" in text
    assert "Exit: Monday 8:30 AM CT open" in text
    assert "no overnight shorting" in text


def test_no_alert_on_weekend_holiday_or_half_day(tmp_path, on):
    desk, q, sent, calls = dell_desk(tmp_path)
    for day in (ct(2026, 11, 28, 17, 0), ct(2026, 11, 26, 17, 0), ct(2026, 11, 27, 17, 0)):
        desk.tick(day)
    assert sent == [] and calls["nasdaq"] == 0


def test_settle_next_morning_with_season_tally(tmp_path, on):
    desk, q, sent, _ = dell_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 17, 0))
    q.rng[("DELL", "2026-11-24")] = {"high": 457.0, "low": 454.0}
    q.live["DELL"] = {"open": 461.60, "last": 462.0}
    desk.tick(ct(2026, 11, 25, 8, 34))
    assert len(sent) == 1
    desk.tick(ct(2026, 11, 25, 8, 35))
    assert len(sent) == 2
    assert sent[1] == ("DELL AH Drift result: entry Limit $455.56 → open $461.60 = +1.3% (shadow). "
                       "Season tally: 1 trades, 1W/0L, +1.3% avg.")
    t = json.loads((tmp_path / "ah.json").read_text())["trades"][0]
    assert t["status"] == "closed" and t["fill_verified"] and t["exit_open"] == 461.6
    desk.tick(ct(2026, 11, 25, 9, 31))
    desk.tick(ct(2026, 11, 25, 15, 6))
    t = json.loads((tmp_path / "ah.json").read_text())["trades"][0]
    assert t["px_930"] == 462.0 and t["exit_close"] == 462.0
    st = desk.status()
    assert st["season_tally"]["trades"] == 1 and st["orders_placed"] == 0


def test_limit_not_reached_is_no_fill(tmp_path, on):
    desk, q, sent, _ = dell_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 17, 0))
    q.rng[("DELL", "2026-11-24")] = {"high": 470.0, "low": 456.0}  # never traded down to 455.56
    q.live["DELL"] = {"open": 461.60}
    desk.tick(ct(2026, 11, 25, 8, 40))
    assert "was not reached 5:00–7:00 PM CT, so no shadow trade" in sent[1]
    assert json.loads((tmp_path / "ah.json").read_text())["trades"][0]["status"] == "no_fill"


def test_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AH_EARNINGS_SHADOW_OFF", "1")
    desk, q, sent, calls = dell_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 17, 0))
    assert sent == [] and calls["nasdaq"] == 0 and not (tmp_path / "ah.json").exists()
    assert desk.status()["enabled"] is False


# ---------------------------------------------------------------- calendar
def test_calendar_nasdaq_then_yahoo_for_unsupplied_times_cached_daily(tmp_path):
    cal, calls = make_calendar(tmp_path, {"DELL": "time-after-hours", "HPQ": "time-after-hours",
                                          "MU": "time-not-supplied", "ADI": "time-pre-market"}, yahoo={"MU"})
    out = cal.amc_names(date(2026, 11, 24))
    assert out["names"] == ["DELL", "MU"]  # HPQ not in the 27
    assert out["sources"] == {"DELL": "nasdaq", "MU": "yahoo"}
    cal.amc_names(date(2026, 11, 24))
    assert calls == {"nasdaq": 1, "yahoo": 1}  # one fetch a day


def test_calendar_falls_back_to_yahoo_and_fails_open(tmp_path):
    cal, calls = make_calendar(tmp_path, nasdaq_exc=RuntimeError("down"), yahoo={"NVDA"})
    out = cal.amc_names(date(2026, 11, 18))
    assert out["names"] == ["NVDA"] and out["errors"] == ["nasdaq: RuntimeError"]
    # both sources down: the cached answer survives (fail open)
    cal2, _ = make_calendar(tmp_path, nasdaq_exc=RuntimeError("down"), yahoo=RuntimeError("down"))
    again = cal2.amc_names(date(2026, 11, 18), refresh=True)
    assert again["names"] == ["NVDA"]
    # nothing cached and both down: empty, no crash
    empty = cal2.amc_names(date(2026, 11, 19))
    assert empty["names"] == [] and empty["complete"] is False


def test_universe_is_the_27_names():
    assert len(S2_UNIVERSE) == 27 and "DELL" in S2_UNIVERSE and "SPY" not in S2_UNIVERSE


# -------------------------------------------------------------- quote source
def test_polygon_used_at_most_once_per_name_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(quotes_mod, "_bars_tradier", lambda *a, **k: [])
    monkeypatch.setattr(quotes_mod, "_bars_yahoo", lambda *a, **k: [])
    bars = [(datetime(2026, 11, 24, h, m, tzinfo=ET), {"open": 1, "high": 1, "low": 1, "close": c})
            for h, m, c in ((15, 55, 100.0), (16, 55, 104.0), (17, 55, 105.0), (18, 30, 105.5))]
    monkeypatch.setattr(quotes_mod, "_bars_polygon", lambda s, d: calls.append(s) or bars)
    qs = quotes_mod.QuoteSource()
    px = qs.ah_prices("DELL", date(2026, 11, 24))
    assert px == {"C": 100.0, "P4": 104.0, "P5": 105.0}
    qs.ah_range("DELL", date(2026, 11, 24))
    qs.ah_prices("DELL", date(2026, 11, 24))
    assert calls == ["DELL"]


# ------------------------------------------------------------ overnight bias
def bias_desk(tmp_path, qqq_last=636.9, iwm_last=264.5):
    q = FakeQuotes()
    q.live["QQQ"] = {"last": qqq_last, "high": 641.25, "low": 636.63}
    q.live["IWM"] = {"last": iwm_last, "high": 265.12, "low": 263.97}
    sent = Sent()
    return overnight_bias.OvernightBias(quotes=q, sender=sent, ledger_path=tmp_path / "ob.json"), q, sent


def test_overnight_bias_context_note_and_ledger(tmp_path, on):
    desk, q, sent = bias_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 14, 54))
    assert sent == []
    desk.tick(ct(2026, 11, 24, 14, 55))
    desk.tick(ct(2026, 11, 24, 14, 57))
    assert len(sent) == 1
    note = sent[0]
    assert note.startswith("🌙 OVERNIGHT BIAS — context only (no order placed)")
    assert "QQQ at 6% of today's range" in note and "MNQ futures" in note
    assert "IWM" not in note  # IWM at 46% of range
    assert "+5–8bp/night" in note and "not a trade signal" in note and "sure winner" not in note.lower()
    st = desk.status()
    assert st["today"]["lines"] and "context only" in st["today"]["lines"][0]
    rows = json.loads((tmp_path / "ob.json").read_text())["nights"]
    assert [(r["symbol"], r["triggered"]) for r in rows] == [("QQQ", True), ("IWM", False)]
    # next morning: close->open for both nights
    q.live["QQQ"] = {"open": 640.0, "prevclose": 637.19}
    q.live["IWM"] = {"open": 264.0, "prevclose": 264.76}
    desk.tick(ct(2026, 11, 25, 8, 35))
    rows = json.loads((tmp_path / "ob.json").read_text())["nights"]
    assert rows[0]["ret"] == round(640.0 / 637.19 - 1, 6) and rows[1]["settled"]
    st = desk.status()
    assert st["by_symbol"]["QQQ"]["triggered"]["nights"] == 1 and st["orders_placed"] == 0


def test_overnight_bias_no_note_when_not_in_bottom_quarter(tmp_path, on):
    desk, q, sent = bias_desk(tmp_path, qqq_last=640.0, iwm_last=265.0)
    desk.tick(ct(2026, 11, 24, 14, 55))
    assert sent == []
    assert len(json.loads((tmp_path / "ob.json").read_text())["nights"]) == 2


def test_overnight_bias_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERNIGHT_BIAS_OFF", "1")
    desk, q, sent = bias_desk(tmp_path)
    desk.tick(ct(2026, 11, 24, 14, 55))
    assert sent == [] and q.calls == 0 and not (tmp_path / "ob.json").exists()


# ------------------------------------------------------------- no orders
def test_package_imports_no_broker_router_or_order_path():
    banned = ("odte_scanner.trading", "router", "webull", "sniper", "auto_trader", "paper")
    for f in PKG.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                assert not any(b in m.lower() for b in banned), f"{f.name} imports {m}"
        src = f.read_text()
        assert "place_order" not in src and "/trading/orders" not in src


def test_full_cycle_places_no_orders_and_loads_no_broker_module(tmp_path):
    """Run a whole alert + settle cycle in a clean interpreter and prove no
    broker/router module was ever imported and no HTTP POST other than the
    (fake) Telegram sender happened."""
    code = f"""
import sys, json, requests
from datetime import datetime, date
posts = []
requests.post = lambda *a, **k: posts.append(a) or (_ for _ in ()).throw(AssertionError('POST'))
requests.Session.post = requests.post
sys.path.insert(0, {str(PKG.parents[1])!r})
from tests.test_ext_hours_shadow import dell_desk, bias_desk, ct
from pathlib import Path
tmp = Path({str(tmp_path)!r})
desk, q, sent, _ = dell_desk(tmp)
desk.tick(ct(2026, 11, 24, 17, 0))
q.rng[("DELL", "2026-11-24")] = {{"high": 457.0, "low": 454.0}}
q.live["DELL"] = {{"open": 461.6}}
desk.tick(ct(2026, 11, 25, 8, 35))
b, bq, bsent = bias_desk(tmp)
b.tick(ct(2026, 11, 24, 14, 55))
import odte_scanner.ext_hours.worker
bad = sorted(m for m in sys.modules if m.startswith('odte_scanner.trading') or 'router' in m or m.startswith('webull'))
print(json.dumps({{"bad": bad, "posts": len(posts), "sent": len(sent) + len(bsent)}}))
"""
    env = {"AH_EARNINGS_SHADOW_OFF": "", "OVERNIGHT_BIAS_OFF": "", "PATH": "/usr/bin:/bin"}
    import os

    env = {**os.environ, **env}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=str(PKG.parents[1]), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"bad": [], "posts": 0, "sent": 3}


# ------------------------------------------------------------------- API
def test_api_endpoints(monkeypatch):
    import odte_scanner.trading.auto_trader as at
    from odte_scanner.ui import create_app

    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: {})
    monkeypatch.setattr(at, "_maybe_close_expired", lambda *a, **k: {})
    c = create_app().test_client()
    a = c.get("/api/ah_earnings_shadow")
    assert a.status_code == 200
    body = a.get_json()
    assert body["shadow"] is True and body["orders_placed"] == 0 and body["label"] == SHADOW_LABEL
    o = c.get("/api/overnight_bias")
    assert o.status_code == 200 and o.get_json()["context_only"] is True and o.get_json()["orders_placed"] == 0
