"""📰 News Desk: classifier, dedupe, feed failures, quiet hours, memory bounds,
the shadow rule, and proof the desk cannot place an order."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from odte_scanner.news import classify as C
from odte_scanner.news import desk as D
from odte_scanner.news import feeds as F

CT = D.CT
NEWS_DIR = Path(D.__file__).resolve().parent
REPO = NEWS_DIR.parents[1]


def kind(headline, **kw):
    return C.classify(headline=headline, **kw)["type"]


# ------------------------------------------------------------------ classifier (real headlines)


@pytest.mark.parametrize(
    "headline,kw,expected",
    [
        ("Johnson & Johnson reports Q3 2026 results, raises 2026 guidance", {"items": ["2.02"]}, "EARN_RAISE"),
        ("Alignment Healthcare Reports Third Quarter 2026 Results, Updates Full-Year Outlook",
         {"items": ["2.02"], "text": "We are taking a prudent approach and lowering our outlook based on current headwinds."},
         "EARN_CUT"),
        # a capex outlook cut is not a business guidance cut
        ("PEBBLEBROOK HOTEL TRUST REPORTS THIRD QUARTER 2026 RESULTS",
         {"items": ["2.02"], "text": "Capital investments: 2026 outlook reduced to $60 to $70 million, reflecting timing."},
         "EARN_PENDING"),
        # guidance pulled because of a pending deal is not a cut
        ("Maplebear Reports Third Quarter 2026 Results",
         {"items": ["2.02"], "text": "In light of the pendency of the merger, the company is withdrawing its outlook."},
         "EARN_PENDING"),
        # an earnings release mentioning an FDA approval is still earnings
        ("Vertex Reports Third Quarter 2026 Financial Results",
         {"items": ["2.02"], "text": "FDA approved ALYFTREK for younger patients during the quarter."}, "EARN_PENDING"),
        ("Apple reports fourth quarter results", {"items": ["2.02"], "eps_surprise_pct": 6.1}, "EARN_BEAT"),
        ("Acme reports third quarter results", {"items": ["2.02"], "eps_surprise_pct": -8.0}, "EARN_MISS"),
        ("Acme reports third quarter results", {"items": ["2.02"], "eps_surprise_pct": 1.0}, "EARN_INLINE"),
        ("Acme to Report Third Quarter 2026 Results on November 12", {"items": ["8.01"]}, "OTHER"),
        ("FDA Expands ANKTIVA Approval for BCG-Unresponsive Non-Muscle-Invasive Bladder Cancer", {}, "FDA_APPROVAL"),
        ("Acme Receives Complete Response Letter from FDA for ACM-101", {}, "FDA_CRL"),
        # Charles River's ticker is CRL; no FDA, no rejection
        ("Charles River Laboratories (NYSE: CRL) to Present at Investor Conference", {}, "OTHER"),
        ("Acme Submits BLA to FDA Seeking Approval of ACM-7", {}, "FDA_OTHER"),
        ("BioCo Phase 3 Trial Did Not Meet Its Primary Endpoint", {}, "TRIAL_FAIL"),
        ("Genmab Announces Statistically Significant, Clinically Meaningful Improvement in Overall Survival", {}, "TRIAL_DATA"),
        ("Churchill Capital Corp XI Shareholders Approve Business Combination with Agility Robotics", {}, "MA_CLOSED"),
        ("Iridium Stockholders Approve Acquisition by Acme", {}, "MA_CLOSED"),
        ("Vertiv Completes Acquisition of UtilityInnovation Group", {"company": "Vertiv Holdings Co"}, "MA_CLOSED"),
        ("ADT to be Acquired by Thrive Holdings in $15.3 Billion Transaction", {"company": "ADT Inc."}, "MA_TARGET"),
        ("Salesforce Signs Definitive Agreement to Acquire Braze", {"company": "Salesforce, Inc."}, "MA_ACQUIRER"),
        ("Salesforce Signs Definitive Agreement to Acquire Braze", {"company": "Braze, Inc."}, "MA_TARGET"),
        ("Haymaker Acquisition Corp V Announces the Separate Trading of its Class A Ordinary Shares and Warrants",
         {"company": "Haymaker Acquisition Corp V"}, "OTHER"),
        ("Diginex Announces Extension of Long Stop Date for Proposed Acquisition of Resulticks", {"company": "Diginex Ltd"}, "OTHER"),
        # Live false positives on Nov 10, 2026 (Polygon opinion pieces sent as "M&A acquirer")
        ("Eli Lilly vs Novo Nordisk: What's the Better GLP-1 Stock to Buy?", {"company": "Novo Nordisk A/S"}, "OPINION"),
        ("AMD vs. Intel: Which Is the Better Artificial Intelligence (AI) Chip Stock to Buy Right Now?", {"company": "Intel Corp"}, "OPINION"),
        ("Pfizer Hits a New 52-Week High. Here's Why It's Not Too Late to Buy the Stock", {"company": "Pfizer Inc."}, "OPINION"),
        ("BioNTech to Acquire US FDA Priority Review Voucher", {"company": "BioNTech SE"}, "OTHER"),
        ("Acme Agrees to Buy Widget for $2 Billion", {"company": "Acme Corp"}, "MA_ACQUIRER"),
        ("Acme Reports Q3 Revenue of $5.1B vs. $4.2B", {"items": ["2.02"]}, "EARN_PENDING"),
        ("Acme upgrades full-year outlook", {}, "GUIDE_RAISE"),
        # CHTR, Nov 10, 2026: context only
        ("Charter Announces Tender Offers for Eight Series of Notes", {"company": "Charter Communications, Inc."}, "DEBT_TENDER"),
        ("Charter Communications Shares Climb 5.4% Following Analyst Upgrade", {}, "ANALYST"),
        ("Halozyme Announces Pricing of Upsized Private Offering of $1.3 Billion of Convertible Senior Notes", {}, "OFFERING"),
        ("HCA Announces Proposed Public Offering of Senior Notes", {}, "DEBT"),
        ("Blackstone Completes Senior Notes Offering", {}, "OFFERING_CLOSED"),
        ("Union Pacific Chief Financial Officer Jennifer Hamann to Retire After 35 Years", {"items": ["5.02"]}, "EXEC_CHANGE"),
        ("Former EY U.S. CEO Julie Boland joins Deere & Company's Board of Directors", {"items": ["5.02"]}, "OTHER"),
        ("TRANSACTION IN OWN SHARES", {"form": "6-K"}, "ROUTINE"),
        ("SHAREHOLDER ALERT: Levi & Korsinsky Notifies Shareholders of WDC", {"subjects": ["ATY"]}, "LAWFIRM"),
        ("Acme files 8-K", {"items": ["1.05"]}, "CYBER"),
        ("Acme files 8-K", {"items": ["4.02"]}, "RESTATEMENT"),
        ("Iovance Biotherapeutics Raises Full Year 2026 Revenue Guidance to $410 to $420 Million", {}, "GUIDE_RAISE"),
        ("Acme Lowers Full-Year 2026 Outlook", {}, "GUIDE_CUT"),
    ],
)
def test_classifier_real_headlines(headline, kw, expected):
    assert kind(headline, **kw) == expected


def test_playbook_notes():
    assert "fade watch" in C.NOTES["TRIAL_DATA"] and "don't chase" in C.NOTES["TRIAL_DATA"]
    assert "pinned near deal price" in C.NOTES["MA_TARGET"]
    assert "watch 9:45 for put setup" in C.NOTES["EARN_MISS"]
    assert "puts watch" in C.NOTES["OFFERING"]
    for note in C.NOTES.values():
        assert "sure winner" not in note.lower()


def test_earnings_by_move():
    assert C.earnings_by_move(-4.0) == "EARN_GAP_DOWN"
    assert C.earnings_by_move(3.0) == "EARN_GAP_UP"
    assert C.earnings_by_move(0.5) == "EARN_OUT"
    assert C.earnings_by_move(None) == "EARN_OUT"


def test_headline_of_strips_exhibit_boilerplate():
    text = "Contact: Stephen Larkin, Douglas Elliman Inc. 917-902-2503 Douglas Elliman Inc. Reports Third Quarter 2026 Results"
    assert C.headline_of(text, company="Douglas Elliman Inc.").startswith("Douglas Elliman Inc. Reports Third Quarter")
    assert C.headline_of("Exhibit 99.1 Apple reports fourth quarter results").startswith("Apple reports")


def test_display_headline_cuts_subheads_and_datelines():
    assert C.display_headline("ADT to be Acquired by Thrive Holdings • ADT stockholders will receive $10.00") == "ADT to be Acquired by Thrive Holdings"
    assert C.display_headline("Salesforce Signs Definitive Agreement to Acquire Braze SAN FRANCISCO & NEW YORK (OCTOBER 20, 2026) — x") == (
        "Salesforce Signs Definitive Agreement to Acquire Braze")
    assert len(C.display_headline("word " * 100)) <= 181


# ------------------------------------------------------------------ feed parsing


ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - Acme Corp (0000000001) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/0000000001-26-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;b&gt;AccNo:&lt;/b&gt; 0000000001-26-000001 &lt;b&gt;Size:&lt;/b&gt; 1 MB&lt;br&gt;Item 2.02: Results of Operations and Financial Condition&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-11-09T16:05:00-05:00</updated><id>urn:tag:sec.gov,2008:accession-number=0000000001-26-000001</id></entry>
<entry><title>8-K/A - Acme Corp (0000000001) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1/000000000126000002/0000000001-26-000002-index.htm"/>
<summary type="html">Item 2.02: Results</summary>
<updated>2026-11-09T16:06:00-05:00</updated><id>urn:tag:sec.gov,2008:accession-number=0000000001-26-000002</id></entry>
</feed>"""


