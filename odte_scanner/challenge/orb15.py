"""Opening Range Breakout (ORB15) — first 15 minutes of RTH.

ORB15 High / Low = max high / min low of bars from 09:30–09:45 America/New_York.
Used by the 0DTE $1K Challenge put playbook (break + hold ORB Low / retest).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
ORB_END = time(9, 45)


@dataclass
class Orb15Levels:
    symbol: str
    session_date: str
    high: float | None = None
    low: float | None = None
    open: float | None = None
    close_at_0950: float | None = None  # last close inside ORB window
    bars: int = 0
    status: str = "incomplete"  # forming | ready | incomplete | offline
    asof: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_et_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    idx = out.index
    if getattr(idx, "tz", None) is None:
        # Yahoo 1m often returns naive UTC or exchange-local; treat as UTC then convert
        try:
            out.index = pd.to_datetime(idx, utc=True).tz_convert(ET)
        except Exception:  # noqa: BLE001
            out.index = pd.to_datetime(idx).tz_localize(ET, ambiguous="infer", nonexistent="shift_forward")
    else:
        out.index = idx.tz_convert(ET)
    return out


def compute_orb15(
    bars_1m: pd.DataFrame | None,
    *,
    symbol: str = "SPY",
    session_day: date | None = None,
    now: datetime | None = None,
) -> Orb15Levels:
    """Compute ORB15 High/Low from 1-minute OHLCV bars."""
    if now is None:
        now_et = datetime.now(ET)
    elif now.tzinfo is None:
        now_et = now.replace(tzinfo=ET)
    else:
        now_et = now.astimezone(ET)

    day = session_day or now_et.date()
    base = Orb15Levels(symbol=str(symbol).upper(), session_date=day.isoformat(), asof=now_et.isoformat())

    if bars_1m is None or bars_1m.empty:
        base.status = "incomplete"
        base.note = "No 1-minute bars for ORB15"
        return base

    df = _to_et_index(bars_1m)
    if df is None or df.empty:
        base.status = "incomplete"
        base.note = "Empty bars after TZ normalize"
        return base

    # Session day filter
    mask_day = df.index.date == day
    day_bars = df.loc[mask_day]
    if day_bars.empty:
        base.status = "incomplete"
        base.note = f"No bars for session {day.isoformat()}"
        return base

    start = datetime.combine(day, RTH_OPEN, tzinfo=ET)
    end = datetime.combine(day, ORB_END, tzinfo=ET)
    orb = day_bars.loc[(day_bars.index >= start) & (day_bars.index < end)]
    if orb.empty:
        # Pre-open or missing — still forming
        if now_et < start:
            base.status = "forming"
            base.note = "Pre-open — ORB15 forms 09:30–09:45 ET"
        elif now_et < end:
            base.status = "forming"
            base.note = "ORB15 window open — collecting first 15m"
        else:
            base.status = "incomplete"
            base.note = "ORB15 window passed but no bars in range"
        return base

    high = float(orb["High"].max()) if "High" in orb.columns else None
    low = float(orb["Low"].min()) if "Low" in orb.columns else None
    open_px = float(orb["Open"].iloc[0]) if "Open" in orb.columns else None
    close_px = float(orb["Close"].iloc[-1]) if "Close" in orb.columns else None
    n = len(orb)

    base.high = round(high, 4) if high is not None else None
    base.low = round(low, 4) if low is not None else None
    base.open = round(open_px, 4) if open_px is not None else None
    base.close_at_0950 = round(close_px, 4) if close_px is not None else None
    base.bars = int(n)

    if now_et < end:
        base.status = "forming"
        base.note = f"ORB15 forming ({n} bars) — levels provisional until 09:45 ET"
    elif high is not None and low is not None and n >= 3:
        # 1m ≈ 15 bars; 5m ≈ 3 bars — both valid ORB15 windows
        base.status = "ready"
        base.note = f"ORB15 ready · High {base.high} · Low {base.low} ({n} bars)"
    else:
        base.status = "incomplete"
        base.note = f"Thin ORB window ({n} bars) — levels unreliable"

    return base


def fetch_orb15_bars(
    symbol: str = "SPY",
    *,
    yahoo_symbol: str | None = None,
    period: str = "1d",
) -> pd.DataFrame | None:
    """Fetch 1-minute bars for ORB15; fall back to 5m (first 3 bars ≈ 15m)."""
    try:
        import yfinance as yf

        t = yf.Ticker(yahoo_symbol or symbol)
        df = t.history(period=period, interval="1m", prepost=False, auto_adjust=False)
        if df is None or df.empty:
            # Try 5d for holidays / early session gaps
            df = t.history(period="5d", interval="1m", prepost=False, auto_adjust=False)
        if df is not None and not df.empty:
            return df
        # 5-minute bars: ORB15 ≈ first three RTH bars (09:30, 09:35, 09:40)
        df5 = t.history(period="5d", interval="5m", prepost=False, auto_adjust=False)
        return df5 if df5 is not None and not df5.empty else None
    except Exception:  # noqa: BLE001
        return None


def synthesize_1m_from_5m(bars_5m: pd.DataFrame | None) -> pd.DataFrame | None:
    """Expand 5m bars into pseudo-1m rows so compute_orb15 still sees 09:30–09:45."""
    if bars_5m is None or bars_5m.empty:
        return None
    df = bars_5m.copy()
    rows: list[dict] = []
    idx: list = []
    for ts, row in df.iterrows():
        try:
            base = pd.Timestamp(ts)
        except Exception:  # noqa: BLE001
            continue
        for m in range(5):
            t2 = base + pd.Timedelta(minutes=m)
            idx.append(t2)
            rows.append(
                {
                    "Open": float(row["Open"]) if "Open" in df.columns else float(row["Close"]),
                    "High": float(row["High"]) if "High" in df.columns else float(row["Close"]),
                    "Low": float(row["Low"]) if "Low" in df.columns else float(row["Close"]),
                    "Close": float(row["Close"]),
                    "Volume": float(row["Volume"]) / 5.0 if "Volume" in df.columns else 0.0,
                }
            )
    if not rows:
        return None
    out = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    return out


def classify_vs_orb(
    last: float | None,
    orb: Orb15Levels,
    *,
    buffer_usd: float = 0.05,
    retest_band_usd: float = 0.40,
) -> dict[str, Any]:
    """Where is spot vs ORB15 Low/High?"""
    out: dict[str, Any] = {
        "broke_orb_low": False,
        "holds_below_low": False,
        "retest_orb_low": False,
        "above_orb_high": False,
        "inside_range": False,
        "dist_to_low_usd": None,
        "dist_to_high_usd": None,
    }
    if last is None or orb.low is None or orb.high is None or orb.status not in {"ready", "forming", "proxy"}:
        return out
    low = float(orb.low)
    high = float(orb.high)
    px = float(last)
    out["dist_to_low_usd"] = round(px - low, 4)
    out["dist_to_high_usd"] = round(px - high, 4)
    out["broke_orb_low"] = px < (low - float(buffer_usd))
    out["holds_below_low"] = px <= low
    # Retest: trading back up toward ORB Low from below (within band under the low)
    out["retest_orb_low"] = (low - float(retest_band_usd)) <= px <= (low + float(buffer_usd)) and px <= low + float(
        buffer_usd
    )
    out["above_orb_high"] = px > (high + float(buffer_usd))
    out["inside_range"] = low <= px <= high
    return out
