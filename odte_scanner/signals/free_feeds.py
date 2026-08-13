"""Free / open keyless feeds for dealer & vol context.

Sources (no API key):
- CBOE delayed SPX options → GEX / walls / flip (see cboe_gex.py)
- CBOE delayed VIX options → VIX call/put wall proxy
- CBOE delayed index quotes → VIX term (VIX1D, VIX9D, VIX3M, VVIX, SKEW)
- SqueezeMetrics DIX.csv → free daily Dark Pool Index + GEX history

These are **not** VolSignals VS3D actual market-maker participant books.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from odte_scanner.signals.cboe_gex import compute_spx_gex

logger = logging.getLogger(__name__)

DIX_CSV_URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
CBOE_QUOTE_TMPL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{sym}.json"
CBOE_VIX_OPT_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json"

VOL_SYMBOLS = ("VIX", "VIX1D", "VIX9D", "VIX3M", "VVIX", "SKEW")


def fetch_squeezemetrics_dix(timeout: float = 25.0) -> dict[str, Any]:
    """Free daily DIX + GEX series from SqueezeMetrics."""
    try:
        r = requests.get(DIX_CSV_URL, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "source": "squeezemetrics_dix"}

    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        try:
            rows.append(
                {
                    "date": row.get("date"),
                    "price": float(row["price"]),
                    "dix": float(row["dix"]),
                    "gex": float(row["gex"]),
                }
            )
        except Exception:  # noqa: BLE001
            continue

    if not rows:
        return {"ok": False, "error": "empty", "source": "squeezemetrics_dix"}

    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    # Heuristic: DIX > ~0.45 often read as supportive dark-pool buying; GEX sign = dealer gamma regime
    dix = last["dix"]
    gex = last["gex"]
    bias = "SUPPORTIVE" if dix >= 0.45 else ("CAUTION" if dix <= 0.40 else "NEUTRAL")
    gex_regime = "LONG_GAMMA" if gex > 0 else "SHORT_GAMMA"

    return {
        "ok": True,
        "source": "squeezemetrics_dix",
        "asof": last["date"],
        "spx": last["price"],
        "dix": round(dix, 4),
        "gex": gex,
        "gex_billions": round(gex / 1e9, 2),
        "bias": bias,
        "gex_regime": gex_regime,
        "dix_chg": round(dix - prev["dix"], 4) if prev else None,
        "gex_chg": (gex - prev["gex"]) if prev else None,
        "history_tail": rows[-10:],
        "note": (
            "SqueezeMetrics free DIX.csv — Dark Index + modeled GEX (EOD, not intraday). "
            "Not VS3D live MM books."
        ),
    }


def fetch_cboe_vol_term(timeout: float = 20.0) -> dict[str, Any]:
    """Free CBOE delayed vol indices: VIX term + VVIX + SKEW."""
    out: dict[str, Any] = {"ok": True, "source": "cboe_delayed_quotes", "levels": {}}
    for sym in VOL_SYMBOLS:
        try:
            r = requests.get(CBOE_QUOTE_TMPL.format(sym=sym), timeout=timeout)
            r.raise_for_status()
            data = (r.json().get("data") or r.json())
            out["levels"][sym] = {
                "last": data.get("current_price") or data.get("last") or data.get("close"),
                "change": data.get("price_change"),
                "change_pct": data.get("price_change_percent"),
            }
        except Exception as exc:  # noqa: BLE001
            out["levels"][sym] = {"error": str(exc)}

    levels = out["levels"]
    vix = (levels.get("VIX") or {}).get("last")
    vix1d = (levels.get("VIX1D") or {}).get("last")
    vix3m = (levels.get("VIX3M") or {}).get("last")
    skew = (levels.get("SKEW") or {}).get("last")
    vvix = (levels.get("VVIX") or {}).get("last")

    notes: list[str] = []
    if isinstance(vix1d, (int, float)) and isinstance(vix, (int, float)):
        if vix1d > vix + 1.5:
            notes.append("VIX1D >> VIX — elevated same-day vol / 0DTE stress")
        elif vix1d + 1.5 < vix:
            notes.append("VIX1D << VIX — calm near-term vs medium-term")
    if isinstance(vix, (int, float)) and isinstance(vix3m, (int, float)):
        if vix3m > vix:
            notes.append("Contango (VIX3M > VIX) — typical calm regime")
        else:
            notes.append("Backwardation (VIX > VIX3M) — stress / hedge demand")
    if isinstance(skew, (int, float)):
        if skew >= 140:
            notes.append(f"SKEW elevated ({skew:.0f}) — tail-hedge demand")
        elif skew <= 120:
            notes.append(f"SKEW low ({skew:.0f}) — complacent tails")
    if isinstance(vvix, (int, float)) and vvix >= 100:
        notes.append(f"VVIX hot ({vvix:.0f}) — vol-of-vol elevated")

    out["read"] = notes
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def fetch_vix_option_walls(timeout: float = 25.0) -> dict[str, Any]:
    """Simple VIX option OI walls from free CBOE VIX chain."""
    try:
        r = requests.get(CBOE_VIX_OPT_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data") or r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "source": "cboe_vix_options"}

    spot = float(data.get("current_price") or 0)
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    for row in data.get("options") or []:
        code = str(row.get("option") or "")
        oi = float(row.get("open_interest") or 0)
        if oi <= 0:
            continue
        try:
            strike = float(code[-8:]) / 1000.0
        except Exception:  # noqa: BLE001
            continue
        if "C" in code[6:12]:
            # crude: after date
            right = "C" if "C" in code[8:] and code.count("C") >= 1 else None
        right = None
        # VIX260819C00010000
        for i, ch in enumerate(code):
            if ch in "CP" and i >= 6 and code[i - 6 : i].isdigit():
                right = ch
                break
        if right == "C":
            calls.append((strike, oi))
        elif right == "P":
            puts.append((strike, oi))

    call_wall = max(calls, key=lambda x: x[1])[0] if calls else None
    put_wall = max(puts, key=lambda x: x[1])[0] if puts else None
    return {
        "ok": True,
        "source": "cboe_vix_options",
        "spot": spot,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "top_calls": sorted(calls, key=lambda x: -x[1])[:5],
        "top_puts": sorted(puts, key=lambda x: -x[1])[:5],
        "note": "VIX option OI walls from CBOE delayed chain (proxy).",
    }


def build_free_dealer_cockpit() -> dict[str, Any]:
    """Aggregate all free dealer/vol feeds for the UI."""
    spx = compute_spx_gex(zero_dte_only=True)
    dix = fetch_squeezemetrics_dix()
    vol = fetch_cboe_vol_term()
    vix_walls = fetch_vix_option_walls()

    summary: list[str] = []
    if spx.get("ok"):
        summary.append(
            f"CBOE SPX 0DTE {spx.get('regime')} · wall {spx.get('call_wall')} · "
            f"flip {spx.get('zero_gamma_flip')}"
        )
    if dix.get("ok"):
        summary.append(
            f"SqueezeMetrics DIX {dix.get('dix')} ({dix.get('bias')}) · "
            f"GEX {dix.get('gex_billions')}B ({dix.get('gex_regime')}) asof {dix.get('asof')}"
        )
    summary.extend(vol.get("read") or [])

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "spx_gex": spx,
        "squeezemetrics": dix,
        "vol_term": vol,
        "vix_walls": vix_walls,
        "available_free": [
            {"name": "CBOE SPX delayed options", "use": "0DTE GEX, call/put wall, flip", "auth": "none"},
            {"name": "CBOE VIX delayed options", "use": "VIX OI walls", "auth": "none"},
            {"name": "CBOE vol indices", "use": "VIX/VIX1D/VIX3M/VVIX/SKEW term", "auth": "none"},
            {"name": "SqueezeMetrics DIX.csv", "use": "Daily dark-pool index + GEX", "auth": "none"},
            {"name": "Yahoo Finance chains", "use": "Single-name OI skew / Red Flag proxy", "auth": "none"},
        ],
        "not_available_without_paid": [
            {"name": "VolSignals VS3D", "why": "Actual MM participant books; no public API"},
            {"name": "SpotGamma HIRO/TRACE", "why": "Dashboard product; no free REST API"},
            {"name": "FlashAlpha SPY/SPX GEX", "why": "Needs API key; index symbols on paid tier"},
            {"name": "Unusual Whales flow", "why": "Paid"},
        ],
        "disclaimer": (
            "Free feeds use delayed exchange quotes + public models. They approximate "
            "dealer hedging pressure; they are not VS3D-grade actual market-maker positions."
        ),
    }
