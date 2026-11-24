"""Prior-day GEX regime + Lunch-Break Credit shadow log. Hermetic: no network."""
from __future__ import annotations

import ast
import inspect
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from odte_scanner.signals import gex_regime as gr
from odte_scanner.signals import lunch_gex_shadow as sh
from odte_scanner.time_cst import CT


def _ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


def _body(spot, rows):
    return {"ticker": "SPY", "spot_price": spot, "strikes": rows, "as_of": "2026-11-23T22:15:00Z"}


# ---------- regime math ----------

def test_net_band_sums_all_expirations_strictly_inside_two_pct():
    rows = [
        {"expiration": "20261124", "strike": 100, "net_gex": 5.0},
        {"expiration": "20261127", "strike": 100, "net_gex": -2.0},  # same strike, other expiry: summed
        {"expiration": "20261124", "strike": 101.9, "net_gex": -10.0},
        {"expiration": "20261124", "strike": 102, "net_gex": 1000.0},  # exactly +2%: excluded
        {"expiration": "20261124", "strike": 98, "net_gex": 1000.0},  # exactly -2%: excluded
        {"expiration": "20261124", "strike": 90, "net_gex": 9999.0},
    ]
    out = gr.net_gex_band(_body(100.0, rows))
    assert out["net_gex"] == pytest.approx(-7.0)
    assert out["strikes_in_band"] == 2
    assert gr.classify(out["net_gex"]) == gr.NEGATIVE
    assert gr.classify(0.0) == gr.NEGATIVE  # backtest: net2 > 0 is posGEX, else negGEX
    assert gr.classify(1.0) == gr.POSITIVE
    assert gr.classify(None) == gr.UNKNOWN


def test_net_band_unusable_bodies():
    assert gr.net_gex_band(None) is None
    assert gr.net_gex_band({"spot_price": 0, "strikes": [{"strike": 1, "net_gex": 1}]}) is None
    assert gr.net_gex_band({"spot_price": 100, "strikes": []}) is None


