"""Stale-while-revalidate: a stale board is served at once and rebuilt in the background."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from odte_scanner.ui import create_app


def _board(age_sec: float) -> dict:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return {
        "generated_at": when.isoformat(),
        "host": "live",
        "scores": [{"symbol": "NVDA"}],
        "actions": {"buy_now": [], "sell_now": []},
        "sure_winners": {"winners": [], "counts": {}},
    }


@pytest.fixture
def stale_disk(monkeypatch):
    monkeypatch.delenv("SIGNAL_DESK_OFFLINE", raising=False)
    monkeypatch.delenv("SNAPSHOT_SWR", raising=False)
    holder = {"board": _board(200)}  # older than the 90s TTL, younger than the 900s soft TTL

    def read(path):
        if str(path).endswith("last_api_snapshot.json"):
            return holder["board"]
        return None

    monkeypatch.setattr("odte_scanner.ui._read_json", read)
    writes = []
    monkeypatch.setattr("odte_scanner.ui.atomic_write_text", lambda *a, **k: writes.append(a))
    return holder


def _wait_free(gate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if gate.acquire(blocking=False):
            gate.release()
            return True
        time.sleep(0.01)
    return False


def test_stale_board_served_without_waiting_for_the_build(stale_disk, monkeypatch):
    started, finish = threading.Event(), threading.Event()

    def slow_build():
        started.set()
        finish.wait(5)
        raise RuntimeError("build gave up")

    monkeypatch.setattr("odte_scanner.ui._free_desk_disk", slow_build)
    app = create_app()
    client = app.test_client()
    t0 = time.time()
    res = client.get("/api/snapshot")
    assert time.time() - t0 < 3
    assert res.status_code == 200
    body = res.get_json()
    assert body["scores"][0]["symbol"] == "NVDA"
    assert not body.get("snapshot_degraded")
    assert started.wait(3), "background rebuild did not start"
    assert not app.snap_gate.acquire(blocking=False)  # the rebuild thread owns the gate
    # a second poll while the rebuild runs still gets the board, no second build
    again = client.get("/api/snapshot")
    assert again.status_code == 200 and again.get_json()["scores"][0]["symbol"] == "NVDA"
    finish.set()
    assert _wait_free(app.snap_gate), "background rebuild never released the gate"
    # a failed background build keeps the last board
    after = client.get("/api/snapshot")
    assert after.get_json()["scores"][0]["symbol"] == "NVDA"


def _sync_marker(monkeypatch):
    calls = []

    def boom():
        calls.append(threading.current_thread().name)
        raise RuntimeError("inline build")

    monkeypatch.setattr("odte_scanner.ui._free_desk_disk", boom)
    return calls


def test_fresh_param_still_builds_inline(stale_disk, monkeypatch):
    calls = _sync_marker(monkeypatch)
    app = create_app()
    body = app.test_client().get("/api/snapshot?fresh=1").get_json()
    assert body["snapshot_degraded"] is True and "inline build" in body["snapshot_error"]
    assert calls and calls[0] != "snapshot-swr"
    assert _wait_free(app.snap_gate)


def test_swr_can_be_turned_off(stale_disk, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_SWR", "0")
    calls = _sync_marker(monkeypatch)
    app = create_app()
    body = app.test_client().get("/api/snapshot").get_json()
    assert body["snapshot_degraded"] is True
    assert calls and calls[0] != "snapshot-swr"


def test_board_older_than_soft_ttl_builds_inline(stale_disk, monkeypatch):
    stale_disk["board"] = _board(2000)
    calls = _sync_marker(monkeypatch)
    app = create_app()
    body = app.test_client().get("/api/snapshot").get_json()
    assert body["snapshot_degraded"] is True
    assert calls and calls[0] != "snapshot-swr"


def test_pages_shaped_board_builds_inline(stale_disk, monkeypatch):
    stale_disk["board"] = dict(_board(200), host="github-pages")
    calls = _sync_marker(monkeypatch)
    app = create_app()
    app.test_client().get("/api/snapshot")
    assert calls and calls[0] != "snapshot-swr"


def test_offline_request_builds_inline(stale_disk, monkeypatch):
    calls = _sync_marker(monkeypatch)
    app = create_app()
    app.test_client().get("/api/snapshot?offline=1")
    assert calls and calls[0] != "snapshot-swr"
