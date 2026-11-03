"""Public scorecard Daily P&L from Webull paper fills."""

from __future__ import annotations

from datetime import datetime, timezone

from odte_scanner.hot_state import apply_hot_overlay
from odte_scanner.scorecard.daily_pnl import PAPER_LABEL, build_scorecard_daily_pnl
from odte_scanner.scorecard.page import render_scorecard_page
from odte_scanner.trading.portal_book import reconcile_pnl, round_trips
from odte_scanner.ui import PAGE

NOW = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)


def _order(**kwargs):
    base = {
        "id": kwargs.get("id", "id"),
        "desk": "sniper",
        "action": "BUY",
        "symbol": "SPY",
        "right": "C",
        "contract": "SPY",
        "expiry": "2026-11-02",
        "strike": 760,
        "quantity": 1,
        "status": "filled",
        "fill_price": 1.0,
        "created_at": "2026-11-02T15:00:00+00:00",
        "updated_at": "2026-11-02T15:00:00+00:00",
        "meta": {},
    }
    meta = dict(base["meta"])
    meta.update(kwargs.pop("meta", {}) or {})
    base.update(kwargs)
    base["meta"] = meta
    return base


def _nov2_book() -> list[dict]:
    """Sniper +79, Diamond +22, Gold +202 carry-in, plus an afternoon fill and noise."""
    return [
        _order(
            id="buy-sniper",
            desk="sniper",
            contract="SPY261102C00760000",
            fill_price=1.80,
            updated_at="2026-11-02T14:50:15+00:00",
            meta={"golden_kind": "sniper"},
        ),
        _order(
            id="sell-sniper",
            desk="golden_exit",
            action="SELL",
            contract="SPY261102C00760000",
            fill_price=2.59,
            updated_at="2026-11-02T15:15:01+00:00",
            reason="take profit",
            meta={"golden_kind": "golden_exit"},
        ),
        _order(
            id="buy-diamond",
            desk="diamond",
            symbol="QQQ",
            contract="QQQ261102C00600000",
            strike=600,
            fill_price=1.00,
            updated_at="2026-11-02T14:40:00+00:00",
            meta={"golden_kind": "diamond"},
        ),
        _order(
            id="sell-diamond",
            desk="golden_exit",
            action="SELL",
            symbol="QQQ",
            contract="QQQ261102C00600000",
            strike=600,
            fill_price=1.22,
            updated_at="2026-11-02T16:10:00+00:00",
            meta={"exit_tier": "diamond"},
        ),
        _order(
            id="buy-gold",
            desk="sure_winner",
            symbol="NVDA",
            contract="NVDA261102C00100000",
            strike=100,
            fill_price=1.00,
            updated_at="2026-10-31T18:00:00+00:00",
            meta={"golden_kind": "sure_winner"},
        ),
        _order(
            id="sell-gold",
            desk="golden_exit",
            action="SELL",
            symbol="NVDA",
            contract="NVDA261102C00100000",
            strike=100,
            fill_price=3.02,
            updated_at="2026-11-02T16:30:00+00:00",
            meta={"exit_tier": "gold"},
        ),
        _order(
            id="buy-afternoon",
            desk="afternoon_puts",
            symbol="SPY",
            right="P",
            contract="SPY261102P00759000",
            strike=759,
            fill_price=1.00,
            updated_at="2026-11-02T16:00:00+00:00",
            meta={"golden_kind": "afternoon_puts"},
        ),
        _order(
            id="sell-afternoon",
            desk="golden_exit",
            action="SELL",
            symbol="SPY",
            right="P",
            contract="SPY261102P00759000",
            strike=759,
            fill_price=1.50,
            updated_at="2026-11-02T19:00:00+00:00",
            meta={"exit_tier": "afternoon"},
        ),
        _order(
            id="buy-spx",
            desk="sniper",
            symbol="SPX",
            contract="SPX261102C07000000",
            fill_price=10.0,
            updated_at="2026-11-02T15:00:00+00:00",
        ),
        _order(
            id="sell-spx",
            desk="golden_exit",
            action="SELL",
            symbol="SPX",
            contract="SPX261102C07000000",
            fill_price=12.0,
            updated_at="2026-11-02T18:00:00+00:00",
            meta={"exit_tier": "sniper"},
        ),
        _order(
            id="buy-lottery",
            desk="lottery",
            symbol="SMCI",
            contract="SMCI261102C00050000",
            fill_price=0.40,
            updated_at="2026-11-02T15:00:00+00:00",
        ),
        _order(
            id="sell-lottery",
            desk="lottery",
            action="SELL",
            symbol="SMCI",
            contract="SMCI261102C00050000",
            fill_price=0.90,
            updated_at="2026-11-02T20:00:00+00:00",
        ),
        _order(
            id="buy-open",
            desk="sniper",
            symbol="AMD",
            contract="AMD261102C00200000",
            strike=200,
            fill_price=1.00,
            updated_at="2026-11-02T17:00:00+00:00",
            meta={"golden_kind": "sniper", "mark": 1.25},
        ),
    ]


