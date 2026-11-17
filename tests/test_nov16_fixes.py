"""Nov 16 fixes: card/fill matching, Gold Setup copy, labels, paper desk locks, headlines, power hour."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from odte_scanner.news.classify import display_headline, headline_of
from odte_scanner.trading.labels import activity_premium_label
from odte_scanner.trading.portal_book import annotate_card, public_order, public_reason, reconcile_pnl

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def _order(**over):
    row = {
        "id": "wb-1",
        "desk": "sure_winner",
        "action": "BUY",
        "symbol": "SLV",
        "right": "C",
        "contract": "SLV261113C00045000",
        "expiry": "2026-11-13",
        "strike": 45,
        "quantity": 1,
        "limit_price": 0.2,
        "fill_price": 0.2,
        "status": "filled",
        "created_at": "2026-11-12T15:00:00+00:00",
        "meta": {"golden_kind": "sure_winner_gold"},
    }
    row.update(over)
    return row


def test_old_fill_never_lands_on_a_new_signal_card():
    """Live bug: today's SLV 11/18 44C card showed the Nov 12 SLV 11/13 45C fill."""
    card = {
        "symbol": "SLV",
        "right": "C",
        "contract": "SLV261118C00044000",
        "expiry": "2026-11-18",
        "strike": 44,
        "ask": 0.31,
        "day_cst": "2026-11-16",
    }
    out = annotate_card(card, [_order()], "gold")
    assert "traded_contract" not in out
    assert "display_price" not in out
    assert out["ask"] == 0.31


def test_card_shows_its_own_same_day_order_and_working_limit():
    card = {"symbol": "SLV", "right": "C", "contract": "SLV261118C00044000", "expiry": "2026-11-18", "strike": 44, "day_cst": "2026-11-16"}
    old = _order()
    mine = _order(
        id="wb-2",
        contract="SLV261118C00044000",
        expiry="2026-11-18",
        strike=44,
        status="submitted",
        fill_price=None,
        limit_price=0.31,
        created_at="2026-11-16T16:00:00+00:00",
    )
    out = annotate_card(card, [old, mine], "gold")
    assert out["traded_contract"] == "SLV261118C00044000"
    assert out["display_price_label"] == "Limit (working)"
    assert out["display_price"] == 0.31
    assert "fill_price" not in out


def test_same_day_moved_strike_still_matches_but_other_expiry_does_not():
    card = {"symbol": "SLV", "right": "C", "contract": "SLV261118C00044000", "expiry": "2026-11-18", "strike": 44, "day_cst": "2026-11-16"}
    moved = _order(contract="SLV261118C00045000", expiry="2026-11-18", strike=45, created_at="2026-11-16T15:00:00+00:00")
    other_expiry = _order(id="wb-3", contract="SLV261120C00044000", expiry="2026-11-20", strike=44, created_at="2026-11-16T15:30:00+00:00")
    assert annotate_card(card, [moved], "gold")["traded_strike"] == 45
    assert "traded_contract" not in annotate_card(card, [other_expiry], "gold")


def test_sell_before_the_matched_buy_is_not_this_cards_exit():
    card = {"symbol": "SLV", "right": "C", "contract": "SLV261118C00044000", "expiry": "2026-11-18", "day_cst": "2026-11-16"}
    buy = _order(contract="SLV261118C00044000", expiry="2026-11-18", strike=44, created_at="2026-11-16T16:00:00+00:00")
    early_sell = _order(id="wb-s", action="SELL", contract="SLV261118C00044000", expiry="2026-11-18", fill_price=0.5, created_at="2026-11-16T15:00:00+00:00")
    out = annotate_card(card, [buy, early_sell], "gold")
    assert "exit_fill" not in out


def test_stored_sure_winner_reason_reads_gold_setup():
    raw = "Sure-winner #HIGH · 2× base size — elite hist + tape · SURE MAX"
    text = public_reason(raw)
    assert "sure" not in text.lower()
    assert text.startswith("Gold Setup #HIGH")
    order = public_order(_order(reason=raw))
    assert "sure" not in order["reason"].lower()
    sell = _order(id="wb-x", action="SELL", fill_price=0.4, reason=raw, created_at="2026-11-12T16:00:00+00:00")
    trips = reconcile_pnl([_order(), sell])["trips"]
    assert trips and "sure" not in trips[0]["reason"].lower()


def test_reconcile_names_its_window():
    sell = _order(id="wb-x", action="SELL", fill_price=0.4, created_at="2026-11-12T16:00:00+00:00", updated_at="2026-11-12T16:00:00+00:00")
    report = reconcile_pnl([_order(), sell])
    assert report["since"] == "2026-11-12"
    assert report["fill_pnl"] == 20.0


