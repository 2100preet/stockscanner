from datetime import datetime, timezone

from odte_scanner.signals import confluence as C


def a(kind, sym, contract, hhmm, side="BUY", ok=True):
    h, m = hhmm.split(":")
    t = datetime(2026, 11, 4, int(h) + 6, int(m), tzinfo=timezone.utc).isoformat()
    return {"kind": kind, "symbol": sym, "contract": contract, "side": side, "ok": ok, "time": t}


GC = "GOOGL261104C00290000"


def test_two_setups_in_window_make_an_event():
    ev = C.find_events([a("Sniper", "GOOGL", GC, "8:55"), a("Diamond", "GOOGL", "GOOGL261104C00292500", "9:01")])
    assert len(ev) == 1 and ev[0]["score"] == 2 and ev[0]["badge"] == "⚡2"
    assert ev[0]["first_ct"] == "8:55" and ev[0]["last_ct"] == "9:01"


def test_outside_window_is_not_an_event():
    assert C.find_events([a("Sniper", "GOOGL", GC, "8:55"), a("Diamond", "GOOGL", GC, "9:11")]) == []


def test_same_setup_twice_is_one_and_exits_ignored():
    rows = [a("Sniper", "QQQ", "QQQ261104C00697000", "9:01"), a("Sniper", "QQQ", "QQQ261104C00698000", "9:04"),
            a("EXIT", "QQQ", "QQQ261104C00697000", "9:05", side="SELL")]
    assert C.find_events(rows) == []


def test_direction_split_and_score_three_sorted_first():
    rows = [a("Diamond", "MSFT", "MSFT261104C00552500", "9:06"), a("Explosive", "MSFT", "MSFT261104C00550000", "9:11"),
            a("RIP/META", "MSFT", "MSFT261104C00550000", "9:11"), a("Sniper", "SPY", "SPY261104C00768000", "9:06"),
            a("Diamond", "SPY", "SPY261104C00768000", "9:04"), a("Explosive", "MSFT", "MSFT261104P00540000", "9:07")]
    ev = C.find_events(rows)
    assert [e["symbol"] for e in ev] == ["MSFT", "SPY"]
    assert ev[0]["score"] == 3 and ev[0]["direction"] == "calls"


def test_lunch_is_bullish_afternoon_bearish_and_gold_label():
    rows = [a("LunchCredit", "SPY", "SPY|2026-11-04|500/498|P", "12:05", side="SELL"),
            a("Sure Winner", "SPY", "SPY261104C00500000", "12:10"),
            a("Afternoon Puts", "QQQ", "QQQ261104P00490000", "13:00"),
            a("Sniper", "QQQ", "QQQ261104P00490000", "13:05")]
    ev = {e["symbol"]: e for e in C.find_events(rows)}
    assert ev["SPY"]["direction"] == "calls"
    assert {s["setup"] for s in ev["SPY"]["setups"]} == {"Lunch-Break Credit", "Gold Setup"}
    assert ev["QQQ"]["direction"] == "puts"
    assert "Sure Winner" not in C.format_line(ev["SPY"])


def test_format_line():
    ev = C.find_events([a("Sniper", "GOOGL", GC, "8:55"), a("Diamond", "GOOGL", GC, "9:01")])[0]
    assert C.format_line(ev) == "⚡ CONFLUENCE ⚡2 GOOGL CALLS · Sniper 8:55 + Diamond 9:01 · display only"


def test_fills_attach_labels_and_pnl():
    ev = C.find_events([a("Sniper", "GOOGL", GC, "8:55"), a("Diamond", "GOOGL", GC, "9:01")])
    orders = [{"desk": "sniper", "action": "BUY", "symbol": "GOOGL", "contract": GC, "status": "working",
               "limit_price": 2.5, "quantity": 1, "meta": {"signal_bid": 2.4, "signal_ask": 2.6}}]
    C.attach_fills(ev, orders)
    p = ev[0]["paper"]
    assert p["price_label"] == "Limit (working) $2.50" and p["quote_label"] == "Bid $2.40 / Ask $2.60"


def test_cycle_dedupes_and_resends_on_score_increase(tmp_path):
    store, hist = tmp_path / "c.json", tmp_path / "h.jsonl"
    sent = []
    send = lambda t: sent.append(t) or {"ok": True}
    rows = [a("Sniper", "GOOGL", GC, "8:55"), a("Diamond", "GOOGL", GC, "9:01")]
    now = datetime(2026, 11, 4, 15, 5, tzinfo=timezone.utc)
    kw = dict(now=now, orders=[], store_path=store, history_path=hist, send=send, em_used=None)
    C.run_confluence_cycle(alerts=rows, **kw)
    C.run_confluence_cycle(alerts=rows, **kw)
    assert len(sent) == 1
    C.run_confluence_cycle(alerts=rows + [a("RIP/META", "GOOGL", GC, "9:05")], **kw)
    assert len(sent) == 2 and "⚡3" in sent[1]
    nxt = dict(kw, now=datetime(2026, 11, 5, 15, 5, tzinfo=timezone.utc))
    C.run_confluence_cycle(alerts=[], **nxt)
    assert hist.read_text().count("GOOGL") >= 1


def test_public_block_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(C, "build_board", boom)
    out = C.public_block()
    assert out["ok"] is False and out["events"] == []
