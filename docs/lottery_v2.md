# Lottery v2 (log only)

Lottery v2 is a separate lane from the existing call-only lottery desk. It logs a ticket when price **holds the direction of the break**. It does not send orders to Webull and it does not open a paper-journal trade.

The provider sample below covers ten sessions. It does **not** justify turning routing on.

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

The live desk marks prices from **Tradier, then Polygon, then Yahoo**. This replay uses that tape. Equity bars are Tradier production 5-minute timesales (every one of the 16 names, all ten weekdays). Option bars are Polygon 1-minute aggregates. Tradier timesales rejects option symbols (HTTP 400). Polygon’s daily flat files return HTTP 403, so the option path is the aggregates API, paced at about one call every 12 seconds. Polygon historical bid/ask quotes also return HTTP 403, so the entry is the **trade close**, not a quoted ask, and the 45% spread filter did not run. A missing bid is not counted as a failed gate.

The window is **2026-10-01 through 2026-10-14** (ten sessions, 149 opening-range breaks, 116 logged tickets). Twenty-four breaks had no minute bars on the strikes that were requested. Seven had bars but no print inside $0.05–$0.40. Two were too thin to log. GEX and flow were not in the history, so they never confirmed and never vetoed. When contract volume cleared 50, that same volume also counted as the third confirm.

| Sample | Tickets | Hit 5× | Hit 10× | Finished ≤ $0.05 | Median per $100 | Mean EV per $100 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-10-01 – 10-14, rules actually logged | 116 | 19 (16%) | 4 (3%) | 53 (46%) | **−$54** | **+$32** |
| Puts only | 58 | 12 | 3 | 23 | −$45 | +$50 |
| Calls only | 58 | 7 | 1 | 30 | −$67 | +$14 |

Exact figures: median EV **−$53.59** per $100, mean EV **+$32.15**. Puts mean +$50.38. Calls mean +$13.91. Worthless means the contract never printed 5× and the last close was at or under $0.05.

The same three days Yahoo could serve (2026-10-12 through 10-14) are 31 Polygon tickets, 8 hits at 5×, 1 hit at 10×, 14 worthless, median **−$30**, mean **+$68**. The earlier Yahoo print sample on those days was 31 tickets, 9 hits at 5×, 2 hits at 10×, 14 worthless, median −$31, mean +$88. The two tapes agree on the shape. The extra seven sessions, which Yahoo had no option bars for, pull the Polygon median from −$30 to −$54 and the mean from +$68 to +$32.

The mean is still a fat right tail, not a typical win. The best rows blended about 5×–8× (SPY call on 10/05, QQQ and AVGO puts on 10/08, and the 10/14 puts in IWM, AMZN, AMD, NVDA, and META). Fifty-three of the 116 never reached 5× and finished at or under $0.05.

SPY on 10/14 is still the cautionary row on this tape. The first hold was the **call** `SPY261015C00783000` at a $0.21 print. It finished worthless (about −$90 per $100). The later SPY put rip was not the ticket. META’s first hold that day was a put, and that one did reach 5× before the giveback exit.

Applying the live cap of 3 tickets a day, ranked by confirms and then a cheaper ask, makes the sample worse: 30 tickets, 2 hits at 5×, none at 10×, 17 worthless, median **−$69**, mean **−$14**. The cap is not what produced the positive mean above. That mean is the uncapped log.

### What should not change

Do not turn routing on. Do not widen the ask band, the 0–1 DTE limit, or the spread rule. The spread rule was not testable: there is still no historical bid. Do not drop the 3-ticket cap to chase the uncapped tail, and do not retune the rank so it prefers the handful of puts that paid 5×. Ten sessions is enough to see that the typical ticket loses about half the premium. It is not enough to rewrite the gate.

PFE’s penny call that was discussed elsewhere was 2 DTE, so this rule does not take it.

### Earlier Yahoo sample

The first pass used Yahoo 5-minute equity bars and Yahoo 1-minute option prints. Yahoo still had option bars only for 2026-10-12 through 10-14. Those numbers are the print sample in the paragraph above. They are not a second independent edge.

### What this does not say

Holding the cheap contracts the **current** call-only flags actually printed, on the days those flags exist, did not reach 3× in the earlier study. That sample is a different rule. It is not this backtest, and it is not a reason to route lottery v2.

Do not turn on Webull routing from this file. The lane stays log only. A later sample would need real asks and a bid before that changed.
