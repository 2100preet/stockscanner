"""Polymarket unusual-flow monitor: hermetic tests + Nov 22, 2026 Jensen replay.

Every Polymarket response comes from tests/fixtures/polymarket_jensen_nov22.json
(frozen from the public Gamma / Data API v2 on Nov 23, 2026) or inline dicts.
No test opens a socket.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from odte_scanner.polymarket import ALERT_LABEL
from odte_scanner.polymarket import monitor as pm_monitor
from odte_scanner.polymarket.client import PolymarketClient, fill_notional, parse_json_list
from odte_scanner.polymarket.detector import (
    DetectorSettings,
    ask_absorption,
    check_odds_spike,
    evaluate_wallet,
    implied_direction,
    is_off_hours,
    market_outlier,
)
from odte_scanner.polymarket.monitor import (
    MonitorState,
    PolymarketMonitor,
    bullflow_confirmation,
    format_alert_text,
    monitor_enabled,
)
from odte_scanner.polymarket.universe import map_ticker, markets_from_events, reject_reason

FIXTURE = Path(__file__).parent / "fixtures" / "polymarket_jensen_nov22.json"
FX = json.loads(FIXTURE.read_text())
COND = "0x27748e74503ff93d998d1e857980c43eca77c38efd61f642dd43d41a18525250"
WALLET = "0xdf8616e18da2ad636713f4bf4367794e810606bb"
YES_TOKEN = "4872670756822880674944283038882529075210700927252109488954070437890542145542"
FILL_1 = 1795396751  # 2026-11-23 01:19:11 UTC = Sun Nov 22 7:19 PM CT
FILL_4 = 1795400900  # 2026-11-23 02:28:20 UTC = Sun Nov 22 8:28 PM CT
TRUMP_POST = int(datetime(2026, 11, 23, 3, 57, tzinfo=timezone.utc).timestamp())  # 9:57 PM CT


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def guard(*a, **k):
        raise AssertionError("network access attempted in a hermetic test")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


class FixtureClient(PolymarketClient):
    """Serves the frozen Nov 22 data as of the replay clock. Counts every call."""

    def __init__(self, clock):
        super().__init__(session=object())
        self.clock = clock
        self.log: list[tuple[str, tuple]] = []

    def _request(self, *a, **k):  # pragma: no cover - must never run
        raise AssertionError("FixtureClient must not hit transport")

    def _now(self):
        return int(self.clock())

    def events_by_tag(self, tag_id, *, limit=100):
        self.calls += 1
        self.log.append(("events_by_tag", (tag_id,)))
        return [FX["event"]] if int(tag_id) == 1330 else []

    def public_search(self, query, *, limit_per_type=10):
        self.calls += 1
        self.log.append(("public_search", (query,)))
        return []

    def trades(self, condition_ids, *, min_cash=250.0, limit=500, taker_only=False):
        self.calls += 1
        ids = list(condition_ids)
        self.log.append(("trades", (tuple(ids), min_cash, taker_only)))
        assert len(ids) <= 20
        rows = [r for r in FX["market_trades"] if r["condition_id"] in ids and r["timestamp"] <= self._now()
                and fill_notional(r) >= min_cash and (taker_only is False)]
        return sorted(rows, key=lambda r: -r["timestamp"])[:limit]

    def user_stats(self, wallet):
        self.calls += 1
        self.log.append(("user_stats", (wallet,)))
        return dict(FX["user_stats"]) if wallet == WALLET else {"proxy_wallet": wallet, "trades": 40, "join_date": 1700000000}

    def activity(self, wallet, condition_id, *, start=None, limit=500):
        self.calls += 1
        self.log.append(("activity", (wallet, condition_id)))
        src = FX["wallet_activity"] if wallet == WALLET else [
            {**r, "type": "TRADE", "usdc_size": fill_notional(r)} for r in FX["market_trades"] if r["proxy_wallet"] == wallet
        ]
        return [r for r in src if r["timestamp"] <= self._now() and r.get("condition_id") == condition_id]

    def midpoints(self, token_ids):
        self.calls += 1
        self.log.append(("midpoints", (len(list(token_ids)),)))
        past = [r for r in FX["market_trades"] if r["timestamp"] <= self._now()]
        if not past:
            return {}
        last = max(past, key=lambda r: r["timestamp"])
        yes = last["price"] if last["outcome"] == "Yes" else 1 - last["price"]
        return {YES_TOKEN: round(yes, 4)}

    def book(self, token_id):
        self.calls += 1
        self.log.append(("book", (token_id,)))
        return {"asks": FX["asks_after_cluster"], "bids": []}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _monitor(tmp_path, clock, **kw):
    sent: list[str] = []
    mon = PolymarketMonitor(
        client=FixtureClient(clock),
        state=MonitorState(tmp_path / "pm_state.json"),
        config={"polymarket": {}},
        sender=lambda text: sent.append(text) or {"ok": True},
        clock=clock,
        **kw,
    )
    return mon, sent


# --------------------------------------------------------------------------
# Jensen Huang Nov 22, 2026 replay (acceptance)
# --------------------------------------------------------------------------
def _replay(tmp_path, start, end, step=60):
    clock = Clock(start)
    mon, sent = _monitor(tmp_path, clock)
    fired = []
    t = start
    while t <= end:
        clock.t = t
        for a in mon.poll_once(t):
            fired.append((t, a))
        t += step
    return mon, sent, fired


def test_jensen_fixture_matches_the_public_record():
    fills = [r for r in FX["wallet_activity"] if r["type"] == "TRADE" and r["condition_id"] == COND
             and FILL_1 <= r["timestamp"] <= FILL_4]
    big = [r for r in fills if r["usdc_size"] > 100]
    assert [r["timestamp"] for r in big] == [1795396751, 1795398530, 1795398871, 1795400900]
    assert sum(r["size"] for r in big) == pytest.approx(71517.8, abs=0.1)
    assert sum(r["size"] * r["price"] for r in big) == pytest.approx(4648.66, abs=0.01)
    assert all(r["price"] == pytest.approx(0.065) for r in big)
    # The public user-stats record: joined ~98h before the last fill, 3 distinct markets
    # (all three are NVIDIA CEO-succession markets). The "81h" in the article is age since first trade.
    assert (FILL_4 - FX["user_stats"]["join_date"]) / 3600 == pytest.approx(98.7, abs=0.1)
    assert (FILL_4 - min(r["timestamp"] for r in FX["wallet_activity"])) / 3600 == pytest.approx(81.5, abs=0.1)
    assert FX["user_stats"]["trades"] == 3
    # ljhggk was the maker on every fill: the default taker_only feed hides it.
    takers = {r["proxy_wallet"] for r in FX["market_trades"] if r["timestamp"] in (FILL_1, FILL_4)}
    assert WALLET in takers  # fixture is the taker_only=false feed


def test_jensen_replay_tier_a_fires_and_updates_89_minutes_before_trump(tmp_path, capsys):
    mon, sent, fired = _replay(tmp_path, FILL_1 - 3600, FILL_4 + 600)
    wallet_alerts = [(t, a) for t, a in fired if a["kind"] == "wallet"]
    assert [a["tier"] for _, a in wallet_alerts] == ["A", "A"]
    (t1, first), (t2, update) = wallet_alerts
    # First alert on the first poll after the 7:19 PM CT $1,565 fill.
    assert FILL_1 <= t1 < FILL_1 + 60
    assert first["cluster_usd"] == pytest.approx(1565.19, abs=0.01)
    assert first["escalation"] is None
    # Update on the first poll after the 8:28 PM CT fill: cluster ~3x the first alert.
    assert FILL_4 <= t2 < FILL_4 + 60
    assert update["escalation"].startswith("cluster grew 3.0x")
    assert (TRUMP_POST - t2) / 60 == pytest.approx(88.7, abs=1.0)  # ~89 min before Trump's post
    assert (TRUMP_POST - t1) / 60 > 155
    # 4 big fills = 71,517.8 sh / $4,648.66, plus one $3.28 fill (50.4 sh) at 7:49 PM CT in the public record.
    assert update["cluster_usd"] == pytest.approx(4651.93, abs=0.02)
    assert update["cluster_shares"] == pytest.approx(71568.22, abs=0.05)
    assert update["n_fills"] == 5
    assert update["avg_price"] == pytest.approx(0.065, abs=1e-4)
    assert update["wallet_age_days"] == pytest.approx(4.11, abs=0.01)
    assert update["distinct_markets"] == 3
    assert update["condition_share"] == pytest.approx(0.742, abs=0.002)
    assert update["absorption_pct"] == pytest.approx(34.0, abs=0.1)
    assert update["jump_x"] == pytest.approx(7.07, abs=0.01)
    assert update["fresh"] and update["concentrated"] and update["longshot"] and update["off_hours"]
    assert update["score"] >= 80
    assert update["ticker"] == "NVDA"
    assert update["priority"] == "high"
    # Both alerts were sent; 7:48 and 7:54 PM fills were deduped (cluster < 2x).
    assert len(sent) == 2
    text = update["text"]
    print("\n--- Jensen replay: Tier A update at", update["ts_ct"], "---\n" + text)
    print("\n--- Jensen replay: first Tier A at", first["ts_ct"], "---\n" + first["text"])
    assert text.splitlines()[0] == f"🛰️ {ALERT_LABEL}"
    assert "Unusual Polymarket flow — review" in text
    assert "Tier A — Fresh-wallet longshot" in text
    assert "Will Jensen Huang rejoin NVIDIA by December 31, 2026?" in text
    assert "Ticker: NVDA" in text
    assert "BUY YES @ 6.5¢ · $4,651.93 · 71,568.2 shares · 5 fills in 2h (7:19 PM–8:28 PM CT)" in text
    assert "Wallet: ljhggk (0xdf86…06bb) · joined 4.1d ago · 3 markets · 74% of volume here" in text
    assert "Ask-book absorption: 34%" in text
    assert "conviction 7.1x prior max fill" in text
    assert "off-hours/weekend" in text
    assert "Bullflow: not configured" in text
    assert "not proof of insider trading" in text
    assert "https://polymarket.com/event/jensen-huang-rejoins-nvidia-byptptpt" in text
    assert "Sure Winner" not in text and "insider trading confirmed" not in text.lower()


def test_jensen_replay_exact_alert_text(tmp_path):
    _, _, fired = _replay(tmp_path, FILL_4 - 120, FILL_4 + 60)
    # Starting at 8:26 PM the 7:19 cluster is already in the 2h window, so the 8:28 poll is the first alert.
    (t, a), = [(t, a) for t, a in fired if a["kind"] == "wallet"]
    assert a["escalation"] is None
    expected = "\n".join([
        "🛰️ Unusual Polymarket flow — review",
        f"Tier A — Fresh-wallet longshot · score {a['score']}/100 · HIGH priority",
        "Will Jensen Huang rejoin NVIDIA by December 31, 2026?",
        "Ticker: NVDA",
        "BUY YES @ 6.5¢ · $4,651.93 · 71,568.2 shares · 5 fills in 2h (7:19 PM–8:28 PM CT)",
        "Wallet: ljhggk (0xdf86…06bb) · joined 4.1d ago · 3 markets · 74% of volume here",
        "Ask-book absorption: 34%",
        "Odds 1h: n/a (mid now 6.5¢)",
        "Why: " + " · ".join(a["reasons"]),
        "Bullflow: not configured",
        "Alert only — no orders placed. Pattern match, not proof of insider trading.",
        "https://polymarket.com/event/jensen-huang-rejoins-nvidia-byptptpt",
    ])
    assert a["text"] == expected
    assert a["reasons"][:3] == ["fresh wallet 4.1d", "concentrated 3 mkts / 74% here", "longshot 6.5¢"]


def test_replay_api_calls_are_bounded_and_public_only(tmp_path):
    mon, _, _ = _replay(tmp_path, FILL_1 - 600, FILL_4 + 120)
    kinds = {k for k, _ in mon.client.log}
    assert kinds <= {"events_by_tag", "public_search", "trades", "user_stats", "activity", "midpoints", "book"}
    polls = mon.polls
    trades_calls = sum(1 for k, _ in mon.client.log if k == "trades")
    assert trades_calls == polls  # one batch of <= 20 conditions per poll
    assert all(args[2] is False for k, args in mon.client.log if k == "trades")  # makers included


# --------------------------------------------------------------------------
# Universe filter
# --------------------------------------------------------------------------
def _event(question, yes="0.10", closed=False, slug="ev", title=None):
    return {
        "id": "1", "slug": slug, "title": title or question, "closed": False,
        "markets": [{
            "question": question, "conditionId": "0x" + str(abs(hash(question)))[:10].ljust(64, "0"),
            "slug": "m", "clobTokenIds": '["1", "2"]', "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps([yes, str(round(1 - float(yes), 3))]), "active": True, "closed": closed,
        }],
    }


@pytest.mark.parametrize("q", [
    "What will NVIDIA (NVDA) hit this week?",
    "Will NVDA close above $180 on November 28?",
    "NVIDIA price range on Friday: $170-$175?",
    "Tesla up or down on November 24?",
])
def test_universe_rejects_price_bins(q):
    assert reject_reason(q, 0.2) == "price-bin"


@pytest.mark.parametrize("q", ["Will Elon Musk and his girlfriend get married in 2026?", "Will Tim Cook tweet about Vision Pro?"])
def test_universe_rejects_gossip(q):
    assert reject_reason(q, 0.2) == "gossip"


def test_universe_rejects_near_certain_and_accepts_corporate_binaries():
    assert reject_reason("Will Tim Cook step down as Apple CEO in 2026?", 0.93) == "near-certain"
    for q in (
        "Will Jensen Huang rejoin NVIDIA by December 31, 2026?",
        "Will Microsoft acquire Discord in 2026?",
        "Kraken IPO by June 30, 2027?",
        "Will the FDA approve retatrutide by March 31?",
        "Will the DOJ indict Sam Bankman-Fried's co-founder?",
        "Will Spirit Airlines file for Chapter 11 again?",
        "Will NVIDIA beat earnings estimates?",
    ):
        assert reject_reason(q, 0.065) is None, q
    assert reject_reason("Will it rain in Chicago tomorrow?", 0.3) == "not a corporate catalyst"


def test_markets_from_events_maps_tickers_and_skips_closed():
    events = [FX["event"], _event("Will Tim Cook step down as Apple CEO in 2026?", "0.12"),
              _event("Will Satya Nadella resign?", "0.95"), _event("Will Tesla acquire xAI?", "0.2", closed=True)]
    markets, rejects = markets_from_events(events)
    by_q = {m.question: m for m in markets}
    assert by_q["Will Jensen Huang rejoin NVIDIA by December 31, 2026?"].ticker == "NVDA"
    assert by_q["Will Tim Cook step down as Apple CEO in 2026?"].ticker == "AAPL"
    assert rejects == {"near-certain": 1, "closed": 1}
    jensen = by_q["Will Jensen Huang rejoin NVIDIA by December 31, 2026?"]
    assert jensen.token_ids[0] == YES_TOKEN and jensen.outcome_for_token(YES_TOKEN) == "Yes"


def test_map_ticker_names_and_focus():
    assert map_ticker("Will Elon Musk leave Tesla?") == "TSLA"
    assert map_ticker("Satya Nadella out as CEO?") == "MSFT"
    assert map_ticker("Will IREN be acquired?", focus=["IREN"]) == "IREN"
    assert map_ticker("Will Acme Corp be acquired?") is None
    assert map_ticker("Will AI be charged with a crime before 2027?", focus=["AI", "IPO"]) is None
    assert map_ticker("Will the IPO price above range?", focus=["IPO"]) is None


# --------------------------------------------------------------------------
# Math & signals
# --------------------------------------------------------------------------
def test_notional_is_size_times_price_and_json_lists():
    assert fill_notional({"size": 39833.037046, "price": 0.065}) == pytest.approx(2589.147, abs=1e-3)
    assert fill_notional({"size": None, "price": 0.5}) == 0
    assert parse_json_list('["Yes", "No"]') == ["Yes", "No"] and parse_json_list("bad") == []


def test_ask_absorption_pct():
    asks = {"asks": [{"price": "0.07", "size": "500"}, {"price": "0.066", "size": "500"}, {"price": "0.2", "size": "9999"}]}
    assert ask_absorption(asks, shares_bought=1000, exec_price=0.065) == pytest.approx(0.5)
    assert ask_absorption(None, shares_bought=1000, exec_price=0.065) is None
    assert ask_absorption({"asks": []}, shares_bought=10, exec_price=0.1) == pytest.approx(1.0)


def _act(ts, usd, price=0.05, token="t1", cond="c1"):
    return {"type": "TRADE", "side": "BUY", "timestamp": ts, "price": price, "size": usd / price,
            "usdc_size": usd, "token_id": token, "condition_id": cond, "outcome": "Yes", "name": "w"}


NOW = 1795400900


def test_null_join_date_is_unknown_not_fresh():
    sig = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                          activity=[_act(NOW - 60, 5000)], user_stats={"join_date": None, "trades": 1})
    assert sig.wallet_age_days is None and sig.fresh is False and sig.tier is None
    assert "wallet age unknown" in sig.reasons and sig.parts["freshness"] == 0


def test_wallet_baseline_conviction_jump():
    prior = [_act(NOW - 86_400 + i, 10) for i in range(5)] + [_act(NOW - 50_000, 200)]
    sig = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                          activity=prior + [_act(NOW - 100, 1500)], user_stats={"join_date": NOW - 86_400 * 2, "trades": 1})
    assert sig.prior_max_usd == 200 and sig.jump_x == pytest.approx(7.5)
    assert sig.parts["conviction"] == 10 and "conviction 7.5x prior max fill" in sig.reasons
    first = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                            activity=[_act(NOW - 100, 1500)], user_stats={"join_date": NOW - 86_400 * 2, "trades": 1})
    assert first.jump_x is None and first.parts["conviction"] == 3


def test_market_relative_outlier():
    pct, z = market_outlier(5000, [250, 300, 300, 400, 500, 500, 600, 800, 900, 1000, 1200, 1500])
    assert pct == 100.0 and z > 5
    assert market_outlier(5000, [300] * 5) == (None, None)
    sig = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW, activity=[_act(NOW - 60, 5000)],
                          user_stats={"join_date": NOW - 86_400, "trades": 1},
                          market_fills_usd=[250, 300, 300, 400, 500, 500, 600, 800, 900, 1000, 1200, 1500])
    assert sig.parts["market_outlier"] == 10


def test_off_hours():
    assert is_off_hours(FILL_4)  # Sunday night
    weekday_mid = int(datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc).timestamp())  # Tue 11:00 ET
    assert not is_off_hours(weekday_mid)
    assert is_off_hours(int(datetime(2026, 11, 24, 21, 5, tzinfo=timezone.utc).timestamp()))  # Tue 4:05 PM ET


def test_tier_a_requires_fresh_concentrated_longshot_and_1k():
    fresh = {"join_date": NOW - 86_400 * 3, "trades": 2}
    a = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW, activity=[_act(NOW - 600, 1200)], user_stats=fresh)
    assert a.tier == "A" and a.tier_label == "Tier A — Fresh-wallet longshot"
    small = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW, activity=[_act(NOW - 600, 900)], user_stats=fresh)
    assert small.tier is None
    old = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW, activity=[_act(NOW - 600, 1200)],
                          user_stats={"join_date": NOW - 86_400 * 40, "trades": 2})
    assert old.tier is None
    spread = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW, activity=[_act(NOW - 600, 1200)],
                             user_stats={"join_date": NOW - 86_400 * 3, "trades": 25, "volume_usdc": 90_000})
    assert spread.tier is None
    pricey = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                             activity=[_act(NOW - 600, 1200, price=0.35)], user_stats=fresh)
    assert pricey.tier is None
    stale = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                            activity=[_act(NOW - 7_300, 1200)], user_stats=fresh)
    assert stale.tier is None  # outside the 2h cluster window


def test_tier_b_whale_any_wallet_age():
    sig = evaluate_wallet(wallet="w", condition_id="c1", token_id="t1", now_ts=NOW,
                          activity=[_act(NOW - 600, 6000, price=0.12), _act(NOW - 300, 6000, price=0.12)],
                          user_stats={"join_date": NOW - 86_400 * 400, "trades": 300})
    assert sig.tier == "B" and sig.tier_label == "Tier B — Whale longshot"


def test_tier_b_odds_spike():
    hist = [[NOW - 3_000, 0.08], [NOW - 2_400, 0.08], [NOW - 600, 0.10]]
    spike = check_odds_spike(hist, now_ts=NOW, mid_now=0.14, recent_volume_usd=5_000)
    assert spike["jump"] == pytest.approx(0.06) and spike["jump_pct"] == pytest.approx(75.0)
    assert check_odds_spike(hist, now_ts=NOW, mid_now=0.14, recent_volume_usd=500) is None  # not unusual volume
    assert check_odds_spike(hist, now_ts=NOW, mid_now=0.11, recent_volume_usd=5_000) is None  # +3c
    assert check_odds_spike([[NOW - 2_400, 0.40]], now_ts=NOW, mid_now=0.46, recent_volume_usd=5_000) is None  # +15%


def test_odds_spike_alert_and_condition_dedupe(tmp_path):
    clock = Clock(FILL_4)
    mon, sent = _monitor(tmp_path, clock)
    mon.set_universe(markets_from_events([FX["event"]])[0], FILL_4)
    mon.state.mids[YES_TOKEN] = [[FILL_4 - 2_400, 0.065], [FILL_4 - 1_800, 0.066]]
    rows = {COND: [{"timestamp": FILL_4 - 100, "size": 50_000, "price": 0.13, "proxy_wallet": "x"}]}
    fired = mon._scan_spikes(rows, {YES_TOKEN: 0.14}, FILL_4)
    assert len(fired) == 1 and fired[0]["tier_label"] == "Tier B — Odds spike"
    assert "Odds spike YES: 6.5¢ → 14.0¢" in fired[0]["text"]
    assert mon._scan_spikes(rows, {YES_TOKEN: 0.15}, FILL_4 + 120) == []
    later = FILL_4 + 86_400 + 1
    mon.state.mids[YES_TOKEN] = [[later - 2_400, 0.065]]
    rows = {COND: [{"timestamp": later - 100, "size": 50_000, "price": 0.13, "proxy_wallet": "x"}]}
    assert mon._scan_spikes(rows, {YES_TOKEN: 0.15}, later)  # 24h later it may fire again
    assert len(sent) == 2


def test_24h_wallet_dedupe_and_escalation_cap(tmp_path):
    mon, _ = _monitor(tmp_path, Clock(NOW))
    key = "w|x|c"
    assert mon._dedupe_decision(key, 1000, NOW) == ""
    mon.state.dedupe[key] = {"ts": NOW, "usd": 1000, "escalations": 0}
    assert mon._dedupe_decision(key, 1500, NOW + 60) is None
    assert mon._dedupe_decision(key, 2100, NOW + 60).startswith("cluster grew 2.1x")
    mon.state.dedupe[key] = {"ts": NOW, "usd": 4000, "escalations": 2}
    assert mon._dedupe_decision(key, 50_000, NOW + 60) is None  # capped
    assert mon._dedupe_decision(key, 1000, NOW + 86_400) == ""  # expired


def test_state_persists_bounded_and_survives_read_only(tmp_path):
    st = MonitorState(tmp_path / "s.json")
    for i in range(6_000):
        st.mark_seen(f"k{i}")
    for i in range(200):
        st.record_mid("tok", NOW - 200 * 60 + i * 60, 0.1)
    st.dedupe["w|a|b"] = {"ts": NOW, "usd": 1.0}
    st.cursors["c"] = NOW
    assert st.save()
    again = MonitorState(tmp_path / "s.json").load()
    assert len(again.seen) == 5_000 and again.is_seen("k5999") and not again.is_seen("k0")
    assert len(again.mids["tok"]) <= 120 and again.cursors["c"] == NOW and "w|a|b" in again.dedupe
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ro = MonitorState(blocker / "nested" / "state.json")
    assert ro.save() is False and ro.read_only


def test_relative_state_path_lands_under_repo_outputs(monkeypatch):
    monkeypatch.delenv("POLYMARKET_STATE_PATH", raising=False)
    st = MonitorState("outputs/polymarket_monitor_state.json")
    assert st.path == pm_monitor.ROOT / "outputs" / "polymarket_monitor_state.json"


# --------------------------------------------------------------------------
# Bullflow: optional single REST call, never SSE
# --------------------------------------------------------------------------
def test_bullflow_confirmation_single_rest_call_no_sse(monkeypatch):
    from odte_scanner.signals import bullflow

    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key-not-real")
    calls = []

    def fake_fetch(**kw):
        calls.append(kw)
        return {"ok": True, "data": [
            {"premium": 2_400_000, "side": "Ask", "optionType": "C"},
            {"premium": 1_100_000, "side": "Above Ask", "optionType": "C"},
            {"premium": 3_000_000, "side": "Bid", "optionType": "C"},
            {"premium": 1_500_000, "side": "Ask", "optionType": "P"},
            {"premium": 400_000, "side": "Ask", "optionType": "C"},
        ]}

    def no_sse(*a, **k):
        raise AssertionError("Bullflow SSE must never be opened by the Polymarket monitor")

    monkeypatch.setattr(bullflow.BullflowTape, "start", no_sse)
    monkeypatch.setattr(bullflow, "start_bullflow_worker", no_sse)
    res = bullflow_confirmation("NVDA", "call", FILL_4, fetch=fake_fetch)
    assert len(calls) == 1 and calls[0]["max_pages"] == 1 and calls[0]["ticker"] == "NVDA"
    assert calls[0]["date"] == "2026-11-20"  # Sunday rolls back to Friday's session
    assert res["confirmed"] and res["same_direction"] == 2 and res["puts_1m"] == 1
    assert "test-key-not-real" not in json.dumps(res)


def test_bullflow_real_fetch_makes_exactly_one_request(monkeypatch):
    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key-not-real")
    from odte_scanner.signals import bullflow

    class Resp:
        status_code = 429
        headers = {"Retry-After": "1"}

    class Sess:
        n = 0

        def get(self, url, **kw):
            Sess.n += 1
            assert "streaming" not in url and kw["headers"]["X-API-Key"] == "test-key-not-real"
            return Resp()

    sess = Sess()
    res = bullflow_confirmation("NVDA", "call", FILL_4, fetch=lambda **kw: bullflow.fetch_historical_options_flow(
        session=sess, sleep=lambda s: None, **kw))
    assert Sess.n == 1 and res["checked"] is False  # fail open


def test_bullflow_fails_open_and_skips_without_key(monkeypatch):
    assert bullflow_confirmation("NVDA", "call", FILL_4, fetch=lambda **k: 1 / 0)["note"] == "not configured"
    monkeypatch.setenv("BULLFLOW_API_KEY", "k")
    res = bullflow_confirmation("NVDA", "call", FILL_4, fetch=lambda **k: (_ for _ in ()).throw(TimeoutError()))
    assert res["checked"] is False and "unavailable" in res["note"]
    assert bullflow_confirmation(None, "call", FILL_4)["checked"] is False


def test_jensen_replay_with_bullflow_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("BULLFLOW_API_KEY", "test-key-not-real")
    calls = []
    clock = Clock(FILL_4)
    mon, sent = _monitor(tmp_path, clock, bullflow_fetch=lambda **kw: calls.append(kw) or {
        "ok": True, "data": [{"premium": 1_070_000, "side": "Ask", "optionType": "C"}]})
    fired = mon.poll_once(FILL_4 + 30)
    (a,) = [x for x in fired if x["kind"] == "wallet"]
    assert len(calls) == 1
    assert a["bullflow"]["confirmed"] and "Bullflow: 1 same-direction $1M+ call sweep(s) on NVDA" in a["text"]
    assert "test-key-not-real" not in a["text"]


def test_implied_direction():
    assert implied_direction("Will Jensen Huang rejoin NVIDIA by December 31, 2026?", "Yes") == "call"
    assert implied_direction("Will Jensen Huang rejoin NVIDIA by December 31, 2026?", "No") == "put"
    assert implied_direction("Will Tim Cook resign as Apple CEO?", "Yes") == "put"
    assert implied_direction("New NVIDIA CEO named by 2026?", "Yes") is None


# --------------------------------------------------------------------------
# Alert-only isolation, copy rules, wiring
# --------------------------------------------------------------------------
def test_polymarket_alerts_never_place_orders(tmp_path, monkeypatch):
    import odte_scanner.trading.webull as webull

    placed = []
    for name in dir(webull):
        obj = getattr(webull, name)
        if callable(obj) and any(w in name.lower() for w in ("place", "order", "submit", "buy", "sell")):
            monkeypatch.setattr(webull, name, lambda *a, _n=name, **k: placed.append(_n))
    _, sent, fired = _replay(tmp_path, FILL_1 - 120, FILL_4 + 120)
    assert fired and sent and placed == []
    import re

    src = "\n".join(p.read_text() for p in (Path(pm_monitor.__file__).parent).glob("*.py"))
    imports = "\n".join(re.findall(r"^\s*(?:from|import)\s+\S+.*$", src, re.MULTILINE)).lower()
    for banned in ("trading", "webull", "sniper", "desks", "live_desk", "fast_exits", "challenge"):
        assert banned not in imports, banned
    for banned in ("place_order", "submit_order", "paper_buy", "open_trade("):
        assert banned not in src, banned
    assert all(a["alert_only"] for _, a in fired)


def test_copy_rules_label_and_no_sure_winner(tmp_path):
    _, _, fired = _replay(tmp_path, FILL_4 - 60, FILL_4 + 60)
    for _, a in fired:
        assert a["label"] == "Unusual Polymarket flow — review"
        assert a["text"].startswith("🛰️ Unusual Polymarket flow — review")
    src = "\n".join(p.read_text() for p in Path(pm_monitor.__file__).parent.glob("*.py"))
    assert "Sure Winner" not in src and "sure winner" not in src.lower()
    from odte_scanner import ui

    start = ui.PAGE.index("function renderPolymarket()")
    block = ui.PAGE[start:ui.PAGE.index("function renderNewsDesk()")]
    assert "Sure Winner" not in block and "Unusual Polymarket flow — review" in block


def test_monitor_toggle_and_worker_off_in_tests(monkeypatch):
    assert monitor_enabled(None) is False  # conftest sets POLYMARKET_MONITOR_ENABLED=0
    assert pm_monitor.start_polymarket_worker(None) is False and pm_monitor.get_monitor() is None
    monkeypatch.delenv("POLYMARKET_MONITOR_ENABLED")
    assert monitor_enabled(None) is True  # on by default in prod
    assert monitor_enabled({"polymarket": {"enabled": False}}) is False
    monkeypatch.setenv("SIGNAL_DESK_OFFLINE", "1")
    assert pm_monitor.start_polymarket_worker(None) is False


def test_poll_errors_are_recorded_not_raised(tmp_path):
    class Boom(FixtureClient):
        def midpoints(self, token_ids):
            from odte_scanner.polymarket.client import PolymarketError

            raise PolymarketError("HTTP 429 rate limited")

    clock = Clock(FILL_4)
    mon = PolymarketMonitor(client=Boom(clock), state=MonitorState(tmp_path / "s.json"), config={}, sender=lambda t: None, clock=clock)
    assert mon.poll_once(FILL_4) == []
    st = mon.status()
    assert st["last_error"] == "HTTP 429 rate limited" and st["ok"] is False and st["polls"] == 1


def test_client_transport_parses_v2_envelope_and_batches():
    class R:
        def __init__(self, body, status=200):
            self.body, self.status_code = body, status

        def json(self):
            return self.body

    class S:
        def __init__(self):
            self.seen = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.seen.append(("GET", url, params))
            if url.endswith("/v2/trades"):
                return R({"data": [{"size": 10, "price": 0.5}], "pagination": {}})
            if url.endswith("/v2/user-stats"):
                return R({"data": {"join_date": None, "trades": 1}})
            return R({"asks": []})

        def post(self, url, json=None, headers=None, timeout=None):
            self.seen.append(("POST", url, len(json)))
            return R({str(x["token_id"]): "0.0595" for x in json})

    s = S()
    c = PolymarketClient(session=s)
    assert c.trades([f"c{i}" for i in range(30)]) == [{"size": 10, "price": 0.5}]
    _, url, params = s.seen[0]
    assert len(params["condition"].split(",")) == 20 and params["taker_only"] == "false"
    assert params["filter_type"] == "CASH" and params["filter_amount"] == 250.0
    assert c.user_stats("0xabc")["join_date"] is None
    mids = c.midpoints([str(i) for i in range(150)])
    assert len(mids) == 150 and mids["0"] == pytest.approx(0.0595)
    assert [x for x in s.seen if x[0] == "POST"] == [("POST", "https://clob.polymarket.com/midpoints", 100),
                                                    ("POST", "https://clob.polymarket.com/midpoints", 50)]


def test_snapshot_and_scan_status_expose_polymarket(tmp_path, monkeypatch):
    from odte_scanner import ui

    clock = Clock(FILL_4 + 30)
    mon, _ = _monitor(tmp_path, clock)
    mon.poll_once(FILL_4 + 30)
    monkeypatch.setattr(pm_monitor, "_MONITOR", mon)
    body = ui._overlay_live({"generated_at": "x"})
    pm = body["polymarket"]
    for key in ("enabled", "universe_count", "last_poll", "last_error", "recent_alerts", "alert_only", "label"):
        assert key in pm
    assert pm["universe_count"] == 1 and pm["recent_alerts"][0]["tier"] == "A" and pm["alert_only"] is True
    app = ui.create_app()
    st = app.test_client().get("/api/scan_status").get_json()
    assert st["polymarket"]["universe_count"] == 1 and st["polymarket"]["label"] == ALERT_LABEL
    assert 'id="polymarketBanner"' in ui.PAGE and "renderPolymarket()" in ui.PAGE


def test_snapshot_block_when_monitor_not_running(monkeypatch):
    from odte_scanner import ui

    monkeypatch.setattr(pm_monitor, "_MONITOR", None)
    pm = ui._overlay_live({})["polymarket"]
    assert pm["enabled"] is False and pm["running"] is False and pm["recent_alerts"] == []


def test_live_desk_starts_polymarket_worker_alongside_others():
    src = Path(pm_monitor.__file__).parents[1].joinpath("live_desk.py").read_text()
    assert "start_polymarket_worker(config_path)" in src