def test_parse_edgar_atom_skips_amendments():
    rows = F.parse_edgar_atom(ATOM)
    assert len(rows) == 1
    row = rows[0]
    assert row["cik"] == 1 and row["form"] == "8-K" and "2.02" in row["items"]
    assert row["id"] == "edgar:000000000126000001"
    assert row["published"].astimezone(timezone.utc).hour == 21


def test_tickers_in_wire_text():
    assert F.tickers_in("Acme Inc. (NASDAQ: ACME) and Beta (NYSE: BETA) today") == ["ACME", "BETA"]


def test_sec_user_agent_is_descriptive(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    ua = F.sec_user_agent()
    assert "stockscanner" in ua and "python-requests" not in ua


def test_redact_error_hides_keys():
    msg = F.redact_error("Max retries exceeded with url: /v2/reference/news?limit=5&apiKey=SECRET123&x=1")
    assert "SECRET123" not in msg and "apiKey=***" in msg


class _Resp:
    def __init__(self, status=200, chunks=(b"x" * 1024,) * 10):
        self.status_code = status
        self._chunks = chunks

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_caps_bytes_and_maps_errors(monkeypatch):
    import requests

    h = {"User-Agent": "t"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(chunks=(b"x" * 4096,) * 100))
    assert len(F._get("https://example.test/feed", headers=h, max_bytes=10_000)) == 10_000  # never reads past the cap
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=429))
    with pytest.raises(F.FeedError, match="429"):
        F._get("https://example.test/feed", headers=h)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=403))
    with pytest.raises(F.FeedAuthError):
        F._get("https://api.polygon.io/v2/reference/news", headers=h)

    def down(*a, **k):
        raise requests.ConnectionError("Max retries exceeded with url: /x?apiKey=SECRET9")

    monkeypatch.setattr(requests, "get", down)
    with pytest.raises(F.FeedError) as err:
        F._get("https://example.test/feed", headers=h)
    assert "SECRET9" not in str(err.value)
    with pytest.raises(F.FeedError):
        F.parse_edgar_atom(b"<feed><entry>truncated")


