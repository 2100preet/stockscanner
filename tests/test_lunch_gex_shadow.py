"""Prior-day GEX regime + Lunch-Break Credit negative-GEX shadow log. Hermetic."""
from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path

import pytest

from odte_scanner.signals import gex_regime, lunch_gex_shadow
from odte_scanner.signals.gex_regime import ensure_today, regime_block, regime_from_eod
from odte_scanner.signals.lunch_gex_shadow import real_desk_fill, shadow_strikes, summary, sync_shadow
from odte_scanner.time_cst import CT

DAY = "2026-11-24"


def ct(h, m, s=0):
    return datetime(2026, 11, 24, h, m, s, tzinfo=CT)


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def guard(*a, **k):
        raise AssertionError("network access attempted in a hermetic test")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


def eod(spot, rows):
    return {"ticker": "SPY", "date": "2026-11-23", "open_interest_date": "2026-11-20", "spot_price": spot,
            "as_of": "2026-11-23T21:15:00.000Z",
            "strikes": [{"expiration": "20261124", "strike": k, "call_gex": max(g, 0), "put_gex": min(g, 0), "net_gex": g} for k, g in rows]}


# ---------------------------------------------------------------- regime math
def test_regime_sums_only_strikes_within_two_percent():
    out = regime_from_eod(eod(600, [(585, 9e9), (588, -3e8), (600, -5e8), (612, 1e8), (613, 9e9)]))
    assert out["strikes_in_band"] == 3 and out["net_gex_band"] == pytest.approx(-7e8)
    assert out["regime"] == "negative" and out["session"] == "2026-11-23"
    assert regime_from_eod(eod(600, [(600, 4e8), (601, -1e8)]))["regime"] == "positive"
    assert regime_from_eod(eod(600, [(500, 4e8)]))["regime"] == "unknown"
    assert regime_from_eod({"strikes": []})["regime"] == "unknown"


# ---------------------------------------------------------------- fetch & cache
def test_ensure_today_fetches_once_paced_and_caches(tmp_path):
    calls, sleeps = [], []

    def fetch(sym, *, api_key):
        calls.append(sym)
        assert api_key == "test-key"
        return {"ok": True, "data": eod(600, [(600, -1e9)] if sym == "SPY" else [(600, 2e9)])}

    path = tmp_path / "g.json"
    block = ensure_today(ct(7, 5), path=path, api_key="test-key", fetch=fetch, sleep=sleeps.append)
    assert calls == ["SPY", "QQQ"] and sleeps == [gex_regime.PACE_SEC]
    assert block["symbols"]["SPY"]["regime"] == "negative" and block["symbols"]["QQQ"]["regime"] == "positive"
    ensure_today(ct(9, 0), path=path, api_key="test-key", fetch=fetch, sleep=sleeps.append)
    assert calls == ["SPY", "QQQ"]  # cached for the day
    assert "test-key" not in path.read_text()


def test_ensure_today_fails_open_with_bounded_retries(tmp_path):
    calls = []

    def boom(sym, *, api_key):
        calls.append(sym)
        return {"ok": False, "status_code": 502, "error": "temporarily unavailable"}

    path = tmp_path / "g.json"
    b = ensure_today(ct(7, 0), path=path, api_key="k", fetch=boom, sleep=lambda s: None)
    assert {v["regime"] for v in b["symbols"].values()} == {"unknown"}
    ensure_today(ct(7, 5), path=path, api_key="k", fetch=boom, sleep=lambda s: None)
    assert len(calls) == 2  # inside the 15-minute retry gap
    for minute in (20, 40, 59):
        ensure_today(ct(7, minute), path=path, api_key="k", fetch=boom, sleep=lambda s: None)
    ensure_today(ct(9, 0), path=path, api_key="k", fetch=boom, sleep=lambda s: None)
    assert len(calls) == 2 * gex_regime.MAX_ATTEMPTS_PER_DAY


def test_no_key_is_unknown_without_any_request(tmp_path):
    b = ensure_today(ct(7, 0), path=tmp_path / "g.json", api_key="", fetch=lambda *a, **k: 1 / 0)
    assert b["symbols"]["SPY"]["regime"] == "unknown" and "not set" in b["symbols"]["SPY"]["note"]


def test_regime_block_is_a_pure_read(tmp_path):
    b = regime_block(ct(8, 0), path=tmp_path / "missing.json")
    assert b["fetched"] is False and b["symbols"]["QQQ"]["regime"] == "unknown"


