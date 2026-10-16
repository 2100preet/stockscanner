"""Snapshot polls must paint a board instead of HTTP 500 or 503."""
from __future__ import annotations

import os

from odte_scanner.ui import create_app


def _no_disk(_path):
    return None


def test_snapshot_build_error_returns_last_board(monkeypatch):
    board = {
        "generated_at": "2026-10-16T12:00:00+00:00",
        "host": "live",
        "scores": [{"symbol": "NVDA"}],
        "actions": {"buy_now": [], "sell_now": []},
        "sure_winners": {"winners": [], "counts": {}},
    }

    def read(path):
        if str(path).endswith("last_api_snapshot.json"):
            return board
        return None

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr("odte_scanner.ui._read_json", read)
    monkeypatch.setattr("odte_scanner.ui._snapshot_offline", boom)
    app = create_app()
    res = app.test_client().get("/api/snapshot?fresh=1")
    assert res.status_code == 200
    body = res.get_json()
    assert body["scores"][0]["symbol"] == "NVDA"
    assert body["snapshot_degraded"] is True
    assert "boom" in body["snapshot_error"]
    assert "error" not in body or body.get("error") != "snapshot busy"


def test_snapshot_build_error_without_cache_is_partial_200(monkeypatch):
    def boom():
        raise RuntimeError("desk exploded")

    monkeypatch.setattr("odte_scanner.ui._read_json", _no_disk)
    monkeypatch.setattr("odte_scanner.ui._snapshot_offline", boom)
    app = create_app()
    res = app.test_client().get("/api/snapshot?fresh=1")
    assert res.status_code == 200
    body = res.get_json()
    assert body["scores"] == []
    assert body["actions"]["buy_now"] == []
    assert body["sure_winners"]["winners"] == []
    assert body["snapshot_degraded"] is True
    assert "desk exploded" in body["snapshot_error"]


def test_snapshot_busy_returns_partial_200_not_503(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_LOCK_WAIT_SEC", "0.05")
    monkeypatch.setattr("odte_scanner.ui._read_json", _no_disk)
    app = create_app()
    assert app.snap_gate.acquire(blocking=False)
    try:
        res = app.test_client().get("/api/snapshot")
    finally:
        app.snap_gate.release()
    assert res.status_code == 200
    body = res.get_json()
    assert body["scores"] == []
    assert body["snapshot_degraded"] is True
    assert "busy" in body["snapshot_error"]


def test_offline_pulse_clears_the_process_flag(monkeypatch):
    import odte_scanner.alert_pulse as pulse

    monkeypatch.delenv("SIGNAL_DESK_OFFLINE", raising=False)

    class _Resp:
        status_code = 200

        def get_json(self):
            return {"scores": [{"symbol": "AMD"}], "host": "github-pages"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _path):
            assert os.environ.get("SIGNAL_DESK_OFFLINE") == "1"
            return _Resp()

    class _App:
        def test_client(self):
            return _Client()

    monkeypatch.setattr("odte_scanner.ui.create_app", lambda config_path=None: _App())
    out = pulse.build_offline_snapshot()
    assert out["scores"][0]["symbol"] == "AMD"
    assert "SIGNAL_DESK_OFFLINE" not in os.environ
