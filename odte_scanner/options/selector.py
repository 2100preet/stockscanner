from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

import yfinance as yf

logger = logging.getLogger(__name__)

DteBucket = Literal["0dte", "weekly"]


@dataclass
class CallCandidate:
    symbol: str
    contract: str
    expiry: str
    dte: int
    strike: float
    spot: float
    bid: float
    ask: float
    mid: float
    volume: int
    open_interest: int
    moneyness_pct: float
    score: float
    thesis: str
    dte_bucket: str = "0dte"  # 0dte | weekly
    synthetic: bool = False
    spread_pct: float = 0.0
    right: str = "C"  # C | P

    def estimated_cost(self, contracts: int = 1) -> float:
        return self.ask * 100 * contracts

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["estimated_cost_1"] = round(self.estimated_cost(1), 2)
        side = "p" if self.right == "P" else "c"
        d["label"] = (
            f"{'0DTE' if self.dte_bucket == '0dte' else '1W'} "
            f"{self.expiry} {self.strike:g}{side} @ ${self.ask:.2f}"
        )
        return d


# Alias — same shape for puts
OptionCandidate = CallCandidate
PutCandidate = CallCandidate


def _bucket(dte: int, odte_max: int = 1) -> DteBucket:
    return "0dte" if dte <= odte_max else "weekly"


def _expiries_in_window(expirations: list[str], max_dte: int) -> list[tuple[str, int]]:
    today = datetime.now().date()
    out: list[tuple[str, int]] = []
    for exp in expirations:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - today).days
        if 0 <= dte <= max_dte:
            out.append((exp, dte))
    return out


def _rank_call(
    *,
    score: float,
    moneyness: float,
    spread: float,
    dte: int,
    vol: int,
    oi: int,
    min_oi: int,
    min_vol: int,
    prefer_closer_dte: bool,
) -> float:
    liq_penalty = 0.0
    if oi < min_oi:
        liq_penalty += 8
    if vol < min_vol:
        liq_penalty += 4
    # Prefer slight OTM / ATM; penalize deep ITM less than far OTM
    money_pen = abs(moneyness) * (6 if moneyness >= 0 else 4)
    dte_pen = dte * (1.5 if prefer_closer_dte else 0.4)
    return score - money_pen - spread * 25 - liq_penalty - dte_pen


def select_calls(
    symbol: str,
    spot: float,
    score: float,
    reasons: list[str],
    *,
    max_dte: int = 7,
    odte_max_dte: int = 1,
    otm_pct_max: float = 3.0,
    itm_pct_max: float = 1.5,
    max_ask: float = 15.0,
    min_open_interest: int = 50,
    min_volume: int = 10,
    yahoo_symbol: str | None = None,
    per_bucket: int = 1,
) -> list[CallCandidate]:
    """
    Pick best *real* ATM/OTM calls from the live chain.
    Returns up to `per_bucket` contracts for 0DTE (DTE<=odte_max_dte) and weekly (DTE<=max_dte).
    Never invents synthetic strikes for trading signals.
    """
    return _select_side(
        symbol,
        spot,
        score,
        reasons,
        right="C",
        max_dte=max_dte,
        odte_max_dte=odte_max_dte,
        otm_pct_max=otm_pct_max,
        itm_pct_max=itm_pct_max,
        max_ask=max_ask,
        min_open_interest=min_open_interest,
        min_volume=min_volume,
        yahoo_symbol=yahoo_symbol,
        per_bucket=per_bucket,
    )


def select_puts(
    symbol: str,
    spot: float,
    score: float,
    reasons: list[str],
    *,
    max_dte: int = 7,
    odte_max_dte: int = 1,
    otm_pct_max: float = 3.0,
    itm_pct_max: float = 1.5,
    max_ask: float = 15.0,
    min_open_interest: int = 50,
    min_volume: int = 10,
    yahoo_symbol: str | None = None,
    per_bucket: int = 1,
) -> list[CallCandidate]:
    """Pick best *real* ATM/OTM puts — mirror of select_calls for bearish sleeve."""
    return _select_side(
        symbol,
        spot,
        score,
        reasons,
        right="P",
        max_dte=max_dte,
        odte_max_dte=odte_max_dte,
        otm_pct_max=otm_pct_max,
        itm_pct_max=itm_pct_max,
        max_ask=max_ask,
        min_open_interest=min_open_interest,
        min_volume=min_volume,
        yahoo_symbol=yahoo_symbol,
        per_bucket=per_bucket,
    )


