"""SPX dealer GEX from free CBOE delayed quotes (OI + exchange Greeks).

This is **not** VS3D. VS3D uses OCC/CBOE participant-type clearing to reconstruct
actual market-maker books. CBOE's public delayed feed gives contract OI, IV, and
precomputed Greeks — we apply the standard public GEX convention:

  GEX ≈ sign * gamma * OI * 100 * spot^2 * 0.01
  calls contribute +, puts contribute −  (dealers modeled short customer options)

~15-minute delay. Better than Yahoo SPY OI skew; still a model, not true MM books.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

CBOE_SPX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"
ET = ZoneInfo("America/New_York")


def _parse_expiry(option_code: str) -> date | None:
    # SPX260813C05500000 or SPXW260813P05500000
    code = option_code.upper().replace("SPXW", "").replace("SPX", "")
    if len(code) < 7:
        return None
    try:
        return datetime.strptime(code[:6], "%y%m%d").date()
    except Exception:  # noqa: BLE001
        return None


def _right(option_code: str) -> str | None:
    code = option_code.upper()
    for i, ch in enumerate(code):
        if ch in "CP" and i >= 6:
            # after YYMMDD
            return ch
    # fallback: find C/P after digits
    for i, ch in enumerate(code):
        if ch in ("C", "P") and i > 3 and code[i - 6 : i].isdigit():
            return ch
    return None


def fetch_cboe_spx_chain(timeout: float = 25.0) -> dict[str, Any] | None:
    try:
        r = requests.get(CBOE_SPX_URL, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CBOE SPX fetch failed: %s", exc)
        return None


def compute_spx_gex(
    *,
    zero_dte_only: bool = True,
    min_oi: float = 10.0,
) -> dict[str, Any]:
    """Compute net GEX / call wall / put wall / zero-gamma approx from CBOE SPX."""
    raw = fetch_cboe_spx_chain()
    if not raw:
        return {
            "ok": False,
            "source": "cboe_delayed",
            "error": "fetch_failed",
            "note": "Could not load CBOE delayed SPX options",
        }

    data = raw.get("data") or raw
    spot = float(data.get("current_price") or 0)
    opts = data.get("options") or []
    today = datetime.now(ET).date()

    by_strike: dict[float, float] = {}
    net = 0.0
    call_gex = 0.0
    put_gex = 0.0
    used = 0
    skipped = 0

    for row in opts:
        code = str(row.get("option") or "")
        exp = _parse_expiry(code)
        if zero_dte_only and exp != today:
            skipped += 1
            continue
        oi = float(row.get("open_interest") or 0)
        gamma = float(row.get("gamma") or 0)
        if oi < min_oi or gamma == 0 or spot <= 0:
            continue
        right = _right(code)
        if right not in {"C", "P"}:
            continue
        # Standard public convention: calls +, puts −
        sign = 1.0 if right == "C" else -1.0
        gex = sign * gamma * oi * 100.0 * (spot ** 2) * 0.01
        # strike from code tail
        try:
            strike = float(code[-8:]) / 1000.0
        except Exception:  # noqa: BLE001
            continue
        by_strike[strike] = by_strike.get(strike, 0.0) + gex
        net += gex
        if right == "C":
            call_gex += gex
        else:
            put_gex += gex
        used += 1

    if not by_strike:
        # fall back to all expiries if 0DTE empty (weekend / early)
        return compute_spx_gex(zero_dte_only=False, min_oi=min_oi) if zero_dte_only else {
            "ok": False,
            "source": "cboe_delayed",
            "error": "no_contracts",
            "spot": spot,
        }

    # Call wall = largest positive GEX strike above spot; put wall = most negative below
    above = {k: v for k, v in by_strike.items() if k >= spot}
    below = {k: v for k, v in by_strike.items() if k <= spot}
    call_wall = max(above.items(), key=lambda kv: kv[1])[0] if above else None
    put_wall = min(below.items(), key=lambda kv: kv[1])[0] if below else None

    # Zero-gamma flip: scan cumulative GEX by strike
    ordered = sorted(by_strike.items())
    cum = 0.0
    flip = None
    prev_cum = None
    prev_k = None
    for k, v in ordered:
        cum += v
        if prev_cum is not None and prev_cum * cum < 0:
            # linear interp
            flip = prev_k + (k - prev_k) * (-prev_cum) / (cum - prev_cum)
            break
        prev_cum = cum
        prev_k = k

    regime = "LONG_GAMMA" if net > 0 else "SHORT_GAMMA"
    if abs(net) < 1e8:  # ~$100M threshold soft
        regime = "NEUTRAL"

    top = sorted(by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:8]

    return {
        "ok": True,
        "source": "cboe_delayed",
        "proxy_note": (
            "CBOE delayed SPX OI+Greeks with standard GEX sign convention — "
            "NOT VolSignals VS3D actual market-maker participant books."
        ),
        "symbol": "SPX",
        "spot": round(spot, 2),
        "zero_dte_only": zero_dte_only,
        "contracts_used": used,
        "net_gex": round(net, 2),
        "call_gex": round(call_gex, 2),
        "put_gex": round(put_gex, 2),
        "regime": regime,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "zero_gamma_flip": round(flip, 2) if flip is not None else None,
        "top_strikes": [{"strike": k, "gex": round(v, 2)} for k, v in top],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