def test_limit_label_reads_limit_price_then_working():
    assert activity_premium_label({"status": "submitted", "limit_price": 1.05}) == "Limit $1.05 (working)"
    assert activity_premium_label({"status": "filled", "fill_price": 1.10}) == "Filled @ $1.10"


def test_customer_page_has_no_sure_badge_text():
    from odte_scanner.ui import PAGE

    assert 'sureTier || "SURE"' not in PAGE
    assert "SURE ${" not in PAGE


def _intraday(end_minute: int) -> pd.DataFrame:
    idx = pd.date_range(datetime(2026, 11, 16, 14, 0, tzinfo=ET), periods=end_minute, freq="1min")
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": [100.0 + i * 0.01 for i in range(end_minute)], "Volume": 1000},
        index=idx,
    )


def test_power_hour_drops_the_forming_15m_bar():
    from odte_scanner.signals.power_hour import resample_15m

    bars = _intraday(38)  # 14:00..14:37 ET
    now = datetime(2026, 11, 16, 14, 38, tzinfo=ET)
    out = resample_15m(bars, now=now)
    assert [ts.strftime("%H:%M") for ts in out.index] == ["14:00", "14:15"]
    later = resample_15m(bars, now=datetime(2026, 11, 16, 14, 45, tzinfo=ET))
    assert [ts.strftime("%H:%M") for ts in later.index] == ["14:00", "14:15", "14:30"]


def test_sec_headline_leads_and_datelines_are_cleaned():
    assert headline_of("aex991x3q26supplementalq Clover Health Reports Third Quarter Results") .startswith("Clover Health")
    assert headline_of("Nov 16, 2026 9:00 AM Eastern Standard Time Air Industries Group Reminds Investors").startswith("Air Industries")
    assert display_headline("\u200b \u200b MetaVia Presents Preclinical Data") == "MetaVia Presents Preclinical Data"
    assert (
        display_headline("Teamshares Reports 3Q 2026 Results; Remains On-Track to Meet 2026 Outlook New York, NY – November 16, 2026 – Teamshares (Nasdaq:TMS)")
        == "Teamshares Reports 3Q 2026 Results; Remains On-Track to Meet 2026 Outlook"
    )
    assert (
        display_headline("Keurig Dr Pepper Announces Proposed Public Offering of Common Stock FRISCO, Texas and BURLINGTON, Mass. – Nov. 12, 2026 – Keurig")
        == "Keurig Dr Pepper Announces Proposed Public Offering of Common Stock"
    )
    assert display_headline("InspireMD Announces Pricing of $37.5 Million Public Offering Miami, FL") == "InspireMD Announces Pricing of $37.5 Million Public Offering"
    # A place that is part of the title stays.
    assert display_headline("Acme opens office in Austin, TX") == "Acme opens office in Austin, TX"
    assert display_headline("Rapid Micro Biosystems Signs Definitive Agreement to be Acquired by Merck KGaA, Darmstadt, Germany").endswith("Germany")


def test_deck_cover_and_bare_filer_name_fall_back_to_the_filing():
    from odte_scanner.news.desk import _not_a_title

    assert _not_a_title("DIAMONDROCK HOSPITALITY COMPANY")
    assert _not_a_title("OAKRIDGE ACQUISITION SAN JOSE, CALIFORNIA NOVEMBER 2026 © 2026 Macerich LEGAL DISCLAIMER This presentation contains")
    assert not _not_a_title("Taboola Finalizes Acquisition of Dianomi")


def test_diamond_imports_cleanly_in_a_fresh_process():
    """signals.diamond first used to hit a half-initialized module through edge/__init__."""
    root = Path(__file__).resolve().parents[1]
    code = "import odte_scanner.signals.diamond; from odte_scanner.edge import build_edge_report; print('ok')"
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]


def test_million_alerts_say_filled_with_the_contract(tmp_path):
    from odte_scanner.desks.million import _contract_text

    trade = {"symbol": "QQQ", "right": "C", "strike": 600.0, "expiry": "2026-11-16", "contract": "QQQ261116C00600000"}
    assert _contract_text(trade) == "QQQ 11/16 600C"


def test_paper_desk_cycles_do_not_overlap(monkeypatch):
    from odte_scanner.desks import service

    started = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def fake_once(cfg, *, cycle, **kwargs):
        calls.append(bool(cycle))
        if cycle:
            started.set()
            release.wait(5)
        return {"cycle": cycle}

    monkeypatch.setattr(service, "_portal_view_once", fake_once)
    worker = threading.Thread(target=lambda: service.portal_view({}, cycle=True))
    worker.start()
    assert started.wait(5)
    second = service.portal_view({}, cycle=True)
    release.set()
    worker.join(5)
    assert second == {"cycle": False}
    assert calls == [True, False]
