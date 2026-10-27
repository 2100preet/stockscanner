"""Walk-forward results recorded on 2026-10-27. Not a live route.

Yahoo daily bars, auto-adjusted, 2023-01-03 through 2026-10-26 (957 sessions).
Costs are 5 bps per side. The second half of each trade list, after sorting
by entry date, is the out-of-sample half. None of these clear a 95% interval
above zero, and none are scanner signals.
"""

from __future__ import annotations

RECORDED_AT = "2026-10-27T03:20:00+00:00"

UNDERLYING_NOTE = (
    "Underlying return after 5 bps per side. Dollars are per $100 of the ETF, "
    "not an option contract. The second half is out of sample. "
    "A positive point estimate whose interval includes zero is not an edge."
)

SPREAD_NOTE = (
    "Defined-risk proxy, not a fill: the same trend trade, capped at a $100 loss "
    "and a $200 gain, minus $2 of slippage. It is a model of the underlying move."
)

STUDY = [
    {
        "name": "SPY trend 50/200, later half",
        "n": 12,
        "win_rate": 0.417,
        "expectancy_per_100": 0.40,
        "lo": -1.38,
        "hi": 2.18,
        "routed": False,
    },
    {
        "name": "QQQ trend 50/200, later half",
        "n": 13,
        "win_rate": 0.308,
        "expectancy_per_100": 0.24,
        "lo": -2.61,
        "hi": 3.08,
        "routed": False,
    },
    {
        "name": "IWM trend 50/200, later half",
        "n": 12,
        "win_rate": 0.500,
        "expectancy_per_100": 1.77,
        "lo": -1.35,
        "hi": 4.89,
        "routed": False,
    },
    {
        "name": "SPY gap fade >1%, later half",
        "n": 35,
        "win_rate": 0.457,
        "expectancy_per_100": 0.09,
        "lo": -0.42,
        "hi": 0.59,
        "routed": False,
    },
    {
        "name": "QQQ gap fade >1%, later half",
        "n": 75,
        "win_rate": 0.480,
        "expectancy_per_100": -0.02,
        "lo": -0.27,
        "hi": 0.23,
        "routed": False,
    },
    {
        "name": "IWM gap fade >1%, later half",
        "n": 78,
        "win_rate": 0.526,
        "expectancy_per_100": 0.31,
        "lo": -0.07,
        "hi": 0.70,
        "routed": False,
    },
    {
        "name": "SPY 30-minute opening range, 60-day sample",
        "n": 60,
        "win_rate": 0.383,
        "expectancy_per_100": -0.09,
        "lo": -0.19,
        "hi": 0.00,
        "routed": False,
    },
    {
        "name": "SPY trend as a capped debit-spread model, later half",
        "n": 12,
        "win_rate": 0.417,
        "expectancy_per_100": -5.80,
        "lo": -73.58,
        "hi": 61.98,
        "routed": False,
    },
    {
        "name": "QQQ trend as a capped debit-spread model, later half",
        "n": 13,
        "win_rate": 0.308,
        "expectancy_per_100": -34.11,
        "lo": -84.82,
        "hi": 16.60,
        "routed": False,
    },
    {
        "name": "IWM trend as a capped debit-spread model, later half",
        "n": 12,
        "win_rate": 0.500,
        "expectancy_per_100": 32.88,
        "lo": -47.40,
        "hi": 113.15,
        "routed": False,
    },
]

# Option dollars measured from the Fly paper books at 2026-10-27T03:04Z.
# These are the audit figures. The live table is recomputed from the ledgers
# and is what the gate uses. Zero-hold flat rows were not treated as trades.
PAPER_AUDIT = [
    {"source": "Lottery", "n": 13, "wins": 1, "losses": 9, "flats_in_book": 3, "expectancy": -28.08, "lo": -49.33, "hi": -6.83},
    {"source": "Options journal", "n": 11, "wins": 1, "losses": 9, "flats_in_book": 1, "expectancy": -8.90, "lo": -13.69, "hi": -4.12},
    {"source": "Weekly sleeve", "n": 7, "wins": 2, "losses": 4, "flats_in_book": 1, "expectancy": -37.46, "lo": -121.49, "hi": 46.58},
    {"source": "Challenge", "n": 6, "wins": 1, "losses": 5, "flats_in_book": 0, "expectancy": -26.00, "lo": -60.89, "hi": 8.89},
    {"source": "0DTE $1K", "n": 6, "wins": 1, "losses": 3, "flats_in_book": 2, "expectancy": -19.33, "lo": -50.19, "hi": 11.53},
    {"source": "RIP", "n": 7, "wins": 2, "losses": 5, "flats_in_book": 0, "expectancy": -20.86, "lo": -53.81, "hi": 12.10},
    {"source": "0DTE clock flats", "n": 8, "wins": 0, "losses": 0, "flats_in_book": 8, "expectancy": 0.0, "lo": 0.0, "hi": 0.0},
]