# ------------------------------------------------------------------ desk harness


class FakeCik:
    def __init__(self, mapping=None, names=None):
        self.mapping = mapping or {}
        self.names = names or {}

    def ensure(self):
        return True

    def ticker(self, cik):
        return self.mapping.get(int(cik)) if cik is not None else None

    def name(self, ticker):
        return self.names.get(ticker)


def liquid(price=50.0, avg=5_000_000, **extra):
    row = {"last": price, "prevclose": price, "close": price, "open": price, "average_volume": avg}
    row.update(extra)
    return row


def make_rt(universe=("ACME", "BETA", "SPX"), quotes=None):
    rt = D.Runtime()
    rt.cik = FakeCik({1: "ACME", 2: "BETA"}, {"ACME": "Acme Corp", "BETA": "Beta Inc"})
    rt.universe = frozenset(universe)
    book = quotes if quotes is not None else {}
    rt.quotes = D.QuoteBook(fetch=lambda syms: {s: book[s] for s in syms if s in book})
    return rt


def wire(iid, title, tickers, when, subjects=None, text=""):
    return {"source": "prnewswire", "id": f"prn:{iid}", "title": title, "text": text, "url": f"https://www.prnewswire.com/{iid}",
            "published": when, "tickers": tickers, "subjects": subjects or []}


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "SEND_MIN_GAP_SEC", 0.0)
    monkeypatch.setenv("NEWS_DESK_TELEGRAM", "1")
    return tmp_path / "news_day.json", tmp_path / "news_shadow.jsonl"


