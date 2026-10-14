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

## Always-on ZeroLoss (near-instant board + Webull)

GitHub Pages is a **static** snapshot (~10m Actions cycle). For BUY/SELL that update
as soon as each focus scan finishes, deploy the Docker UI:

1. [Railway](https://railway.app/new) or [Render](https://render.com/) → this repo  
2. Set secrets: Tradier / UW / Telegram / Webull as needed  
3. Webull paper (sandbox) submits: set the sandbox key, secret, and account id, then `WEBULL_PAPER_SUBMIT=1`  
4. Open the service URL — background **live desk worker** scans continuously in RTH  

See [`docs/HOW_SIGNALS_WORK.md`](docs/HOW_SIGNALS_WORK.md).

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
```

## Always-on (24×7) hosting

**Public desk (GitHub Pages — uses your GitHub only):**  
https://2100preet.github.io/stockscanner/

Actions runs a focus scan on a weekday schedule and publishes a **read-only** snapshot. Tap **Actions → Signal Desk Pages → Run workflow** to refresh now. Scan / Webull buttons need a live Flask host.

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


## Webull paper bridge (golden signals only)

The auto-trade bridge stages orders for two rows only. Everything else stays on the board and is not sent:

- Sure Winner **gold** tier (`confidence_tier: MAX`, color gold)
- Unusual options flow tape rows with tier **golden**

`live_trading.dry_run` stays `true` and `live_trading.enabled` stays `false`. Keys in the environment do not submit anything.

Webull's official OpenAPI paper account is the **sandbox** trading environment (`api.sandbox.webull.com`), labeled "Using OpenAPI service in Paper Trading" in the developer portal. It uses the same options order (`order_v2.place_option` / `POST /trading/orders/place`, `instrument_type: OPTION`). Sandbox keys and the sandbox account id are separate from production, and sandbox fills do not land in the live brokerage account. The in-app paper center at `act.webullbroker.com` is not this API.

On Fly (secrets, not the repo):

| Env | Required | Effect |
|-----|----------|--------|
| `WEBULL_APP_KEY` | yes, to submit | Sandbox app key |
| `WEBULL_APP_SECRET` | yes, to submit | Sandbox app secret |
| `WEBULL_ACCOUNT_ID` | yes, to submit | Sandbox account id (`get_account_list` on the sandbox host) |
| `WEBULL_PAPER_ACCOUNT_ID` | optional | Overrides `WEBULL_ACCOUNT_ID` while paper mode is on |
| `WEBULL_PAPER_SUBMIT=1` | yes, to submit | Enables submit and turns dry-run off, still on the sandbox host |
| `WEBULL_GOLDEN_ONLY=0` | optional | Route every desk again (default is golden-only) |

Do not set `WEBULL_LIVE=1` for this. That flag targets production `api.webull.com` and is ignored while `live_trading.paper: true` (the default). Production also requires `paper: false`.

Until `WEBULL_PAPER_SUBMIT=1` and the three sandbox values are set, golden signals are dry-run rows in `outputs/webull_orders.json` only.

## Tests

```bash
pytest -q
```
