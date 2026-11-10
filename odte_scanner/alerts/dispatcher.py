"""Collect BUY/SELL pulses from a desk snapshot and push Telegram / WhatsApp alerts.

Gold Setup (Sure Winner MAX/HIGH) and Golden Flow prints are part of this
personal-chat pulse. They are collected first so the per-cycle send cap cannot
drop them behind a long RIP tape. Each of those desks sends one message per
symbol: the strongest contract is the headline and up to two others are extra
lines. SOLID cards, non-golden flow, and chart triangles stay off this path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.alerts.telegram import configured as telegram_configured
from odte_scanner.alerts.telegram import send_telegram_text
from odte_scanner.alerts.whatsapp import configured as whatsapp_configured
from odte_scanner.alerts.whatsapp import send_whatsapp_text
from odte_scanner.trading.golden import is_golden_unusual_flow, is_sure_winner_gold

logger = logging.getLogger(__name__)

_ALERT_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_PINNED_PREFIXES = ("Sniper|", "Diamond|", "Gold Setup|", "Golden Flow|", "EXIT|")
_HOT_BUY_STATUSES = {"submitted", "filled", "partial", "dry_run", "staged", "cancelled"}

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEN = ROOT / "outputs" / "desk_alert_seen.json"
_ORIGINAL_SEEN = DEFAULT_SEEN
LEGACY_SEEN = ROOT / "outputs" / "whatsapp_alert_seen.json"

_BUY_ACTIONS = {
    "BUY_NOW",
    "BUY_RIP",
    "BUY_BEAUTY",
    "BUY_LEVEL",
    "BUY_PUT",
    "PUT_NOW",
    "CALL_NOW",
    "ENTRY",
    "RADAR_HOT",
}
_SELL_ACTIONS = {
    "SELL_NOW",
    "EXIT",
    "SELL_PUT",
    "SELL_CALL",
}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


_GROUPED_DESKS = frozenset({"Gold Setup", "Golden Flow"})


def _load_seen(path: Path) -> tuple[set[str], dict[str, list[float]]]:
    # Only fall back to legacy when using the default desk seen path
    candidates = [path]
    # Only the real desk file falls back to the legacy store. A test path
    # that happens to be assigned to DEFAULT_SEEN must not inherit it.
    try:
        use_legacy = path.resolve() == _ORIGINAL_SEEN.resolve() and LEGACY_SEEN.exists()
    except OSError:
        use_legacy = False
    if use_legacy:
        candidates.append(LEGACY_SEEN)
    for p in candidates:
        if p is None or not p.exists():
            continue
        from odte_scanner.json_util import load_json_state

        raw = load_json_state(p)
        if isinstance(raw, list):
            return {str(x) for x in raw}, {}
        if isinstance(raw, dict):
            keys = raw.get("keys") or raw.get("seen") or []
            strengths: dict[str, list[float]] = {}
            raw_strengths = raw.get("strengths") or {}
            if isinstance(raw_strengths, dict):
                for key, value in raw_strengths.items():
                    if isinstance(value, (list, tuple)):
                        strengths[str(key)] = [float(part) for part in value]
            return {str(x) for x in keys}, strengths
    return set(), {}


def _save_seen(
    path: Path,
    keys: set[str],
    strengths: dict[str, list[float]] | None = None,
    *,
    keep: int = 2000,
) -> None:
    """Keep Sniper/Diamond keys. Alphabetical trim was re-sending Diamond all morning."""
    from odte_scanner.json_util import load_json_state

    prior: list[str] = []
    if path.exists():
        raw = load_json_state(path)
        if isinstance(raw, dict) and isinstance(raw.get("keys"), list):
            prior = [str(item) for item in raw["keys"]]
        elif isinstance(raw, list):
            prior = [str(item) for item in raw]
    ordered = [key for key in prior if key in keys]
    have = set(ordered)
    for key in sorted(keys):
        if key not in have:
            ordered.append(key)
            have.add(key)
    pinned = [key for key in ordered if str(key).startswith(_PINNED_PREFIXES)][-4000:]
    pinned_set = set(pinned)
    rest = [key for key in ordered if key not in pinned_set]
    ordered = pinned + rest[-keep:]
    from odte_scanner.json_util import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    kept = set(ordered)
    stored = {
        key: value
        for key, value in (strengths or {}).items()
        if key in kept and isinstance(value, list)
    }
    payload = json.dumps({"keys": ordered, "strengths": stored}, indent=2)
    atomic_write_text(path, payload)
    # Keep the legacy file in sync only for the real desk path.
    try:
        if path.resolve() == _ORIGINAL_SEEN.resolve():
            LEGACY_SEEN.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(LEGACY_SEEN, payload)
    except Exception:  # noqa: BLE001
        pass


def _row_key(row: dict[str, Any], side: str, desk: str) -> str:
    sym = str(row.get("symbol") or "").upper()
    occ = str(row.get("contract") or row.get("occ") or "").upper()
    act = str(row.get("action") or row.get("alert_action") or side).upper()
    strike = row.get("strike")
    exp = str(row.get("expiry") or "")[:10]
    signaled = str(row.get("signaled_at") or row.get("signaled_at_cst") or "")[:19]
    return f"{desk}|{side}|{sym}|{act}|{occ or f'{strike}:{exp}'}|{signaled}"


_OCC_RE = re.compile(r"([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})")


def _occ_parts(contract: Any) -> dict[str, Any]:
    """Read right, strike, and expiry from an OCC symbol when the row omitted them."""
    raw = str(contract or "").upper().replace("O:", "").replace(" ", "")
    match = _OCC_RE.search(raw)
    if not match:
        return {}
    _root, yymmdd, right, strike_raw = match.groups()
    return {
        "right": right,
        "expiry": f"{2000 + int(yymmdd[:2]):04d}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "strike": int(strike_raw) / 1000.0,
    }


def _resolved_contract(row: dict[str, Any]) -> tuple[str, Any, str]:
    occ = _occ_parts(row.get("contract") or row.get("occ"))
    right = str(row.get("right") or "").strip().upper()[:1]
    if right not in {"C", "P"}:
        right = str(occ.get("right") or "")
    strike = row.get("strike")
    if strike is None or strike == "":
        strike = occ.get("strike")
    expiry = str(row.get("expiry") or occ.get("expiry") or "")
    return right, strike, expiry


def _contract_circle(row: dict[str, Any]) -> str:
    """Green for calls and bullish prints, red for puts and bearish prints."""
    right, _strike, _expiry = _resolved_contract(row)
    if right == "P":
        return "🔴"
    if right == "C":
        return "🟢"
    sentiment = str(row.get("sentiment") or "").strip().lower()
    if sentiment in {"bearish", "short"}:
        return "🔴"
    return "🟢"


def _compact_expiry(raw: Any) -> str:
    text = str(raw or "")[:10]
    parts = text.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[1])}/{int(parts[2])}"
    return text


def _compact_strike(strike: Any, right: str) -> str:
    if strike is None or strike == "":
        return right
    try:
        number = float(strike)
    except (TypeError, ValueError):
        base = str(strike)
    else:
        base = str(int(number)) if number.is_integer() else f"{number:g}"
    return f"{base}{right}" if right else base


def _signal_headline(row: dict[str, Any], desk: str) -> str | None:
    """Plain-text first line for Gold Setup and Golden Flow. No Markdown."""
    if desk not in {"Gold Setup", "Golden Flow", "Diamond", "Sniper", "AfternoonPuts"}:
        return None
    right, strike, expiry = _resolved_contract(row)
    parts: list[str] = []
    if desk == "Gold Setup":
        parts.extend(["⭐", _contract_circle(row), "GOLD SETUP"])
    elif desk == "Diamond":
        parts.extend(["💎", "DIAMOND"])
    elif desk == "Sniper":
        parts.extend(["🎯", "SNIPER"])
    elif desk == "AfternoonPuts":
        parts.extend(["🌅", "AFTERNOON", "PUTS"])
    else:
        parts.extend([_contract_circle(row), "GOLDEN FLOW"])
    parts.append(str(row.get("symbol") or "?").upper())
    strike_s = _compact_strike(strike, right)
    if strike_s:
        parts.append(strike_s)
    expiry_s = _compact_expiry(expiry)
    if expiry_s:
        parts.append(expiry_s)
    if desk == "Gold Setup":
        tier = str(row.get("confidence_tier") or "").strip().upper()
        if tier:
            parts.append(tier)
    return " ".join(parts)


def _num(value: Any) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _contract_strength(row: dict[str, Any]) -> list[float]:
    """Higher is a stronger headline. Tier, then score, then flow, then premium."""
    tier = str(row.get("confidence_tier") or "").strip().upper()
    tier_rank = {"MAX": 2.0, "HIGH": 1.0}.get(tier, 0.0)
    score = _num(row.get("score") if row.get("score") is not None else row.get("ensemble_score"))
    flow = abs(_num(row.get("flow_score")))
    premium = _num(row.get("premium_notional"))
    volume = _num(row.get("volume"))
    return [tier_rank, score, flow, premium, volume]


def _strength_gt(current: list[float] | None, previous: list[float] | None) -> bool:
    cur = [float(part) for part in (current or [])]
    prev = [float(part) for part in (previous or [])]
    width = max(len(cur), len(prev))
    cur.extend([0.0] * (width - len(cur)))
    prev.extend([0.0] * (width - len(prev)))
    return cur > prev


def _session_day() -> str:
    from odte_scanner.time_cst import CT

    return datetime.now(CT).strftime("%Y-%m-%d")


def _session_key(alert: dict[str, Any]) -> str:
    return f"session|{_session_day()}|{alert['desk']}|{alert['side']}|{alert['symbol']}"


def _contract_id(row: dict[str, Any]) -> str:
    occ = str(row.get("contract") or row.get("occ") or "").upper()
    if occ:
        return occ
    right, strike, expiry = _resolved_contract(row)
    return f"{strike}:{expiry}:{right}"


def _positive_price(row: dict[str, Any], *keys: str) -> bool:
    """True when the row has a live quote or a paper limit/fill to show."""
    for key in keys:
        try:
            if float(row.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _money(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN
        return None
    return f"${number:.2f}"


def _also_line(row: dict[str, Any]) -> str:
    right, strike, expiry = _resolved_contract(row)
    strike_s = _compact_strike(strike, right)
    expiry_s = _compact_expiry(expiry)
    ask = row.get("ask")
    if ask is None and row.get("mid") is not None:
        ask = row.get("mid")
    bid_s = _money(row.get("bid")) or "—"
    ask_s = _money(ask) or "—"
    bits = [_contract_circle(row)]
    if strike_s:
        bits.append(strike_s)
    if expiry_s:
        bits.append(expiry_s)
    return f"Also {' '.join(bits)} · Bid {bid_s} / Ask {ask_s}"


_QUOTE_DESKS = {"Sniper", "Diamond", "Gold Setup", "AfternoonPuts"}


def _quote_line(row: dict[str, Any]) -> str:
    """Live bid and ask. A missing side stays a dash and is never the limit."""
    bid = row.get("live_bid") if row.get("live_bid") not in (None, "") else row.get("bid")
    ask = row.get("live_ask") if row.get("live_ask") not in (None, "") else row.get("ask")
    bid_s = _money(bid) or "—"
    ask_s = _money(ask) or "—"
    spot_s = _money(row.get("spot") or row.get("last"))
    quote = f"Bid {bid_s} / Ask {ask_s}"
    if spot_s:
        return f"{quote} · spot {spot_s}"
    return quote


def _order_price_line(row: dict[str, Any]) -> str | None:
    """Paper order, labeled Limit or Filled. Never called an ask."""
    status = str(row.get("order_status") or "").lower()
    fill = _money(row.get("order_fill") if row.get("order_fill") not in (None, "") else row.get("fill_price"))
    limit = _money(row.get("order_limit") if row.get("order_limit") not in (None, "") else row.get("limit_price"))
    if status in {"filled", "partial"} and fill:
        return f"Filled @ {fill}"
    if status in {"submitted", "staged", "deferred", "dry_run"} and limit:
        return f"Limit {limit} (working)"
    if status in {"cancelled", "rejected"} and limit:
        return f"Limit {limit}"
    return None


def format_unfilled_cancel(order: dict[str, Any], kind: str) -> str:
    """Short note: the paper buy did not fill and was cancelled."""
    labels = {
        "sniper": ("🎯", "SNIPER"),
        "diamond": ("💎", "DIAMOND"),
        "afternoon_puts": ("🌅", "AFTERNOON PUTS"),
        "gold": ("⭐", "GOLD SETUP"),
    }
    emoji, name = labels.get(kind, ("🎯", "SNIPER"))
    sym = str(order.get("symbol") or "").upper()
    right = str(order.get("right") or "C").upper()[:1]
    strike = _compact_strike(order.get("strike"), right)
    expiry = _compact_expiry(order.get("expiry"))
    bits = [emoji, name, sym]
    if strike:
        bits.append(strike)
    if expiry:
        bits.append(expiry)
    head = " ".join(bits)
    limit_s = _money(order.get("limit_price"))
    if limit_s:
        return f"{head} · Limit {limit_s} · not filled — cancelled"
    return f"{head} · not filled — cancelled"


def notify_unfilled_cancel(order: dict[str, Any], kind: str) -> str:
    """Send the cancel note. SPX is not a Webull order and gets no note."""
    text = format_unfilled_cancel(order, kind)
    if str(order.get("symbol") or "").upper() == "SPX":
        return text
    try:
        send_telegram_text(text)
    except Exception:  # noqa: BLE001
        logger.info("unfilled cancel note failed for %s", order.get("symbol"))
    return text


_ALERT_DESK_TIER = {"Sniper": "sniper", "Diamond": "diamond", "Gold Setup": "gold", "AfternoonPuts": "afternoon"}


def _paper_skipped_line(row: dict[str, Any], desk: str) -> str | None:
    """The signal still goes out. Say so when the paper entry was not taken."""
    try:
        from odte_scanner.trading import risk_controls as rc_mod

        if row.get("paper_entry_skipped"):
            return rc_mod.skipped_line({"short": str(row["paper_entry_skipped"])})
        if str(row.get("symbol") or "").upper() == "SPX":
            return None
        from odte_scanner.hot_state import read_ledger_orders

        block = rc_mod.entry_block(_ALERT_DESK_TIER.get(desk, desk), orders=read_ledger_orders())
        return rc_mod.skipped_line(block)
    except Exception:  # noqa: BLE001
        return None


def _fmt_alert(
    row: dict[str, Any],
    side: str,
    desk: str,
    extras: list[dict[str, Any]] | None = None,
) -> str:
    sym = str(row.get("symbol") or "?").upper()
    headline = _signal_headline(row, desk)
    if headline:
        right, strike, exp = _resolved_contract(row)
        exp = str(exp or "")[:10]
    else:
        right = str(row.get("right") or "C").upper()[:1]
        strike = row.get("strike")
        exp = str(row.get("expiry") or "")[:10]
    when = row.get("signaled_at_cst") or ""
    detail = str(row.get("detail") or row.get("headline") or row.get("reason") or "")[:180]
    strike_s = f"{strike}{right}" if strike is not None else right
    if headline:
        lines = [headline]
    else:
        emoji = "🟢" if side == "BUY" else "🔴"
        lines = [f"{emoji} {side} · {desk}"]
    lines.append(f"{sym} {strike_s} · exp {exp or '—'}")
    if desk in _QUOTE_DESKS:
        lines.append(_quote_line(row))
        order_line = _order_price_line(row)
        if order_line:
            lines.append(order_line)
        if side == "BUY":
            skipped = _paper_skipped_line(row, desk)
            if skipped:
                lines.append(skipped)
    else:
        ask = row.get("ask")
        spot = row.get("spot") or row.get("last")
        ask_s = f"${float(ask):.2f}" if ask is not None else "—"
        spot_s = f"${float(spot):.2f}" if spot is not None else "—"
        lines.append(f"Ask {ask_s} · spot {spot_s}")
    for extra in extras or []:
        if isinstance(extra, dict):
            lines.append(_also_line(extra))
    if desk in {"Sniper", "Diamond", "AfternoonPuts"}:
        from odte_scanner.signals.detected import signal_sent_line

        sent_line = signal_sent_line(str(row.get("at_ct") or ""), row.get("detected_at"))
        if sent_line:
            lines.append(sent_line)
        elif when:
            lines.append(f"Asked {when}")
    elif when:
        lines.append(f"Asked {when}")
    if detail:
        lines.append(detail)
    if desk == "AfternoonPuts":
        from odte_scanner.signals.expected_move import format_em_used

        em_used = format_em_used(row.get("em_used_pct"))
        if em_used:
            lines.append(em_used)
    lines.append("Signal Desk · stockscanner")
    return "\n".join(lines)


def _option_flow(snapshot: dict[str, Any]) -> dict[str, Any]:
    echo = snapshot.get("echo") if isinstance(snapshot.get("echo"), dict) else {}
    flow = echo.get("option_flow") if isinstance(echo.get("option_flow"), dict) else None
    if flow is None and isinstance(snapshot.get("option_flow"), dict):
        flow = snapshot["option_flow"]
    return flow or {}


def _golden_flow_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Golden unusual-flow prints only. Chart-pattern triangles are not flow."""
    flow = _option_flow(snapshot)
    rows = [row for row in (flow.get("golden") or []) if isinstance(row, dict)]
    if not rows:
        rows = [row for row in (flow.get("prints") or []) if isinstance(row, dict)]
    return [row for row in rows if is_golden_unusual_flow(row)]