def test_nov2_matches_the_three_desk_read_and_keeps_the_rest_apart():
    board = build_scorecard_daily_pnl(
        _nov2_book(),
        now=NOW,
        spx_rows=[
            {
                "signal_type": "SPX credit",
                "signaled_at": "2026-11-02T15:00:00+00:00",
                "outcomes": {"status": "closed", "pnl": 40.0, "win": True},
            }
        ],
        spx_exits=[
            {
                "symbol": "SPX",
                "tier": "afternoon",
                "day": "2026-11-02",
                "ask": 2.0,
                "option_pct": 10.0,
            }
        ],
        journal_book={
            "trades": [
                {
                    "id": "j1",
                    "symbol": "SMCI",
                    "right": "C",
                    "dte_bucket": "lottery",
                    "status": "closed",
                    "entered_at": "2026-11-02T15:00:00+00:00",
                    "exited_at": "2026-11-02T20:00:00+00:00",
                    "entry_ask": 0.4,
                    "exit_bid": 0.9,
                    "pnl_usd": 50.0,
                    "contracts": 1,
                }
            ]
        },
    )
    day = board["days"][0]
    assert day["day"] == "2026-11-02"
    desks = day["desks"]
    assert desks["Sniper"]["pnl"] == 79.0
    assert desks["Diamond"]["pnl"] == 22.0
    assert desks["Gold Setup"]["pnl"] == 202.0
    assert desks["Afternoon Puts"]["pnl"] == 50.0
    assert round(desks["Sniper"]["pnl"] + desks["Diamond"]["pnl"] + desks["Gold Setup"]["pnl"], 2) == 303.0
    # Afternoon is a Webull fill, so the paper total is the three desks plus that fill.
    assert day["realized_pnl_usd"] == 353.0
    assert day["trades"] == 4
    assert day["wins"] == 4
    assert day["win_rate_pct"] == 100.0
    assert day["cumulative_pnl_usd"] == 353.0
    assert board["totals"]["realized_pnl_usd"] == 353.0
    assert board["spx_modeled"]["included_in_webull_total"] is False
    assert board["spx_modeled"]["realized_pnl_usd"] == 60.0
    assert board["journal"]["included_in_webull_total"] is False
    assert board["journal"]["realized_pnl_usd"] == 50.0
    assert board["journal"]["webull_sleeve_pnl_usd"] == 50.0
    assert "Sure Winner" not in str(board)
    assert board["label"] == PAPER_LABEL
    assert "aged out" in board["gap_note"]
    assert day["open_n"] == 1
    assert day["unrealized_pnl_usd"] == 25.0
    assert day["unrealized_label"] == "unrealized"
    assert board["open_positions"][0]["unrealized"] is True
    assert board["chart"][-1]["cumulative"] == 353.0


def test_exit_day_is_chicago_and_stored_entry_beats_the_buy():
    orders = [
        _order(
            id="buy",
            fill_price=1.00,
            updated_at="2026-11-02T15:00:00+00:00",
            contract="QQQ",
        ),
        _order(
            id="sell-late",
            action="SELL",
            desk="golden_exit",
            fill_price=2.00,
            contract="QQQ",
            updated_at="2026-11-03T05:30:00+00:00",
            meta={"entry_price": 1.50, "exit_tier": "sniper"},
        ),
        _order(
            id="sell-next",
            action="SELL",
            desk="golden_exit",
            symbol="IWM",
            contract="IWM",
            fill_price=2.00,
            updated_at="2026-11-03T06:30:00+00:00",
            meta={"exit_tier": "diamond"},
        ),
        _order(
            id="buy-iwm",
            desk="diamond",
            symbol="IWM",
            contract="IWM",
            fill_price=1.00,
            updated_at="2026-11-03T06:00:00+00:00",
            meta={"golden_kind": "diamond"},
        ),
    ]
    board = build_scorecard_daily_pnl(orders, now=datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc))
    by = {row["day"]: row for row in board["days"]}
    # 05:30Z is still Nov 2 in Chicago. Stored entry 1.50, not the 1.00 buy.
    assert by["2026-11-02"]["desks"]["Sniper"]["pnl"] == 50.0
    assert by["2026-11-03"]["desks"]["Diamond"]["pnl"] == 100.0
    assert board["days"][0]["day"] == "2026-11-03"
    assert by["2026-11-03"]["cumulative_pnl_usd"] == 150.0
    assert by["2026-11-02"]["cumulative_pnl_usd"] == 50.0


