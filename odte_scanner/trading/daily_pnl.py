"""Unified daily P&L across all auto-taken paper sleeves + recommendation log.

Sources (auto-took):
  - signal journal  → 0DTE / weekly / swing / lottery / ML6 BUY→SELL fills
  - challenge ledger → $1k→$1M + RIP meta
  - odte_1k ledger   → 0DTE $1K ORB15 puts

Optional (recommended pulse, may not have been auto-taken):
  - recommendation_log closed/open rows tagged recommended=True
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import to_cst_label

CT = ZoneInfo("America/Chicago")


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _day_key(iso: str | None) -> str | None:
    dt = _parse_dt(iso)
    if not dt:
        return None
    return dt.astimezone(CT).date().isoformat()


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _infer_journal_category(t: dict[str, Any]) -> str:
    desk = str(t.get("desk") or "").lower()
    if desk in {"ml6", "lottery", "explosive", "0dte", "weekly", "swing"}:
        return "ml6" if desk == "ml6" else ("lottery" if desk in {"lottery", "explosive"} else desk)
    bucket = str(t.get("dte_bucket") or "").lower()
    reason = f"{t.get('entry_reason') or ''} {t.get('exit_reason') or ''}".upper()
    if "ML6" in reason or bucket == "ml6":
        return "ml6"
    if bucket in {"lottery", "explosive"} or "LOTTERY" in reason or "EXPLOSIVE" in reason:
        return "lottery"
    if bucket in {"0dte", "1dte", "odte"}:
        return "0dte"
    if bucket in {"weekly", "1w", "week"}:
        return "weekly"
    if bucket in {"swing", "monthly", "1mo", "leap"}:
        return "swing"
    # Fall back by DTE on contract expiry if present
    dte = t.get("dte")
    try:
        if dte is not None and int(dte) <= 1:
            return "0dte"
        if dte is not None and int(dte) <= 10:
            return "weekly"
    except (TypeError, ValueError):
        pass
    return bucket or "journal"


def _infer_challenge_category(t: dict[str, Any]) -> str:
    reason = f"{t.get('entry_reason') or ''} {t.get('last_action') or ''} {t.get('exit_reason') or ''}".upper()
    if "RIP" in reason or str(t.get("hold_style") or "").lower() == "sprint" and "BUY_RIP" in reason:
        return "rip"
    hz = str(t.get("horizon") or t.get("hold_style") or "challenge").lower()
    if hz in {"sprint", "fast", "1-3d", "1_3d"}:
        return "challenge"
    if hz in {"rip", "meta"}:
        return "rip"
    return "challenge"


def _normalize_trade(
    t: dict[str, Any],
    *,
    category: str,
    sleeve: str,
    took: bool = True,
    recommended: bool = True,
) -> dict[str, Any] | None:
    symbol = str(t.get("symbol") or "").upper()
    if not symbol:
        return None
    status = str(t.get("status") or "open").lower()
    if status not in {"open", "closed"}:
        # rec_log uses open/closed/lapsed
        if status == "lapsed":
            status = "lapsed"
        elif t.get("exited_at") or t.get("closed_at") or t.get("exit_bid") is not None or t.get("exit_price") is not None:
            status = "closed"
        else:
            status = "open"

    entry_ask = _as_float(t.get("entry_ask") if t.get("entry_ask") is not None else t.get("entry_price"))
    exit_bid = _as_float(t.get("exit_bid") if t.get("exit_bid") is not None else t.get("exit_price"))
    entered_at = t.get("entered_at") or t.get("recommended_at")
    exited_at = t.get("exited_at") or t.get("closed_at")
    entered_cst = t.get("entered_at_cst") or to_cst_label(entered_at)
    exited_cst = t.get("exited_at_cst") or to_cst_label(exited_at)
    pnl = _as_float(t.get("pnl_usd"))
    pct = _as_float(t.get("profit_pct"))
    contracts = int(t.get("contracts") or 1)
    if pnl is None and entry_ask and exit_bid is not None and status == "closed":
        pnl = round((exit_bid - entry_ask) * 100 * contracts, 2)
        if entry_ask > 0:
            pct = round((exit_bid - entry_ask) / entry_ask * 100.0, 2)

    right = str(t.get("right") or "C").upper()
    if right not in {"C", "P"}:
        right = "C"

    return {
        "id": str(t.get("id") or f"{sleeve}-{symbol}-{entered_at or ''}"),
        "symbol": symbol,
        "right": right,
        "contract": t.get("contract"),
        "expiry": t.get("expiry"),
        "strike": t.get("strike"),
        "category": category,
        "sleeve": sleeve,
        "status": status,
        "took": bool(took),
        "recommended": bool(recommended),
        "entry_ask": entry_ask,
        "exit_bid": exit_bid,
        "entry_spot": _as_float(t.get("entry_spot") or t.get("spot")),
        "exit_spot": _as_float(t.get("exit_spot")),
        "entered_at": entered_at,
        "exited_at": exited_at,
        "entered_at_cst": entered_cst,
        "exited_at_cst": exited_cst,
        "day_entry": _day_key(entered_at),
        "day_exit": _day_key(exited_at),
        "pnl_usd": pnl,
        "profit_pct": pct,
        "unrealized_pnl_usd": _as_float(t.get("unrealized_pnl_usd")),
        "unrealized_pct": _as_float(t.get("unrealized_pct")),
        "mark": _as_float(t.get("mark")),
        "contracts": contracts,
        "entry_reason": t.get("entry_reason") or t.get("headline") or t.get("open_action"),
        "exit_reason": t.get("exit_reason") or t.get("reason"),
        "horizon": t.get("horizon") or t.get("dte_bucket"),
        "hist_win_pct": t.get("hist_win_pct") or t.get("win_pct"),
    }


def _collect_journal(insights: dict[str, Any] | None, journal_book: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    if isinstance(insights, dict):
        trades.extend(insights.get("closed_trades") or [])
        trades.extend(insights.get("open_positions") or [])
    if isinstance(journal_book, dict) and journal_book.get("trades"):
        # Prefer full book when available (includes all fields)
        trades = list(journal_book.get("trades") or [])
    seen: set[str] = set()
    for t in trades:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        cat = _infer_journal_category(t)
        row = _normalize_trade(t, category=cat, sleeve="journal", took=True, recommended=True)
        if row:
            rows.append(row)
    return rows


def _collect_challenge(challenge: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(challenge, dict):
        return rows
    book = challenge.get("book") or {}
    for t in book.get("trades") or []:
        if not isinstance(t, dict):
            continue
        cat = _infer_challenge_category(t)
        row = _normalize_trade(t, category=cat, sleeve="challenge", took=True, recommended=True)
        if row:
            rows.append(row)
    return rows


def _collect_odte_1k(odte_1k: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(odte_1k, dict):
        return rows
    book = odte_1k.get("book") or {}
    for t in book.get("trades") or []:
        if not isinstance(t, dict):
            continue
        row = _normalize_trade(t, category="odte_1k", sleeve="odte_1k", took=True, recommended=True)
        if row:
            rows.append(row)
    return rows


def _collect_rec_log(
    rec_log: dict[str, Any] | None,
    *,
    taken_keys: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Recommended pulses that were NOT already auto-taken into a sleeve ledger."""
    rows: list[dict[str, Any]] = []
    if not isinstance(rec_log, dict):
        return rows
    candidates = list(rec_log.get("closed_recs") or []) + list(rec_log.get("open_recs") or [])
    # Also flatten by_section if top-level empty
    if not candidates:
        by_sec = rec_log.get("by_section") or {}
        for sec in by_sec.values():
            if isinstance(sec, dict):
                candidates.extend(sec.get("closed_recs") or [])
                candidates.extend(sec.get("open_recs") or [])
            elif isinstance(sec, list):
                candidates.extend(sec)
    for r in candidates:
        if not isinstance(r, dict):
            continue
        # Skip pure journal-sourced rows already in paper journal
        src = str(r.get("source") or "").lower()
        if src in {"journal", "paper"}:
            continue
        section = str(r.get("section") or "signal").lower()
        symbol = str(r.get("symbol") or "").upper()
        right = str(r.get("right") or "C").upper()
        key = (symbol, right, section)
        # Dedup against auto-took by symbol+right+approx category
        cat_map = {
            "odte": "0dte",
            "0dte": "0dte",
            "weekly": "weekly",
            "swing": "swing",
            "lottery": "lottery",
            "challenge": "challenge",
            "odte_1k": "odte_1k",
            "radar": "radar",
            "ml6": "ml6",
            "rip": "rip",
        }
        cat = cat_map.get(section, section or "signal")
        if (symbol, right, cat) in taken_keys or (symbol, right, section) in taken_keys:
            continue
        status = str(r.get("status") or "").lower()
        if status == "lapsed":
            # Keep lapsed as recommended-not-filled for transparency
            pass
        row = _normalize_trade(
            {
                **r,
                "entered_at": r.get("recommended_at") or r.get("last_recommended_at"),
                "exited_at": r.get("closed_at"),
                "entry_ask": r.get("entry_price"),
                "exit_bid": r.get("exit_price"),
                "status": status or ("closed" if r.get("exit_price") is not None else "open"),
            },
            category=cat,
            sleeve="rec_log",
            took=False,
            recommended=True,
        )
        if row:
            rows.append(row)
    return rows


