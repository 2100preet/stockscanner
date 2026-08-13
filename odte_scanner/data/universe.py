"""Liquid equity universe for screener tabs (Signa/Intellectia-style breadth).

Full market scan via Yahoo is rate-limited; we use a curated liquid set (~S&P 100
+ high-volume growth/ETF names) that covers most optionable names traders care about.
"""

from __future__ import annotations

from typing import Any

# S&P 100-ish + liquid optionables / ETFs commonly on Signa/Intellectia screens
LIQUID_UNIVERSE: list[str] = [
    # Index / mega ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XBI", "SMH", "SOXX",
    "GLD", "SLV", "TLT", "HYG", "EEM", "USO", "UNG",
    # Mega / large
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "BRK-B",
    "JPM", "V", "MA", "UNH", "XOM", "JNJ", "WMT", "PG", "HD", "COST",
    "ABBV", "MRK", "CVX", "PEP", "KO", "BAC", "CRM", "AMD", "NFLX", "ADBE",
    "TMO", "ACN", "CSCO", "MCD", "LIN", "ABT", "DHR", "WFC", "TXN", "DIS",
    "INTC", "QCOM", "AMAT", "INTU", "IBM", "GE", "CAT", "BA", "GS", "MS",
    "AXP", "BLK", "BKNG", "ISRG", "AMGN", "PFE", "PM", "T", "VZ", "NEE",
    "UPS", "LOW", "SBUX", "MDT", "GILD", "CVS", "CMCSA", "ORCL", "NOW", "PANW",
    # High-beta / growth optionables
    "MU", "ARM", "PLTR", "COIN", "MSTR", "HOOD", "UBER", "LYFT", "SHOP", "SQ",
    "SNOW", "CRWD", "NET", "DDOG", "MDB", "ZS", "OKTA", "ROKU", "SNAP", "PINS",
    "RBLX", "U", "SOFI", "AFRM", "UPST", "RIVN", "LCID", "NIO", "MARA", "RIOT",
    "SMCI", "DELL", "HPE", "TSM", "ASML", "BABA", "PDD", "JD", "SE", "MELI",
    # Energy / cyclicals / others
    "COP", "SLB", "OXY", "HAL", "F", "GM", "NKE", "LULU", "DE", "HON",
    "UNP", "RTX", "LMT", "NOC", "SPGI", "CME", "ICE", "SCHW", "C", "USB",
    # ML6 neocloud / AI infra / data-center earnings sleeve (keep liquid mega names above)
    "FRMI", "TSSI", "IREN", "APLD", "CORZ", "NBIS", "CRWV",
]

# ML6 earnings-catalyst neocloud sleeve (also listed in LIQUID_UNIVERSE)
ML6_UNIVERSE: list[str] = [
    "FRMI",  # Fermi — Aug 13 BMO
    "TSSI",  # TSS Inc — Aug 13 AMC
    "IREN",  # ~Aug 27 AMC
    "ORCL",  # ~Sep 10 AMC (already in liquid mega)
    "APLD",  # ~Oct 8 est
    "CORZ",  # ~Oct 23 est
    "NBIS",  # Nebius — peer / reference
    "CRWV",  # CoreWeave — peer / reference
]

# Focus list stays smaller for 0DTE options chains (rate limits)
FOCUS_DEFAULT: list[str] = [
    "SPY", "QQQ", "IWM", "SPX", "XSP", "GLD", "SLV", "TLT", "SMH", "XLF",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "AMZN", "GOOGL", "AVGO",
    "INTC", "MU", "USO", "UNG", "NFLX", "CRM", "ORCL", "ADBE", "QCOM", "AMAT",
    "ARM", "PLTR", "COIN", "MSTR", "HOOD", "UBER", "JPM", "BAC", "XOM", "COST",
    "TSM", "DIA", "XLK", "XLE", "SOXX", "HYG", "EEM",
]


def liquid_universe() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in LIQUID_UNIVERSE:
        u = s.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ml6_universe() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in ML6_UNIVERSE:
        u = s.replace(".", "-").upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_scan_universe(cfg: dict[str, Any], *, mode: str | None = None) -> list[str]:
    """
    mode:
      focus    — config.tickers / calendar focus (options 0DTE)
      liquid   — liquid_universe screener set
      screener — alias of liquid
      all      — union of focus + liquid
      ml6      — neocloud / AI infra earnings sleeve
    """
    uni = cfg.get("universe") or {}
    mode = (mode or uni.get("mode") or "focus").lower()
    from odte_scanner.calendars import resolve_universe

    focus = resolve_universe(cfg)
    liquid = list(uni.get("liquid_tickers") or liquid_universe())
    # Normalize BRK.B style
    liquid = [s.replace(".", "-").upper() for s in liquid]

    if mode in ("liquid", "screener"):
        return liquid
    if mode == "ml6":
        return ml6_universe()
    if mode == "all":
        seen: set[str] = set()
        out: list[str] = []
        for s in focus + liquid + ml6_universe():
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out
    # focus: keep existing liquid names; also ensure ML6 sleeve is discoverable in "all"
    return focus