def cycle(paths, rt, items, now, sent, **kw):
    store, shadow = paths
    fetchers = {"prnewswire": (lambda: items)} if not callable(items) else {"prnewswire": items}
    fetchers.update(kw.pop("extra_fetchers", {}))
    return D.run_cycle(now=now, store_path=store, shadow_path=shadow, rt=rt, fetchers=fetchers,
                       send=lambda text: (sent.append(text) or {"ok": True}), force=True,
                       extended_fn=kw.pop("extended_fn", lambda s, m: None), **kw)


TUE_7AM = datetime(2026, 11, 10, 7, 0, tzinfo=CT)


def test_alert_text_and_one_alert_per_ticker_per_event(paths):
    rt = make_rt(quotes={"ACME": liquid()})
    sent: list[str] = []
    items = [wire("a1", "Acme Corp (NASDAQ: ACME) Receives FDA Approval for ACM-101", ["ACME"], TUE_7AM - timedelta(minutes=2))]
    res = cycle(paths, rt, items, TUE_7AM, sent)
    assert res["sent"] == 1 and len(sent) == 1
    text = sent[0]
    assert text.startswith("📰 NEWS · ACME · FDA approval")
    assert text.rstrip().endswith("· alert only")
    assert "6:58 AM CT" in text and "Playbook:" in text and "https://www.prnewswire.com/a1" in text
    assert "Sure Winner" not in text
    # same item again: seen; a second source for the same story: deduped
    items.append(wire("a2", "FDA Approves Acme's ACM-101 (NASDAQ: ACME)", ["ACME"], TUE_7AM - timedelta(minutes=1)))
    res = cycle(paths, rt, items, TUE_7AM + timedelta(minutes=1), sent)
    assert len(sent) == 1 and res["sent"] == 0
    state = json.loads(paths[0].read_text())
    assert [e["ticker"] for e in state["events"]] == ["ACME"]


def test_filters_price_volume_universe_and_stale(paths):
    rt = make_rt(universe=("ACME",), quotes={"ACME": liquid(price=8.0), "BIGC": liquid(), "THIN": liquid(avg=200_000)})
    sent: list[str] = []
    when = TUE_7AM - timedelta(minutes=1)
    items = [
        wire("p1", "Acme (NASDAQ: ACME) Announces Pricing of $100 Million Public Offering of Common Stock", ["ACME"], when),
        wire("p2", "BigCo (NYSE: BIGC) Announces Pricing of $500 Million Public Offering of Common Stock", ["BIGC"], when),
        wire("p3", "Thin (NASDAQ: THIN) Receives FDA Approval for TX-1", ["THIN"], when),
        wire("p4", "Old (NASDAQ: ACME) Receives FDA Approval", ["ACME"], TUE_7AM - timedelta(hours=3)),
        wire("p5", "NoQuote (NASDAQ: NOQ) Receives FDA Approval for NQ-1", ["NOQ"], when),
    ]
    cycle(paths, rt, items, TUE_7AM, sent)
    assert sent == []
    why = {e["ticker"]: e["why_not_sent"] for e in json.loads(paths[0].read_text())["events"]}
    assert why["ACME"].startswith("price $8.00")
    assert why["BIGC"].startswith("outside the scan universe")  # offering is not a catalyst type
    assert why["THIN"].startswith("avg volume")
    assert why["NOQ"].startswith("liquidity unknown")
    assert "OLD" not in why and len(why) == 4  # stale item is never listed


def test_quiet_hours_record_without_sending(paths):
    rt = make_rt(quotes={"ACME": liquid()})
    sent: list[str] = []
    saturday = datetime(2026, 11, 14, 10, 0, tzinfo=CT)
    night = datetime(2026, 11, 10, 22, 0, tzinfo=CT)
    for i, now in enumerate((saturday, night)):
        items = [wire(f"q{i}", "Acme (NASDAQ: ACME) Receives FDA Approval for ACM-1", ["ACME"], now - timedelta(minutes=1))]
        cycle(paths, rt, items, now, sent)
    assert sent == []
    events = json.loads(paths[0].read_text())["events"]
    assert events and all("quiet hours" in e["why_not_sent"] for e in events)


