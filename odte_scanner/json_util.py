"""Strict JSON helpers — browsers reject NaN/Infinity that Python json.dumps allows."""
from __future__ import annotations

import json
import math
from typing import Any


def finite_float(value: Any, default: float | None = None) -> float | None:
    """Coerce to a finite float, else ``default`` (usually None)."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def sanitize_for_json(obj: Any) -> Any:
    """Recursively replace NaN/Inf with None so dumps are browser-safe."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    # numpy / pandas scalars
    if hasattr(obj, "item") and callable(obj.item):
        try:
            return sanitize_for_json(obj.item())
        except Exception:  # noqa: BLE001
            return None
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj


def dumps_strict(obj: Any, **kwargs: Any) -> str:
    """json.dumps with NaN/Inf stripped; refuse to emit non-standard tokens."""
    clean = sanitize_for_json(obj)
    kwargs.setdefault("allow_nan", False)
    return json.dumps(clean, **kwargs)