def test_matches_backtest_cache_when_available():
    """Cross-check against the backtest's cached eodGex body (box only; skipped in CI)."""
    import hashlib

    params = {"ticker": "SPY", "date": "2026-11-20"}
    name = hashlib.md5(("/v1/data/eodGex" + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    path = Path("/workspace/bullflow_bt/cache") / f"{name}.json"
    if not path.exists():
        pytest.skip("backtest cache not on this machine")
    body = json.loads(path.read_text())["body"]
    assert gr.net_gex_band(body)["net_gex"] == pytest.approx(-9364297012.28, rel=1e-6)


def test_prior_session_skips_weekend_and_holiday():
    assert gr.prior_session(date(2026, 11, 24)) == date(2026, 11, 23)
    assert gr.prior_session(date(2026, 11, 23)) == date(2026, 11, 20)  # Monday -> Friday
    assert gr.prior_session(date(2026, 11, 27)) == date(2026, 11, 25)  # skips Thanksgiving


# ---------- caching / fail open ----------

def _fake_fetch(calls, bodies):
    def fetch(ticker, d):
        calls.append((ticker, d))
        b = bodies.get(ticker)
        if isinstance(b, Exception):
            return None, None, type(b).__name__
        return 200, b, None
    return fetch


def test_ensure_fetches_once_per_ticker_per_day(tmp_path):
    calls = []
    bodies = {
        "SPY": _body(100.0, [{"strike": 100, "net_gex": 5e9}]),
        "QQQ": _body(50.0, [{"strike": 50, "net_gex": -2e9}]),
    }
    store = tmp_path / "g.json"
    now = _ct(2026, 11, 24, 7, 0)
    block = gr.ensure_regimes(now, store_path=store, fetch=_fake_fetch(calls, bodies))
    assert calls == [("SPY", "2026-11-23"), ("QQQ", "2026-11-23")]
    assert block["tickers"]["SPY"]["regime"] == "positive"
    assert block["tickers"]["QQQ"]["regime"] == "negative"
    assert block["tickers"]["SPY"]["label"] == "Positive GEX"
    gr.ensure_regimes(_ct(2026, 11, 24, 12, 0), store_path=store, fetch=_fake_fetch(calls, bodies))
    assert len(calls) == 2  # cached on disk
    # Next day fetches again.
    gr.ensure_regimes(_ct(2026, 11, 25, 7, 0), store_path=store, fetch=_fake_fetch(calls, bodies))
    assert len(calls) == 4 and calls[-1] == ("QQQ", "2026-11-24")


def test_fail_open_unknown_and_bounded_retries(tmp_path):
    calls = []
    store = tmp_path / "g.json"
    bodies = {"SPY": RuntimeError("down"), "QQQ": RuntimeError("down")}
    block = gr.ensure_regimes(_ct(2026, 11, 24, 7, 0), store_path=store, fetch=_fake_fetch(calls, bodies))
    assert block["tickers"]["SPY"]["regime"] == "unknown"
    assert len(calls) == 2
    gr.ensure_regimes(_ct(2026, 11, 24, 7, 10), store_path=store, fetch=_fake_fetch(calls, bodies))
    assert len(calls) == 2  # waits RETRY_SEC
    for minute in (8, 9, 10, 11, 12):
        gr.ensure_regimes(_ct(2026, 11, 24, minute, 0), store_path=store, fetch=_fake_fetch(calls, bodies))
    assert len(calls) == 2 * gr.MAX_ATTEMPTS


def test_fetch_raising_never_propagates(tmp_path):
    def boom(t, d):
        raise ValueError("x")

    block = gr.ensure_regimes(_ct(2026, 11, 24, 7, 0), store_path=tmp_path / "g.json", fetch=boom)
    assert block["tickers"]["QQQ"]["regime"] == "unknown"


def test_no_fetch_on_weekend_or_before_six(tmp_path):
    calls = []
    gr.ensure_regimes(_ct(2026, 11, 22, 9, 0), store_path=tmp_path / "g.json", fetch=_fake_fetch(calls, {}))
    gr.ensure_regimes(_ct(2026, 11, 24, 5, 0), store_path=tmp_path / "g.json", fetch=_fake_fetch(calls, {}))
    assert calls == []


def test_fetch_eod_gex_without_key_is_unknown_and_no_network(monkeypatch):
    class Boom:
        def get(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("network")

    status, body, err = gr.fetch_eod_gex("SPY", "2026-11-23", session=Boom())
    assert (status, body) == (None, None) and "not set" in err


def test_fetch_eod_gex_uses_header_key_and_rest_path(monkeypatch):
    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key-123")
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return _body(100.0, [{"strike": 100, "net_gex": 1.0}])

    class Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params, headers=headers)
            return Resp()

    status, body, err = gr.fetch_eod_gex("SPY", "2026-11-23", session=Sess())
    assert status == 200 and err is None
    assert seen["url"].endswith("/v1/data/eodGex")
    assert "streaming" not in seen["url"]
    assert seen["params"] == {"ticker": "SPY", "date": "2026-11-23"}
    assert seen["headers"]["X-API-Key"] == "test-key-123"
    assert "test-key-123" not in seen["url"]


def test_http_error_is_unknown(monkeypatch):
    monkeypatch.setenv("BULLFLOW_API_KEY", "k")

    class Resp:
        status_code = 503

    class Sess:
        def get(self, *a, **k):
            return Resp()

    status, body, err = gr.fetch_eod_gex("SPY", "2026-11-23", session=Sess())
    assert body is None and err == "HTTP 503"


def test_module_never_touches_the_sse_stream():
    src = inspect.getsource(gr) + inspect.getsource(sh)
    assert "LIVE_STREAM_PATH" not in src
    assert "/v1/streaming" not in src.replace('"/v1/streaming" not in', "")
    assert "get_tape" not in src and "start_bullflow_worker" not in src


def test_shadow_module_imports_no_broker():
    tree = ast.parse(inspect.getsource(sh))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    assert not any("webull" in n or "router" in n or "sniper" in n or "trading" in n for n in names)


# ---------- board / alert / regime_for ----------

def _seed(store, day="2026-11-24", spy="negative", qqq="positive"):
    rows = {}
    for t, reg in (("SPY", spy), ("QQQ", qqq)):
        rows[t] = {"ticker": t, "gex_date": "2026-11-23", "regime": reg, "label": gr.LABELS[reg], "net_gex": -9.36e9 if reg == "negative" else 4.2e9, "spot": 600.0}
    store.write_text(json.dumps({"days": {day: rows}}))


def test_attach_to_board_and_spx_proxy(tmp_path):
    store = tmp_path / "g.json"
    _seed(store)
    board = {"signals": [{"symbol": "SPY"}, {"symbol": "QQQ"}, {"symbol": "SPX"}]}
    gr.attach_to_lunch_board(board, _ct(2026, 11, 24, 12, 30), store_path=store)
    s, q, x = board["signals"]
    assert s["gex_regime"] == "negative" and s["gex_regime_label"] == "Negative GEX"
    assert q["gex_regime"] == "positive"
    assert x["gex_regime"] == "negative" and x["gex_regime_ticker"] == "SPY"
    assert board["gex_regime"]["tickers"]["SPY"]["regime"] == "negative"


def test_attach_without_store_is_unknown(tmp_path):
    board = {"signals": [{"symbol": "SPY"}]}
    gr.attach_to_lunch_board(board, _ct(2026, 11, 24, 12, 30), store_path=tmp_path / "none.json")
    assert board["signals"][0]["gex_regime"] == "unknown"


def test_lunch_alert_text_has_regime_line():
    from odte_scanner.alerts.lunch_credit import format_lunch_entry

    card = {"symbol": "SPY", "short_strike": 600, "long_strike": 598, "expiry": "2026-11-24", "credit": 0.8,
            "gex_regime": "negative", "gex_regime_label": "Negative GEX", "gex_regime_ticker": "SPY", "gex_net": -9.36e9}
    text = format_lunch_entry(card)
    assert "Prior-day GEX: Negative GEX (±2% net −9.36B) · context only" in text
    assert "Sure Winner" not in text
    card2 = dict(card, symbol="SPX", gex_regime="unknown", gex_regime_label="GEX unknown")
    assert "Prior-day GEX: unknown via SPY" in format_lunch_entry(card2)
    plain = {k: v for k, v in card.items() if not k.startswith("gex")}
    assert "GEX" not in format_lunch_entry(plain)


def test_lunch_cycle_stamps_regime_without_changing_route(monkeypatch, tmp_path):
    from odte_scanner import fast_desk

    _seed(gr.DEFAULT_STORE)
    seen = {}

    def route(board, moment):
        seen["cards"] = [dict(c) for c in board["signals"]]
        return {"ok": True}

    board = {"ok": True, "signals": [{"symbol": "SPY", "send": False, "send_order": True, "contracts": 1}]}
    out = fast_desk.run_lunch_credit_cycle(
        now=_ct(2026, 11, 24, 12, 30), board=board, route=route, dispatch=lambda snap: {"ok": True, "sent": 0}
    )
    assert out["ok"] is True
    card = seen["cards"][0]
    assert card["gex_regime"] == "negative"
    assert card["send_order"] is True and card["contracts"] == 1


# ---------- shadow log ----------

def _chain(quotes, day="2026-11-24"):
    return [{"right": "P", "strike": k, "expiry": day, "bid": b, "ask": a} for k, (b, a) in quotes.items()]


def _card(sym="SPY", short=600.0, long=598.0):
    return {"symbol": sym, "short_strike": short, "long_strike": long, "send_order": True, "credit_mid": 0.9, "at_ct": "12:35"}


def _neg_block(reg="negative"):
    return {"gex_date": "2026-11-23", "tickers": {"SPY": {"regime": reg, "label": gr.LABELS[reg], "net_gex": -1.0, "gex_date": "2026-11-23"},
                                                 "QQQ": {"regime": "positive", "label": "Positive GEX", "net_gex": 1.0, "gex_date": "2026-11-23"}}}


ENTRY_Q = {600: (1.40, 1.50), 598: (0.50, 0.60), 596: (0.20, 0.30), 594: (0.05, 0.15)}


def test_negative_day_records_base_and_wide_then_target_and_stop(tmp_path):
    store = tmp_path / "s.json"
    board = {"signals": [_card()]}
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store,
                              fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    row = blk["recent"][0]
    assert row["regime"] == "negative" and row["shadow_only"] is True
    assert row["base"]["short_strike"] == 600 and row["base"]["long_strike"] == 598
    assert row["wide"]["short_strike"] == 598 and row["wide"]["long_strike"] == 596
    assert row["base"]["credit"] == pytest.approx(0.9)
    assert row["wide"]["credit"] == pytest.approx(0.3)
    # Rally: base spread decays to <= 50% (target); wide decays too.
    q2 = {600: (0.40, 0.50), 598: (0.05, 0.15), 596: (0.00, 0.05), 594: (0.0, 0.05)}
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 13, 0), store_path=store,
                              fetch_chain=lambda s: _chain(q2), regime_block=_neg_block(), force_marks=True)
    row = blk["recent"][0]
    assert row["base"]["status"] == "closed" and row["base"]["exit_reason"] == "target"
    assert row["base"]["pnl_usd"] == pytest.approx(55.0)
    # A second cycle does not duplicate the record.
    assert len(blk["recent"]) == 1


