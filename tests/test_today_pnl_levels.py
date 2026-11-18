"""Today's P&L board, pre-market levels card, and the EOD Telegram line."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from odte_scanner.market import levels as lv
from odte_scanner.scorecard import today_pnl as tp


def _order(action, contract, price, at, desk="sniper", status="filled", qty=1):
    return {
        "action": action,
        "contract": contract,
        "symbol": contract[:3],
        "fill_price": price,
        "status": status,
        "desk": desk,
        "quantity": qty,
        "created_at": at,
        "updated_at": at,
        "right": "call",
        "strike": 100,
        "expiry": "2026-11-20",
    }


NOW = datetime(2026, 11, 17, 21, 30, tzinfo=timezone.utc)  # 3:30 PM CT


def _body():
    return {
        "million_challenge": {
            "realized_today": 108.25,
            "equity": 1108.25,
            "open": [],
            "closed": [
                {"pnl": 108.25, "exit_at": "2026-11-17T11:25:54-06:00", "entry_at": "2026-11-17T11:22:45-06:00"}
            ],
        },
        "kotegawa": {
            "realized_today": 0.0,
            "unrealized_pnl": -100.0,
            "open": [{"symbol": "NVDA", "entry_at": "2026-11-17T14:51:00-06:00", "unrealized_pnl": -100.0}],
            "closed": [],
        },
        "daily_pnl": {
            "by_day": [
                {"day": "2026-11-17", "trades": [{"category": "weekly", "pnl_usd": -15.0}, {"category": "lottery", "pnl_usd": 40.0}]}
            ],
            "open": [],
        },
        "news_desk": {"day": "2026-11-17", "sent_today": 8, "n_events": 48},
        "squeeze_fire": {
            "day": "2026-11-17",
            "today": [{"signal_ask": 1.0, "marks": {"+60m": {"bid": 1.5}, "2:55 PM": {"bid": 0.8}}}],
        },
        "power_hour": {"counts": {"long": 2, "short": 1}},
        "scorecard_daily_pnl": {"spx_modeled": {"days": [{"day": "2026-11-17", "realized_pnl_usd": 3250.0, "trades": 3}]}},
    }


def _book():
    return [
        _order("BUY", "AAA261120C00100000", 1.0, "2026-11-17T15:00:00+00:00", desk="sniper"),
        _order("SELL", "AAA261120C00100000", 2.0, "2026-11-17T16:00:00+00:00", desk="sniper"),
        _order("BUY", "BBB261120C00100000", 2.0, "2026-11-17T15:10:00+00:00", desk="sure_winner"),
        _order("SELL", "BBB261120C00100000", 1.5, "2026-11-17T17:00:00+00:00", desk="sure_winner"),
        # yesterday's exit is not today's P&L
        _order("BUY", "CCC261120C00100000", 1.0, "2026-11-16T15:00:00+00:00", desk="sniper"),
        _order("SELL", "CCC261120C00100000", 3.0, "2026-11-16T16:00:00+00:00", desk="sniper"),
        # still open
        _order("BUY", "DDD261120C00100000", 1.2, "2026-11-17T18:00:00+00:00", desk="sniper"),
        # journal sleeve on Webull stays out of the Webull total
        _order("BUY", "EEE261120C00100000", 1.0, "2026-11-17T15:00:00+00:00", desk="challenge"),
        _order("SELL", "EEE261120C00100000", 0.5, "2026-11-17T16:00:00+00:00", desk="challenge"),
    ]


def test_today_pnl_groups_stay_separate(tmp_path: Path):
    log = tmp_path / "desk_alert_log.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"time": "2026-11-17T17:29:29+00:00", "kind": "Million", "ok": True},
                {"time": "2026-11-17T17:29:30+00:00", "kind": "Million", "ok": True},
                {"time": "2026-11-17T15:00:00+00:00", "kind": "Golden Flow", "ok": True},
                {"time": "2026-11-17T15:00:00+00:00", "kind": "Golden Flow", "ok": False},
                {"time": "2026-11-16T15:00:00+00:00", "kind": "Sniper", "ok": True},
            ]
        )
    )
    board = tp.build_today_pnl(_body(), _book(), now=NOW, alert_log=log, fetch_marks=False)
    web = {r["name"]: r for r in board["webull"]["rows"]}
    assert web["Sniper"]["realized"] == 100.0 and web["Sniper"]["trades"] == 1
    assert web["Sniper"]["open_n"] == 1 and web["Sniper"]["unrealized"] is None
    assert web["Gold Setup"]["realized"] == -50.0 and web["Gold Setup"]["losses"] == 1
    assert "Challenge" not in web
    assert board["webull"]["total"]["realized"] == 50.0
    inner = {r["name"]: r for r in board["internal"]["rows"]}
    assert inner["Million"]["realized"] == 108.25 and inner["Million"]["wins"] == 1
    assert inner["Million"]["alerts"] == 2
    assert inner["Kotegawa"]["unrealized"] == -100.0 and inner["Kotegawa"]["entries_today"] == 1
    assert inner["Weekly"]["realized"] == -15.0 and inner["Lottery"]["realized"] == 40.0
    assert "Challenge" in inner
    assert board["internal"]["total"]["realized"] == 133.25
    alerts = {r["name"]: r for r in board["alert_only"]["rows"]}
    assert alerts["News Desk"]["alerts"] == 8
    assert alerts["Squeeze Fire"]["signals"] == 1 and alerts["Squeeze Fire"]["hypothetical"] == -20.0
    assert alerts["Golden Flow"]["alerts"] == 1
    assert alerts["SPX plan (modeled)"]["modeled_realized"] == 3250.0
    text = board["telegram_text"]
    assert text.startswith("📊 TODAY'S P&L") and "paper only" in text
    assert "Webull paper desks (fills): +$50.00" in text
    assert "Internal paper ledgers (not Webull): +$133.25" in text
    assert "Million +$108.25 (1W/0L)" in text
    assert "SPX modeled +$3,250.00 (not an order)" in text
    assert "Sure Winner" not in text and "sure_winner" not in json.dumps(board)


def test_eod_sends_once_in_window(tmp_path: Path):
    sent: list[str] = []
    state = tmp_path / "eod.json"
    build = lambda: {"telegram_text": "📊 TODAY'S P&L · paper only"}  # noqa: E731
    send = lambda text: (sent.append(text) or {"ok": True})  # noqa: E731
    early = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)  # 2:00 PM CT
    assert tp.maybe_send_eod(early, state_path=state, build=build, send=send)["sent"] is False
    at = datetime(2026, 11, 17, 21, 15, tzinfo=timezone.utc)  # 3:15 PM CT
    assert tp.maybe_send_eod(at, state_path=state, build=build, send=send)["sent"] is True
    assert tp.maybe_send_eod(at, state_path=state, build=build, send=send)["sent"] is False
    assert sent == ["📊 TODAY'S P&L · paper only"]
    saturday = datetime(2026, 11, 21, 21, 15, tzinfo=timezone.utc)
    assert tp.maybe_send_eod(saturday, state_path=state, build=build, send=send)["sent"] is False


def _bars():
    return {
        "^GSPC": [("2026-11-13", 7561.67), ("2026-11-16", 7416.59), ("2026-11-17", 7340.53), ("2026-11-18", 7300.0)],
        "SPY": [("2026-11-13", 754.37), ("2026-11-16", 739.95), ("2026-11-17", 732.44)],
        "QQQ": [("2026-11-13", 679.89), ("2026-11-16", 661.75), ("2026-11-17", 656.95)],
        "^VIX": [("2026-11-13", 19.3), ("2026-11-16", 22.91), ("2026-11-17", 25.46)],
        "^VXN": [("2026-11-13", 29.38), ("2026-11-16", 32.9), ("2026-11-17", 34.44)],
    }


def test_levels_math_and_regime():
    card = lv.compute_card(_bars(), session=date(2026, 11, 18), now=NOW)
    assert card["ok"] and card["info_only"] and card["orders"] is False
    assert card["regime"] == "FAST" and card["vix_prior"] == 25.46
    rows = {r["symbol"]: r for r in card["symbols"]}
    spx = rows["SPX"]
    # partial Nov 18 bar is ignored; prior close is Nov 17
    assert spx["prior_close"] == 7340.53 and spx["prior_close_day"] == "2026-11-17"
    em = 7340.53 * 25.46 / 100 / (252 ** 0.5)
    assert abs(spx["em"] - round(em, 2)) < 0.011
    assert abs(spx["band"][1] - round(7340.53 + em, 2)) < 0.011
    assert abs(spx["shelves"][0] - round(7340.53 - em / 2, 2)) < 0.011
    assert rows["QQQ"]["vol_name"] == "VXN" and rows["QQQ"]["vol"] == 34.44
    week = spx["week"]
    assert week["anchor_day"] == "2026-11-13" and week["anchor"] == 7561.67
    assert abs(week["half_width"] - round(7561.67 * 19.3 / 100 / (252 ** 0.5) * (5 ** 0.5), 2)) < 0.011
    assert "range > EM 41%" in spx["stats_line"]
    assert rows["QQQ"]["stats"]["range_gt_em"] == 0.53


def test_levels_calm_and_event_day():
    bars = _bars()
    bars["^VIX"] = [("2026-12-03", 15.0)]
    bars["^GSPC"] = [("2026-12-03", 7000.0)]
    card = lv.compute_card(bars, session=date(2026, 12, 4), now=NOW)
    assert card["regime"] == "calm"
    assert card["event"]["name"] == "Jobs report (NFP)"
    assert card["stat_group"] == "event"


def test_session_rolls_after_close_and_skips_holiday():
    # Wed Nov 25 2026, 5 PM ET -> Thanksgiving closed -> Fri Nov 27
    assert lv.session_for(datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)) == date(2026, 11, 27)
    # Tue 8 AM CT -> same day
    assert lv.session_for(datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)) == date(2026, 11, 17)
    # Friday evening -> Monday
    assert lv.session_for(datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)) == date(2026, 11, 23)


def test_levels_card_caches_per_session(tmp_path: Path):
    calls = []

    def fetch():
        calls.append(1)
        return _bars()

    path = tmp_path / "levels.json"
    at = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)
    first = lv.levels_card(now=at, path=path, fetch=fetch)
    again = lv.levels_card(now=at, path=path, fetch=fetch)
    assert first["ok"] and again["session"] == "2026-11-18"
    assert len(calls) == 1


def test_expected_move_premarket_prior_close_is_last_regular_print():
    from odte_scanner.signals.expected_move import mark_from_meta

    # 8:25 AM CT Nov 17: Yahoo range=1d still shows Nov 16; chartPreviousClose is Nov 13.
    nov16_close = int(datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc).timestamp())
    meta = {
        "regularMarketPrice": 7416.59,
        "regularMarketTime": nov16_close,
        "chartPreviousClose": 7561.67,
        "preMarketPrice": None,
    }
    mark = mark_from_meta(meta, now=datetime(2026, 11, 17, 14, 25, tzinfo=timezone.utc))
    assert mark["prev_close"] == 7416.59
    # During the session the chart's previous close stands.
    nov17_mid = int(datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc).timestamp())
    live = mark_from_meta(
        {"regularMarketPrice": 7350.0, "regularMarketTime": nov17_mid, "chartPreviousClose": 7416.59},
        now=datetime(2026, 11, 17, 17, 5, tzinfo=timezone.utc),
    )
    assert live["prev_close"] == 7416.59 and live["last"] == 7350.0


def test_page_has_top_banners():
    from odte_scanner.ui import PAGE

    for needle in ("todayPnlBanner", "paperDeskTopBanner", "levelsBanner", "renderTodayPnl", "renderLevelsCard"):
        assert needle in PAGE
    assert PAGE.index("todayPnlBanner") < PAGE.index('id="mustTradeBanner"')
    assert "Sure Winner" not in PAGE.split("renderTodayPnl")[1].split("function renderPaperDeskCards")[0]
