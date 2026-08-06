"""Earnings catalyst helper for swing + $1k→$1M challenge.

Strategy bias (research / paper):
- Prefer **post-earnings** continuation (IV already crushed) for swing/LEAP calls & puts.
- Treat **pre-earnings** (today → next ~2 weeks) as high-risk for short-dated long premium;
  flag clearly and prefer LEAP / WAIT into the print.
- Surface an **earnings watch** list (today / this week / next week) even when a name
  is not yet a hist-win ENTRY.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = ROOT / "outputs" / "earnings_cache.json"

# this week + next week for swing/challenge awareness
PRE_EARNINGS_DAYS = 14
POST_EARNINGS_DAYS = 10
SOON_EARNINGS_DAYS = 21  # label-only horizon beyond pre window


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date()  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001
            pass
    text = str(value)
    if not text or text.lower() == "nan":
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:  # noqa: BLE001
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            return None


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def fetch_earnings_row(
    symbol: str,
    *,
    yahoo_symbol: str | None = None,
    cache_path: str | Path | None = None,
    force: bool = False,
    max_age_hours: float = 18.0,
) -> dict[str, Any]:
    """Return next/last earnings dates for a symbol (cached)."""
    path = Path(cache_path) if cache_path else DEFAULT_CACHE
    cache = _load_cache(path)
    key = symbol.upper()
    now = datetime.now(timezone.utc)
    hit = cache.get(key)
    if hit and not force:
        try:
            fetched = datetime.fromisoformat(str(hit.get("fetched_at")).replace("Z", "+00:00"))
            age_h = (now - fetched).total_seconds() / 3600.0
            # Reuse successes; retry recent failures after 2h
            if hit.get("available") and age_h <= max_age_hours:
                return hit
            if (not hit.get("available")) and age_h < 2.0:
                return hit
        except Exception:  # noqa: BLE001
            pass

    row: dict[str, Any] = {
        "symbol": key,
        "next_earnings": None,
        "last_earnings": None,
        "fetched_at": now.isoformat(),
        "source": "yfinance",
        "available": False,
        "error": None,
    }
    try:
        import yfinance as yf

        t = yf.Ticker(yahoo_symbol or key)
        ed = t.get_earnings_dates(limit=8)
        if ed is None or getattr(ed, "empty", True):
            row["error"] = "no earnings dates"
        else:
            today = _today()
            next_d: date | None = None
            last_d: date | None = None
            for idx in ed.index:
                d = _parse_day(idx)
                if d is None:
                    continue
                reported = None
                try:
                    reported = ed.loc[idx].get("Reported EPS")
                except Exception:  # noqa: BLE001
                    reported = None
                has_report = reported is not None and str(reported).lower() != "nan"
                if d >= today and (next_d is None or d < next_d):
                    next_d = d
                if (d < today or has_report) and d <= today and (last_d is None or d > last_d):
                    last_d = d
            row["next_earnings"] = next_d.isoformat() if next_d else None
            row["last_earnings"] = last_d.isoformat() if last_d else None
            row["available"] = bool(next_d or last_d)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings fetch %s: %s", key, exc)
        row["error"] = str(exc)

    cache[key] = row
    try:
        _save_cache(path, cache)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings cache write failed: %s", exc)
    return row


def classify_earnings(
    row: dict[str, Any] | None,
    *,
    as_of: date | None = None,
    pre_days: int = PRE_EARNINGS_DAYS,
    post_days: int = POST_EARNINGS_DAYS,
    soon_days: int = SOON_EARNINGS_DAYS,
) -> dict[str, Any]:
    """Classify catalyst window for challenge / swing strategy."""
    as_of = as_of or _today()
    out = {
        "window": "none",  # none | earnings_day | pre_earnings | earnings_soon | post_earnings
        "next_earnings": None,
        "last_earnings": None,
        "days_to_earnings": None,
        "days_since_earnings": None,
        "label": "No near-term earnings catalyst",
        "strategy_bias": "neutral",
        "prefer_leap": False,
        "boost": 0,
        "bucket": None,  # today | this_week | next_week | soon | post | none
    }
    if not row:
        return out
    next_d = _parse_day(row.get("next_earnings"))
    last_d = _parse_day(row.get("last_earnings"))
    out["next_earnings"] = next_d.isoformat() if next_d else None
    out["last_earnings"] = last_d.isoformat() if last_d else None

    if next_d is not None:
        days_to = (next_d - as_of).days
        out["days_to_earnings"] = days_to
        if days_to == 0:
            out["window"] = "earnings_day"
            out["bucket"] = "today"
            out["label"] = "Earnings TODAY — IV crush risk extreme for long premium"
            out["strategy_bias"] = "avoid_short_premium"
            out["prefer_leap"] = True
            out["boost"] = -2
            return out
        if 0 < days_to <= 7:
            out["window"] = "pre_earnings"
            out["bucket"] = "this_week"
            out["label"] = f"Earnings in {days_to}d ({next_d.isoformat()}) — this week · IV crush risk"
            out["strategy_bias"] = "caution_pre"
            out["prefer_leap"] = True
            out["boost"] = -1
            return out
        if 7 < days_to <= pre_days:
            out["window"] = "pre_earnings"
            out["bucket"] = "next_week"
            out["label"] = f"Earnings in {days_to}d ({next_d.isoformat()}) — next week · IV crush risk"
            out["strategy_bias"] = "caution_pre"
            out["prefer_leap"] = True
            out["boost"] = -1
            return out
        if pre_days < days_to <= soon_days:
            out["window"] = "earnings_soon"
            out["bucket"] = "soon"
            out["label"] = f"Earnings in {days_to}d ({next_d.isoformat()}) — on the radar"
            out["strategy_bias"] = "watch"
            out["prefer_leap"] = False
            out["boost"] = 0
            # fall through — still check post window first? post takes priority if recent
            # actually if next is soon, last is usually old — return soon
            return out

    if last_d is not None:
        days_since = (as_of - last_d).days
        out["days_since_earnings"] = days_since
        if 0 <= days_since <= post_days:
            out["window"] = "post_earnings"
            out["bucket"] = "post"
            out["label"] = f"{days_since}d after earnings ({last_d.isoformat()}) — prefer continuation"
            out["strategy_bias"] = "prefer_post"
            out["prefer_leap"] = False
            out["boost"] = 2
            return out

    if next_d is not None and out["days_to_earnings"] is not None:
        out["label"] = f"Next earnings {next_d.isoformat()} ({out['days_to_earnings']}d)"
        out["bucket"] = "none"
    return out


def earnings_map_for(
    symbols: list[str],
    *,
    aliases: dict[str, str] | None = None,
    fetch: bool = True,
    max_fetch: int = 24,
    cache_path: str | Path | None = None,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Build symbol → classified earnings for a candidate list."""
    aliases = aliases or {}
    out: dict[str, dict[str, Any]] = {}
    fetched = 0
    path = Path(cache_path) if cache_path else DEFAULT_CACHE
    cache = _load_cache(path)
    for sym in symbols:
        key = str(sym).upper()
        row = cache.get(key)
        need = force or row is None or not row.get("available")
        if fetch and need and fetched < max_fetch:
            row = fetch_earnings_row(
                key, yahoo_symbol=aliases.get(key), cache_path=path, force=force
            )
            fetched += 1
            cache = _load_cache(path)
        out[key] = classify_earnings(row)
        out[key]["raw"] = row
        out[key]["symbol"] = key
    return out