def collect_trade_alerts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror the UI Buy/Sell Now pulse set, plus Gold Setup and Golden Flow.

    Gold and golden rows are first. ``dispatch_snapshot_alerts`` sends at most
    eight new messages, and a RIP burst would otherwise use every slot.
    """
    out: list[dict[str, Any]] = []
    seen_local: set[str] = set()

    def push(row: dict[str, Any] | None, side: str, desk: str, *, force: bool = False) -> None:
        if not isinstance(row, dict):
            return
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            return
        act = str(
            row.get("alert_action") or row.get("desk_action") or row.get("action") or side
        ).upper()
        if not force:
            if side == "BUY" and act not in _BUY_ACTIONS and "BUY" not in act and act not in {
                "PUT_NOW",
                "CALL_NOW",
                "ENTRY",
                "RADAR_HOT",
            }:
                if not any(x in act for x in ("BUY", "PUT_NOW", "CALL_NOW", "ENTRY", "HOT")):
                    return
            if side == "SELL" and act not in _SELL_ACTIONS and "SELL" not in act and act != "EXIT":
                if "EXIT" not in act and "SELL" not in act:
                    return
        if side == "BUY" and not row.get("routed_order"):
            from odte_scanner.options.quote_sanity import quote_bundle_ok

            if not quote_bundle_ok(row):
                return
        key = _row_key(row, side, desk)
        if key in seen_local:
            return
        seen_local.add(key)
        if desk in _QUOTE_DESKS and str(sym).upper() != "SPX":
            from odte_scanner.options.contract_quote import refresh_traded_quote

            refresh_traded_quote(row)
        out.append(
            {
                "key": key,
                "side": side,
                "desk": desk,
                "symbol": sym,
                "action": act,
                "contract": row.get("contract"),
                "strike": row.get("strike"),
                "expiry": row.get("expiry"),
                "ask": row.get("ask"),
                "message": _fmt_alert(row, side, desk),
                "row": row,
            }
        )

    sure = snapshot.get("sure_winners") if isinstance(snapshot.get("sure_winners"), dict) else {}
    for raw in list(sure.get("winners") or []) + list(sure.get("buy_now") or []):
        if not isinstance(raw, dict) or not is_sure_winner_gold(raw):
            continue
        row = dict(raw)
        row["action"] = row.get("action") or "SURE_WINNER"
        tier = str(row.get("confidence_tier") or "").strip().upper()
        if tier and tier not in str(row.get("detail") or ""):
            prefix = f"{tier} Gold Setup"
            row["detail"] = f"{prefix} · {row['detail']}" if row.get("detail") else prefix
        push(row, "BUY", "Gold Setup", force=True)

    diamond = snapshot.get("diamond") if isinstance(snapshot.get("diamond"), dict) else {}
    sent_symbols: set[str] = set()
    for raw in diamond.get("signals") or []:
        if not isinstance(raw, dict) or not raw.get("send"):
            continue
        if len(sent_symbols) >= 3:
            break
        row = dict(raw)
        row["action"] = "DIAMOND"
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in sent_symbols:
            continue
        if not _positive_price(row, "ask", "live_ask", "order_fill", "fill_price", "order_limit", "limit_price"):
            continue
        sent_symbols.add(sym)
        push(row, "BUY", "Diamond", force=True)

    sniper = snapshot.get("sniper") if isinstance(snapshot.get("sniper"), dict) else {}
    sniper_symbols: set[str] = set()
    for raw in sniper.get("signals") or []:
        if not isinstance(raw, dict) or not raw.get("send"):
            continue
        if len(sniper_symbols) >= 3:
            break
        row = dict(raw)
        row["action"] = "SNIPER"
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in sniper_symbols:
            continue
        if not _positive_price(row, "ask", "live_ask", "order_fill", "fill_price", "order_limit", "limit_price"):
            continue
        sniper_symbols.add(sym)
        push(row, "BUY", "Sniper", force=True)

    afternoon = snapshot.get("afternoon_puts") if isinstance(snapshot.get("afternoon_puts"), dict) else {}
    afternoon_symbols: set[str] = set()
    for raw in afternoon.get("signals") or []:
        if not isinstance(raw, dict) or not raw.get("send"):
            continue
        if len(afternoon_symbols) >= 2:
            break
        row = dict(raw)
        row["action"] = "AFTERNOON_PUTS"
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in afternoon_symbols:
            continue
        if not _positive_price(row, "ask", "live_ask", "order_fill", "fill_price", "order_limit", "limit_price"):
            continue
        afternoon_symbols.add(sym)
        push(row, "BUY", "AfternoonPuts", force=True)

    lunch = snapshot.get("lunch_credit") if isinstance(snapshot.get("lunch_credit"), dict) else {}
    from odte_scanner.alerts.lunch_credit import format_lunch_entry, format_lunch_exit

    lunch_symbols: set[str] = set()
    for raw in lunch.get("signals") or []:
        if not isinstance(raw, dict) or not raw.get("send"):
            continue
        if len(lunch_symbols) >= 2:
            break
        row = dict(raw)
        row["action"] = "LUNCH_CREDIT"
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in lunch_symbols:
            continue
        if sym in {"SPY", "QQQ"} and not _positive_price(row, "ask", "live_ask", "order_fill", "fill_price", "order_limit", "limit_price"):
            continue
        lunch_symbols.add(sym)
        message = format_lunch_entry(row)
        key = _row_key(row, "BUY", "LunchCredit")
        if key in seen_local:
            continue
        seen_local.add(key)
        out.append(
            {
                "key": key,
                "side": "BUY",
                "desk": "LunchCredit",
                "symbol": sym,
                "action": "LUNCH_CREDIT",
                "contract": row.get("contract"),
                "strike": row.get("short_strike"),
                "expiry": row.get("expiry"),
                "ask": row.get("ask"),
                "message": message,
                "row": row,
            }
        )
    for raw in lunch.get("notices") or []:
        if not isinstance(raw, dict) or not raw.get("message"):
            continue
        sym = str(raw.get("symbol") or "").upper()
        side = str(raw.get("side") or "EXIT").upper()
        text = str(raw.get("message"))
        if side == "EXIT" and "P&L" not in text:
            text = format_lunch_exit(raw.get("card") if isinstance(raw.get("card"), dict) else raw)
        key = f"lunch|{side}|{sym}|{text[:40]}"
        if key in seen_local:
            continue
        seen_local.add(key)
        out.append(
            {
                "key": key,
                "side": side,
                "desk": "LunchCredit",
                "symbol": sym,
                "action": side,
                "contract": raw.get("contract"),
                "message": text,
                "row": raw,
            }
        )

    from odte_scanner.edge.filters import golden_flow_skip_reason

    for raw in _golden_flow_rows(snapshot):
        row = dict(raw)
        # A reason stamped at scan time, or the same check if this snapshot is older.
        reason = row.get("edge_skip") or golden_flow_skip_reason(row)
        if reason:
            continue
        row["action"] = "GOLDEN_FLOW"
        if row.get("ask") is None and row.get("mid") is not None:
            row["ask"] = row["mid"]
        if not row.get("detail"):
            premium = row.get("premium_notional")
            try:
                row["detail"] = f"Golden flow · premium ${float(premium):,.0f}"
            except (TypeError, ValueError):
                row["detail"] = "Golden flow"
        push(row, "BUY", "Golden Flow", force=True)

    acts = snapshot.get("actions") or {}
    for r in acts.get("buy_now") or []:
        push(r, "BUY", "Options")
    for r in acts.get("sell_now") or []:
        push(r, "SELL", "Options")

    lot = snapshot.get("lottery") or {}
    for r in lot.get("buy_now") or []:
        push(r, "BUY", "Explosive")
    for r in lot.get("sell_now") or []:
        push(r, "SELL", "Explosive")

    rip = snapshot.get("rip_radar") or {}
    for r in rip.get("buy_rip") or rip.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_RIP"}, "BUY", "RIP/META")

    beauty = snapshot.get("beauty_monthly") or {}
    for r in beauty.get("buy_beauty") or beauty.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_BEAUTY"}, "BUY", "Beauty")

    levels = snapshot.get("level_watch") or {}
    for r in levels.get("buy_level") or levels.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_LEVEL"}, "BUY", "Levels")

    ml = (snapshot.get("ml6") or {}).get("actions") or snapshot.get("ml6") or {}
    for r in ml.get("buy_now") or []:
        push(r, "BUY", "ML6")
    for r in ml.get("sell_now") or []:
        push(r, "SELL", "ML6")

    ch = snapshot.get("challenge") or {}
    for r in ch.get("entry") or ch.get("buy_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "BUY_NOW"}, "BUY", "Challenge")
    for r in ch.get("exit") or ch.get("sell_now") or []:
        push({**(r or {}), "action": (r or {}).get("action") or "SELL_NOW"}, "SELL", "Challenge")

    o1k = snapshot.get("odte_1k") or {}
    for r in o1k.get("put_now") or o1k.get("entry") or o1k.get("in") or []:
        push(
            {
                **(r or {}),
                "action": (r or {}).get("alert_action") or "BUY_NOW",
                "right": (r or {}).get("right") or "P",
            },
            "BUY",
            "0DTE $1K",
        )
    for r in o1k.get("exit_now") or o1k.get("exit") or o1k.get("out") or []:
        push(
            {**(r or {}), "action": (r or {}).get("alert_action") or "SELL_NOW"},
            "SELL",
            "0DTE $1K",
        )

    board = snapshot.get("spx_strategy") if isinstance(snapshot.get("spx_strategy"), dict) else {}
    for setup in board.get("setups") or []:
        if not isinstance(setup, dict) or str(setup.get("action") or "").upper() != "ENTER":
            continue
        message = str(setup.get("message") or "").strip()
        sym = str(setup.get("symbol") or "SPX").upper()
        if not message or not sym:
            continue
        strategy = str(setup.get("strategy") or "")
        contract = str(setup.get("structure") or "")
        day = str(setup.get("day") or "")
        key = f"SPX Strategy|ENTER|{sym}|{strategy}|{contract}|{day}"
        if key in seen_local:
            continue
        seen_local.add(key)
        out.append(
            {
                "key": key,
                "side": "BUY",
                "desk": "SPX Strategy",
                "symbol": sym,
                "action": "ENTER",
                "contract": contract,
                "strike": None,
                "expiry": setup.get("expiry"),
                "ask": setup.get("credit") if setup.get("credit") is not None else setup.get("debit"),
                "message": message,
                "row": setup,
            }
        )

    return _collapse_symbol_alerts(out)


def _collapse_symbol_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Gold Setup / Golden Flow message per symbol. Headline is the strongest contract."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[Any] = []
    for alert in alerts:
        if alert.get("desk") not in _GROUPED_DESKS:
            order.append(alert)
            continue
        bucket = (str(alert.get("desk")), str(alert.get("symbol")))
        if bucket not in grouped:
            grouped[bucket] = []
            order.append(bucket)
        grouped[bucket].append(alert)

    collapsed: list[dict[str, Any]] = []
    for item in order:
        if not isinstance(item, tuple):
            collapsed.append(item)
            continue
        by_contract: dict[str, dict[str, Any]] = {}
        for alert in grouped[item]:
            row = alert.get("row") if isinstance(alert.get("row"), dict) else {}
            cid = _contract_id(row)
            prev = by_contract.get(cid)
            if prev is None or _strength_gt(_contract_strength(row), _contract_strength(prev.get("row") or {})):
                by_contract[cid] = alert
        ranked = sorted(
            by_contract.values(),
            key=lambda alert: _contract_strength(alert.get("row") or {}),
            reverse=True,
        )
        primary = ranked[0]
        extras = [alert.get("row") or {} for alert in ranked[1:3]]
        merged = dict(primary)
        merged["key"] = _session_key(primary)
        merged["grouped"] = True
        merged["strength"] = _contract_strength(primary.get("row") or {})
        merged["message"] = _fmt_alert(primary.get("row") or {}, primary["side"], primary["desk"], extras)
        collapsed.append(merged)
    return collapsed


def _mark_alert(alert: dict[str, Any], seen: set[str], strengths: dict[str, list[float]]) -> None:
    seen.add(alert["key"])
    if alert.get("grouped"):
        strengths[str(alert["key"])] = [float(part) for part in (alert.get("strength") or [])]


def _alert_is_fresh(alert: dict[str, Any], seen: set[str], strengths: dict[str, list[float]]) -> bool:
    key = alert["key"]
    if key not in seen:
        return True
    if not alert.get("grouped"):
        return False
    previous = strengths.get(key)
    if not previous:
        return False
    return _strength_gt(alert.get("strength"), previous)


def _provider_labels(tg_ok: bool, wa_cfg: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if tg_ok:
        labels.append("telegram")
    if wa_cfg.get("ok"):
        if wa_cfg.get("callmebot"):
            labels.append("callmebot")
        elif wa_cfg.get("twilio"):
            labels.append("twilio")
        elif wa_cfg.get("meta"):
            labels.append("meta")
        else:
            labels.append("whatsapp")
    return labels


def _send_all_channels(message: str) -> tuple[list[str], list[str]]:
    """Send to every configured channel. Returns (ok_providers, errors)."""
    ok_providers: list[str] = []
    errors: list[str] = []

    tg = telegram_configured()
    if tg.get("ok"):
        res = send_telegram_text(message)
        if res.get("ok"):
            ok_providers.append("telegram")
        else:
            errors.append(f"telegram: {res.get('error') or res}")

    wa = whatsapp_configured()
    if wa.get("ok") and _env_flag("WHATSAPP_ALERTS_ENABLED", True):
        res = send_whatsapp_text(message)
        if res.get("ok"):
            ok_providers.append(str(res.get("provider") or "whatsapp"))
        else:
            errors.append(f"whatsapp: {res.get('error') or res}")

    return ok_providers, errors


def _hot_desk(row: dict[str, Any]) -> str | None:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    desk = str(row.get("desk") or "").lower()
    kind = str(meta.get("golden_kind") or "").lower()
    if desk == "sniper" or kind == "sniper":
        return "sniper"
    if desk == "diamond" or kind == "diamond":
        return "diamond"
    if desk == "afternoon_puts" or kind == "afternoon_puts":
        return "afternoon"
    return None


def promote_routed_hot_alerts(snapshot: dict[str, Any], orders: list[dict[str, Any]] | None) -> None:
    """A routed Sniper/Diamond buy still alerts after ``send`` flips false.

    The 10:30 window flag must not swallow an order that already went to the
    paper account. SPX is never promoted: it is alert-only.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    rank = {"filled": 0, "partial": 1, "submitted": 2, "staged": 3, "dry_run": 4, "cancelled": 5}
    for row in orders or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or "").upper() != "BUY":
            continue
        status = str(row.get("status") or "")
        if status not in _HOT_BUY_STATUSES:
            continue
        desk = _hot_desk(row)
        sym = str(row.get("symbol") or "").upper()
        if not desk or not sym or sym == "SPX":
            continue
        current = best.get((desk, sym))
        if current is None or rank.get(status, 9) < rank.get(str(current.get("status") or ""), 9):
            best[(desk, sym)] = row
    boards = (("sniper", "sniper"), ("diamond", "diamond"), ("afternoon", "afternoon_puts"))
    for name, key in boards:
        board = snapshot.get(key)
        if not isinstance(board, dict):
            continue
        for card in board.get("signals") or []:
            if not isinstance(card, dict):
                continue
            sym = str(card.get("symbol") or "").upper()
            order = best.get((name, sym))
            if order is None or sym == "SPX":
                continue
            card["send"] = True
            card["routed_order"] = True
            if order.get("contract"):
                card["contract"] = order.get("contract")
            if order.get("strike") is not None:
                card["signal_strike"] = card.get("signal_strike", card.get("strike"))
                card["strike"] = order.get("strike")
            if order.get("expiry"):
                card["expiry"] = order.get("expiry")
            if order.get("right"):
                card["right"] = order.get("right")
            card["order_status"] = order.get("status")
            card["order_limit"] = order.get("limit_price")
            card["order_fill"] = order.get("fill_price")
            try:
                traded = float(card.get("strike"))
                signal = float(card.get("signal_strike"))
                differs = abs(traded - signal) > 0.001
            except (TypeError, ValueError):
                differs = False
            if differs:
                # The card's bid/ask were the signal strike. Do not keep them,
                # and never copy the paper limit into the ask.
                card["signal_ask"] = card.get("signal_ask", card.get("ask"))
                card["bid"] = None
                card["ask"] = None
            if str(sym).upper() != "SPX":
                from odte_scanner.options.contract_quote import refresh_traded_quote

                refresh_traded_quote(card)


