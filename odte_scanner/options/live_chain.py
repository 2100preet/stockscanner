from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class LiveOptionQuote:
    symbol: str
    contract: str
    expiry: str
    strike: float
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    change: float | None = None
    percent_change: float | None = None
    spot: float | None = None
    moneyness_pct: float | None = None
    source: str = "yahoo"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_live_option_quote(
    symbol: str,
    expiry: str,
    strike: float,
    *,
    yahoo_symbol: str | None = None,
    right: str = "call",
    contract: str | None = None,
) -> LiveOptionQuote | None:
    """Refresh a single listed option's bid/ask — Tradier first, Yahoo fallback."""
    fetch_sym = yahoo_symbol or symbol
    right_norm = "put" if str(right).lower().startswith("p") else "call"

    # Prefer Tradier when TRADIER_ACCESS_TOKEN is set (better marks for stops/exits)
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_option_quote

        if access_token_from_env():
            tq = fetch_option_quote(
                symbol=fetch_sym,
                expiry=str(expiry),
                strike=float(strike),
                right=right_norm,
                contract=contract,
            )
            if tq and (tq.get("ask") or tq.get("bid") or tq.get("last")):
                bid = float(tq.get("bid") or 0)
                ask = float(tq.get("ask") or 0)
                last = float(tq.get("last") or 0)
                if ask <= 0 and last > 0:
                    ask = last
                if bid <= 0 and last > 0:
                    bid = last * 0.95
                return LiveOptionQuote(
                    symbol=symbol,
                    contract=str(tq.get("symbol") or contract or ""),
                    expiry=str(expiry),
                    strike=float(tq.get("strike") or strike),
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=int(tq.get("volume") or 0),
                    open_interest=int(tq.get("open_interest") or 0),
                    change=None,
                    percent_change=None,
                    spot=None,
                    moneyness_pct=None,
                    source="tradier",
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tradier option quote fallback %s: %s", symbol, exc)

    try:
        t = yf.Ticker(fetch_sym)
        chain = t.option_chain(expiry)
        table = chain.calls if right_norm == "call" else chain.puts
        if table is None or table.empty:
            return None
        # Exact strike match with tolerance for float noise
        rows = table[abs(table["strike"].astype(float) - float(strike)) < 1e-6]
        if rows.empty:
            # nearest strike
            idx = (table["strike"].astype(float) - float(strike)).abs().idxmin()
            row = table.loc[idx]
        else:
            row = rows.iloc[0]

        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        last = float(row.get("lastPrice") or 0)
        if ask <= 0 and last > 0:
            ask = last
        if bid <= 0 and last > 0:
            bid = last * 0.95

        spot = None
        try:
            spot = float(t.fast_info.last_price)
        except Exception:  # noqa: BLE001
            pass

        moneyness = None
        if spot and spot > 0:
            moneyness = (float(row["strike"]) - spot) / spot * 100

        chg = row.get("change")
        pct = row.get("percentChange")
        return LiveOptionQuote(
            symbol=symbol,
            contract=str(row.get("contractSymbol") or ""),
            expiry=expiry,
            strike=float(row["strike"]),
            bid=bid,
            ask=ask,
            last=last,
            volume=int(row.get("volume") or 0),
            open_interest=int(row.get("openInterest") or 0),
            change=float(chg) if chg is not None else None,
            percent_change=float(pct) if pct is not None else None,
            spot=spot,
            moneyness_pct=moneyness,
            source="yahoo",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("live option quote failed %s %s %s: %s", symbol, expiry, strike, exc)
        return None


def refresh_candidate_quote(candidate: dict[str, Any], *, yahoo_symbol: str | None = None) -> dict[str, Any]:
    """Return candidate dict with live bid/ask/spot overlaid."""
    out = dict(candidate)
    expiry = candidate.get("expiry")
    strike = candidate.get("strike")
    if not expiry or strike is None:
        return out
    right = str(candidate.get("right") or "C")
    q = fetch_live_option_quote(
        str(candidate.get("symbol")),
        str(expiry),
        float(strike),
        yahoo_symbol=yahoo_symbol,
        right="put" if right.upper().startswith("P") else "call",
        contract=str(candidate.get("contract") or "") or None,
    )
    if not q:
        out["quote_stale"] = True
        return out
    mark_src = str(getattr(q, "source", None) or "yahoo")
    out.update(
        {
            "bid": q.bid,
            "ask": q.ask,
            "contract": q.contract or out.get("contract"),
            "strike": q.strike,
            "option_last": q.last,
            "option_change": q.change,
            "option_percent_change": q.percent_change,
            "option_volume": q.volume,
            "live_spot": q.spot,
            "moneyness_pct": q.moneyness_pct,
            "quote_stale": False,
            "synthetic": False,
            "mark_source": mark_src,
        }
    )
    return out
