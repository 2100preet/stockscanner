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

The auto-trade bridge stages Gold Setup cards the desk renders in gold (`sure-max` and `sure-high`: confidence tiers MAX and HIGH) on `WEBULL_ACCOUNT_ID`. The exit study's second sleeve, challenge, stages on `WEBULL_ACCOUNT_ID_2` when that internal account id is set. SOLID cards and Golden unusual-flow prints stay on the board and are not sent.

`live_trading.dry_run` stays `true` and `live_trading.enabled` stays `false`. Keys in the environment do not submit anything.

Webull's official OpenAPI paper account is the **sandbox** trading environment (`api.sandbox.webull.com`), labeled "Using OpenAPI service in Paper Trading" in the developer portal. It uses the same options order (`order_v2.place_option` / `POST /trading/orders/place`, `instrument_type: OPTION`). Sandbox keys and the sandbox account id are separate from production, and sandbox fills do not land in the live brokerage account. The in-app paper center at `act.webullbroker.com` is not this API.

On Fly (secrets, not the repo):

| Env | Required | Effect |
|-----|----------|--------|
| `WEBULL_APP_KEY` | yes, to submit | Sandbox app key |
| `WEBULL_APP_SECRET` | yes, to submit | Sandbox app secret |
| `WEBULL_ACCOUNT_ID` | yes, to submit | Sandbox margin account id for Gold Setup cards (`get_account_list`) |
| `WEBULL_ACCOUNT_ID_2` | for the second desk | Internal id of sandbox cash account `DEA32RD3`, not the account number |
| `WEBULL_PAPER_ACCOUNT_ID` | optional | Overrides `WEBULL_ACCOUNT_ID` while paper mode is on |
| `WEBULL_PAPER_SUBMIT=1` | yes, to submit | Enables submit and turns dry-run off, still on the sandbox host |
| `WEBULL_GOLDEN_ONLY=0` | optional | Route every desk again (default is golden-only) |

`WEBULL_LIVE=1` does not place production orders. The bridge refuses `api.webull.com` and stays on the paper sandbox. One Webull client is reused, and sandbox order calls are spaced by `live_trading.submit_interval_sec` (default 1.5s). A 429 is retried with backoff on the same client order id; if it still fails the row stays `deferred` and the next sync retries it instead of placing a second order. `live_trading.max_golden_buys_per_sync` (default 2) caps new golden buys in one pass. The outgoing order uses the signal's OCC contract for right, expiry, and strike. New entries stop at 15:45 ET (0DTE stops at 15:00 ET). A sell is staged for a filled or dry-run Gold Setup card, reconciled with sandbox positions. A SUBMITTED day order that has not filled is cancelled at the exit or at 15:45 ET. 2DTE and weeklies are not flattened at 15:45. Positions opened under the old golden-flow rule are left alone unless `live_trading.close_legacy_positions` is true.

Until `WEBULL_PAPER_SUBMIT=1` and the three sandbox values are set, golden signals are dry-run rows in `outputs/webull_orders.json` only.

## Sparky Signals subscriptions

Public pricing lives at `subscribe/` on the GitHub Pages site (and at `/subscribe/` on the Fly host). The desk's paper-trading bridge is unchanged: Webull still stages gold MAX/HIGH cards on the sandbox account and does not route Golden Flow.

Customer-facing notes call those cards **Gold Setup**. Alerts are impersonal and identical for every subscriber. Each one ends with: "For educational and informational purposes only. Not investment advice. Trading involves substantial risk of loss."

| Plan | Price | What the MVP actually does |
|------|-------|----------------------------|
| Morning Spark | $0 | Saves an email and posts a weekday digest to the public Telegram channel |
| Pro | $29/mo or $249/yr | Real-time Gold Setup and Golden Flow alerts in the private Telegram channel. 7-day trial, card required |
| Pro+ | $59/mo or $499/yr | Same alerts as Pro. Watchlist filters, the members chat, and the monthly session are not built yet |
| Founding Member | $19/mo, first 100 | Same alerts as Pro. The Stripe price you create is the price those subscribers keep |

Canceling in the Stripe customer portal revokes the invite link and removes the Telegram user who joined with it. `invoice.payment_failed` marks the row `past_due` and leaves channel access in place until the subscription is canceled or unpaid.

### Environment variables

Set these as Fly secrets (`fly secrets set ... -a stockscanner`). Do not commit them.

