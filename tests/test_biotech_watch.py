"""🧬 Biotech Catalyst Watch: alert only, $1 floor, spikes, dilution, rate limits."""
import ast
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.news import biotech_watch as B

CT = ZoneInfo("America/Chicago")
TUE_10 = datetime(2026, 11, 17, 10, 0, tzinfo=CT)
TUE_22 = datetime(2026, 11, 17, 22, 0, tzinfo=CT)


def _rt(subs=None):
    rt = B.Runtime()
    rt.watchlist = ("SLS",)
    rt.dilution = B.DilutionBook(fetch=lambda cik: subs or {"form": [], "filingDate": []})
    return rt


def _quote(last, prev, vol, avg):
    return {"last": last, "prevclose": prev, "volume": vol, "average_volume": avg}


def _ev(tk="SLS", kind="TRIAL_DATA", headline="SELLAS Announces Positive Topline Results from Phase 3 REGAL", **kw):
    return {"ticker": tk, "type": kind, "headline": headline, "url": "https://example.com/x", "published_ct": "7:05 AM",
            "stale": False, "cik": 1390478, **kw}


def test_module_never_imports_broker_or_trader():
    tree = ast.parse(Path(B.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert not any("webull" in m or "auto_trader" in m or "broker" in m for m in mods)


def test_catalyst_kinds_and_dates():
    assert B.catalyst_kind({"type": "FDA_CRL", "headline": "x"}) == "FDA_CRL"
    assert B.catalyst_kind({"type": "OTHER", "headline": "Acme gets PDUFA date"}) == "PDUFA"
    assert B.catalyst_kind({"type": "OTHER", "headline": "Acme to present at FDA advisory committee"}) == "ADCOM"
    assert B.catalyst_kind({"type": "OTHER", "headline": "Acme reports Phase 2 topline data"}) == "TOPLINE"
    assert B.catalyst_kind({"type": "EARN_BEAT", "headline": "Acme beats"}) is None
    text = "The FDA assigned a PDUFA target action date of March 3, 2027 for the NDA."
    assert B.catalyst_date(text, "PDUFA") == "March 3, 2027"
    assert B.catalyst_date("Topline data from the Phase 3 trial are expected in the first quarter of 2027.", "TOPLINE")


def test_spike_rule_and_floor():
    assert B.is_spike(B.quote_view(_quote(2.4, 2.0, 9_000_000, 2_000_000)))
    assert not B.is_spike(B.quote_view(_quote(2.1, 2.0, 9_000_000, 2_000_000)))  # +5%
    assert not B.is_spike(B.quote_view(_quote(2.4, 2.0, 3_000_000, 2_000_000)))  # 1.5x
    assert not B.is_spike(B.quote_view(_quote(0.6, 0.5, 9_000_000, 1_000_000)))  # under $1


def test_recent_dilution():
    subs = {"form": ["8-K", "424B5", "S-3", "10-Q"], "filingDate": ["2026-11-10", "2026-10-02", "2026-06-01", "2026-05-01"]}
    out = B.recent_dilution(subs, date(2026, 11, 17))
    assert out["status"] == "warn" and "424B5" in out["detail"] and "S-3" not in out["detail"]
    assert B.recent_dilution({"form": ["10-Q"], "filingDate": ["2026-11-01"]}, date(2026, 11, 17))["status"] == "clear"


def test_catalyst_alert_text_and_dedupe(tmp_path):
    sent = []
    rt = _rt({"form": ["424B5"], "filingDate": ["2026-11-01"]})
    quotes = {"SLS": _quote(3.1, 2.2, 40_000_000, 5_000_000)}
    store = tmp_path / "bio.json"
    out = B.run_pass([_ev()], quotes, TUE_10, rt=rt, store_path=store, send=lambda t: sent.append(t) or {"ok": True},
                     telegram=True)
    assert out["sent"] == 1
    msg = sent[0]
    assert "SLS" in msg and "alert only" in msg and "⚠️ Dilution" in msg and "424B5" in msg
    assert "+40.9% today" in msg and "8.0x avg" in msg
    assert "Sure Winner" not in msg
    B.run_pass([_ev()], quotes, TUE_10, rt=rt, store_path=store, send=lambda t: sent.append(t) or {"ok": True},
               telegram=True)
    assert len(sent) == 1  # one alert per ticker per catalyst kind per day
    block = B.build_block(store)
    assert block["alert_only"] and block["orders"] is False and block["events"][0]["ticker"] == "SLS"


def test_floor_skips_sub_dollar_and_news_desk_sent(tmp_path):
    sent = []
    rt = _rt()
    quotes = {"PENY": _quote(0.4, 0.3, 1e7, 1e6), "BIG": _quote(50, 45, 1e7, 1e6)}
    evs = [_ev("PENY"), _ev("BIG", sent=True)]
    out = B.run_pass(evs, quotes, TUE_10, rt=rt, store_path=tmp_path / "b.json",
                     send=lambda t: sent.append(t) or {"ok": True}, telegram=True)
    assert out["rows"] == 0 and not sent


def test_volume_spike_sweep_and_quiet_hours(tmp_path):
    sent = []
    rt = _rt()
    qf = lambda syms: {"SLS": _quote(2.6, 2.0, 12_000_000, 3_000_000)}
    out = B.run_pass([], {}, TUE_10, rt=rt, quote_fn=qf, store_path=tmp_path / "s.json",
                     send=lambda t: sent.append(t) or {"ok": True}, telegram=True, force_spike=True)
    assert out["sent"] == 1 and "Volume spike up" in sent[0]
    rt2 = _rt()
    out = B.run_pass([_ev()], {"SLS": _quote(3, 2, 1e7, 1e6)}, TUE_22, rt=rt2, store_path=tmp_path / "q.json",
                     send=lambda t: sent.append(t) or {"ok": True}, telegram=True)
    assert out["sent"] == 0
    assert B.build_block(tmp_path / "q.json")["events"][0]["why_not_sent"].startswith("quiet hours")


def test_hourly_rate_limit(tmp_path):
    sent = []
    rt = _rt()
    evs = [_ev(f"B{i}") for i in range(B.SEND_MAX_PER_HOUR + 3)]
    quotes = {f"B{i}": _quote(5, 4, 1e7, 1e6) for i in range(len(evs))}
    out = B.run_pass(evs, quotes, TUE_10, rt=rt, store_path=tmp_path / "r.json",
                     send=lambda t: sent.append(t) or {"ok": True}, telegram=True)
    assert len(sent) == B.SEND_MAX_PER_HOUR and out["rows"] == len(evs)


def test_upcoming_calendar_seed_learn_and_prune(tmp_path):
    sent = []
    rt = _rt()
    rt.upcoming_seeds = ({"ticker": "SLS", "label": "SLS009 topline", "date": "Q4 2026", "source": "8-K"},
                         {"ticker": "SLS", "label": "Old thing", "date": "Q1 2026"})
    ev = _ev("ACME", kind="OTHER", headline="Acme announces PDUFA target action date",
             bio_text="The FDA set a PDUFA target action date of March 3, 2027 for the BLA.")
    store = tmp_path / "u.json"
    B.run_pass([ev, _ev()], {"ACME": _quote(4, 3.9, 1e6, 1e6), "SLS": _quote(3.1, 2.2, 4e7, 5e6)}, TUE_10, rt=rt,
               store_path=store, send=lambda t: sent.append(t) or {"ok": True}, telegram=True)
    block = B.build_block(store)
    ups = {(u["ticker"], u["label"]): u["date"] for u in block["upcoming"]}
    assert ups[("SLS", "SLS009 topline")] == "Q4 2026"
    assert ("SLS", "Old thing") not in ups  # past window pruned
    assert ups[("ACME", "PDUFA date")] == "March 3, 2027"
    sls_msg = [m for m in sent if "· SLS ·" in m][0]
    assert "Upcoming: SLS009 topline — expected Q4 2026" in sls_msg
    assert "alert only" in sls_msg


def test_date_bound():
    assert B.date_bound("Q4 2026") == date(2026, 12, 31)
    assert B.date_bound("March 3, 2027") == date(2027, 3, 3)
    assert B.date_bound("first half of 2027") == date(2027, 6, 30)
    assert B.date_bound("mid-2027") == date(2027, 7, 31)
    assert B.date_bound("no date") is None
