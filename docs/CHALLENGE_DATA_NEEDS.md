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

2. **Tradier** (broker + options marks) — preferred next
   - Why: live bid/ask for ENTRY/EXIT + sandbox/paper orders in one API
   - Use: replace Yahoo marks in `live_chain` / challenge `refresh_open_marks`
   - Env secrets:
     - `TRADIER_ACCESS_TOKEN` (production token for realtime marks)
     - `TRADIER_ACCOUNT_ID` (for orders later)
     - `TRADIER_SANDBOX` optional (`true` = 15m delayed sandbox)
   - Client: `odte_scanner/data/tradier.py`

3. **Polygon.io Options Starter+** (or Massive / Intrinio options)
   - Why: reliable bid/ask marks so stops fire at −25/−35% not −90%
   - Use: replace Yahoo live_chain for challenge open marks + entry asks
   - Env: `POLYGON_API_KEY`

4. **ORATS** or **LiveVol** (IV surface / skew / earnings IV crush)
   - Why: avoid buying rich premium into IV crush; prefer cheap convexity
   - Use: skip ENTRY when IV rank > 80 without dump/rip tape
   - Env: `ORATS_API_KEY`

5. **Benzinga** or **Briefing.com** movers / news websocket
   - Why: catch catalyst opens (FDA, guidance, upgrades) in first minutes
   - Use: boost CORE_MEGAS + named movers onto challenge ENTRY same bar
   - Env: `BENZINGA_API_KEY`

6. **Optional broker paper API** (Webull already stubbed; Tradier preferred)
   - Why: fills + live greeks instead of Yahoo snapshots on Pages cron
   - Already partially stubbed under live_trading / webull

## Not worth it for this sleeve

- Full tick L2 equity books (costly; options mark quality matters more)
- Random Twitter sentiment scrapers (noise)
