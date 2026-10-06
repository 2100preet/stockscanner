# How ZeroLoss Desk signals & Webull orders actually work

## Short answer

**No — the GitHub Pages ZeroLoss site does not show BUY/SELL the instant an API tick arrives.**
It is a **static snapshot** rebuilt by GitHub Actions (~every 10 minutes in RTH). Between deploys the browser only reloads the same file.

**Yes — a live host (Railway / Render / `python -m odte_scanner ui`) can update as fast as one focus scan finishes** (back-to-back), refresh the board in the browser every ~15s, Telegram new pulses, and stage/submit Webull on that same cycle.

True sub-second “API tick → button turns green” is not how this desk works: signals need a **focus option scan** (chains, gates, hist win, RIP/Lottery/Challenge), which takes minutes on the full universe.

## Data path

```
APIs (Tradier / UW / Polygon / Yahoo)
        │
        ▼
   focus scan  ──► action board (BUY NOW / SELL NOW / …)
        │
        ├──► Telegram (new pulses only)
        ├──► Webull auto_sync (stage or live submit)
        └──► UI snapshot
                 │
                 ├── Live host: browser polls /api/snapshot ~15s
                 └── Pages: Actions writes snapshot.json ~every scan deploy
```

## GitHub Pages (`2100preet.github.io/stockscanner`)

| Piece | Cadence |
|-------|---------|
| Signal Desk Live Actions | ~every 10m RTH (scan → export → deploy → Telegram → Webull stage) |
| Browser | Re-fetches static `snapshot.json` every few minutes — **file unchanged until deploy** |

## Live ZeroLoss host (instant-*as-possible*)

Deploy the Docker/`Procfile` UI (Railway/Render). On boot it starts **`LIVE_DESK_LOOP`** (default on):

1. Focus scan  
2. Telegram pulse  
3. Snapshot rebuild → Webull `auto_sync`  
4. Short pause (`LIVE_DESK_PAUSE_SEC`, default 15) → repeat during RTH  

Browser polls every **15 seconds**, so once a scan finishes, BUY/SELL show up on the next poll.

Env knobs:

| Env | Meaning |
|-----|---------|
| `LIVE_DESK_LOOP=0` | Disable background worker |
| `LIVE_DESK_PAUSE_SEC=15` | Pause between cycles |
| `LIVE_DESK_EXTENDED=1` | Wider weekday window |
| `LIVE_DESK_ALWAYS=1` | Run even outside RTH |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram |
| `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` / `WEBULL_ACCOUNT_ID` | Broker API |
| `WEBULL_LIVE=1` | **Real submits** (enabled + dry_run off). Without this, orders are staged/dry-run only |

## Webull

- Every live snapshot with `auto_sync: true` runs the AutoTrader.
- Default config: `live_trading.enabled: false`, `dry_run: true` → **ledger / deep-link only**.
- Set secrets + `WEBULL_LIVE=1` for instantaneous *submit attempts* on the same cycle a BUY/SELL appears (still gated by hist-win / desk rules — not every alert becomes an order).

## What “instantaneous” means here

| Expectation | Reality |
|-------------|---------|
| Tick → UI in &lt;1s | Not supported on Pages; live host still waits for scan |
| Signal → Telegram / Webull as soon as scan decides | Yes on live host; ~10m batch on Pages Actions |
| Webull fill without keys / WEBULL_LIVE | No — staging only |