def test_fetch_eod_gex_is_one_rest_get_with_header_key():
    seen = []

    class R:
        status_code = 200

        def json(self):
            return eod(600, [(600, 1.0)])

    class S:
        def get(self, url, headers=None, params=None, timeout=None):
            seen.append((url, headers, params))
            return R()

    res = gex_regime.fetch_eod_gex("SPY", api_key="test-key", session=S())
    assert res["ok"] and len(seen) == 1
    url, headers, params = seen[0]
    assert url == "https://api.bullflow.io/v1/data/eodGex" and "stream" not in url
    assert headers["X-API-Key"] == "test-key" and params == {"ticker": "SPY"}


def test_gex_module_never_touches_sse(monkeypatch, tmp_path):
    from odte_scanner.signals import bullflow

    def no_sse(*a, **k):
        raise AssertionError("SSE must never be opened")

    monkeypatch.setattr(bullflow.BullflowTape, "start", no_sse)
    monkeypatch.setattr(bullflow, "start_bullflow_worker", no_sse)
    ensure_today(ct(7, 0), path=tmp_path / "g.json", api_key="k",
                 fetch=lambda s, *, api_key: {"ok": True, "data": eod(600, [(600, -1.0)])}, sleep=lambda s: None)
    src = Path(gex_regime.__file__).read_text() + Path(lunch_gex_shadow.__file__).read_text()
    assert "live-options-flow" not in src and "LIVE_STREAM_PATH" not in src


# ---------------------------------------------------------------- shadow
def card(sym="SPY", short=600.0, long=598.0, send=True):
    return {"symbol": sym, "send": send, "send_order": send and sym != "SPX", "short_strike": short,
            "long_strike": long, "expiry": DAY, "at_ct": "12:35", "credit": 0.80, "limit_price": 0.80,
            "width": abs(short - long)}


SH = "SPY261124P00598000"
SL = "SPY261124P00596000"


def quotes_for(mid_short, mid_long, spread=0.02):
    def q(contracts):
        assert set(contracts) <= {SH, SL}
        return {SH: (mid_short - spread / 2, mid_short + spread / 2), SL: (mid_long - spread / 2, mid_long + spread / 2)}

    return q


NEG = lambda sym: "negative"  # noqa: E731
POS = lambda sym: "positive"  # noqa: E731


def test_shadow_strikes_two_dollars_further_out_same_width():
    assert shadow_strikes(600, 2) == (598, 596)


def test_negative_gex_entry_opens_shadow_priced_from_live_quotes(tmp_path):
    p = tmp_path / "s.json"
    board = {"signals": [card()]}
    before = json.dumps(board, sort_keys=True)
    sync_shadow(board, ct(12, 36), regime_for=NEG, quotes=quotes_for(0.90, 0.30), path=p, ledger=tmp_path / "none.json")
    assert json.dumps(board, sort_keys=True) == before  # the real card is untouched
    (row,) = json.loads(p.read_text())["trades"]
    assert row["shadow_only"] and row["status"] == "open" and row["regime"] == "negative"
    assert (row["short_strike"], row["long_strike"], row["width"]) == (598.0, 596.0, 2.0)
    assert (row["short_contract"], row["long_contract"]) == (SH, SL)
    assert row["credit"] == pytest.approx(0.60) and row["desk_short_strike"] == 600.0


def test_no_shadow_on_positive_or_unknown_gex_or_unfired_or_spx(tmp_path):
    p = tmp_path / "s.json"
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=POS, quotes=quotes_for(0.9, 0.3), path=p)
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=lambda s: "unknown", quotes=quotes_for(0.9, 0.3), path=p)
    sync_shadow({"signals": [card(send=False)]}, ct(12, 36), regime_for=NEG, quotes=quotes_for(0.9, 0.3), path=p)
    sync_shadow({"signals": [card("SPX", 6000, 5995)]}, ct(12, 36), regime_for=NEG, quotes=lambda c: {}, path=p)
    assert json.loads(p.read_text())["trades"] == []


@pytest.mark.parametrize("marks,reason,pnl", [
    ([(0.85, 0.30), (0.45, 0.20)], "target", 35.0),   # 0.60 credit -> 0.25 mark <= 0.30
    ([(1.70, 0.40)], "stop", -70.0),                   # 1.30 mark >= 1.20 stop
])
def test_shadow_exits_take_profit_and_stop(tmp_path, marks, reason, pnl):
    p = tmp_path / "s.json"
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=NEG, quotes=quotes_for(0.90, 0.30), path=p)
    t = 37
    for s, l in marks:
        sync_shadow({"signals": [card()]}, ct(12, t), regime_for=NEG, quotes=quotes_for(s, l), path=p)
        t += 1
    (row,) = json.loads(p.read_text())["trades"]
    assert row["status"] == "closed" and row["exit_reason"] == reason and row["pnl_usd"] == pytest.approx(pnl)


