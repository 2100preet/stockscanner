# ZeroLoss — miss-prevention desk

This is **not** an only-winning-stocks indicator. Nothing will recover a half-million on a promise. ZeroLoss exists because the previous Signal Desk **never scanned MRNA** on 2026-08-19 (Phase 3 melanoma readout: gap ~+84%, close +177%, ~45× volume). The hist-win ≥80% gate hid movers; the focus list was AI/0DTE names.

**What changed**
- Always-on **biotech / event sleeve** (MRNA, BNTX, XBI, LLY, MRK, …)
- **Do not miss** lane: gap ≥8% **or** day ≥12% **or** ≥5× relative volume **or** news + move
- Bullflow-style flow table from **Yahoo chains + delayed FINRA ATS** — not OPRA, not affiliated with [bullflow.io](https://bullflow.io)

Live Pages site:  
https://2100preet.github.io/zeroloss/

> **Not financial advice.** Options can go to zero. Binary events gap both ways. Paper / research only.

The older multi-horizon desks (0DTE / 1W / swing / ML6) are still in the app under other tabs.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Focus list (options 0DTE names) — also scores ZeroLoss catalyst sleeve
python -m odte_scanner scan --no-paper

# Event / biotech sleeve + focus (MRNA-class names)
python -m odte_scanner scan --universe catalyst --no-paper

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
```

## Always-on (24×7) hosting

**Public desk (GitHub Pages — uses your GitHub only):**  
https://2100preet.github.io/zeroloss/

Actions runs a focus scan on a weekday schedule and publishes a **read-only** snapshot. Tap **Actions → ZeroLoss Pages → Run workflow** to refresh now. Scan / Webull buttons need a live Flask host.

Cursor cloud agents and free tunnels (`trycloudflare`, `localhost.run`) are **ephemeral** — they die when the agent sleeps.

**Interactive 24×7 Flask (Railway / Render):**

1. [Railway → New Project → Deploy from GitHub](https://railway.app/new) → `2100preet/stockscanner` (`Dockerfile` / `railway.toml`)
2. Or [Render → New Web Service](https://render.com/) → same repo (`render.yaml`)

```bash
# Local Docker
docker build -t signal-desk .
docker run -p 8787:8787 -e PORT=8787 -v signal-desk-data:/app/outputs signal-desk
```

```bash
# Build the Pages site locally
python -m odte_scanner export-pages --out site
```

Mount a volume on `/app/outputs` so journals and recommendation logs persist.

Demo tunnels (not 24×7):

```bash
./scripts/run_public.sh   # UI + cloudflared quick tunnel
```

```bash
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