def test_feed_failure_is_isolated(paths):
    rt = make_rt(quotes={"ACME": liquid()})
    sent: list[str] = []

    def boom():
        raise F.FeedError("HTTP 503")

    def polygon_denied():
        raise F.FeedAuthError("HTTP 403")

    items = [wire("f1", "Acme (NASDAQ: ACME) Receives FDA Approval for ACM-2", ["ACME"], TUE_7AM)]
    cycle(paths, rt, items, TUE_7AM, sent, extra_fetchers={"edgar8k": boom, "polygon": polygon_denied})
    state = json.loads(paths[0].read_text())
    assert len(sent) == 1
    assert state["sources"]["edgar8k"]["ok"] is False and "503" in state["sources"]["edgar8k"]["error"]
    assert state["sources"]["prnewswire"]["ok"] is True
    assert state["polygon_off_day"] == "2026-11-10"
    # polygon stays off for the rest of the day
    calls = []
    cycle(paths, rt, [], TUE_7AM + timedelta(minutes=5), sent,
          extra_fetchers={"polygon": lambda: calls.append(1) or []})
    assert calls == []


def test_telegram_toggle_and_rate_limits(paths, monkeypatch):
    monkeypatch.setenv("NEWS_DESK_TELEGRAM", "0")
    rt = make_rt(quotes={"ACME": liquid()})
    sent: list[str] = []
    cycle(paths, rt, [wire("t1", "Acme (NASDAQ: ACME) Receives FDA Approval", ["ACME"], TUE_7AM)], TUE_7AM, sent)
    assert sent == []
    assert json.loads(paths[0].read_text())["events"][0]["why_not_sent"] == "NEWS_DESK_TELEGRAM off"

    monkeypatch.setenv("NEWS_DESK_TELEGRAM", "1")
    monkeypatch.setattr(D, "SEND_MAX_PER_HOUR", 2)
    names = [f"T{i}" for i in range(4)]
    rt = make_rt(universe=names, quotes={n: liquid() for n in names})
    paths[0].unlink()
    items = [wire(f"r{i}", f"Co (NASDAQ: {n}) Receives FDA Approval", [n], TUE_7AM) for i, n in enumerate(names)]
    cycle(paths, rt, items, TUE_7AM, sent)
    assert len(sent) == 2
    whys = [e.get("why_not_sent") for e in json.loads(paths[0].read_text())["events"]]
    assert sum(1 for w in whys if w and w.startswith("rate limit")) == 2


def test_memory_bounds(paths, monkeypatch):
    rt = make_rt(quotes={})
    sent: list[str] = []
    old = TUE_7AM - timedelta(hours=5)
    flood = [wire(f"m{i}", f"Co {i} (NASDAQ: ACME) Receives FDA Approval", ["ACME"], old) for i in range(5000)]
    cycle(paths, rt, flood, TUE_7AM, sent)
    state = json.loads(paths[0].read_text())
    assert len(state["seen"]) <= D.MAX_SEEN
    monkeypatch.setattr(D, "SEND_MAX_PER_DAY", 0)
    fresh = [wire(f"n{i}", f"Co (NASDAQ: X{i:04d}) Receives FDA Approval for D{i}", [f"X{i:04d}"], TUE_7AM) for i in range(400)]
    rt.universe = frozenset(f"X{i:04d}" for i in range(400))
    cycle(paths, rt, fresh, TUE_7AM, sent)
    state = json.loads(paths[0].read_text())
    assert len(state["events"]) <= D.MAX_EVENTS
    book = D.QuoteBook(fetch=lambda syms: {s: liquid() for s in syms})
    book.get([f"S{i}" for i in range(D.QUOTE_CACHE_MAX + 200)])
    assert len(book) <= D.QUOTE_CACHE_MAX


