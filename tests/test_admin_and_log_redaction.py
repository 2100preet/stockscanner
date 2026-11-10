"""Admin POSTs need PAPER_ADMIN_TOKEN; Webull credentials never reach logs."""
from __future__ import annotations

import io
import json
import logging

import pytest

import odte_scanner.edge  # noqa: F401
from odte_scanner.trading import log_redact
from odte_scanner.trading.log_redact import MASK, RedactFilter, install_webull_log_redaction, redact

FAKE_KEY = "AKfakeappkey0123456789"
FAKE_SECRET = "SKfakesecret9876543210"
FAKE_SIG = "c2lnbmF0dXJlLXZhbHVlLWZha2U="


# --- admin endpoints ---------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from odte_scanner.ui import create_app

    calls = {"reconcile": 0}
    import odte_scanner.trading.auto_trader as at

    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: calls.__setitem__("reconcile", calls["reconcile"] + 1) or {})
    monkeypatch.setattr(at, "_maybe_close_expired", lambda *a, **k: {})
    monkeypatch.setattr("odte_scanner.alerts.telegram.send_telegram_text", lambda *a, **k: None, raising=False)
    app = create_app()
    c = app.test_client()
    c.calls = calls
    return c


@pytest.mark.parametrize("path", ["/api/webull/reconcile", "/api/paper/kill-switch?on=1&reason=t"])
def test_admin_posts_are_disabled_without_a_configured_token(client, monkeypatch, path):
    monkeypatch.delenv("PAPER_ADMIN_TOKEN", raising=False)
    res = client.post(path, headers={"X-Admin-Token": "anything"})
    assert res.status_code == 403
    assert "PAPER_ADMIN_TOKEN is not set" in res.get_json()["error"]
    assert client.calls["reconcile"] == 0


@pytest.mark.parametrize("path", ["/api/webull/reconcile", "/api/paper/kill-switch?on=1&reason=t"])
def test_admin_posts_reject_missing_or_wrong_token(client, monkeypatch, path):
    monkeypatch.setenv("PAPER_ADMIN_TOKEN", "right-token-123")
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert client.post(path, headers={"X-Admin-Token": ""}).status_code == 403
    assert client.calls["reconcile"] == 0


def test_kill_switch_works_with_the_right_token_and_limits_get_stays_open(client, monkeypatch):
    from odte_scanner.trading import risk_controls as rc

    monkeypatch.setenv("PAPER_ADMIN_TOKEN", "right-token-123")
    res = client.post("/api/paper/kill-switch?on=1&reason=test", headers={"X-Admin-Token": "right-token-123"})
    assert res.status_code == 200
    assert rc.kill_switch_state()["on"] is True
    res = client.post("/api/paper/kill-switch?on=0", headers={"X-Admin-Token": "right-token-123"})
    assert res.status_code == 200 and rc.kill_switch_state()["on"] is False
    assert client.get("/api/paper/limits").status_code == 200  # read-only stays public


def test_reconcile_runs_with_the_right_token(client, monkeypatch):
    monkeypatch.setenv("PAPER_ADMIN_TOKEN", "right-token-123")
    res = client.post("/api/webull/reconcile", headers={"X-Admin-Token": "right-token-123"})
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert client.calls["reconcile"] == 1


# --- redaction ---------------------------------------------------------------


def test_redact_header_shapes(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", FAKE_KEY)
    samples = [
        json.dumps({"headers": {"x-app-key": FAKE_KEY, "x-signature": FAKE_SIG, "x-access-token": "tok123456789"}}),
        str({"x-app-key": FAKE_KEY, "x-signature": FAKE_SIG, "Authorization": "Bearer abc.def.ghi"}),
        f"x-app-key={FAKE_KEY}&x-signature={FAKE_SIG}",
        f"init_token finished. token:tok123456789 expires:1 status:NORMAL",
        f"string_to_sign:POST&/openapi/x&{FAKE_KEY}",
        f"plain mention of {FAKE_KEY} in an exception",
    ]
    for text in samples:
        out = redact(text)
        assert FAKE_KEY not in out and FAKE_SIG not in out and "tok123456789" not in out and "abc.def.ghi" not in out, out
        assert MASK in out


def test_redact_leaves_ordinary_lines_alone():
    line = "Webull submit rejected wb-123 OPENAPI_ETF_OPTION_CAN_NOT_TRADING: Limit $1.25 qty=2"
    assert redact(line) == line
    assert redact("admin token required (X-Admin-Token)") == "admin token required (X-Admin-Token)"


def _capture(name: str):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    log = logging.getLogger(name)
    log.addHandler(handler)
    return log, handler, stream


def test_sdk_error_dump_is_redacted_on_every_handler(monkeypatch):
    log_redact.remember_secret(FAKE_SECRET)
    sdk, handler, stream = _capture("webull.core.client")
    sdk.propagate = False
    try:
        install_webull_log_redaction()
        request = {"headers": {"x-app-key": FAKE_KEY, "x-signature": FAKE_SIG}, "secret": FAKE_SECRET}
        sdk.error("get_response exception. %s", json.dumps(request, indent=2))
        try:
            raise RuntimeError(f"HTTP 500 headers={{'x-app-key': '{FAKE_KEY}'}}")
        except RuntimeError:
            sdk.exception("boom")
        text = stream.getvalue()
        assert "get_response exception" in text and "boom" in text
        assert FAKE_KEY not in text and FAKE_SIG not in text and FAKE_SECRET not in text
    finally:
        sdk.removeHandler(handler)
        sdk.propagate = True


def test_sdk_loggers_are_raised_above_debug_and_install_is_idempotent():
    noisy = logging.getLogger("webull.core.http.response")
    noisy.setLevel(logging.DEBUG)  # what the SDK does at import
    install_webull_log_redaction()
    install_webull_log_redaction()
    assert noisy.level >= logging.WARNING
    assert sum(isinstance(f, RedactFilter) for f in noisy.filters) == 1


def test_app_logger_lines_are_redacted_at_root_handlers():
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    try:
        install_webull_log_redaction()
        logging.getLogger("odte_scanner.trading.webull").warning("detail failed: %s", f"{{'x-app-key': '{FAKE_KEY}'}}")
        assert FAKE_KEY not in stream.getvalue() and "detail failed" in stream.getvalue()
    finally:
        root.removeHandler(handler)


def test_build_trade_api_stops_sdk_default_handlers(tmp_path, monkeypatch):
    """The SDK must not add its stdout/file handlers (they print headers)."""
    import webull.trade.trade_client as tc

    from odte_scanner.trading.webull import WebullBroker

    seen = {}

    class FakeTrade:
        def __init__(self, client):
            seen["stream"] = getattr(client, "_stream_logger_set", None)
            seen["file"] = getattr(client, "_file_logger_set", None)
            self.order_v2 = object()

    monkeypatch.setattr(tc, "TradeClient", FakeTrade)
    broker = WebullBroker(enabled=True, dry_run=False, app_key=FAKE_KEY, app_secret=FAKE_SECRET,
                          account_id="a", ledger_path=tmp_path / "wb.json")
    broker._build_trade_api()
    assert seen == {"stream": True, "file": True}
    assert FAKE_SECRET not in redact(f"secret value {FAKE_SECRET}")
    assert not (tmp_path / "webull_trade_sdk.log").exists()
