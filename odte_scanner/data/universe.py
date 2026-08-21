"""Liquid equity universe for screener tabs (Signa/Intellectia-style breadth).

Full market scan via Yahoo is rate-limited; we use a curated liquid set (~S&P 100
+ high-volume growth/ETF names) that covers most optionable names traders care about.
Includes mid/small-cap and DRAM/memory sleeves for the $1k→$1M challenge + earnings watch.
"""

from __future__ import annotations

from typing import Any

# DRAM / memory / semi-equipment thematic sleeve (earnings watch + challenge breadth)
DRAM_MEMORY_UNIVERSE: list[str] = [
    "DRAM",
    "MU",
    "WDC",
    "STX",
    "SNDK",
    "AMAT",
    "LRCX",
    "KLAC",
    "ENTG",
    "TER",
    "ON",
    "MRVL",
    "NXPI",
    "AVGO",
    "AMD",
    "NVDA",
    "TSM",
    "INTC",
    "QCOM",
    "TXN",
    "SMH",
    "SOXX",
]

# Liquid mid / small-cap optionables (challenge + screener breadth)
MID_SMALL_UNIVERSE: list[str] = [
    # Mid-cap growth / thematic
    "DKNG", "CELH", "PATH", "IOT", "DUOL", "GTLB", "CFLT", "BILL", "TOST", "APP",
    "TTD", "MDB", "ENPH", "SEDG", "FSLR", "RUN", "CHPT", "BLNK", "WOLF", "ON",
    "MRVL", "WDC", "STX", "ENTG", "TER", "KLAC", "LRCX", "CDNS", "SNPS", "ANET",
    # Small / high-beta optionables
    "IONQ", "RGTI", "QBTS", "ASTS", "JOBY", "RKLB", "LUNR", "OKLO", "SMR", "BE",
    "HIMS", "OSCR", "CLOV", "RXRX", "CRSP", "NTLA", "BEAM", "DNA", "TEM", "TDOC",
    "SOUN", "BBAI", "AI", "PLUG", "FCEL", "SPCE", "OPEN", "CVNA", "W", "CHWY",
    "PTON", "BYND", "SPWR", "LAZR", "VLDR", "NKLA", "GOEV", "FFIE",
    "IREN",  # Iris Energy — high-beta AI / bitcoin infra optionable
    # AI connectivity / infra / miners elevated to focus
    "ALAB", "CRDO", "VRT", "APLD", "CIFR", "WULF", "CEG", "GEV",
]

# Rare earth / uranium / lithium / critical minerals — MP was absent the same
# way MRNA was: never on the focus scan. USAR is an earnings darling but was
# not on the 0DTE focus list Pages publishes.
CRITICAL_MINERALS_UNIVERSE: list[str] = [
    "MP",     # MP Materials
    "USAR",   # USA Rare Earth
    "UUUU",   # Energy Fuels
    "UEC",    # Uranium Energy
    "CCJ",    # Cameco
    "DNN",    # Denison
    "NXE",    # NexGen
    "LEU",    # Centrus
    "TMC",    # TMC the metals company
    "CRML",   # Critical Metals
    "ALB",    # Albemarle
    "SQM",    # Sociedad Química
    "LAC",    # Lithium Americas
    "FCX",    # Freeport
    "AA",     # Alcoa
    "X",      # US Steel
    "NEM",    # Newmont
    "GOLD",   # Barrick
]

# Event / biotech sleeve — MRNA was missing from every scan list on 2026-08-19
# when it gapped +84% / closed +177% on a Phase 3 melanoma readout.
BIOTECH_CATALYST_UNIVERSE: list[str] = [
    "MRNA",
    "BNTX",
    "NVAX",
    "REGN",
    "VRTX",
    "GILD",
    "AMGN",
    "BIIB",
    "MRK",
    "BMY",
    "LLY",
    "NVO",
    "PFE",
    "JNJ",
    "ABBV",
    "XBI",
    "IBB",
    "CRSP",
    "NTLA",
    "BEAM",
    "RXRX",
]