def test_stop_and_flat(tmp_path):
    store = tmp_path / "s.json"
    board = {"signals": [_card()]}
    sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    selloff = {600: (2.40, 2.50), 598: (0.55, 0.65), 596: (0.25, 0.35), 594: (0.10, 0.20)}
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 13, 30), store_path=store, fetch_chain=lambda s: _chain(selloff), regime_block=_neg_block(), force_marks=True)
    row = blk["recent"][0]
    assert row["base"]["exit_reason"] == "stop"  # 1.85 >= 2 x 0.90 = 1.80
    assert row["wide"]["status"] == "open"  # 0.30 -> 0.20, no exit
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 14, 45), store_path=store, fetch_chain=lambda s: _chain(selloff), regime_block=_neg_block())
    row = blk["recent"][0]
    assert row["wide"]["exit_reason"] == "flat"
    neg = blk["by_regime"]["negative"]
    assert neg["base"]["stops"] == 1 and neg["wide"]["closed"] == 1
    assert blk["wide_minus_base_usd"]["n"] == 1


def test_positive_day_logs_base_only(tmp_path):
    store = tmp_path / "s.json"
    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 12, 36), store_path=store,
                              fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block("positive"), force_marks=True)
    row = blk["recent"][0]
    assert row["regime"] == "positive" and row["wide"] is None and row["base"]["status"] == "open"


