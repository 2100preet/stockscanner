"""RIP-v2 shadow mode: logs only, never orders, prices labeled, flat 2:45 PM CT."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals import rip_v2_shadow as S

CT = ZoneInfo("America/Chicago")
LIVE_OK = "MSFT261109C00520000"  # 0.4% OTM vs 518
LIVE_FAR = "MSFT261109C00540000"  # 4.2% OTM


def at(h, m, day=9):
    return datetime(2026, 11, day, h, m, tzinfo=CT)


def ticket(contract=LIVE_OK, strike=520.0, mny=0.39, ask=2.0, symbol="MSFT", signaled=None):
    return {
        "action": "BUY_RIP",
        "symbol": symbol,
        "contract": contract,
        "strike": strike,
        "expiry": "2026-11-09",
        "dte": 0,
        "right": "C",
        "ask": ask,
        "spot": 518.0,
        "moneyness_pct": mny,
        "signaled_at": (signaled or at(10, 0)).isoformat(),
    }


class Quotes:
    def __init__(self, prices):
        self.prices = dict(prices)
        self.calls = []

    def __call__(self, row):
        self.calls.append(row.get("contract"))
        p = self.prices.get(row.get("contract"))
        if p is None:
            return None
        bid, ask = p
        return {"bid": bid, "ask": ask, "source": "test"}


def no_pick(symbol, spot, blocked):
    raise AssertionError("re-pick should not run when the live contract fits")


def run(tmp_path, *, now, board=None, quotes=None, pick=no_pick, sent=None, **kw):
    return S.run_shadow_cycle(
        rip_radar=board,
        now=now,
        store_path=tmp_path / "day.json",
        history_path=tmp_path / "hist.jsonl",
        quote=quotes or Quotes({}),
        pick=pick,
        send=(sent.append if sent is not None else (lambda text: {"ok": True})),
        orders=kw.pop("orders", []),
        **kw,
    )


def day(tmp_path):
    return json.loads((tmp_path / "day.json").read_text())


def test_fits_v2_band():
    assert S.fits_v2(0.0) and S.fits_v2(1.0) and S.fits_v2(-0.35)
    assert not S.fits_v2(1.01) and not S.fits_v2(-0.5) and not S.fits_v2(None)


def test_live_contract_reused_entry_at_live_ask(tmp_path):
    q = Quotes({LIVE_OK: (2.05, 2.15)})
    out = run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    assert out["new"] == 1
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert v2["status"] == "open" and v2["contract"] == LIVE_OK
    assert v2["entry_ask"] == 2.15 and v2["entry_label"] == "Ask $2.15"
    assert v2["same_as_live"] is True


def test_far_otm_live_contract_repicks_within_1pct(tmp_path):
    picked = {"contract": LIVE_OK, "strike": 520.0, "expiry": "2026-11-09", "dte": 0, "ask": 2.2, "bid": 2.1,
              "moneyness_pct": 0.39, "source": "tradier"}
    seen = {}

    def pick(symbol, spot, blocked):
        seen["args"] = (symbol, spot)
        return picked

    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket(LIVE_FAR, 540.0, 4.25)]}, pick=pick,
        quotes=Quotes({LIVE_OK: (2.1, 2.2)}))
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert seen["args"] == ("MSFT", 518.0)
    assert v2["status"] == "open" and v2["contract"] == LIVE_OK and v2["entry_label"] == "Ask $2.20"
    assert v2["same_as_live"] is False


def test_no_fitting_contract_is_no_v2_trade_with_reason(tmp_path):
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket(LIVE_FAR, 540.0, 4.25)]}, pick=lambda *a: None)
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert v2["status"] == "no_trade" and "1% OTM" in v2["reason"]
    # No-trade rows go straight to history.
    hist = S.read_history(tmp_path / "hist.jsonl")
    assert len(hist) == 1 and hist[0]["v2"]["status"] == "no_trade"


def test_weekly_fallback_outside_1pct_is_rejected(tmp_path):
    weekly = {"contract": "MSFT261113C00525000", "strike": 525.0, "expiry": "2026-11-13", "dte": 4, "ask": 4.0,
              "moneyness_pct": 1.35, "weekly_fallback": True}
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket(LIVE_FAR, 540.0, 4.25)]}, pick=lambda *a: weekly)
    assert day(tmp_path)["signals"][0]["v2"]["status"] == "no_trade"


def test_never_rebuy_losing_occ(tmp_path):
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, loss_cooldown_contracts={LIVE_OK},
        pick=lambda *a: None)
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert v2["status"] == "no_trade" and "never rebuy" in v2["reason"]


def test_after_flat_time_and_spx_take_no_trade(tmp_path):
    run(tmp_path, now=at(14, 50), board={"buy_rip": [ticket()]})
    assert "2:45" in day(tmp_path)["signals"][0]["v2"]["reason"]
    run(tmp_path, now=at(14, 51), board={"buy_rip": [ticket("SPX261109C06000000", 6000.0, 0.2, symbol="SPX")]})
    assert day(tmp_path)["signals"][1]["v2"]["reason"] == "SPX stays alert-only"


def test_same_signal_logged_once(tmp_path):
    q = Quotes({LIVE_OK: (2.0, 2.1)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    out = run(tmp_path, now=at(10, 3), board={"buy_rip": [ticket()]}, quotes=q)
    assert out["new"] == 0 and len(day(tmp_path)["signals"]) == 1


def test_target_exit_at_bid(tmp_path):
    q = Quotes({LIVE_OK: (1.9, 2.0)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    q.prices[LIVE_OK] = (3.05, 3.15)
    run(tmp_path, now=at(10, 30), quotes=q)
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert v2["status"] == "closed" and v2["exit_reason"] == "target +50%"
    assert v2["exit_label"] == "Bid $3.05" and v2["pnl_usd"] == 105.0


def test_stop_and_trail(tmp_path):
    assert S.exit_decision(-36, 0, at(10, 0)) == "stop −35%"
    assert S.exit_decision(20, 30, at(10, 0)) is None
    assert S.exit_decision(15, 30, at(10, 0)).startswith("trail")
    assert S.exit_decision(5, 10, at(14, 45)) == "flat 2:45 PM CT"
    assert S.exit_decision(5, 10, at(14, 44)) is None


def test_flat_at_245_then_one_summary_line(tmp_path):
    q = Quotes({LIVE_OK: (1.9, 2.0)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    q.prices[LIVE_OK] = (2.2, 2.3)
    sent = []
    assert S.flat_window_due(at(14, 46), tmp_path / "day.json")
    run(tmp_path, now=at(14, 46), quotes=q, sent=sent)
    v2 = day(tmp_path)["signals"][0]["v2"]
    assert v2["status"] == "closed" and v2["exit_reason"] == "flat 2:45 PM CT"
    assert len(sent) == 1 and sent[0].endswith("· shadow only")
    assert "Sure Winner" not in sent[0]
    run(tmp_path, now=at(14, 50), quotes=q, sent=sent)
    assert len(sent) == 1
    assert not S.flat_window_due(at(14, 51), tmp_path / "day.json")
    hist = S.read_history(tmp_path / "hist.jsonl")
    assert len(hist) == 1 and hist[0]["v2"]["status"] == "closed"


def test_rollover_closes_leftover_and_never_holds_overnight(tmp_path):
    q = Quotes({LIVE_OK: (1.9, 2.0)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    q.prices[LIVE_OK] = (2.1, 2.2)
    run(tmp_path, now=at(10, 30), quotes=q)
    run(tmp_path, now=at(9, 0, day=10), quotes=Quotes({}))
    hist = S.read_history(tmp_path / "hist.jsonl")
    assert hist[0]["v2"]["status"] == "closed" and "missed" in hist[0]["v2"]["exit_reason"]
    assert day(tmp_path)["day"] == "2026-11-10" and day(tmp_path)["signals"] == []


def test_never_places_orders(tmp_path, monkeypatch):
    from odte_scanner.trading import webull

    def boom(*a, **k):
        raise AssertionError("shadow mode must not touch the broker")

    for name in dir(webull):
        obj = getattr(webull, name)
        if isinstance(obj, type):
            for meth in ("submit", "stage", "cancel"):
                if hasattr(obj, meth):
                    monkeypatch.setattr(obj, meth, boom, raising=False)
    q = Quotes({LIVE_OK: (1.9, 2.0)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    run(tmp_path, now=at(14, 46), quotes=q)


def test_block_stats_current_from_fills_vs_v2(tmp_path):
    q = Quotes({LIVE_OK: (1.9, 2.0)})
    run(tmp_path, now=at(10, 1), board={"buy_rip": [ticket()]}, quotes=q)
    q.prices[LIVE_OK] = (3.1, 3.2)
    run(tmp_path, now=at(10, 30), quotes=q)
    orders = [
        {"action": "BUY", "status": "filled", "symbol": "MSFT", "contract": LIVE_OK, "fill_price": 2.0,
         "quantity": 2, "desk": "sure_winner", "created_at": "2026-11-09T16:02:00+00:00",
         "updated_at": "2026-11-09T16:02:00+00:00", "meta": {}},
        {"action": "SELL", "status": "filled", "symbol": "MSFT", "contract": LIVE_OK, "fill_price": 1.0,
         "quantity": 2, "desk": "golden_exit", "updated_at": "2026-11-09T18:00:00+00:00",
         "meta": {"entry_price": 2.0}},
    ]
    block = S.build_block(store_path=tmp_path / "day.json", history_path=tmp_path / "hist.jsonl", orders=orders)
    assert block["shadow_only"] and block["not_traded"]
    cur, v2 = block["stats"]["current"], block["stats"]["v2"]
    assert cur["n"] == 1 and cur["win_rate_pct"] == 0.0 and cur["pnl_usd"] == -100.0
    assert cur["pnl_usd_actual_qty"] == -200.0 and cur["profit_factor"] == 0.0
    assert v2["n"] == 1 and v2["wins"] == 1 and v2["pnl_usd"] == 110.0
    row = block["today"][0]
    assert row["entry_label"] == "Ask $2.00" and row["exit_label"] == "Bid $3.10"
    assert "Sure Winner" not in json.dumps(block)


def test_side_stats_profit_factor():
    s = S.side_stats([50, -35, 20], [100, -70, 40])
    assert s["n"] == 3 and s["wins"] == 2 and s["profit_factor"] == 2.0 and s["pnl_usd"] == 70.0


def test_public_block_never_raises(monkeypatch):
    monkeypatch.setattr(S, "build_block", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert S.public_block()["ok"] is False


def test_format_summary_line():
    line = S.format_summary({"start_day": "2026-11-09", "v2": S.side_stats([50], [100]),
                             "current": S.side_stats([-40], [-80])}, day="2026-11-09")
    assert line.startswith("🧪 RIP-v2 shadow Nov 9") and line.endswith("· shadow only")


def test_shadow_tick_runs_from_side_cycles_after_window(monkeypatch):
    """After the stack's loop rewrite the shadow tick lives in _side_cycles,
    which also runs outside the entry windows (2:45-4:00 PM CT flatten/summary)."""
    from odte_scanner import fast_desk as fd
    from odte_scanner.signals import confluence, expected_move
    from odte_scanner.signals import rip_v2_shadow as rs

    calls = []
    monkeypatch.setattr(expected_move, "run_expected_move_cycle", lambda: None)
    monkeypatch.setattr(confluence, "run_confluence_cycle", lambda: None)
    monkeypatch.setattr(fd, "in_hot_window", lambda *a, **k: False)
    monkeypatch.setattr(fd, "in_afternoon_window", lambda *a, **k: False)
    monkeypatch.setattr(rs, "flat_window_due", lambda *a, **k: True)
    monkeypatch.setattr(rs, "run_shadow_cycle", lambda **k: calls.append(k))
    fd._side_cycles()
    assert calls == [{"rip_radar": None}]

    calls.clear()
    monkeypatch.setattr(rs, "flat_window_due", lambda *a, **k: False)
    fd._side_cycles()
    assert calls == []


def test_shadow_tick_failure_does_not_break_side_cycles(monkeypatch):
    from odte_scanner import fast_desk as fd
    from odte_scanner.signals import confluence, expected_move
    from odte_scanner.signals import rip_v2_shadow as rs

    monkeypatch.setattr(expected_move, "run_expected_move_cycle", lambda: None)
    monkeypatch.setattr(confluence, "run_confluence_cycle", lambda: None)
    monkeypatch.setattr(rs, "flat_window_due", lambda *a, **k: True)

    def boom(**_k):
        raise RuntimeError("x")

    monkeypatch.setattr(rs, "run_shadow_cycle", boom)
    fd._side_cycles()  # logged, not raised
