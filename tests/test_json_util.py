"""Strict JSON must never emit NaN (breaks browser JSON.parse)."""
import json
import math

from odte_scanner.json_util import dumps_strict, finite_float, sanitize_for_json


def test_sanitize_replaces_nan_and_inf():
    payload = {
        "entry_spot": float("nan"),
        "ok": 1.5,
        "nested": {"x": float("inf"), "y": [float("-inf"), 2]},
    }
    clean = sanitize_for_json(payload)
    assert clean["entry_spot"] is None
    assert clean["ok"] == 1.5
    assert clean["nested"]["x"] is None
    assert clean["nested"]["y"][0] is None
    assert clean["nested"]["y"][1] == 2


def test_dumps_strict_browser_safe():
    raw = dumps_strict({"entry_spot": float("nan"), "ask": 0.64})
    assert "NaN" not in raw
    parsed = json.loads(raw)  # std lib also ok with null
    assert parsed["entry_spot"] is None
    # Simulate browser: strict constants
    json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def test_finite_float():
    assert finite_float(float("nan")) is None
    assert finite_float("bad") is None
    assert finite_float(12.5) == 12.5
    assert math.isfinite(finite_float(0.0))


def test_live_snapshot_nan_pattern_fixed():
    """Regression shape from Pages: entry_spot NaN broke Load."""
    badly = {"rec_log": {"all": [{"symbol": "LUNR", "entry_spot": float("nan"), "entry_price": 0.64}]}}
    text = dumps_strict(badly)
    assert ": NaN" not in text
    assert json.loads(text)["rec_log"]["all"][0]["entry_spot"] is None