def _select_side(
    symbol: str,
    spot: float,
    score: float,
    reasons: list[str],
    *,
    right: str,
    max_dte: int = 7,
    odte_max_dte: int = 1,
    otm_pct_max: float = 3.0,
    itm_pct_max: float = 1.5,
    max_ask: float = 15.0,
    min_open_interest: int = 50,
    min_volume: int = 10,
    yahoo_symbol: str | None = None,
    per_bucket: int = 1,
) -> list[CallCandidate]:
    right = right.upper()
    fetch_sym = yahoo_symbol or symbol
    try:
        t = yf.Ticker(fetch_sym)
        expirations = list(t.options or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Options chain unavailable for %s: %s", fetch_sym, exc)
        return []

    if not expirations or spot <= 0:
        return []

    targets = _expiries_in_window(expirations, max_dte)
    if not targets:
        logger.info("No expiries within %s DTE for %s (have %s)", max_dte, symbol, expirations[:5])
        return []

    best_by_bucket: dict[str, list[tuple[float, CallCandidate]]] = {"0dte": [], "weekly": []}
    thesis = "; ".join(reasons) or ("ensemble bearish" if right == "P" else "ensemble bullish")

    for expiry, dte in targets:
        try:
            chain = t.option_chain(expiry)
            table = chain.puts if right == "P" else chain.calls
        except Exception as exc:  # noqa: BLE001
            logger.debug("chain %s %s failed: %s", symbol, expiry, exc)
            continue
        if table is None or table.empty:
            continue

        bucket = _bucket(dte, odte_max_dte)
        for _, row in table.iterrows():
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            # Call OTM% = (K-S)/S; put OTM% = (S-K)/S  (positive = OTM)
            if right == "P":
                moneyness = (spot - strike) / spot * 100
            else:
                moneyness = (strike - spot) / spot * 100
            if moneyness < -itm_pct_max or moneyness > otm_pct_max:
                continue

            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last_px = float(row.get("lastPrice") or 0)
            if ask <= 0 and last_px > 0:
                ask = last_px
                bid = bid or last_px * 0.95
            if ask <= 0 or ask > max_ask:
                continue

            def _safe_int(v: object, default: int = 0) -> int:
                try:
                    if v is None:
                        return default
                    f = float(v)
                    if f != f:  # NaN
                        return default
                    return int(f)
                except (TypeError, ValueError):
                    return default

            vol = _safe_int(row.get("volume"))
            oi = _safe_int(row.get("openInterest"))
            mid = (bid + ask) / 2 if bid > 0 else ask
            spread = (ask - bid) / ask if ask else 1.0
            if spread > 0.45 and oi < min_open_interest:
                continue

            rank = _rank_call(
                score=score,
                moneyness=moneyness,
                spread=spread,
                dte=dte,
                vol=vol,
                oi=oi,
                min_oi=min_open_interest,
                min_vol=min_volume,
                prefer_closer_dte=(bucket == "0dte"),
            )
            contract = str(row.get("contractSymbol") or "")
            if not contract or contract.endswith("_SYN"):
                continue

            cand = CallCandidate(
                symbol=symbol,
                contract=contract,
                expiry=expiry,
                dte=dte,
                strike=strike,
                spot=spot,
                bid=bid,
                ask=ask,
                mid=mid,
                volume=vol,
                open_interest=oi,
                moneyness_pct=moneyness,
                score=score,
                thesis=thesis,
                dte_bucket=bucket,
                synthetic=False,
                spread_pct=spread * 100,
                right=right,
            )
            best_by_bucket[bucket].append((rank, cand))

    out: list[CallCandidate] = []
    for bucket, rows in best_by_bucket.items():
        rows.sort(key=lambda x: x[0], reverse=True)
        seen: set[str] = set()
        picked = 0
        for _, cand in rows:
            if cand.contract in seen:
                continue
            seen.add(cand.contract)
            out.append(cand)
            picked += 1
            if picked >= per_bucket:
                break
    return out


def select_call(
    symbol: str,
    spot: float,
    score: float,
    reasons: list[str],
    **kwargs: Any,
) -> CallCandidate | None:
    """Back-compat: best single contract (prefers 0DTE, else weekly)."""
    kwargs.setdefault("per_bucket", 1)
    cands = select_calls(symbol, spot, score, reasons, **kwargs)
    if not cands:
        return None
    zero = [c for c in cands if c.dte_bucket == "0dte"]
    return zero[0] if zero else cands[0]