# Earnings Whispers–style most-anticipated names (often missing from S&P100 lists).
# Keep in liquid + earnings watch even when Yahoo coverage lags on new IPOs.
EARNINGS_DARLINGS_UNIVERSE: list[str] = [
    # AI / infra / compute
    "CRWV",  # CoreWeave
    "CBRS",  # Cerebras
    "NBIS",  # Nebius
    "INFQ",  # Infleqtion
    "QUBT",
    "ENVX",
    "LITE",
    "COHR",
    # Space / aerospace / energy
    "FLY",   # Firefly Aerospace
    "BETA",  # Beta Technologies
    "XE",    # X-Energy
    "FAC",   # Factorial
    "VG",    # Venture Global
    "BTDR",
    "USAR",
    "TMC",
    # Consumer / growth / fintech prints in the spotlight
    "FIGR",  # Figure
    "GEMI",  # Gemini
    "TMS",   # Teamshares
    "BLSH",  # Bullish
    "CRCL",  # Circle
    "CAVA",
    "ONON",
    "SMCI",  # Super Micro — high-attention AI server / earnings print
    # Liquid EW names that belong on earnings + flow desks (not mega megas like CSCO/AMAT)
    "SE", "HIMS", "RKLB", "ASTS", "PLUG", "LUNR",
]

