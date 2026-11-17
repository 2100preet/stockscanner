# Focus candidates: HUBS, DUOL, CRM (Nov 16, 2026)

PR #187 added HUBS and DUOL to the focus scan (`config.yaml` tickers and
`FOCUS_DEFAULT`). CRM was already on both lists. This is the backtest that
decided whether they go in. Script: `scripts/backtest_focus_candidates.py`.
Results: `analysis/focus_candidates_backtest_5y.json` (daily legs over 5 years)
and `analysis/focus_candidates_backtest_2y.json` (the 2-year window
`backtest/win_rates.py` uses).

## Method

- **Daily quality signal** (feeds the Gold Setup hist-win gate and BUY NOW):
  the `win_rates.py` walk-forward. `score_ticker` on bars up to day *i* with
  the config weights, quality gate per horizon. On a signal, buy an ATM call
  at the close: 1DTE held 1 session (`0dte`), 7DTE held 5 sessions (`weekly`).
- **Power Hour**: 5-minute bars (Yahoo keeps 60 days). `decide_power_hour`
  at 15:00–15:45 ET every 5 minutes on bars up to the decision clock (no
  forming bar). First LONG/SHORT buys a 0DTE ATM call/put; exit at the signal
  stop or the 15:50 ET flatten.
- **Option costs**: Black-Scholes, IV = 1.1 × 20-day realized vol; pay
  3% of premium (min $0.025) over mid on entry and give it up on exit;
  $0.65/contract commission each side. P&L is % of premium paid.
- **Halves**: each leg split at its median signal date across the universe.
- **Baseline**: the 122 current focus names (SPX/XSP excluded).
- **Rule**: a name goes in only if every leg's average option P&L per trade
  is positive or not worse than the focus average, in both halves.

## Results (5-year daily legs; trades · option win % · avg option P&L % per trade)

| Leg | Half | Focus avg (pooled) | HUBS | DUOL | CRM (already in focus) |
|---|---|---|---|---|---|
| 0dte | 1st | 1409 · 31.6% · +12.9% | 14 · 42.9% · **−8.5%** | 8 · 12.5% · +70.6% | 19 · 31.6% · +59.7% |
| 0dte | 2nd | 1409 · 35.6% · +25.6% | 8 · 50.0% · +302% | 16 · 25.0% · +30.3% | 13 · 46.2% · +186% |
| weekly | 1st | 1214 · 34.2% · +12.6% | 13 · 38.5% · **−5.4%** | 19 · 36.8% · +39.7% | 8 · 50.0% · **−16.0%** |
| weekly | 2nd | 1222 · 34.9% · +13.3% | 7 · 28.6% · +59.9% | 10 · 50.0% · **−14.3%** | 9 · 22.2% · +12.9% |
| Power Hour | 1st | 3237 · 9.6% · −43.7% | 30 · 10.0% · −34.5% | 29 · 20.7% · −34.7% | 26 · 3.8% · −34.7% |
| Power Hour | 2nd | 3195 · 11.1% · −41.8% | 28 · 14.3% · −40.1% | 30 · 10.0% · −23.6% | 24 · 8.3% · **−59.4%** |

Bold = fails the rule. On the 2-year window HUBS fires the daily gate twice
(both losers) and DUOL nine times (second half −80% per trade).

**Verdict:** none of the three is added. HUBS fails both daily legs in the
first half; DUOL fails the weekly leg in the second half; CRM stays where it
already is (it fails the weekly first half and the Power Hour second half;
removing a name was out of scope). Re-run the script before revisiting; DUOL
is the closest.

## #187's liquid-volume warm

Measured on the box against Tradier: 273 liquid names, one batched quote call
of ~1.8 s, 265 rows, a 38 KB cache file, ~43 MB Python peak during the call.
Eight symbols never come back (BRK-B, SQ, ROKU, CFLT, VLDR, NKLA, GOEV, FFIE),
so the "missing" list is never empty and a background thread re-asks for them
on every snapshot build. With no names added it is not needed; it was not
merged.