| Env | Required | Effect |
|-----|----------|--------|
| `STRIPE_SECRET_KEY` | for checkout | Stripe secret key (`sk_live_...` or `sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | for webhooks | Signing secret from the webhook endpoint (`whsec_...`) |
| `STRIPE_PRICE_PRO_MONTHLY` | for Pro monthly | Price ID for $29/mo |
| `STRIPE_PRICE_PRO_YEARLY` | for Pro annual | Price ID for $249/yr |
| `STRIPE_PRICE_PRO_PLUS_MONTHLY` | for Pro+ monthly | Price ID for $59/mo |
| `STRIPE_PRICE_PRO_PLUS_YEARLY` | for Pro+ annual | Price ID for $499/yr |
| `STRIPE_PRICE_FOUNDING_MONTHLY` | for founding | Price ID for $19/mo |
| `TELEGRAM_BOT_TOKEN` | for alerts and invites | Bot token from BotFather. Also used by the existing personal desk alerts |
| `TELEGRAM_PAID_CHANNEL_ID` | for paid alerts | Private channel id (`-100...`). Paid alerts go here, not to `TELEGRAM_CHAT_ID` |
| `TELEGRAM_FREE_CHANNEL_ID` | for the digest | Public channel id or `@name` |
| `TELEGRAM_WEBHOOK_SECRET` | recommended | `secret_token` Telegram sends as `X-Telegram-Bot-Api-Secret-Token` |
| `PUBLIC_SITE_URL` | optional | Pages origin plus path. Default `https://2100preet.github.io/stockscanner` |
| `PUBLIC_API_BASE` | optional | Fly origin. Default `https://stockscanner.fly.dev`. Baked into the Pages export |
| `BILLING_ADMIN_TOKEN` | optional | Bearer token for `POST /api/billing/digest/publish` |
| `SUBSCRIPTION_ALERTS_ENABLED` | optional | `0` pauses paid alerts. First run seeds the current board and does not post it |
| `SUBSCRIPTION_ALERTS_SEND_EXISTING` | optional | `1` posts the board that is already on screen |
| `FOUNDING_MEMBER_CAP` | optional | Default `100` |

`TELEGRAM_CHAT_ID` is still only the operator's personal desk chat. Leave `TELEGRAM_PAID_CHANNEL_ID` off the GitHub Actions secrets so Pages and Fly do not both post.

Subscriber rows are stored in `outputs/subscribers.json` on the Fly volume (`/app/outputs`). That file is not copied into the Pages site.

### Stripe

1. In Stripe, create products and recurring prices: Pro $29/month, Pro $249/year, Pro+ $59/month, Pro+ $499/year, Founding Member $19/month. Copy each price ID (`price_...`) into the env vars above. Do not change the founding price later if those subscribers should keep $19.
2. Turn on the Customer Portal (Settings → Billing → Customer portal) so people can cancel.
3. Add a webhook endpoint `https://stockscanner.fly.dev/api/billing/webhook` for `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, and `invoice.payment_failed`. Copy the signing secret to `STRIPE_WEBHOOK_SECRET`.
4. Pro prices can use a 7-day trial. The checkout request also sets `trial_period_days` for Pro only. Card is required because Checkout collects it.

```bash
fly secrets set \
  STRIPE_SECRET_KEY=sk_live_replace_me \
  STRIPE_WEBHOOK_SECRET=whsec_replace_me \
  STRIPE_PRICE_PRO_MONTHLY=price_replace_me \
  STRIPE_PRICE_PRO_YEARLY=price_replace_me \
  STRIPE_PRICE_PRO_PLUS_MONTHLY=price_replace_me \
  STRIPE_PRICE_PRO_PLUS_YEARLY=price_replace_me \
  STRIPE_PRICE_FOUNDING_MONTHLY=price_replace_me \
  -a stockscanner
```

### Telegram

1. In BotFather, `/newbot`, then copy the token to `TELEGRAM_BOT_TOKEN`.
2. Create a **private** channel for paid alerts and a **public** channel for Morning Spark. Add the bot as an admin on both, with permission to invite users and post.
3. Forward a channel post to `@userinfobot` or call `getChat`, and set `TELEGRAM_PAID_CHANNEL_ID` and `TELEGRAM_FREE_CHANNEL_ID`.
4. Point Telegram at the Fly webhook so a join request can be approved and the user id stored for a later removal:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://stockscanner.fly.dev/api/telegram/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -d 'allowed_updates=["chat_join_request"]'
```

5. Set the remaining secrets on Fly:

```bash
fly secrets set \
  TELEGRAM_BOT_TOKEN=replace_me \
  TELEGRAM_PAID_CHANNEL_ID=-100replace_me \
  TELEGRAM_FREE_CHANNEL_ID=@replace_me \
  TELEGRAM_WEBHOOK_SECRET=replace_me \
  -a stockscanner
```

Paid alerts post when a scan emits a Gold Setup (MAX or HIGH) or a Golden Flow print. The weekday digest posts from the live desk between 7:45 AM and noon America/Chicago. `POST /api/billing/digest/publish` with `Authorization: Bearer $BILLING_ADMIN_TOKEN` sends it on demand (`{"force": true}` sends again the same day).

Checkout from GitHub Pages calls `PUBLIC_API_BASE` (`https://stockscanner.fly.dev` unless you override it before `export-pages`). The success page then asks `/api/billing/access` for the invite link.

## Tests

```bash
pytest -q
```