def _trade_name(r: dict[str, Any]) -> str:
    """Human trade label, e.g. 'LUNR 14.5P' or 'NVDA 100C'."""
    sym = str(r.get("symbol") or "?").upper()
    right = str(r.get("right") or "C").upper()
    side = "P" if right.startswith("P") else "C"
    strike = r.get("strike")
    try:
        if strike is not None:
            k = float(strike)
            strike_s = f"{k:g}"
            return f"{sym} {strike_s}{side}"
    except (TypeError, ValueError):
        pass
    return f"{sym} {side}"


def _trade_summary(r: dict[str, Any]) -> dict[str, Any]:
    pnl = float(r.get("pnl_usd") or 0)
    return {
        "symbol": r.get("symbol"),
        "name": _trade_name(r),
        "right": r.get("right"),
        "strike": r.get("strike"),
        "category": r.get("category"),
        "sleeve": r.get("sleeve"),
        "pnl_usd": r.get("pnl_usd"),
        "profit_pct": r.get("profit_pct"),
        "contract": r.get("contract"),
        "result": "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
    }


def _rollup(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rows if r.get("status") == "closed" and r.get("took")]
    open_rows = [r for r in rows if r.get("status") == "open" and r.get("took")]
    # Attach display name on every row for the UI
    for r in rows:
        r["trade_name"] = _trade_name(r)

    realized = round(sum(float(r.get("pnl_usd") or 0) for r in closed), 2)
    unreal = round(sum(float(r.get("unrealized_pnl_usd") or 0) for r in open_rows), 2)
    wins = sum(1 for r in closed if float(r.get("pnl_usd") or 0) > 0)
    losses = sum(1 for r in closed if float(r.get("pnl_usd") or 0) < 0)
    flats = sum(1 for r in closed if float(r.get("pnl_usd") or 0) == 0)
    decided = wins + losses
    win_rate = round(wins / decided * 100.0, 1) if decided else None

    by_day: dict[str, dict[str, Any]] = {}
    for r in closed:
        day = r.get("day_exit") or r.get("day_entry")
        if not day:
            continue
        bucket = by_day.setdefault(
            day,
            {
                "day": day,
                "realized_pnl_usd": 0.0,
                "closed_n": 0,
                "win_n": 0,
                "loss_n": 0,
                "by_category": {},
                "trades": [],
                "winners": [],
                "losers": [],
            },
        )
        pnl = float(r.get("pnl_usd") or 0)
        summary = _trade_summary(r)
        bucket["trades"].append(summary)
        bucket["realized_pnl_usd"] = round(bucket["realized_pnl_usd"] + pnl, 2)
        bucket["closed_n"] += 1
        if pnl > 0:
            bucket["win_n"] += 1
            bucket["winners"].append(f"{summary['name']} +${pnl:.2f}")
        elif pnl < 0:
            bucket["loss_n"] += 1
            bucket["losers"].append(f"{summary['name']} −${abs(pnl):.2f}")
        cat = str(r.get("category") or "other")
        c = bucket["by_category"].setdefault(
            cat, {"realized_pnl_usd": 0.0, "closed_n": 0, "trades": [], "winners": [], "losers": []}
        )
        c["realized_pnl_usd"] = round(c["realized_pnl_usd"] + pnl, 2)
        c["closed_n"] += 1
        c["trades"].append(summary)
        if pnl > 0:
            c["winners"].append(f"{summary['name']} +${pnl:.2f}")
        elif pnl < 0:
            c["losers"].append(f"{summary['name']} −${abs(pnl):.2f}")

    by_cat: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not r.get("took"):
            continue
        cat = str(r.get("category") or "other")
        c = by_cat.setdefault(
            cat,
            {
                "category": cat,
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "closed_n": 0,
                "open_n": 0,
                "win_n": 0,
                "loss_n": 0,
                "trades": [],
                "winners": [],
                "losers": [],
                "open_names": [],
            },
        )
        if r.get("status") == "closed":
            pnl = float(r.get("pnl_usd") or 0)
            summary = _trade_summary(r)
            c["realized_pnl_usd"] = round(c["realized_pnl_usd"] + pnl, 2)
            c["closed_n"] += 1
            c["trades"].append(summary)
            if pnl > 0:
                c["win_n"] += 1
                c["winners"].append(f"{summary['name']} +${pnl:.2f}")
            elif pnl < 0:
                c["loss_n"] += 1
                c["losers"].append(f"{summary['name']} −${abs(pnl):.2f}")
        elif r.get("status") == "open":
            c["open_n"] += 1
            c["unrealized_pnl_usd"] = round(
                c["unrealized_pnl_usd"] + float(r.get("unrealized_pnl_usd") or 0), 2
            )
            c["open_names"].append(_trade_name(r))

    day_list = sorted(by_day.values(), key=lambda d: d["day"], reverse=True)
    for d in day_list:
        # Sort trades winners first then by |pnl|
        d["trades"] = sorted(
            d["trades"],
            key=lambda t: (-(1 if (t.get("pnl_usd") or 0) > 0 else 0), -abs(float(t.get("pnl_usd") or 0))),
        )
        d["by_category"] = [
            {"category": k, **v}
            for k, v in sorted(d["by_category"].items(), key=lambda kv: -abs(kv[1]["realized_pnl_usd"]))
        ]

    cat_list = sorted(by_cat.values(), key=lambda c: -abs(c["realized_pnl_usd"]))
    for c in cat_list:
        c["trades"] = sorted(
            c["trades"],
            key=lambda t: (-(1 if (t.get("pnl_usd") or 0) > 0 else 0), -abs(float(t.get("pnl_usd") or 0))),
        )

    def _sort_key(r: dict[str, Any]) -> str:
        return str(r.get("exited_at") or r.get("entered_at") or "")

    closed_sorted = sorted(closed, key=_sort_key, reverse=True)
    open_sorted = sorted(open_rows, key=lambda r: str(r.get("entered_at") or ""), reverse=True)
    recommended_only = [r for r in rows if r.get("recommended") and not r.get("took")]
    recommended_only = sorted(
        recommended_only,
        key=lambda r: str(r.get("exited_at") or r.get("entered_at") or ""),
        reverse=True,
    )

    return {
        "totals": {
            "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unreal,
            "closed_n": len(closed),
            "open_n": len(open_rows),
            "win_n": wins,
            "loss_n": losses,
            "flat_n": flats,
            "win_rate_pct": win_rate,
            "recommended_not_taken_n": len(recommended_only),
        },
        "by_day": day_list,
        "by_category": cat_list,
        "closed": closed_sorted[:200],
        "open": open_sorted[:80],
        "recommended_not_taken": recommended_only[:80],
    }


