"""Live desk worker + Webull env flags."""

from __future__ import annotations

from odte_scanner.live_desk import resolve_webull_live_flags, run_live_desk_cycle


def test_webull_live_env_enables_submit(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.setenv("WEBULL_LIVE", "1")
    flags = resolve_webull_live_flags({"enabled": False, "dry_run": True})
    assert flags["enabled"] is True
    assert flags["dry_run"] is False
    assert flags["keys_ok"] is True


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