def scan_earnings_calendar(
    symbols: list[str],
    *,
    aliases: dict[str, str] | None = None,
    fetch: bool = True,
    max_fetch: int = 30,
    within_pre_days: int = PRE_EARNINGS_DAYS,
    within_post_days: int = POST_EARNINGS_DAYS,
    within_soon_days: int = SOON_EARNINGS_DAYS,
) -> list[dict[str, Any]]:
    """Return near-term earnings rows sorted: today → this week → next week → post → soon."""
    emap = earnings_map_for(
        symbols, aliases=aliases, fetch=fetch, max_fetch=max_fetch, force=False
    )
    bucket_rank = {"today": 0, "this_week": 1, "next_week": 2, "post": 3, "soon": 4}
    rows: list[dict[str, Any]] = []
    for sym, c in emap.items():
        win = c.get("window") or "none"
        days_to = c.get("days_to_earnings")
        days_since = c.get("days_since_earnings")
        keep = False
        if win in {"earnings_day", "pre_earnings", "post_earnings", "earnings_soon"}:
            keep = True
        elif days_to is not None and 0 <= int(days_to) <= within_soon_days:
            keep = True
        elif days_since is not None and 0 <= int(days_since) <= within_post_days:
            keep = True
        if not keep:
            continue
        rows.append(
            {
                "symbol": sym,
                "window": win,
                "bucket": c.get("bucket") or "none",
                "label": c.get("label"),
                "next_earnings": c.get("next_earnings"),
                "last_earnings": c.get("last_earnings"),
                "days_to_earnings": days_to,
                "days_since_earnings": days_since,
                "strategy_bias": c.get("strategy_bias"),
                "prefer_leap": c.get("prefer_leap"),
                "boost": c.get("boost") or 0,
            }
        )
    rows.sort(
        key=lambda r: (
            bucket_rank.get(str(r.get("bucket")), 9),
            int(r.get("days_to_earnings") if r.get("days_to_earnings") is not None else 999),
            int(r.get("days_since_earnings") if r.get("days_since_earnings") is not None else 999),
            r.get("symbol") or "",
        )
    )
    return rows