def test_shadow_flat_at_245_ct(tmp_path):
    p = tmp_path / "s.json"
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=NEG, quotes=quotes_for(0.90, 0.30), path=p)
    sync_shadow({"signals": [card()]}, ct(14, 44), regime_for=NEG, quotes=quotes_for(0.80, 0.30), path=p)
    assert json.loads(p.read_text())["trades"][0]["status"] == "open"
    sync_shadow({"signals": [card()]}, ct(14, 45), regime_for=NEG, quotes=quotes_for(0.70, 0.30), path=p)
    (row,) = json.loads(p.read_text())["trades"]
    assert row["exit_reason"] == "flat" and row["exit_debit"] == pytest.approx(0.40) and row["pnl_usd"] == pytest.approx(20.0)


def test_shadow_without_quotes_waits_then_no_quote(tmp_path):
    p = tmp_path / "s.json"
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=NEG, quotes=lambda c: {}, path=p)
    assert json.loads(p.read_text())["trades"][0]["status"] == "pending_quote"
    sync_shadow({"signals": [card()]}, ct(14, 16), regime_for=NEG, quotes=lambda c: {}, path=p)
    assert json.loads(p.read_text())["trades"][0]["status"] == "no_quote"


def _ledger(tmp_path):
    rows = [
        {"desk": "lunch_credit", "symbol": "SPY", "status": "filled", "fill_price": 0.82, "limit_price": 0.82, "strike": 600,
         "created_at": "2026-11-24T18:35:30+00:00", "meta": {"role": "open", "short_strike": 600, "long_strike": 598}},
        {"desk": "lunch_credit", "symbol": "SPY", "status": "filled", "fill_price": 0.40, "limit_price": 0.41,
         "created_at": "2026-11-24T19:10:00+00:00", "meta": {"role": "close", "exit_reason": "target"}},
        {"desk": "sniper", "symbol": "SPY", "status": "filled", "fill_price": 3.0, "created_at": "2026-11-24T18:00:00+00:00", "meta": {}},
    ]
    path = tmp_path / "webull_orders.json"
    path.write_text(json.dumps({"orders": rows}))
    return path


def test_real_desk_fill_sits_next_to_shadow(tmp_path):
    led = _ledger(tmp_path)
    fill = real_desk_fill("SPY", DAY, ledger=led)
    assert fill["credit"] == 0.82 and fill["exit_debit"] == 0.40 and fill["pnl_usd"] == pytest.approx(42.0)
    p = tmp_path / "s.json"
    sync_shadow({"signals": [card()]}, ct(12, 36), regime_for=NEG, quotes=quotes_for(0.90, 0.30), path=p, ledger=led)
    sync_shadow({"signals": [card()]}, ct(13, 10), regime_for=NEG, quotes=quotes_for(0.45, 0.20), path=p, ledger=led)
    res = summary(now=ct(15, 0), path=p)
    assert res["closed"] == 1 and res["shadow_pnl_usd"] == pytest.approx(35.0) and res["desk_pnl_usd_same_days"] == pytest.approx(42.0)
    assert res["rows"][0]["desk"]["credit"] == 0.82 and res["shadow_only"] is True


def test_summary_window_is_30_days(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"trades": [
        {"id": "a", "day": "2026-10-01", "status": "closed", "pnl_usd": 10.0},
        {"id": "b", "day": "2026-11-10", "status": "closed", "pnl_usd": -5.0},
    ]}))
    res = summary(now=ct(15, 0), path=p)
    assert res["trades"] == 1 and res["shadow_pnl_usd"] == -5.0