def build_daily_pnl(
    *,
    insights: dict[str, Any] | None = None,
    journal_book: dict[str, Any] | None = None,
    challenge: dict[str, Any] | None = None,
    odte_1k: dict[str, Any] | None = None,
    rec_log: dict[str, Any] | None = None,
    include_recommended_not_taken: bool = True,
) -> dict[str, Any]:
    """Build cross-desk daily P&L payload for the snapshot / dashboard tab."""
    rows: list[dict[str, Any]] = []
    rows.extend(_collect_journal(insights, journal_book))
    rows.extend(_collect_challenge(challenge))
    rows.extend(_collect_odte_1k(odte_1k))

    taken_keys: set[tuple[str, str, str]] = set()
    for r in rows:
        taken_keys.add((r["symbol"], r["right"], r["category"]))

    if include_recommended_not_taken:
        rows.extend(_collect_rec_log(rec_log, taken_keys=taken_keys))

    out = _rollup(rows)
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    out["note"] = (
        "Daily P&L attributes realized $ to exit day (America/Chicago). "
        "Auto-took = paper journal + challenge + 0DTE $1K. "
        "Recommended-not-taken = signal log pulses without a sleeve fill. "
        "Beauty / Levels boards are not auto-journaled."
    )
    out["categories"] = sorted({r["category"] for r in rows if r.get("took")})
    return out