def test_day_rollover_keeps_seen_and_open_shadow(paths):
    store, _ = paths
    D.write_state({"day": "2026-11-09", "seen": ["x"] * 10, "events": [{"ticker": "A"}],
                   "shadow": [{"key": "k", "status": "watching", "reaction_day": "2026-11-10", "ticker": "ACME"}],
                   "sent_today": 9}, store)
    rt = make_rt(quotes={"ACME": liquid()})
    cycle(paths, rt, [], TUE_7AM, [])
    state = json.loads(store.read_text())
    assert state["day"] == "2026-11-10" and state["events"] == [] and state["sent_today"] == 0
    assert state["seen"] and state["shadow"][0]["key"] == "k"


# ------------------------------------------------------------------ earnings wait + shadow rule


def test_earnings_wait_then_tag_by_move_and_shadow(paths):
    rt = make_rt(quotes={"ACME": liquid(price=100.0)})
    sent: list[str] = []
    released = datetime(2026, 11, 10, 15, 5, tzinfo=CT)
    items = [wire("e1", "Acme Corp (NASDAQ: ACME) Reports Third Quarter 2026 Results", ["ACME"], released, subjects=["ERN"])]
    cycle(paths, rt, items, released + timedelta(minutes=1), sent)
    state = json.loads(paths[0].read_text())
    assert sent == [] and len(state["pending"]) == 1  # waiting for an EPS surprise
    cycle(paths, rt, items, released + timedelta(minutes=12), sent, extended_fn=lambda s, m: 95.0)
    state = json.loads(paths[0].read_text())
    assert state["pending"] == [] and len(sent) == 1
    assert "Earnings · trading down" in sent[0] and "After hours -5.0%" in sent[0]
    assert "watch 9:45 for put setup" in sent[0]
    assert state["shadow"][0]["reaction_day"] == "2026-11-11" and state["shadow"][0]["status"] == "watching"


def test_eps_surprise_from_nasdaq_wins(paths):
    rt = make_rt(quotes={"ACME": liquid()})
    sent: list[str] = []
    released = datetime(2026, 11, 10, 6, 0, tzinfo=CT)
    items = [wire("e2", "Acme Corp (NASDAQ: ACME) Reports Third Quarter 2026 Results", ["ACME"], released, subjects=["ERN"])]
    nasdaq = {"ACME": {"eps": 0.8, "forecast": 1.0, "surprise_pct": -20.0, "time": "time-pre-market"}}
    cycle(paths, rt, items, released + timedelta(minutes=1), sent, extra_fetchers={"nasdaq": lambda: nasdaq})
    assert len(sent) == 1 and "Earnings miss (EPS -20.0% vs est.)" in sent[0]


def _shadow_state(store, **row):
    base = {"key": "2026-11-11|ACME", "ticker": "ACME", "type": "EARN_MISS", "reaction_day": "2026-11-11", "status": "watching"}
    base.update(row)
    D.write_state({"day": "2026-11-11", "seen": [], "events": [], "shadow": [base]}, store)


