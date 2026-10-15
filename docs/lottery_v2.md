# Lottery v2 (log only)

Lottery v2 is a separate lane from the existing call-only lottery desk. It logs a ticket when price **holds the direction of the break**. It does not send orders to Webull and it does not open a paper-journal trade.

The measured sample below is small. It does **not** justify turning routing on.

## Rules

Trade the break, not a fixed call bias.

- **Put** when the last price holds **below** the 15-minute opening range (09:30–09:45 ET high/low) **and** below session VWAP.
- **Call** when the last price holds **above** the opening-range high **and** above session VWAP.
- If either the opening range or VWAP is missing, the ticket is `SKIP`. Day high/low is not used as a stand-in for the opening range.

Hard filters:

- Ask about **$0.05–$0.40**.
- **0–1 DTE** only. A 2 DTE weekly is out, even if it looks cheap.
- Liquidity: option volume **≥ 50** or open interest **≥ 100**. A missing bid is not an automatic fail. A quoted spread wider than **45%** of the ask is a skip.
- New entries only from **09:35–15:30 ET**. That skips the first five minutes and the last thirty. Hard time exit **15:45 ET**.

Confirms, need **at least 3**:

1. Opening-range hold (required for a side).
2. VWAP side (required for a side).
3. One more of: relative volume ≥ 1.1 or contract volume ≥ 50, a VIX regime that matches the side (calls if VIX &lt; 20, puts if VIX ≥ 16 or VIX is rising), non-positive GEX / short-gamma (a confirm for either side, not a veto when GEX is positive), or flow sentiment that matches the side.

Flow that is present and opposite the side is a hard veto. Missing flow is not a veto. Missing VIX is not a confirm.

Universe: the existing mega list (META, AAPL, NVDA, TSLA, and the rest of `MEGA_RIP_SYMBOLS`), plus SPY, QQQ, IWM, DIA, plus any names passed in `lottery_v2_focus`.

Size: **$50** when the ask is ≤ $0.20, otherwise **$25**, stepping up only enough to buy one contract and never past $50. Max **3 tickets a day**, ranked by confirms, then cheaper ask.

Exit ladder, recorded on the log and not sent to a broker:

- Sell half on the first close at **5×**.
- Sell the rest at **10×**, or on a **50% giveback** from the peak close after 5× has printed.
- Otherwise the hard **15:45 ET** exit, or the last print.
- The stop is the premium. A contract that never reaches 5× and finishes at or under $0.05 is a full loss.

Each logged row has `action: LOG`, `log_only: true`, and `routed: false`. The desk labels it **LOG ONLY** so it is not a BUY NOW toast.

## Configuration

Keys live under `actions` in `config.yaml` and all start with `lottery_v2_`. `lottery_v2_enabled` turns the lane on. `lottery_v2_log_only` stays true. Logged tickets append to `outputs/lottery_v2_log.json` when a snapshot actually logs one. That file is not a broker blotter.

## Where it shows

The Lottery tab (the old Explosive tab) has its own **Lottery v2 — LOG ONLY** panel. The combined board pins four headings that stay visible when empty: **Lottery**, **Lottery v2**, **RIP/META**, and **Patterns**. Patterns no longer occupies the only special-desk slot when lottery and RIP rows are gated off.

## Backtest

Replay uses Yahoo 5-minute equity bars (about 2026-10-01 through 2026-10-14) and Yahoo 1-minute option bars. Polygon and Tradier were not reachable from this environment (`flyctl` is not installed and `FLY_API_TOKEN` is unset), so there is no Fly `ssh` tape.

A session is a ticket only when the first post-break 1-minute **print** (not a quoted ask) is inside $0.05–$0.40, the contract is 0–1 DTE, session volume through that print is at least 50, and `decide_lottery_v2` returns `LOG`. Expired contracts Yahoo no longer serves are missing data, not zeros. The ladder runs on later prints the same session through 15:45 ET. GEX and flow were not available historically, so those confirms did not fire. VIX and relative volume did.

Numbers below are the whole reachable sample, not a cherry-picked 10/14 anecdote. Entries are Yahoo **prints**, not asks, and the replay had no bid, so the spread filter did not fire. GEX and flow were not in the history, so they never confirmed and never vetoed. When contract volume cleared 50, that same volume also counted as the third confirm. A separate VIX or relative-volume confirm was not required on those tickets.

| Sample | Tickets | Hit 5× | Hit 10× | Finished ≤ $0.05 | Median result per $100 | Mean EV per $100 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-12 – 10-14, rules actually logged | 31 | 9 (29%) | 2 (6%) | 14 (45%) | **−$31** | **+$88** |
| Puts only | 18 | 7 | 1 | 7 |  | +$149 |
| Calls only | 13 | 2 | 1 | 7 |  | +$3 |
| 2026-10-01 – 10-09 | 0 |  |  |  |  | no option tape |

Equity bars exist for 2026-10-01 through 2026-10-14 (16 names). That is 149 opening-range breaks. Yahoo still had 1-minute option bars only for contracts that traded on 10/12–10/14. Earlier expirations returned no bars. Those are missing, not zeros. On the three days with option tape, 45 breaks fired and 14 had no 0–1 DTE print inside $0.05–$0.40 (DIA, NFLX, JNJ, and PFE among them). PFE’s penny call that was discussed elsewhere was 2 DTE, so this rule does not take it.

The mean is positive because a handful of puts blended about 4×–8× (AMD, IWM, AMZN, META, NVDA on 10/14, plus AMD on 10/13). The typical ticket lost money. Fourteen of the thirty-one never reached 5× and finished at or under $0.05.

SPY on 10/14 is the cautionary row. The first hold was a **call** at 09:45 ET (`SPY261015C00783000` around $0.20). It went to about zero. The later SPY put rip was not the ticket. META’s first hold that day was a put, and that one did reach 5× before the giveback exit.

This is three sessions and 31 prints. It is not a reason to route the lane.

### What this does not say

Holding the cheap contracts the **current** call-only flags actually printed, on the days those flags exist, did not reach 3× in the earlier study. That sample is a different rule. It is not this backtest, and it is not a reason to route lottery v2.

Do not turn on Webull routing from this file. The lane stays log only until a larger sample, with real asks and a bid, says otherwise.