# ---------------------------------------------------------------- isolation
def test_shadow_path_never_calls_any_order_function(tmp_path, monkeypatch):
    import odte_scanner.trading.lunch_router as router
    import odte_scanner.trading.webull as webull

    called = []

    def trap(name):
        def _t(*a, **k):
            called.append(name)
            raise AssertionError(f"order function {name} called from the shadow path")

        return _t

    for mod in (webull, router):
        for name in dir(mod):
            obj = getattr(mod, name)
            if callable(obj) and any(w in name.lower() for w in ("place", "order", "submit", "stage", "cancel", "buy", "sell", "build_vertical")):
                monkeypatch.setattr(mod, name, trap(f"{mod.__name__}.{name}"))
    for name in ("stage", "submit", "cancel", "_submit_one"):
        if hasattr(webull.WebullBroker, name):
            monkeypatch.setattr(webull.WebullBroker, name, trap(f"WebullBroker.{name}"))
    for name in ("sync", "_open", "_place_combo", "_place_legs", "_close", "_close_legs", "_chase"):
        monkeypatch.setattr(router.LunchRouter, name, trap(f"LunchRouter.{name}"))

    led = _ledger(tmp_path)
    before = led.read_bytes()
    p = tmp_path / "s.json"
    for when, q in ((ct(12, 36), quotes_for(0.9, 0.3)), (ct(13, 0), quotes_for(0.8, 0.3)), (ct(14, 45), quotes_for(0.7, 0.3))):
        sync_shadow({"signals": [card(), card("QQQ", 520, 518)]}, when, regime_for=NEG, quotes=q if when else None,
                    path=p, ledger=led)
    assert called == [] and led.read_bytes() == before
    import re

    src = Path(lunch_gex_shadow.__file__).read_text() + Path(gex_regime.__file__).read_text()
    imports = "\n".join(re.findall(r"^\s*(?:from|import)\s+\S+.*$", src, re.MULTILINE))
    for banned in ("trading", "webull", "lunch_router", "sniper", "desks"):
        assert banned not in imports, banned


def test_lunch_cycle_runs_shadow_after_unchanged_route(tmp_path, monkeypatch):
    from odte_scanner import fast_desk

    monkeypatch.setenv("FAST_DESK_ALWAYS", "1")
    monkeypatch.setattr(lunch_gex_shadow, "tradier_quotes", quotes_for(0.9, 0.3))
    monkeypatch.setattr(gex_regime, "fetch_eod_gex",
                        lambda sym, *, api_key: {"ok": True, "data": eod(600, [(600, -1e9)])})
    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key")
    routed = []
    board = {"signals": [card()], "phase": "triggered"}

    def route(b, now):
        routed.append(json.dumps(b["signals"], sort_keys=True))
        return {"ok": True}

    out = fast_desk.run_lunch_credit_cycle(now=ct(12, 36), board=board, route=route, dispatch=lambda s: {"ok": True, "sent": 0})
    assert out["ok"] and len(routed) == 1
    assert routed[0] == json.dumps([card()], sort_keys=True)  # route saw the desk card exactly as built
    assert board["gex_regime"]["symbols"]["SPY"]["regime"] == "negative"
    assert board["gex_shadow"]["trades"] == 1 and board["gex_shadow"]["shadow_only"] is True
    assert board["signals"][0]["send_order"] is True and board["signals"][0]["short_strike"] == 600.0


def test_lunch_cycle_survives_shadow_failure(tmp_path, monkeypatch):
    from odte_scanner import fast_desk

    monkeypatch.setenv("FAST_DESK_ALWAYS", "1")
    monkeypatch.setattr(lunch_gex_shadow, "sync_shadow", lambda *a, **k: 1 / 0)
    out = fast_desk.run_lunch_credit_cycle(now=ct(12, 36), board={"signals": [card()]}, route=lambda b, n: {"ok": True},
                                           dispatch=lambda s: {"ok": True, "sent": 0})
    assert out["ok"] and out["route"] == {"ok": True}


# ---------------------------------------------------------------- api / ui
def test_snapshot_status_and_results_endpoint(tmp_path, monkeypatch):
    from odte_scanner import ui

    ensure_today(None, api_key="k", sleep=lambda s: None,
                 fetch=lambda sym, *, api_key: {"ok": True, "data": eod(600, [(600, -1e9 if sym == "SPY" else 1e9)])})
    snap = ui._overlay_live({})
    assert snap["gex_regime"]["symbols"]["SPY"]["regime"] == "negative"
    assert snap["gex_regime"]["symbols"]["QQQ"]["regime"] == "positive"
    client = ui.create_app().test_client()
    st = client.get("/api/scan_status").get_json()
    assert st["gex_regime"]["symbols"]["SPY"]["regime"] == "negative"
    res = client.get("/api/lunch_gex_shadow").get_json()
    assert res["shadow_only"] is True and res["days"] == 30 and "rows" in res
    raw = json.dumps(snap["gex_regime"]) + json.dumps(res)
    assert "X-API-Key" not in raw and "api_key" not in raw


def test_lunch_tile_shows_regime():
    from odte_scanner import ui

    a = ui.PAGE.index("function renderLunchBanner()")
    fn = ui.PAGE[a:ui.PAGE.index("function renderExpectedMove()")]
    assert "DATA.gex_regime" in fn and fn.count("${gexLine}") == 2
    assert "positive" in fn and "negative" in fn and "unknown" in fn and "not traded" in fn
    assert "Sure Winner" not in fn
