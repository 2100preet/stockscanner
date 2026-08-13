# Signal Desk — Multi-Horizon Call / Swing Scanner

Signa-style **action cards** (LONG/WAIT + entry/stop/R:R) and Intellectia-style **horizons** with separate algos and win rates:

| Horizon | Hold | Algos emphasize | Win% forward |
|---------|------|-----------------|--------------|
| **0DTE** | same / next session | gap-and-go, breakout, volume, VIX | 1 session |
| **1 Week** | ~5 sessions | EMA/MACD/RS, pullbacks | 5 sessions |
| **Swing** | 1–3 months | stage analysis, trend structure, medium RS | ~42 sessions |
| **ML6** | earnings catalyst | drawdown + days-to-print + neocloud theme + liquidity; **reaction gate** (no blind BUY) | catalyst window |
| **Red Flag** | 0DTE session | VolSignals-inspired dealer/charm proxy on SPY — blocks index 0DTE long calls when active | intraday |

Quality gates (score + confirming algos) filter for **fewer, higher-conviction** signals to raise measured win rates. **ML6** is a separate earnings-catalyst neocloud / AI infra model (NBIS/CRWV style) — not the 0DTE ensemble. **Red Flag** uses public option-chain proxy data (not VS3D proprietary positioning).

> **Not financial advice.** Options can go to zero. Research / paper-trading only. Not affiliated with Signa or Intellectia.

## What it does

1. Pulls daily OHLCV (and VIX) via Yahoo Finance  
2. Scores each symbol on **three separate ensembles**  
3. Surfaces quality **action cards** + live 0DTE/1W call candidates  
4. Screener covers ~100 liquid names (full market scan isn’t practical on free Yahoo limits)  
5. Tabbed UI: Overview · 0DTE · 1 Week · Swing · **ML6 Neocloud** · Screener · Journal  
6. Paper journal: enter on BUY NOW, exit on SELL NOW with profit%
7. **ML6 board**: FRMI / TSSI / IREN (+ ORCL, APLD, CORZ, NBIS, CRWV) with earnings, theme, score, and WATCH / WAIT_FOR_CONFIRMATION / BUY_ONLY_IF_ACCEPTED statuses

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Focus list (options 0DTE names)
python -m odte_scanner scan --no-paper

# Broader liquid universe (~S&P100 + optionables)
python -m odte_scanner scan --universe liquid --no-paper

# ML6 — earnings-catalyst neocloud / AI infra (reaction-gated; no auto BUY on print)
python -m odte_scanner scan --horizon ml6 --no-paper

# Continuous 24/5 watch (extended-hours quotes + repeated scans). Ctrl+C to stop.
python -m odte_scanner watch --interval 60
python -m odte_scanner watch --tickers MU,NVDA,TSLA,SPY,QQQ --interval 30 --cycles 3

# Web UI dashboard (scores, strikes to buy, 24h movers)
python -m odte_scanner ui
# then open http://127.0.0.1:8787

# Backtest signals
python -m odte_scanner backtest --tickers SPY,QQQ,NVDA,TSLA

# Paper ledger
python -m odte_scanner ledger
```

## Config

Edit `config.yaml`:

- `tickers` — full scan universe (**Fridays are kept**)  
- `expiry_calendars.everyday` — Mon–Fri daily 0DTE: **SPY, QQQ, IWM, SPX, XSP**  
- `expiry_calendars.monday_wednesday` — Mon+Wed short-dated: **GLD, SLV, TLT, MU, AVGO, TSLA, NVDA, GOOGL, …**  
- `expiry_calendars.wednesday_only` — **USO, UNG**  
- `expiry_calendars.friday_weeklies` — extra liquid Friday names (NFLX, PLTR, …) — **not dropped**  
- `options.prefer_expiry_weekdays: [0,1,2,3,4]` — Mon through Fri  
- `symbol_aliases` — maps `SPX` → `^SPX` for Yahoo  
- `scan.min_score`, `options.max_dte`, risk caps, live trading flag  

On Mon/Wed the scanner **boosts** Mon/Wed/everyday names but still scores Friday-weekly names. Friday expiries are always included in option selection.

## Insights / journal

The UI tracks **suggested entries and exits**:

- **BUY NOW** → paper enter @ live ask  
- **SELL NOW** → paper exit @ live bid  
- Records **entered at / exited at**, **profit%**, P&L $, hold time  
- Performance cards: **win rate**, avg profit%, account return  

Data file: `outputs/signal_journal.json`


## Live trading

Live order routing is **disabled**. To go live you must:

1. Implement a broker adapter under `odte_scanner/trading/`  
2. Set credentials via environment variables  
3. Flip `live_trading.enabled: true`  

Until then, only paper trades are recorded.

## Tests

```bash
pytest -q
```