def test_unknown_regime_logs_base_only(tmp_path):
    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 12, 36), store_path=tmp_path / "s.json",
                              fetch_chain=lambda s: _chain(ENTRY_Q), regime_block={"tickers": {}}, force_marks=True)
    assert blk["recent"][0]["regime"] == "unknown" and blk["recent"][0]["wide"] is None


def test_skips_spx_and_unrouted_cards_and_outside_window(tmp_path):
    store = tmp_path / "s.json"
    board = {"signals": [_card("SPX", 6000, 5995), dict(_card(), send_order=False)]}
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block())
    assert blk["recent"] == []
    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 14, 20), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block())
    assert blk["recent"] == []


def test_missing_quotes_pending_then_no_quote(tmp_path):
    store = tmp_path / "s.json"
    board = {"signals": [_card()]}
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=lambda s: [], regime_block=_neg_block(), force_marks=True)
    assert blk["recent"][0]["base"]["status"] == "pending"
    blk = sh.run_shadow_cycle(board, _ct(2026, 11, 24, 14, 30), store_path=store, fetch_chain=lambda s: [], regime_block=_neg_block(), force_marks=True)
    assert blk["recent"][0]["base"]["status"] == "no_quote"


def test_chain_errors_fail_quietly(tmp_path):
    def boom(sym):
        raise RuntimeError("yahoo down")

    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 12, 36), store_path=tmp_path / "s.json", fetch_chain=boom, regime_block=_neg_block(), force_marks=True)
    assert blk["ok"] and blk["recent"][0]["base"]["status"] == "pending"


