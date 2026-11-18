"""Live desk worker + Webull env flags."""

from __future__ import annotations

from odte_scanner.live_desk import resolve_webull_live_flags, run_live_desk_cycle


def test_webull_live_env_cannot_arm_production(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "live-acct")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    monkeypatch.delenv("WEBULL_PAPER_SUBMIT", raising=False)
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True, "paper": False})
    assert flags["enabled"] is False
    assert flags["dry_run"] is True
    assert flags["sandbox"] is True
    assert flags["host"] == "api.sandbox.webull.com"
    assert flags["keys_ok"] is True


def test_webull_live_does_not_override_paper(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "acct")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True, "paper": True})
    assert flags["dry_run"] is True
    assert flags["sandbox"] is True
    assert flags["host"] == "api.sandbox.webull.com"


def test_webull_keys_alone_stay_dry_run(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.delenv("WEBULL_LIVE", raising=False)
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True})
    assert flags["dry_run"] is True
    assert flags["enabled"] is False


def test_live_desk_cycle(monkeypatch):
    monkeypatch.setattr(
        "odte_scanner.scanner.run_scan",
        lambda *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        "odte_scanner.alert_pulse.pulse_desk_alerts",
        lambda *_a, **_k: {"ok": True, "sent": 0, "providers": ["telegram"]},
    )
    out = run_live_desk_cycle()
    assert out["desk_alerts"]["ok"] is True
    assert out.get("scan_error") is None


def test_rth_window_follows_new_york_clock_in_winter():
    from datetime import datetime, timezone

    from odte_scanner.live_desk import _in_rth_window

    # Nov 17 2026 (EST, UTC-5): 2:55 PM CT = 3:55 PM ET = 20:55 UTC must be inside.
    assert _in_rth_window(now=datetime(2026, 11, 17, 20, 55, tzinfo=timezone.utc))
    assert _in_rth_window(now=datetime(2026, 11, 17, 14, 30, tzinfo=timezone.utc))
    assert not _in_rth_window(now=datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc))
    assert not _in_rth_window(now=datetime(2026, 11, 17, 21, 10, tzinfo=timezone.utc))
    assert _in_rth_window(extended=True, now=datetime(2026, 11, 17, 21, 30, tzinfo=timezone.utc))
    # Saturday is closed.
    assert not _in_rth_window(now=datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc))


def test_rth_window_follows_new_york_clock_in_summer():
    from datetime import datetime, timezone

    from odte_scanner.live_desk import _in_rth_window

    # Jul 15 2026 (EDT, UTC-4): 9:30 AM ET = 13:30 UTC; 4:10 PM ET = 20:10 UTC.
    assert _in_rth_window(now=datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    assert not _in_rth_window(now=datetime(2026, 7, 15, 20, 10, tzinfo=timezone.utc))
