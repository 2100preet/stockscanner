"""UI trade-alert wiring checks (embedded JS in ui.py)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "odte_scanner" / "ui.py"


def _alert_block() -> str:
    text = UI.read_text()
    start = text.index("function collectTradeAlerts()")
    end = text.index("function pushToast(", start)
    return text[start:end]


def test_collect_trade_alerts_includes_all_desks():
    block = _alert_block()
    for needle in (
        'push(r, "BUY", "Options")',
        'push(r, "SELL", "Options")',
        'push(r, "BUY", "Lottery")',
        'push({...r, action: "ENTRY"}, "BUY", "Challenge")',
        'push(r, "BUY", "ML6")',
        'push(r, "SELL", "ML6")',
        '"0DTE $1K"',
    ):
        assert needle in block, f"missing alert source: {needle}"


def test_alert_seen_persisted_in_local_storage():
    text = UI.read_text()
    assert "localStorage.getItem(ALERT_SEEN)" in text
    assert "localStorage.setItem(ALERT_SEEN" in text
    assert "sessionStorage.getItem(ALERT_SEEN)" not in text


def test_mobile_visibility_refresh_hook():
    text = UI.read_text()
    assert "visibilitychange" in text
    assert "vibrateAlert" in text


def test_manifest_exported():
    text = Path(__file__).resolve().parents[1].joinpath("odte_scanner/pages_export.py").read_text()
    assert "manifest.webmanifest" in text
