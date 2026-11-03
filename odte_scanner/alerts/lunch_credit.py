"""Telegram copy for Lunch-Break Credit. The log kind is LunchCredit.

Prices follow the desk convention: live quotes are Bid / Ask on the exact
contracts, and a paper order is Limit (working) or Filled @. Customer copy
says Lunch-Break Credit, never the retired gold name.
"""

from __future__ import annotations

from typing import Any

from odte_scanner.signals.lunch_credit import LOG_KIND, max_risk_usd, spread_pnl

CUSTOMER = "🌤️ LUNCH-BREAK CREDIT"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _px(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"${number:.2f}"


def _strike(value: Any) -> str:
    number = _num(value)
    if number is None:
        return ""
    base = str(int(number)) if float(number).is_integer() else f"{number:g}"
    return f"{base}P"


def _expiry(value: Any) -> str:
    text = str(value or "")[:10]
    parts = text.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[1])}/{int(parts[2])}"
    return text


def _dollars(value: float) -> str:
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    amount = abs(value)
    if abs(amount - round(amount)) < 0.005:
        return f"{sign}${amount:.0f}"
    return f"{sign}${amount:.2f}"


def _leg_line(label: str, leg: dict[str, Any] | None, strike: Any) -> str:
    row = leg if isinstance(leg, dict) else {}
    return f"{label} {_strike(row.get('strike') if row.get('strike') is not None else strike)} · Bid {_px(row.get('bid'))} / Ask {_px(row.get('ask'))}"


def _head(card: dict[str, Any]) -> str:
    symbol = str(card.get("symbol") or "").upper()
    short = _strike(card.get("short_strike"))
    long = _strike(card.get("long_strike"))
    expiry = _expiry(card.get("expiry"))
    bits = [CUSTOMER, symbol, f"SELL {short} / BUY {long}"]
    if expiry:
        bits.append(expiry)
    return " ".join(bit for bit in bits if bit)


def format_lunch_entry(card: dict[str, Any]) -> str:
    """Entry alert. Both legs, the credit, and the max risk."""
    contracts = int(card.get("contracts") or 1)
    credit = _num(card.get("credit") or card.get("credit_mid") or card.get("fill_price"))
    width = _num(card.get("width")) or 2.0
    status = str(card.get("order_status") or "").lower()
    fill = _num(card.get("fill_price") if card.get("fill_price") not in (None, "") else card.get("order_fill"))
    limit = _num(card.get("limit_price") if card.get("limit_price") not in (None, "") else card.get("order_limit"))
    lines = [_head(card)]
    lines.append(_leg_line("SELL", card.get("short") if isinstance(card.get("short"), dict) else None, card.get("short_strike")))
    lines.append(_leg_line("BUY", card.get("long") if isinstance(card.get("long"), dict) else None, card.get("long_strike")))
    if status in {"filled", "partial"} and fill:
        lines.append(f"Filled @ {_px(fill)}")
        credit = fill
    elif limit:
        lines.append(f"Limit {_px(limit)} (working)")
    if credit:
        risk = card.get("max_risk_usd")
        if risk is None:
            risk = max_risk_usd(credit, width=width, contracts=contracts)
        lines.append(f"Credit {_px(credit)} · max risk {_px(risk)} · {contracts} contract")
    when = str(card.get("at_ct") or "")
    if when:
        lines.append(f"Signal {when} bar")
    detail = str(card.get("detail") or "").strip()
    if detail:
        lines.append(detail[:180])
    lines.append("Signal Desk · stockscanner")
    return "\n".join(lines)


def format_lunch_exit(card: dict[str, Any]) -> str:
    """Exit alert. Debit, dollars, and percent of the credit."""
    contracts = int(card.get("contracts") or 1)
    credit = float(card.get("credit") or card.get("fill_price") or 0)
    debit = float(card.get("exit_debit") or 0)
    dollars, pct = spread_pnl(credit, debit, contracts)
    noun = "contract" if contracts == 1 else "contracts"
    lines = [f"{_head(card)} · {contracts} {noun}"]
    short = card.get("short") if isinstance(card.get("short"), dict) else None
    long = card.get("long") if isinstance(card.get("long"), dict) else None
    if short or long:
        lines.append(_leg_line("SELL", short, card.get("short_strike")))
        lines.append(_leg_line("BUY", long, card.get("long_strike")))
    entry_at = str(card.get("entry_at") or card.get("at_ct") or "")
    exit_at = str(card.get("exit_at") or card.get("exit_at_ct") or "")
    entry_bits = f"Entry Filled @ {_px(credit)}"
    if entry_at:
        entry_bits += f" @ {entry_at} CT"
    lines.append(entry_bits)
    exit_bits = f"Exit Filled @ {_px(debit)}"
    if exit_at:
        exit_bits += f" @ {exit_at} CT"
    lines.append(exit_bits)
    lines.append(f"Debit {_px(debit)} · P&L {_dollars(dollars)} · {pct:+.1f}%")
    lines.append("Signal Desk · stockscanner")
    return "\n".join(lines)


def format_lunch_cancel(order: dict[str, Any], reason: str | None = None) -> str:
    """Short note. The phrase the desk sends is 'not filled — cancelled'."""
    symbol = str(order.get("symbol") or "").upper()
    meta = order.get("meta") if isinstance(order.get("meta"), dict) else {}
    short = _strike(meta.get("short_strike") if meta.get("short_strike") is not None else order.get("strike"))
    long = _strike(meta.get("long_strike"))
    head = f"{CUSTOMER} {symbol} SELL {short} / BUY {long}".strip()
    limit = _px(order.get("limit_price"))
    text = reason or str(meta.get("cancel_reason") or "")
    if "not filled — cancelled" not in text:
        text = "not filled — cancelled"
    if limit != "—":
        return f"{head} · Limit {limit} · {text}"
    return f"{head} · {text}"


def lunch_log_row(card: dict[str, Any], *, side: str, ok: bool = True) -> dict[str, Any]:
    """One alert-log line. ``kind`` is LunchCredit."""
    return {
        "symbol": str(card.get("symbol") or "").upper(),
        "contract": card.get("contract"),
        "kind": LOG_KIND,
        "side": side,
        "ok": ok,
        "error": None if ok else "not sent",
    }