def dispatch_hot_alerts_async(
    snapshot: dict[str, Any],
    *,
    seen_path: str | Path | None = None,
) -> dict[str, Any]:
    """Queue the hot Telegram send and return without waiting on the network."""
    holder: dict[str, Any] = {}

    def _run() -> None:
        try:
            meta = dispatch_snapshot_alerts(snapshot, seen_path=seen_path, prime=False)
        except Exception as exc:  # noqa: BLE001
            meta = {"ok": False, "error": str(exc)[:200], "attempts": []}
            logger.exception("hot alert dispatch failed: %s", exc)
        try:
            from odte_scanner.alert_pulse import append_alert_log

            append_alert_log(meta.get("attempts") if isinstance(meta, dict) else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hot alert log failed: %s", exc)
        holder["meta"] = meta

    thread = threading.Thread(target=_run, name="hot-desk-alert", daemon=True)
    thread.start()
    return {"ok": True, "queued": True, "thread": thread, "holder": holder}


def dispatch_snapshot_alerts(
    snapshot: dict[str, Any],
    *,
    seen_path: str | Path | None = None,
    dry_run: bool = False,
    max_send: int = 8,
    edge_gate: bool = False,
    prime: bool = True,
) -> dict[str, Any]:
    """Send Telegram/WhatsApp for *new* BUY/SELL pulses. No-op if none configured.

    The fast Sniper/Diamond path passes ``prime=False`` so an empty seen file
    still sends. The focus-scan pulse keeps the first-run seed. The HTTP send
    is outside the alert lock so a slow focus pulse cannot hold the fast loop.
    """
    with _ALERT_LOCK:
        plan = _plan_snapshot_alerts(
            snapshot,
            seen_path=seen_path,
            dry_run=dry_run,
            max_send=max_send,
            edge_gate=edge_gate,
            prime=prime,
        )
    batch = plan.pop("_batch", None)
    if not batch:
        return plan
    try:
        sent_alerts, attempts, errors = _deliver_alerts(batch)
    finally:
        with _ALERT_LOCK:
            for alert in batch:
                _INFLIGHT.discard(str(alert.get("key") or ""))
    if sent_alerts:
        with _ALERT_LOCK:
            path = Path(seen_path) if seen_path else DEFAULT_SEEN
            seen, strengths = _load_seen(path)
            for alert, _providers in sent_alerts:
                _mark_alert(alert, seen, strengths)
            _save_seen(path, seen, strengths)
    sent = [
        {
            "key": alert["key"],
            "symbol": alert["symbol"],
            "contract": alert.get("contract"),
            "side": alert["side"],
            "desk": alert["desk"],
            "providers": providers,
            "provider": providers[0],
        }
        for alert, providers in sent_alerts
    ]
    return {
        "ok": len(sent) > 0 or not errors,
        "configured": True,
        "sent": len(sent),
        "results": sent,
        "attempts": attempts,
        "log_only": 0,
        "fresh": plan.get("fresh", len(batch)),
        "errors": errors[:8],
        "providers": plan.get("providers") or [],
        "provider": (plan.get("providers") or [None])[0],
    }


def _deliver_alerts(
    batch: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], list[str]]], list[dict[str, Any]], list[str]]:
    """HTTP send. Caller must not hold the alert lock."""
    sent: list[tuple[dict[str, Any], list[str]]] = []
    attempts: list[dict[str, Any]] = []
    errors: list[str] = []
    for alert in batch:
        ok_providers, errs = _send_all_channels(alert["message"])
        errors.extend(errs)
        ok = bool(ok_providers)
        attempts.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "symbol": alert["symbol"],
                "contract": alert.get("contract"),
                "kind": alert["desk"],
                "side": alert["side"],
                "ok": ok,
                "error": None if ok else "; ".join(str(err) for err in errs)[:200],
            }
        )
        if ok_providers:
            sent.append((alert, ok_providers))
    return sent, attempts, errors