def test_quote_fetch_throttled(tmp_path):
    calls = []

    def fetch(sym):
        calls.append(sym)
        return _chain(ENTRY_Q)

    store = tmp_path / "s.json"
    board = {"signals": [_card()]}
    sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=fetch, regime_block=_neg_block())
    sh.run_shadow_cycle(board, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=fetch, regime_block=_neg_block())
    assert calls == ["SPY"]


def test_stale_open_leg_closes_next_day(tmp_path):
    store = tmp_path / "s.json"
    sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    blk = sh.run_shadow_cycle({"signals": []}, _ct(2026, 11, 25, 9, 0), store_path=store, fetch_chain=lambda s: [], regime_block=_neg_block())
    assert blk["recent"][0]["base"]["exit_reason"] == "flat_unmarked"


def test_auto_expires_after_30_days(tmp_path):
    store = tmp_path / "s.json"
    sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 11, 24, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    blk = sh.build_block(store_path=store, now=_ct(2026, 11, 24, 13, 0))
    assert blk["started_on"] == "2026-11-24" and blk["expires_on"] == "2026-12-24" and blk["active"]
    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 12, 24, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    assert blk["active"] is False
    assert len(blk["recent"]) == 1  # no new record after expiry; old rows stay readable
    blk = sh.run_shadow_cycle({"signals": [_card()]}, _ct(2026, 12, 23, 12, 36), store_path=store, fetch_chain=lambda s: _chain(ENTRY_Q), regime_block=_neg_block(), force_marks=True)
    assert len(blk["recent"]) == 2


def test_lunch_cycle_shadow_respects_flag(monkeypatch):
    from odte_scanner import fast_desk

    started = []
    monkeypatch.setattr(sh, "run_shadow_cycle_async", lambda board, now=None: started.append(1) or True)
    fast_desk._lunch_gex_shadow({"signals": []}, datetime.now(timezone.utc))
    assert started == []  # conftest sets LUNCH_GEX_SHADOW=0
    monkeypatch.setenv("LUNCH_GEX_SHADOW", "1")
    fast_desk._lunch_gex_shadow({"signals": []}, datetime.now(timezone.utc))
    assert started == [1]


# ---------- API ----------

def test_api_snapshot_scan_status_and_shadow_endpoint(tmp_path):
    _seed(gr.DEFAULT_STORE, day=datetime.now(timezone.utc).astimezone(CT).date().isoformat())
    from odte_scanner import ui

    overlaid = ui._overlay_live({"lunch_credit": {"ok": True, "signals": [{"symbol": "QQQ"}]}})
    assert overlaid["gex_regime"]["tickers"]["SPY"]["regime"] == "negative"
    cards = overlaid["lunch_credit"]["signals"]
    assert cards
    want = {"SPY": "negative", "QQQ": "positive", "SPX": "negative"}
    assert all(c["gex_regime"] == want[c["symbol"]] for c in cards)
    assert overlaid["lunch_gex_shadow"]["shadow_only"] is True
    app = ui.create_app() if hasattr(ui, "create_app") else None
    if app is None:
        pytest.skip("no app factory")
    client = app.test_client()
    status = client.get("/api/scan_status").get_json()
    assert status["gex_regime"]["tickers"]["QQQ"]["regime"] == "positive"
    body = client.get("/api/lunch_gex_shadow").get_json()
    assert body["shadow"]["shadow_only"] is True and body["shadow"]["orders"] is False
    assert body["gex_regime"]["tickers"]["SPY"]["label"] == "Negative GEX"


def test_portal_tile_renders_regime():
    from odte_scanner import ui

    src = inspect.getsource(ui)
    i = src.index("function renderLunchBanner")
    seg = src[i : src.index("function renderExpectedMove", i)]
    assert "lunchGexRegime" in seg and "Prior-day GEX" in seg and "Negative GEX" in seg
    assert "Sure Winner" not in seg
