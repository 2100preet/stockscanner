# Data / infra that would raise $1k→$1M challenge hit-rate

If you can buy one or more of these, wire credentials as repo secrets / env
and tell the agent — Yahoo-only is the current bottleneck for marks + flow.

## Add Unusual Whales key (required for live Pages)

1. Open https://github.com/2100preet/stockscanner/settings/secrets/actions
2. **New repository secret**
3. Name: `UNUSUAL_WHALES_API_KEY`
4. Value: your Unusual Whales API token
5. Save — then re-run **Signal Desk Pages** (or wait for the next cron)

Cloud Agents cannot write GitHub secrets (403). Do **not** commit the key.

## Priority (highest leverage first)

1. **Unusual Whales** or **FlowAlgo / Cheddar Flow** (options flow)
   - Why: real-time sweep / dark-pool / premium flow beats Yahoo OI lag
   - Use: gate challenge ENTRY to symbols with bullish call flow that session
   - Env: `UNUSUAL_WHALES_API_KEY` or `FLOWALGO_USER` / `FLOWALGO_PASS`
   - Client: `odte_scanner/signals/unusual_whales.py` (flow-alerts → challenge board)
   - Also wired (desk enrich, focus tickers):
     - `GET /api/stock/{ticker}/greek-exposure/expiry` — GEX/delta by expiry
     - `GET /api/stock/{ticker}/flow-per-expiry` — call/put premium by expiry
     - `GET /api/option-contract/{id}/intraday` — 1m bid/ask/mid volume ticks
   - Config (optional): `actions.uw_focus_tickers`, `uw_focus_contracts`,
     `uw_max_focus_tickers` (default 4), `uw_max_focus_contracts` (default 2)

2. **Tradier** (broker + options marks) — preferred next
   - Why: live bid/ask for ENTRY/EXIT + sandbox/paper orders in one API
   - Use: replace Yahoo marks in `live_chain` / challenge `refresh_open_marks`
   - Env secrets:
     - `TRADIER_ACCESS_TOKEN` (production token for realtime marks)
     - `TRADIER_ACCOUNT_ID` (for orders later)
     - `TRADIER_SANDBOX` optional (`true` = 15m delayed sandbox)
   - Client: `odte_scanner/data/tradier.py`
   - Wired feeds (when token set — including Pages offline export):
     - `/markets/quotes` (+ POST batch) → equity + OCC marks (prefer over Yahoo)
     - `/markets/options/chains` + `/expirations` + `/strikes` → contract pick
     - `/markets/timesales` → ORB15 / Power Hour 1m bars
     - `/markets/history` → daily bars fallback
     - `/markets/clock` → session state in snapshot `tradier.clock`
   - Desk `data_confidence.pct` rises when UW + Tradier both ok (soft cap ~80%)

3. **Polygon.io / Massive** (options + equity snapshots) — backup marks
   - Why: denser bid/ask snapshots when Tradier is thin; second mark source for stops
   - Use: fallback after Tradier in `live_chain` / equity quotes
   - Env secrets (either name works):
     - `POLYGON_API_KEY` (preferred name in this repo)
     - `MASSIVE_API_KEY` (alias — Massive is current Polygon brand)
   - Client: `odte_scanner/data/polygon.py`
   - Desk `data_confidence` soft-cap rises to ~88% when Polygon probe ok

4. **ORATS Data API** (IV rank / IV percentile / earnings crush)
   - Why: skip rich premium into IV crush; prefer cheap convexity
   - **Which product:** ORATS **Data API / API Access** (summaries with `ivRank` / `ivPctile`) — not chart-only UI
   - Use: skip ENTRY when IV rank > 80 without dump/rip tape
   - Env: `ORATS_API_KEY`
   - After key is in GitHub secrets, tell the agent to wire it

5. **Benzinga** or **Briefing.com** movers / news websocket
   - Why: catch catalyst opens (FDA, guidance, upgrades) in first minutes
   - Use: boost CORE_MEGAS + named movers onto challenge ENTRY same bar
   - Env: `BENZINGA_API_KEY`

6. **Optional broker paper API** (Webull already stubbed; Tradier preferred)
   - Why: fills + live greeks instead of Yahoo snapshots on Pages cron
   - Already partially stubbed under live_trading / webull

7. **Telegram / WhatsApp BUY/SELL alerts** (entry + exit pulses)
   - Why: phone push on new BUY NOW / SELL NOW / RIP / Challenge pulses
   - **Delivery:** workflow **Signal Desk Alerts** (`alert-loop`) runs back-to-back
     focus scans during RTH and Telegram-pings as soon as each cycle finishes
     (typically ~scan length, not the 30m Pages cron). Pages still deploys the
     site on its own schedule with `SKIP_DESK_ALERTS=1` so you don't get dupes.
   - **Easiest — Telegram (recommended):**
     1. In Telegram, open **@BotFather** → `/newbot` → copy the **bot token**
     2. Message your new bot once (any text), then open  
        `https://api.telegram.org/bot<TOKEN>/getUpdates` → find `"chat":{"id": ...}`
        (or use @userinfobot for your user id)
     3. GitHub secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - **WhatsApp (optional):** CallMeBot (`WHATSAPP_TO` + `CALLMEBOT_APIKEY`), Twilio, or Meta Cloud
   - Client: `odte_scanner/alerts/` + `odte_scanner/alert_pulse.py`
   - First pulse after secrets are set **seeds** the board; later *new* pulses alert

## Not worth it for this sleeve

- Full tick L2 equity books (costly; options mark quality matters more)
- Random Twitter sentiment scrapers (noise)