def _plan_snapshot_alerts(
    snapshot: dict[str, Any],
    *,
    seen_path: str | Path | None = None,
    dry_run: bool = False,
    max_send: int = 8,
    edge_gate: bool = False,
    prime: bool = True,
) -> dict[str, Any]:
    """Send Telegram/WhatsApp for *new* BUY/SELL pulses. No-op if none configured.

    ``edge_gate`` is accepted and ignored. The Edge tab is information. It does
    not mute a desk. Golden Flow skips happen in ``collect_trade_alerts``.
    """
    del edge_gate
    # Global pause
    if not _env_flag("DESK_ALERTS_ENABLED", True):
        return {"ok": False, "skipped": True, "reason": "DESK_ALERTS_ENABLED=0"}

    tg_cfg = telegram_configured()
    wa_cfg = whatsapp_configured()
    wa_enabled = _env_flag("WHATSAPP_ALERTS_ENABLED", True)
    any_channel = bool(tg_cfg.get("ok") or (wa_cfg.get("ok") and wa_enabled))

    alerts = collect_trade_alerts(snapshot)
    path = Path(seen_path) if seen_path else DEFAULT_SEEN
    seen, strengths = _load_seen(path)
    providers = _provider_labels(bool(tg_cfg.get("ok")), wa_cfg if wa_enabled else {})

    # First-ever run with a channel: seed without spamming the whole board.
    # Do not seed when nothing can send — that would permanently swallow the
    # open board, and those keys would stay silent after secrets are added.
    prime_only = (os.environ.get("DESK_ALERTS_PRIME") or os.environ.get("WHATSAPP_ALERTS_PRIME") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if prime and not seen and not prime_only:
        if not any_channel:
            return {
                "ok": False,
                "configured": False,
                "primed": False,
                "seeded": 0,
                "sent": 0,
                "fresh": len(alerts),
                "skipped": True,
                "providers": providers,
                "error": "No alert channel secrets set (Telegram or WhatsApp)",
                "note": "Not seeded — this board can still alert once a channel is configured",
            }
        for a in alerts:
            _mark_alert(a, seen, strengths)
        _save_seen(path, seen, strengths)
        return {
            "ok": True,
            "configured": True,
            "primed": True,
            "seeded": len(alerts),
            "sent": 0,
            "providers": providers,
            "provider": providers[0] if providers else None,
            "note": "Seeded current board — next new BUY/SELL will alert",
        }

    fresh = [a for a in alerts if _alert_is_fresh(a, seen, strengths)]
    log_only: list[dict[str, Any]] = []
    if not fresh:
        return {
            "ok": True,
            "configured": any_channel,
            "sent": 0,
            "candidates": len(alerts),
            "providers": providers,
            "log_only": len(log_only),
            "attempts": log_only,
            "note": "edge gate: log-only" if log_only else "no new pulses",
        }

    if not any_channel or dry_run:
        if dry_run:
            for a in fresh:
                _mark_alert(a, seen, strengths)
            _save_seen(path, seen, strengths)
        return {
            "ok": bool(dry_run),
            "configured": any_channel,
            "dry_run": dry_run,
            "would_send": [a["message"] for a in fresh[:max_send]],
            "sent": 0,
            "fresh": len(fresh),
            "skipped": not any_channel,
            "log_only": len(log_only),
            "attempts": log_only,
            "providers": providers,
            "error": None if any_channel else "No alert channel secrets set (Telegram or WhatsApp)",
        }

    batch = []
    for alert in fresh[: max(1, int(max_send))]:
        key = str(alert.get("key") or "")
        if not key or key in _INFLIGHT:
            continue
        _INFLIGHT.add(key)
        batch.append(alert)
    if not batch:
        return {
            "ok": True,
            "configured": True,
            "sent": 0,
            "candidates": len(alerts),
            "providers": providers,
            "attempts": [],
            "note": "no new pulses",
        }
    return {"_batch": batch, "fresh": len(fresh), "providers": providers}