# S&P 100-ish + liquid optionables / ETFs commonly on Signa/Intellectia screens
LIQUID_UNIVERSE: list[str] = [
    # Index / mega ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XBI", "SMH", "SOXX",
    "GLD", "SLV", "TLT", "HYG", "EEM", "USO", "UNG", "IBIT",
    # Mega / large
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "BRK-B",
    "JPM", "V", "MA", "UNH", "XOM", "JNJ", "WMT", "PG", "HD", "COST",
    "ABBV", "MRK", "MRNA", "BNTX", "NVAX", "LLY", "NVO", "BMY", "XBI", "IBB",
    "CVX", "PEP", "KO", "BAC", "CRM", "AMD", "NFLX", "ADBE",
    "TMO", "ACN", "CSCO", "MCD", "LIN", "ABT", "DHR", "WFC", "TXN", "DIS",
    "INTC", "QCOM", "AMAT", "INTU", "IBM", "GE", "CAT", "BA", "GS", "MS",
    "AXP", "BLK", "BKNG", "ISRG", "AMGN", "PFE", "PM", "T", "VZ", "NEE",
    "UPS", "LOW", "SBUX", "MDT", "GILD", "CVS", "CMCSA", "ORCL", "NOW", "PANW",
    # High-beta / growth optionables
    "MU", "ARM", "PLTR", "COIN", "MSTR", "HOOD", "UBER", "LYFT", "SHOP", "SQ",
    "SNOW", "CRWD", "NET", "DDOG", "MDB", "ZS", "OKTA", "ROKU", "SNAP", "PINS",
    "RBLX", "U", "SOFI", "AFRM", "UPST", "RIVN", "LCID", "NIO", "MARA", "RIOT",
    "SMCI", "DELL", "HPE", "TSM", "ASML", "BABA", "PDD", "JD", "SE", "MELI",
    "MRNA",  # Moderna — event / biotech catalyst (was missing on Phase 3 day)
    # Recent mega IPOs / high-attention names (missed when only on curated lists)
    "SPCX",
    # AI power / cooling / connectivity (focus-elevated)
    "VRT", "CEG", "GEV", "ALAB", "CRDO", "APLD", "CIFR", "WULF",
    "IBIT",  # iShares Bitcoin Trust — liquid BTC ETF options
    # Energy / cyclicals / others
    "COP", "SLB", "OXY", "HAL", "F", "GM", "NKE", "LULU", "DE", "HON",
    "UNP", "RTX", "LMT", "NOC", "SPGI", "CME", "ICE", "SCHW", "C", "USB",
    # ML6 neocloud / AI infra / data-center earnings sleeve (keep liquid mega names above)
    "FRMI", "TSSI", "IREN", "APLD", "CORZ", "NBIS", "CRWV",
    # Mid / small sleeve
    *MID_SMALL_UNIVERSE,
    # DRAM / memory thematic
    *DRAM_MEMORY_UNIVERSE,
    # This week's most-anticipated earnings darlings
    *EARNINGS_DARLINGS_UNIVERSE,
    # Biotech / event names the 0DTE focus list previously dropped
    *BIOTECH_CATALYST_UNIVERSE,
    *CRITICAL_MINERALS_UNIVERSE,
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

_ETF = {
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XBI", "SMH", "SOXX",
    "GLD", "SLV", "TLT", "HYG", "EEM", "USO", "UNG", "IBIT",
}

# Explicit small-cap sleeve (rest of MID_SMALL_UNIVERSE treated as mid)
_SMALL = {
    "IONQ", "RGTI", "QBTS", "ASTS", "JOBY", "RKLB", "LUNR", "OKLO", "SMR", "BE",
    "CLOV", "RXRX", "CRSP", "NTLA", "BEAM", "DNA", "TEM", "TDOC",
    "SOUN", "BBAI", "AI", "PLUG", "FCEL", "SPCE", "OPEN", "PTON", "BYND",
    "SPWR", "LAZR", "VLDR", "NKLA", "GOEV", "FFIE", "CHPT", "BLNK", "WOLF",
    # Newer high-beta earnings darlings
    "FLY", "BETA", "INFQ", "QUBT", "ENVX", "BTDR", "USAR", "TMC", "TMS",
    "FAC", "XE", "BLSH",
}

_MID = {
    "COIN", "MSTR", "HOOD", "LYFT", "SQ", "NET", "DDOG", "MDB", "ZS", "OKTA",
    "ROKU", "SNAP", "PINS", "RBLX", "U", "SOFI", "AFRM", "UPST", "RIVN", "LCID",
    "NIO", "MARA", "RIOT", "SMCI", "HPE", "PDD", "JD", "SE", "SLB", "OXY", "HAL",
    "F", "GM", "DKNG", "CELH", "PATH", "IOT", "DUOL", "GTLB", "CFLT", "BILL",
    "TOST", "APP", "TTD", "ENPH", "SEDG", "FSLR", "RUN", "ON", "WDC", "STX",
    "ENTG", "TER", "HIMS", "OSCR", "CVNA", "W", "CHWY", "DRAM", "SNDK", "NXPI",
    # Mid-tier earnings darlings
    "CRWV", "CBRS", "NBIS", "FIGR", "GEMI", "VG", "CAVA", "ONON", "LITE", "COHR",
    "CRCL",
} | (set(MID_SMALL_UNIVERSE) - _SMALL)

_DRAM = frozenset(DRAM_MEMORY_UNIVERSE)
_DARLINGS = frozenset(s.replace(".", "-").upper() for s in EARNINGS_DARLINGS_UNIVERSE)

# Focus list stays smaller for 0DTE options chains (rate limits)
FOCUS_DEFAULT: list[str] = [
    "SPY", "QQQ", "IWM", "SPX", "XSP", "GLD", "SLV", "TLT", "SMH", "XLF",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "AMZN", "GOOGL", "AVGO",
    "INTC", "MU", "USO", "UNG", "NFLX", "CRM", "ORCL", "ADBE", "QCOM", "AMAT",
    "ARM", "PLTR", "COIN", "MSTR", "HOOD", "UBER", "JPM", "BAC", "XOM", "COST",
    "TSM", "DIA", "XLK", "XLE", "SOXX", "HYG", "EEM", "SPCX", "DDOG",
    # Most-anticipated / high-attention names (swing / earnings / focus scan)
    "CRWV", "CBRS", "FLY", "FIGR", "GEMI", "NBIS", "BETA", "XE",
    "CRCL", "NOW", "SMCI", "DELL",
    "SE", "HIMS", "RKLB", "ASTS", "PLUG", "CSCO", "IREN",
    # Curated high-potential (memory / AI infra / power / liquid mid) — not full S&P 500
    "SNDK", "WDC", "STX", "LRCX", "KLAC", "MRVL", "ANET", "ALAB", "CRDO", "APP",
    "VRT", "CEG", "GEV", "APLD", "CIFR", "WULF",
    # Sticky-note + event tape — never drop MRNA-class / IBIT / SOFI from focus
    "IBIT", "SOFI", "MRNA", "BNTX", "XBI", "LLY", "NVO", "MRK", "PFE", "NKE",
    # Critical minerals / rare earth / uranium — MP/USAR were the same miss class
    "MP", "USAR", "UUUU", "CCJ", "ALB", "TMC",
]


def _dedupe(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        u = str(s).replace(".", "-").upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ml6_universe() -> list[str]:
    return _dedupe(list(ML6_UNIVERSE))


def mid_small_universe() -> list[str]:
    return _dedupe(list(MID_SMALL_UNIVERSE))


def dram_memory_universe() -> list[str]:
    return _dedupe(list(DRAM_MEMORY_UNIVERSE))


def earnings_darlings_universe() -> list[str]:
    return _dedupe(list(EARNINGS_DARLINGS_UNIVERSE))


def liquid_universe() -> list[str]:
    return _dedupe(list(LIQUID_UNIVERSE))


def biotech_catalyst_universe() -> list[str]:
    return _dedupe(list(BIOTECH_CATALYST_UNIVERSE))


def catalyst_universe() -> list[str]:
    """Always-on sleeve for ZeroLoss Do-Not-Miss (gaps, news, unusual volume)."""
    return _dedupe(
        list(BIOTECH_CATALYST_UNIVERSE)
        + list(CRITICAL_MINERALS_UNIVERSE)
        + list(EARNINGS_DARLINGS_UNIVERSE)
        + [
            "SPY",
            "QQQ",
            "IWM",
            "NVDA",
            "TSLA",
            "SMCI",
            "CRWV",
            "NBIS",
            "AAPL",
            "MSFT",
            "AMZN",
            "META",
            "GOOGL",
            "AMD",
            "MU",
            "COIN",
            "MSTR",
            "PLTR",
            "OKLO",
            "SMR",
            "PFE",
            "NKE",
        ]
    )


def challenge_hist_universe(
    *,
    max_mid_small: int = 45,
    max_liquid: int = 35,
    max_darlings: int = 25,
) -> list[str]:
    """Symbols that need walk-forward hist win rates for the $1k→$1M challenge board."""
    seen: set[str] = set()
    out: list[str] = []
    for sym in (
        mid_small_universe()[:max_mid_small]
        + earnings_darlings_universe()[:max_darlings]
        + dram_memory_universe()
        + liquid_universe()[:max_liquid]
    ):
        key = str(sym).replace(".", "-").upper()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def market_cap_tier(symbol: str) -> str:
    """Return etf | mega_large | mid | small | dram_memory | unknown for challenge reasons."""
    s = str(symbol).replace(".", "-").upper()
    if s in _ETF:
        return "etf"
    if s in _SMALL:
        return "small"
    if s in _MID:
        return "mid"
    # Known liquid mega/large (before DRAM-only leftovers like niche memory names)
    if s in set(LIQUID_UNIVERSE) - set(MID_SMALL_UNIVERSE) - _DRAM - _DARLINGS:
        return "mega_large"
    if s in set(LIQUID_UNIVERSE) - set(MID_SMALL_UNIVERSE) - _DARLINGS:
        return "mega_large"
    if s in _DRAM:
        return "dram_memory"
    if s in _DARLINGS:
        return "mid"
    return "unknown"


def pages_ci_option_symbols(
    by_horizon: dict[str, list[Any]],
    *,
    min_score: float,
    cap: int = 8,
    min_hist_win_pct: float = 80.0,
    min_hist_win_samples: int = 5,
    win_table: dict[str, Any] | None = None,
) -> set[str]:
    """Small chain set for GitHub Pages so BUY NOW can fire without a full Yahoo fan-out."""
    ranked: list[tuple[int, float, str]] = []
    for hz in ("0dte", "weekly"):
        for ts in by_horizon.get(hz) or []:
            quality = bool(getattr(ts, "quality", False) if not isinstance(ts, dict) else ts.get("quality"))
            score = float(
                getattr(ts, "ensemble_score", 0)
                if not isinstance(ts, dict)
                else (ts.get("ensemble_score") or 0)
            )
            if not quality and score < min_score:
                continue
            raw = getattr(ts, "symbol", "") if not isinstance(ts, dict) else (ts.get("symbol") or "")
            sym = str(raw).upper()
            if not sym:
                continue
            row = ((win_table or {}).get("symbols") or {}).get(sym) or {}
            gated = False
            for bucket in (hz, "0dte", "weekly", "swing", "monthly"):
                stats = row.get(bucket) or {}
                win = stats.get("win_pct")
                n = int(stats.get("trades") or 0)
                if win is not None and float(win) >= min_hist_win_pct and n >= min_hist_win_samples:
                    gated = True
                    break
            ranked.append((0 if gated else 1, -score, sym))
    ranked.sort()
    out: list[str] = []
    seen: set[str] = set()
    for _gate, _score, sym in ranked:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= cap:
            break
    return set(out)


def resolve_scan_universe(cfg: dict[str, Any], *, mode: str | None = None) -> list[str]:
    """
    mode:
      focus    — config.tickers / calendar focus (options 0DTE)
      liquid   — liquid_universe screener set
      screener — alias of liquid
      all      — union of focus + liquid
      ml6      — neocloud / AI infra earnings sleeve
      catalyst / zeroloss — event + biotech sleeve unioned with focus
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
    if mode in ("catalyst", "zeroloss"):
        return _dedupe(catalyst_universe() + focus)
    if mode == "all":
        return _dedupe(focus + liquid + ml6_universe())
    return focus