def test_shadow_rule_entry_stop_and_log(paths):
    store, shadow = paths
    rt = make_rt()
    _shadow_state(store)
    quotes = {"ACME": {"open": 95.0, "prevclose": 100.0, "last": 94.0}}
    pick = lambda sym, day: {"contract": "ACME261120P00095000", "strike": 95.0, "expiry": "2026-11-20", "dte": 9,
                             "delta": -0.45, "bid": 2.0, "ask": 2.1}
    marks = {"bid": 2.6}
    kw = dict(shadow_kwargs={"quotes_fn": lambda s: quotes, "pick": pick, "option_quote": lambda row: dict(marks)})
    cycle(paths, rt, [], datetime(2026, 11, 11, 8, 45, 10, tzinfo=CT), [], **kw)
    row = json.loads(store.read_text())["shadow"][0]
    assert row["status"] == "open" and row["entry_label"] == "Limit Ask $2.10" and row["gap_pct"] == -5.0
    quotes["ACME"]["last"] = 95.5  # reclaims the open -> stop at the bid
    cycle(paths, rt, [], datetime(2026, 11, 11, 9, 5, tzinfo=CT), [], **kw)
    lines = [json.loads(x) for x in shadow.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["status"] == "closed"
    assert lines[0]["exit_reason"] == "stop: reclaimed the open" and lines[0]["pnl_usd"] == 50.0
    stats = D.shadow_stats(D.read_shadow_history(shadow))
    assert stats["trades"] == 1 and stats["wins"] == 1
    block = D.build_block(store_path=store, shadow_path=shadow)
    assert block["alert_only"] is True and block["orders"] is False and block["shadow"]["stats"]["trades"] == 1


def test_shadow_flat_at_1030_and_gap_filter(paths):
    store, shadow = paths
    rt = make_rt()
    _shadow_state(store, status="open", open=95.0, entry_ask=2.0, contract="ACME261120P00095000",
                  expiry="2026-11-20", strike=95.0)
    kw = dict(shadow_kwargs={"quotes_fn": lambda s: {"ACME": {"last": 90.0}}, "pick": None,
                             "option_quote": lambda row: {"bid": 1.5}})
    cycle(paths, rt, [], datetime(2026, 11, 11, 10, 30, 5, tzinfo=CT), [], **kw)
    row = [json.loads(x) for x in shadow.read_text().splitlines()][-1]
    assert row["exit_reason"] == "flat 10:30 CT" and row["pnl_usd"] == -50.0
    # a 20% gap is outside the rule
    _shadow_state(store, key="2026-11-11|BIG")
    kw = dict(shadow_kwargs={"quotes_fn": lambda s: {"ACME": {"open": 80.0, "prevclose": 100.0, "last": 79.0}},
                             "pick": lambda *a: pytest.fail("must not pick"), "option_quote": lambda r: None})
    cycle(paths, rt, [], datetime(2026, 11, 11, 8, 46, tzinfo=CT), [], **kw)
    row = [json.loads(x) for x in shadow.read_text().splitlines()][-1]
    assert row["status"] == "no_trade" and "outside" in row["reason"]


def test_shadow_market_closed_and_missed_window(paths):
    store, shadow = paths
    rt = make_rt()
    _shadow_state(store, reaction_day="2026-11-14")  # a Saturday
    cycle(paths, rt, [], datetime(2026, 11, 14, 9, 0, tzinfo=CT), [],
          shadow_kwargs={"quotes_fn": lambda s: {}, "pick": None, "option_quote": None})
    assert [json.loads(x) for x in shadow.read_text().splitlines()][-1]["reason"] == "market closed"
    _shadow_state(store)
    cycle(paths, rt, [], datetime(2026, 11, 11, 9, 30, tzinfo=CT), [],
          shadow_kwargs={"quotes_fn": lambda s: {"ACME": {"open": 95.0, "prevclose": 100.0, "last": 94.0}},
                         "pick": None, "option_quote": None})
    assert "missed" in [json.loads(x) for x in shadow.read_text().splitlines()][-1]["reason"]


def test_spx_alerts_only_and_never_enters_shadow(paths):
    ev = {"ticker": "SPX", "type": "EARN_MISS", "session": "post", "reaction_day": "2026-11-11"}
    assert D._shadow_candidate(ev) is None
    text = D.format_alert({"ticker": "SPX", "label": "Guidance cut", "headline": "x", "url": "https://x.test",
                           "note": "Sure Winner", "published_ct": "3:05 PM CT"})
    assert "Sure Winner" not in text and text.endswith("· alert only")


# ------------------------------------------------------------------ no orders, ever


_FORBIDDEN = ("webull", "auto_trader", "broker", "order", "paper_trade", "challenge", "odte_1k", "odte1k")


def test_news_package_imports_nothing_that_trades():
    for path in NEWS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            for name in names:
                low = name.lower()
                assert not any(bad in low for bad in _FORBIDDEN), f"{path.name} imports {name}"


def test_a_full_cycle_loads_no_trading_module(tmp_path):
    """Run a cycle in a clean interpreter and prove no broker/order module was imported."""
    script = f"""
import sys, json
from datetime import datetime
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from odte_scanner.news import desk as D
class Cik:
    def ensure(self): return True
    def ticker(self, c): return "ACME"
    def name(self, t): return "Acme Corp"
rt = D.Runtime(); rt.cik = Cik(); rt.universe = frozenset({{"ACME"}})
rt.quotes = D.QuoteBook(fetch=lambda s: {{"ACME": {{"last": 50, "prevclose": 50, "average_volume": 5e6}}}})
now = datetime(2026, 11, 10, 7, 0, tzinfo=D.CT)
item = {{"source": "prnewswire", "id": "x", "title": "Acme (NASDAQ: ACME) Receives FDA Approval", "url": "https://x.test",
        "published": now, "tickers": ["ACME"], "subjects": []}}
D.SEND_MIN_GAP_SEC = 0
res = D.run_cycle(now=now, store_path=Path({str(tmp_path / 's.json')!r}), shadow_path=Path({str(tmp_path / 'h.jsonl')!r}),
                  rt=rt, fetchers={{"prnewswire": lambda: [item]}}, send=lambda t: {{"ok": True}}, force=True,
                  extended_fn=lambda s, m: None)
bad = sorted(m for m in sys.modules if any(k in m for k in ("webull", "auto_trader", "trading.")))
print(json.dumps({{"sent": res["sent"], "bad": bad}}))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["sent"] == 1
    assert got["bad"] == []


def test_worker_start_respects_flags(monkeypatch):
    started = []
    monkeypatch.setattr(D.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: started.append(k)})())
    monkeypatch.setattr(D, "_worker_started", False)
    monkeypatch.setenv("SIGNAL_DESK_OFFLINE", "1")
    assert D.start_news_desk_worker() is False
    monkeypatch.delenv("SIGNAL_DESK_OFFLINE")
    monkeypatch.setenv("NEWS_DESK_LOOP", "0")
    assert D.start_news_desk_worker() is False
    monkeypatch.delenv("NEWS_DESK_LOOP")
    monkeypatch.setattr(D, "news_desk_enabled", lambda config_path=None: True)
    assert D.start_news_desk_worker() is True
    assert D.start_news_desk_worker() is False  # once per process
    assert started and started[0]["name"] == "news-desk-worker" and started[0]["daemon"] is True


def test_config_flag_defaults_on():
    from odte_scanner.config import load_config

    assert load_config()["actions"]["news_desk_enabled"] is True


def test_snapshot_block_and_tile(monkeypatch, tmp_path):
    from odte_scanner import ui

    monkeypatch.setattr(D, "DAY_STORE", tmp_path / "none.json")
    monkeypatch.setattr(D, "SHADOW_STORE", tmp_path / "none.jsonl")
    block = ui._news_desk_block()
    assert block["ok"] is True and block["alert_only"] is True and block["events"] == []
    assert block["label"] == "📰 News Desk · alert only"
    assert 'id="newsDeskBanner"' in ui.PAGE and "renderNewsDesk()" in ui.PAGE


def test_a_crash_mid_cycle_never_repeats_an_alert(paths, monkeypatch):
    rt = make_rt(quotes={"ACME": liquid(), "BETA": liquid()})
    sent: list[str] = []
    items = [
        wire("c1", "Acme (NASDAQ: ACME) Receives FDA Approval for ACM-9", ["ACME"], TUE_7AM),
        wire("c2", "Beta (NASDAQ: BETA) Receives FDA Approval for B-9", ["BETA"], TUE_7AM),
        {"source": "prnewswire", "id": "junk", "title": None, "published": "not a date", "tickers": None},
    ]
    real = D.move_from_quote

    def flaky(quote, session, ext=None):
        if quote is rt.quotes.get(["BETA"])["BETA"]:
            raise RuntimeError("boom")
        return real(quote, session, ext)

    monkeypatch.setattr(D, "move_from_quote", flaky)
    cycle(paths, rt, items, TUE_7AM, sent)
    assert len(sent) == 1
    cycle(paths, rt, items, TUE_7AM + timedelta(minutes=1), sent)
    assert len(sent) == 1  # nothing re-sent; seen ids were saved despite the error


def test_context_items_listed_for_liquid_names_never_sent(paths):
    rt = make_rt(universe=("ACME",), quotes={"CHTR": liquid(price=121.2, avg=1_179_184), "TINY": liquid(price=4.0)})
    sent: list[str] = []
    items = [
        wire("x1", "Charter Communications (NASDAQ: CHTR) Announces Tender Offers for Eight Series of Notes", ["CHTR"], TUE_7AM),
        wire("x2", "Tiny (NASDAQ: TINY) Announces Tender Offers for Its Senior Notes", ["TINY"], TUE_7AM),
        wire("x3", "Eli Lilly vs Novo Nordisk (NYSE: NVO): What's the Better Stock to Buy?", ["NVO"], TUE_7AM),
    ]
    cycle(paths, rt, items, TUE_7AM, sent)
    assert sent == []
    events = json.loads(paths[0].read_text())["events"]
    assert [(e["ticker"], e["type"]) for e in events] == [("CHTR", "DEBT_TENDER")]
    assert events[0]["why_not_sent"] == "not an alert type"
