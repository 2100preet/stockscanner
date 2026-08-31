"""Vol/OI deltas between cached and fresh option ladders (Tier-1 flow proxy)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "outputs" / "echo_ladders"


def _strike_key(row: dict[str, Any], *, side: str) -> tuple[str, float, str]:
    return (
        str(row.get("expiry") or ""),
        float(row.get("strike") or 0),
        side,
    )


def _index_ladder(ladder: dict[str, Any]) -> dict[tuple[str, float, str], dict[str, Any]]:
    out: dict[tuple[str, float, str], dict[str, Any]] = {}
    for side in ("calls", "puts"):
        right = "C" if side == "calls" else "P"
        for row in ladder.get(side) or []:
            if not isinstance(row, dict):
                continue
            k = _strike_key(row, side=right)
            out[k] = row
    return out


def diff_ladders(
    prev: dict[str, Any] | None,
    curr: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-strike volume/OI change between two ladder snapshots."""
    if not curr:
        return []
    prev_idx = _index_ladder(prev) if prev else {}
    curr_idx = _index_ladder(curr)
    deltas: list[dict[str, Any]] = []
    for key, row in curr_idx.items():
        expiry, strike, right = key
        p = prev_idx.get(key) or {}
        vol = int(row.get("volume") or 0)
        oi = int(row.get("open_interest") or 0)
        p_vol = int(p.get("volume") or 0)
        p_oi = int(p.get("open_interest") or 0)
        d_vol = vol - p_vol
        d_oi = oi - p_oi
        mid = float(row.get("mid") or row.get("ask") or row.get("bid") or 0)
        deltas.append(
            {
                "expiry": expiry,
                "strike": strike,
                "right": right,
                "volume": vol,
                "open_interest": oi,
                "delta_volume": d_vol,
                "delta_oi": d_oi,
                "premium_delta": round(max(0, d_vol) * mid * 100, 2),
                "vol_gt_oi": vol > oi > 0,
                "vol_oi_delta": round(vol / oi, 3) if oi > 0 else None,
            }
        )
    return deltas


def load_all_cached_ladders(*, ttl_sec: int = 86400 * 7) -> list[dict[str, Any]]:
    """Load every symbol ladder cache (for offline flow leaders)."""
    import time

    if not CACHE_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in CACHE_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
            age = time.time() - float(raw.get("_cached_at") or 0)
            if age > ttl_sec:
                continue
            ladder = raw.get("ladder")
            if isinstance(ladder, dict):
                out.append(ladder)
        except Exception as exc:  # noqa: BLE001
            logger.debug("skip ladder cache %s: %s", path.name, exc)
    return out


def attach_deltas_to_ladder(
    ladder: dict[str, Any],
    *,
    prev: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return ladder copy with per-row delta fields merged in."""
    if not ladder:
        return ladder
    deltas = diff_ladders(prev, ladder)
    by_key = {
        (d["expiry"], d["strike"], d["right"]): d
        for d in deltas
    }
    out = dict(ladder)
    for side, right in (("calls", "C"), ("puts", "P")):
        rows = []
        for row in ladder.get(side) or []:
            r = dict(row)
            k = (str(r.get("expiry") or ""), float(r.get("strike") or 0), right)
            d = by_key.get(k)
            if d:
                r.update(
                    {
                        "delta_volume": d["delta_volume"],
                        "delta_oi": d["delta_oi"],
                        "premium_delta": d["premium_delta"],
                    }
                )
            rows.append(r)
        out[side] = rows
    return out