def test_other_webull_desk_is_in_the_total_and_journal_routes_are_not():
    orders = [
        _order(id="b", desk="golden_flow", contract="A", fill_price=1.0, meta={"golden_kind": "golden_flow"}),
        _order(
            id="s",
            action="SELL",
            desk="golden_exit",
            contract="A",
            fill_price=1.3,
            meta={"entry_price": 1.0},
        ),
    ]
    # The sell has no tier and the buy is not gold/sniper/diamond/afternoon.
    priced = round_trips(orders)
    assert priced == []
    report = reconcile_pnl(orders)
    assert report["fill_pnl"] == 0.0
    board = build_scorecard_daily_pnl(orders, now=NOW)
    assert board["totals"]["realized_pnl_usd"] == 30.0
    assert "Golden Flow" in board["desks"]


def test_multi_contract_is_noted_and_a_gap_before_oct_28_is_not_invented():
    orders = [
        _order(
            id="b",
            quantity=2,
            fill_price=1.0,
            contract="A",
            updated_at="2026-10-28T15:00:00+00:00",
        ),
        _order(
            id="s",
            action="SELL",
            desk="golden_exit",
            quantity=2,
            fill_price=1.4,
            contract="A",
            updated_at="2026-10-28T20:00:00+00:00",
            meta={"exit_tier": "sniper"},
        ),
    ]
    board = build_scorecard_daily_pnl(orders, now=NOW)
    day = board["days"][0]
    assert day["day"] == "2026-10-28"
    assert day["realized_pnl_usd"] == 80.0
    assert "more than 1 contract" in day["contracts_note"]
    assert "aged out" not in board["gap_note"]
    assert board["history_from"] == "2026-10-28"


def test_retained_log_backfills_a_dropped_sell_without_double_counting(tmp_path):
    path = tmp_path / "paper_round_trips.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"id":"sell-kept:1","sell_id":"sell-kept","source":"Gold Setup","contract":"NVDA","pnl":202.0,"at":"2026-11-02T16:30:00+00:00","qty":1}',
                '{"id":"sell-old:1","sell_id":"sell-old","source":"Sniper","contract":"QQQ","pnl":15.0,"at":"2026-10-28T18:00:00+00:00","qty":1}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    orders = [
        _order(id="buy-gold", desk="sure_winner", contract="NVDA", fill_price=1.0, updated_at="2026-10-31T18:00:00+00:00"),
        _order(
            id="sell-kept",
            action="SELL",
            desk="golden_exit",
            contract="NVDA",
            fill_price=3.02,
            updated_at="2026-11-02T16:30:00+00:00",
            meta={"exit_tier": "gold"},
        ),
    ]
    board = build_scorecard_daily_pnl(orders, now=NOW, round_trip_path=path)
    by = {row["day"]: row for row in board["days"]}
    assert by["2026-11-02"]["realized_pnl_usd"] == 202.0
    assert by["2026-10-28"]["realized_pnl_usd"] == 15.0
    assert by["2026-10-28"]["from_retained_log"] is True
    assert "2026-10-28" in board["gap_note"]
    assert board["totals"]["realized_pnl_usd"] == 217.0


def test_unpriced_sell_is_omitted_instead_of_invented():
    orders = [
        _order(id="s", action="SELL", desk="golden_exit", fill_price=2.0, contract="MISSING", meta={"exit_tier": "sniper"}),
    ]
    board = build_scorecard_daily_pnl(orders, now=NOW)
    assert board["totals"]["trades"] == 0
    assert board["totals"]["realized_pnl_usd"] == 0.0


def test_overlay_and_page_expose_the_section_without_the_banned_name(monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.scorecard.daily_pnl._load_side_files",
        lambda: ([], None, []),
    )
    body = apply_hot_overlay({"sure_winners": {"winners": []}}, orders=_nov2_book()[:2])
    assert body["pnl_reconcile"]["fill_pnl"] == 79.0
    assert body["scorecard_daily_pnl"]["totals"]["realized_pnl_usd"] == 79.0
    page = render_scorecard_page(live=False, initial={"rows": [], "summary": {}, "chain": {"ok": True}})
    assert PAPER_LABEL in page
    assert 'id="dailyPnlTable"' in page
    assert "Sure Winner" not in page
    start = PAGE.index('id="tab-scorecard"')
    section = PAGE[start : PAGE.index("<footer>", start)]
    assert PAPER_LABEL in section
    assert 'id="dailyPnlTable"' in section
    assert "Sure Winner" not in section
    assert "/api/scorecard/daily_pnl" in PAGE


def test_daily_pnl_route(monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.hot_state.read_ledger_orders",
        lambda path=None: _nov2_book()[:2],
    )
    monkeypatch.setattr(
        "odte_scanner.scorecard.daily_pnl._load_side_files",
        lambda: ([], None, []),
    )
    from odte_scanner.ui import create_app

    client = create_app().test_client()
    res = client.get("/api/scorecard/daily_pnl")
    assert res.status_code == 200
    body = res.get_json()
    assert body["days"][0]["desks"]["Sniper"]["pnl"] == 79.0
    assert body["label"] == PAPER_LABEL
    assert "Sure Winner" not in res.get_data(as_text=True)
