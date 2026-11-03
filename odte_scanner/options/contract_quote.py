"""Live bid/ask for one exact option contract.

Tradier is first. Polygon is the fallback. A quote whose strike, expiry, or
right does not match the requested contract is dropped, so a 280 put cannot
price a 277.5 put.
"""

from __future__ import annotations

import logging
from typing import Any

from odte_scanner.options.quote_sanity import parse_occ

logger = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _identity(row: dict[str, Any]) -> tuple[str, str, float, str] | None:
    symbol = str(row.get("symbol") or "").upper()
    expiry = str(row.get("expiry") or "")[:10]
    strike = _num(row.get("strike"))
    right = str(row.get("right") or "").upper()[:1]
    parts = parse_occ(row.get("contract") or row.get("occ"))
    if parts:
        if not right:
            right = str(parts.get("right") or "")
        if strike is None:
            strike = _num(parts.get("strike"))
        if len(expiry) != 10:
            expiry = str(parts.get("expiry") or "")[:10]
    if not symbol or symbol == "SPX" or strike is None or len(expiry) != 10 or right not in {"C", "P"}:
        return None
    return symbol, expiry, float(strike), right


def _accept(quote: dict[str, Any], *, expiry: str, strike: float, right: str, source: str) -> dict[str, Any] | None:
    got_strike = _num(quote.get("strike"))
    if got_strike is not None and abs(got_strike - strike) > 0.02:
        return None
    got_expiry = str(quote.get("expiry") or "")[:10]
    if len(got_expiry) == 10 and got_expiry != expiry:
        return None
    got_right = str(quote.get("right") or quote.get("option_type") or quote.get("type") or "").lower()
    if got_right:
        is_put = got_right.startswith("p")
        if is_put != (right == "P"):
            return None
    bid = _num(quote.get("bid"))
    ask = _num(quote.get("ask"))
    if (ask is None or ask <= 0) and (bid is None or bid <= 0):
        return None
    return {
        "bid": bid if bid is not None and bid > 0 else None,
        "ask": ask if ask is not None and ask > 0 else None,
        "strike": strike,
        "expiry": expiry,
        "right": right,
        "source": source,
    }


def fetch_contract_quote(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bid and ask for this strike and expiry. None when neither source has it."""
    if not isinstance(row, dict):
        return None
    ident = _identity(row)
    if ident is None:
        return None
    symbol, expiry, strike, right = ident
    contract = str(row.get("contract") or "") or None
    want = "put" if right == "P" else "call"
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_option_quote

        if access_token_from_env():
            quote = fetch_option_quote(
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                right=want,
                contract=contract,
            )
            if isinstance(quote, dict):
                accepted = _accept(quote, expiry=expiry, strike=strike, right=right, source="tradier")
                if accepted is not None:
                    return accepted
    except Exception as exc:  # noqa: BLE001
        logger.debug("tradier contract quote %s: %s", symbol, exc)
    try:
        from odte_scanner.data.polygon import api_key_from_env, fetch_option_quote

        if api_key_from_env():
            quote = fetch_option_quote(
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                right=want,
                contract=contract,
            )
            if isinstance(quote, dict):
                accepted = _accept(quote, expiry=expiry, strike=strike, right=right, source="polygon")
                if accepted is not None:
                    return accepted
    except Exception as exc:  # noqa: BLE001
        logger.debug("polygon contract quote %s: %s", symbol, exc)
    return None


def refresh_traded_quote(row: dict[str, Any]) -> dict[str, Any]:
    """Replace bid/ask with the live market of this row's contract.

    When the signal strike is a different contract, the old bid and ask are
    removed first. A missed fetch must not leave the other strike on the tile.
    """
    signal = _num(row.get("signal_strike"))
    strike = _num(row.get("strike"))
    differs = signal is not None and strike is not None and abs(signal - strike) > 0.001
    if differs:
        row["bid"] = None
        row["ask"] = None
        row.pop("mid", None)
        row.pop("mark", None)
    quote = fetch_contract_quote(row)
    if not quote:
        return row
    if quote.get("bid"):
        row["bid"] = quote["bid"]
        row["live_bid"] = quote["bid"]
    if quote.get("ask"):
        row["ask"] = quote["ask"]
        row["live_ask"] = quote["ask"]
    row["quote_source"] = quote.get("source")
    row["quote_ok"] = True
    return row
