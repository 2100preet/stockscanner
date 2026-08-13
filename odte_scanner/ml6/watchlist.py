"""ML6 neocloud / AI infra earnings watchlist + hard bottom-line rules."""

from __future__ import annotations

from typing import Any

# Trade-gate statuses shown on the ML6 board
STATUS_WATCH = "WATCH"
STATUS_WAIT = "WAIT_FOR_CONFIRMATION"
STATUS_BUY_IF = "BUY_ONLY_IF_ACCEPTED"

# Theme tags used for scoring / UI
THEME_NEOCLOUD = "neocloud"
THEME_AI_INFRA = "ai_infra"
THEME_DATA_CENTER = "data_center"
THEME_POWER = "power"
THEME_CLOUD = "cloud"

# Curated ML6 sleeve — earnings-catalyst AI / neocloud / data-center upside
# session: bmo | amc | est
ML6_WATCHLIST: dict[str, dict[str, Any]] = {
    "FRMI": {
        "name": "Fermi",
        "earnings_date": "2026-08-13",
        "session": "bmo",
        "themes": [THEME_NEOCLOUD, THEME_AI_INFRA, THEME_DATA_CENTER, THEME_POWER],
        "peer_refs": ["NBIS", "CRWV"],
        "status": STATUS_WAIT,
        "rule_key": "frmi",
        "blurb": (
            "If you want maximum possible post-earnings upside, watch FRMI tomorrow morning "
            "(Aug 13 BMO)—but trade the confirmed reaction, not the report blindly."
        ),
        "gate": (
            "WAIT for open/hold above key level or AH high/VWAP acceptance. "
            "Do NOT auto BUY on the report alone."
        ),
    },
    "TSSI": {
        "name": "TSS Inc",
        "earnings_date": "2026-08-13",
        "session": "amc",
        "themes": [THEME_DATA_CENTER, THEME_AI_INFRA],
        "peer_refs": ["NBIS", "CRWV"],
        "status": STATUS_BUY_IF,
        "rule_key": "tssi",
        "blurb": (
            "If you want a more earnings-driven data-center infrastructure trade, use TSSI "
            "tomorrow after close (Aug 13 AMC), with a strict rule: only take it if the market "
            "accepts the report through the after-hours high/VWAP."
        ),
        "gate": "BUY_ONLY_IF_ACCEPTED — AH high / VWAP acceptance required after the print.",
    },
    "IREN": {
        "name": "Iris Energy",
        "earnings_date": "2026-08-27",
        "session": "amc",
        "themes": [THEME_NEOCLOUD, THEME_AI_INFRA, THEME_DATA_CENTER, THEME_POWER],
        "peer_refs": ["NBIS", "CRWV"],
        "status": STATUS_WATCH,
        "rule_key": "iren",
        "blurb": (
            "If you want the best liquid swing into late August, keep IREN on watch for Aug. 27; "
            "it is the most comparable business theme to NBIS/CRWV, but only take it if the print "
            "confirms AI ARR / contracted revenue ramp (not just a sympathy bounce)."
        ),
        "gate": (
            "WATCH into Aug 27 AMC. BUY only if earnings confirm AI ARR / contracted revenue "
            "progress vs sector sympathy alone."
        ),
    },
    "ORCL": {
        "name": "Oracle",
        "earnings_date": "2026-09-10",
        "session": "amc",
        "themes": [THEME_AI_INFRA, THEME_DATA_CENTER, THEME_CLOUD],
        "peer_refs": ["MSFT", "AMZN"],
        "status": STATUS_WATCH,
        "rule_key": "orcl",
        "blurb": (
            "Liquid AI infra mega-cap print ~Sep 10 AMC — watch for cloud/AI backlog confirmation."
        ),
        "gate": "WATCH — wait for post-print acceptance; do not chase the print blind.",
    },
    "APLD": {
        "name": "Applied Digital",
        "earnings_date": "2026-10-08",
        "session": "est",
        "themes": [THEME_DATA_CENTER, THEME_AI_INFRA, THEME_NEOCLOUD],
        "peer_refs": ["NBIS", "CRWV"],
        "status": STATUS_WATCH,
        "rule_key": "apld",
        "blurb": "Data-center / AI hosting sleeve — ~Oct 8 est. Watch drawdown + catalyst window.",
        "gate": "WATCH — prefer confirmed reaction after the print.",
    },
    "CORZ": {
        "name": "Core Scientific",
        "earnings_date": "2026-10-23",
        "session": "est",
        "themes": [THEME_DATA_CENTER, THEME_NEOCLOUD, THEME_POWER],
        "peer_refs": ["NBIS", "CRWV", "IREN"],
        "status": STATUS_WATCH,
        "rule_key": "corz",
        "blurb": "Neocloud / HPC hosting peer — ~Oct 23 est. Beaten-down catalyst watch.",
        "gate": "WATCH — reaction gate after print; no blind BUY.",
    },
    "NBIS": {
        "name": "Nebius",
        "earnings_date": "2026-08-12",
        "session": "bmo",
        "themes": [THEME_NEOCLOUD, THEME_AI_INFRA],
        "peer_refs": ["CRWV", "IREN"],
        "status": STATUS_WATCH,
        "rule_key": "nbis",
        "blurb": "Peer / reference neocloud name for ML6 theme scoring (NBIS/CRWV style).",
        "gate": "Reference peer — use for theme comparison; still require reaction confirmation.",
    },
    "CRWV": {
        "name": "CoreWeave",
        "earnings_date": "2026-08-11",
        "session": "amc",
        "themes": [THEME_NEOCLOUD, THEME_AI_INFRA, THEME_DATA_CENTER],
        "peer_refs": ["NBIS", "IREN"],
        "status": STATUS_WATCH,
        "rule_key": "crwv",
        "blurb": "Peer / reference GPU cloud name for ML6 theme scoring (NBIS/CRWV style).",
        "gate": "Reference peer — use for theme comparison; still require reaction confirmation.",
    },
}

# Hard bottom-line rules — must appear prominently in UI + encode trade gates
BOTTOM_LINE_RULES: list[dict[str, Any]] = [
    {
        "ticker": "FRMI",
        "headline": "FRMI (tomorrow morning / Aug 13 BMO)",
        "rule": ML6_WATCHLIST["FRMI"]["blurb"],
        "status": STATUS_WAIT,
        "priority": 1,
    },
    {
        "ticker": "TSSI",
        "headline": "TSSI (Aug 13 AMC)",
        "rule": ML6_WATCHLIST["TSSI"]["blurb"],
        "status": STATUS_BUY_IF,
        "priority": 2,
    },
    {
        "ticker": "IREN",
        "headline": "IREN (Aug 27)",
        "rule": ML6_WATCHLIST["IREN"]["blurb"],
        "status": STATUS_WATCH,
        "priority": 3,
    },
]


def ml6_tickers() -> list[str]:
    return list(ML6_WATCHLIST.keys())


def watch_row(symbol: str) -> dict[str, Any] | None:
    key = str(symbol).replace(".", "-").upper()
    row = ML6_WATCHLIST.get(key)
    if not row:
        return None
    return {"symbol": key, **row}
