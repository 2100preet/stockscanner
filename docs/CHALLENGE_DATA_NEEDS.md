# Data / infra that would raise $1k→$1M challenge hit-rate

If you can buy one or more of these, wire credentials as repo secrets / env
and tell the agent — Yahoo-only is the current bottleneck for marks + flow.

## Priority (highest leverage first)

1. **Unusual Whales** or **FlowAlgo / Cheddar Flow** (options flow)
   - Why: real-time sweep / dark-pool / premium flow beats Yahoo OI lag
   - Use: gate challenge ENTRY to symbols with bullish call flow that session
   - Env: `UNUSUAL_WHALES_API_KEY` or `FLOWALGO_USER` / `FLOWALGO_PASS`

2. **Polygon.io Options Starter+** (or Massive / Intrinio options)
   - Why: reliable bid/ask marks so stops fire at −25/−35% not −90%
   - Use: replace Yahoo live_chain for challenge open marks + entry asks
   - Env: `POLYGON_API_KEY`

3. **ORATS** or **LiveVol** (IV surface / skew / earnings IV crush)
   - Why: avoid buying rich premium into IV crush; prefer cheap convexity
   - Use: skip ENTRY when IV rank > 80 without dump/rip tape
   - Env: `ORATS_API_KEY`

4. **Benzinga** or **Briefing.com** movers / news websocket
   - Why: catch catalyst opens (FDA, guidance, upgrades) in first minutes
   - Use: boost CORE_MEGAS + named movers onto challenge ENTRY same bar
   - Env: `BENZINGA_API_KEY`

5. **Optional broker paper API** (Webull / IBKR paper)
   - Why: fills + live greeks instead of Yahoo snapshots on Pages cron
   - Already partially stubbed under live_trading / webull

## Not worth it for this sleeve

- Full tick L2 equity books (costly; options mark quality matters more)
- Random Twitter sentiment scrapers (noise)
