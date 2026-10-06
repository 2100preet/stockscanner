"""Tests for fast desk alert pulse / SKIP_DESK_ALERTS."""

from __future__ import annotations

from pathlib import Path

from odte_scanner import alert_pulse as ap


def test_pulse_skips_when_env_set(monkeypatch):
    monkeypatch.setenv("SKIP_DESK_ALERTS", "1")
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("should not build snapshot when skipped")

    monkeypatch.setattr(ap, "build_offline_snapshot", boom)
    res = ap.pulse_desk_alerts()
    assert res.get("skipped") is True
    assert called["n"] == 0


def test_pulse_dispatches(monkeypatch, tmp_path):
    monkeypatch.delenv("SKIP_DESK_ALERTS", raising=False)
    monkeypatch.setattr(
        ap,
        "build_offline_snapshot",
        lambda *_a, **_k: {
            "generated_at": "2026-10-06T12:00:00+00:00",
            "actions": {"counts": {"sell_now": 1, "buy_now_puts": 0}},
        },
    )

    def fake_dispatch(payload, **_k):
        assert "actions" in payload
        return {
            "ok": True,
            "configured": True,
            "sent": 1,
            "primed": False,
            "providers": ["telegram"],
            "provider": "telegram",
        }

    monkeypatch.setattr(
        "odte_scanner.alerts.dispatch_snapshot_alerts",
        fake_dispatch,
        raising=False,
    )
    # Patch via import path used inside pulse_desk_alerts
    import odte_scanner.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "dispatch_snapshot_alerts", fake_dispatch)

    # Point ROOT outputs into tmp
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)

    res = ap.pulse_desk_alerts()
    assert res["ok"] is True
    assert res["sent"] == 1
    assert (tmp_path / "outputs" / "desk_alerts_last.json").exists()


def test_alert_loop_respects_max_cycles(monkeypatch):
    scans = {"n": 0}
    pulses = {"n": 0}
    exports = {"n": 0}

    def fake_scan(*_a, **_k):
        scans["n"] += 1
        return {}

    def fake_pulse(*_a, **_k):
        pulses["n"] += 1
        return {"ok": True, "sent": 0}

    def fake_export(*_a, **_k):
        exports["n"] += 1
        return Path("/tmp/site")

    monkeypatch.setattr("odte_scanner.scanner.run_scan", fake_scan)
    monkeypatch.setattr(ap, "pulse_desk_alerts", fake_pulse)
    monkeypatch.setattr("odte_scanner.pages_export.export_pages", fake_export)
    monkeypatch.setattr(ap.time, "sleep", lambda *_a, **_k: None)

    class _Now:
        hour = 15
        minute = 0

        def isoformat(self):
            return "2026-10-06T15:00:00+00:00"

    class _DT:
        @staticmethod
        def now(tz=None):
            return _Now()

    monkeypatch.setattr(ap, "datetime", _DT)

    n = ap.run_alert_loop(max_cycles=2, pause_sec=0)
    assert n == 2
    assert scans["n"] == 2
    assert pulses["n"] == 2
    assert exports["n"] == 2
