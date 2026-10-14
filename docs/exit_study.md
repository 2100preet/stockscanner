# Exit study — 14 Oct 2026

No orders were sent. This compares the desk's current premium exit (take profit +80%, stop −50%, and a 15:45 ET flatten on intraday desks) with fixed targets, a trailing stop, a 45-minute time exit, a hold to 16:00 ET, and a hold to the next session's close.

## What was measured

Logged entries come from three places that this environment can actually read:

- The GitHub Pages recommendation log (`data/ledgers/recommendation_log.json`, updated 14 Oct 2026 21:03 UTC). 416 rows, of which 160 are `BUY_NOW` or `ENTRY`.
- The live snapshot at `https://stockscanner.fly.dev/api/snapshot` (same afternoon). The API returns per-desk totals plus the latest 30 rows, not the full file.
- Webull sandbox fills still in `/api/webull/status` (`recent`, 40 rows).

One position per OCC contract per entry session. Option paths are Yahoo 1-minute bars for contracts Yahoo still lists (`range=5d`). The fill for a rule is the bar **close**. High and low are the excursion only.

Max favorable excursion (MFE) is the best high after entry versus the logged entry premium. Max adverse excursion (MAE) is the worst low. Both are percent of that entry premium.

P&L is dollars per contract: `(exit − entry) × 100`. A win is more than $1.

## What could not be computed

- The Fly volume's recommendation log reports **21,953 closed** rows. The public API does not return them. Those totals are also not usable as P&L: sample rows book a stop-loss reason (`unreal −60% @ $0.79`) against an entry of $0.03, which prints a four-digit percent gain. That population is excluded.
- Yahoo has no 1-minute history for contracts that have already expired. Most Oct 9 and earlier entries returned HTTP 404. That drops the journal's older 0DTE scratches.
- Polygon and Tradier are configured on the Fly app (the snapshot says both are up). Those keys are not in this environment, so their historical option bars were not fetched.
- Sure Winner MAX / HIGH / SOLID is not a section in the recommendation log, so it has no separate path. Beauty, Patterns, ML6, and swing have no closed option path in the files above.
- Golden flow has two filled buys with a usable path, both from the afternoon of 14 Oct. That is not a sample.
- `next_day` is not a finished hold for entries dated 14 Oct. The next session had not happened. Those marks are the latest bar on the entry day. The 0DTE bucket's `next_day` average is therefore not a result.
- Averages are pulled around by a few expensive contracts (two META weeklies, ALAB and AMAT puts, JPM and TSLA challenge calls). Medians are in the table for that reason.

The "current" column replays +80% / −50%, and also flattens intraday desks at 15:45 ET. It is not the dollar figure stored in the log. Many logged 0DTE exits are a flat `exit == entry` time-stop with `unreal +0%`, which is the bug this study is checking.

## Entry quality

| Desk | Path n | Median MFE | Median MAE | Reading |
|---|---:|---:|---:|---|
| challenge | 15 | +58% | −75% | Premium often runs in favor, then gives it back. Exit timing matters. |
| 0DTE (log section) | 21 | +3.8% | −18% | No large favorable excursion. Most of these contracts are 2 DTE, not same-day. |
| weeklies | 9 | +10% | −34% | Weak except two META calls (MFE about +200%). |
| RIP | 3 | +26% | −42% | Too few names. |
| golden flow | 2 | +2% | −5% | Same-afternoon fills only. |
| lottery | 2 | −16% | −100% | Both went to the stop. Favorable excursion never showed up. |
| 0DTE $1K | 2 | +5% | −76% | Too few. |
| Sure Winner, swing, Beauty, Patterns, ML6 | 0 | — | — | No tagged option path. |

The belief that entries are good and exits are wrong fits **challenge**: the median favorable move is +58% and the median adverse move is −75%. It does not fit the 0DTE section, where the favorable move is a few percent and the adverse move is larger.

## Exit comparison

Same contracts under every rule. `trail` arms after +20% and sells on a 20% giveback from the high, with the −50% stop still on. `time` is 45 minutes. `eod` and `next_day` do **not** use the premium stop. They mark the close.

| Desk | n | Current win% | Current avg $/ct | Current median | Best other rule | Best avg $/ct | Best median | Caveat |
|---|---:|---:|---:|---:|---|---:|---:|---|
| challenge | 15 | 40% | +45.67 | −3 | tp100 | +53.93 | −3 | JPM + TSLA + SLV are most of the dollars. tp30 wins more often (53%) at a lower average (+20.87, median +7). |
| weeklies | 9 | 33% | +60.00 | −39 | tp100 | +115.89 | −39 | Two META strikes are the whole average. Without them the desk loses. |
| 0DTE | 21 | 43% | +0.57 | −3 | eod | +35.57 | +1 | `next_day` (+40.67) is not finished: 19 of 21 entered on 14 Oct. The eod average is a few large names not getting stopped. |
| RIP | 3 | 33% | −23.67 | −14 | tp30 | +3.67 | +39 | n=3. |
| golden flow | 2 | 50% | +3.00 | +3 | — | — | — | Every later rule is the same last bar. Do not change gold. |
| lottery | 2 | 0% | −204 | −204 | none better | −204 | −204 | Both hit −50% and kept falling. |
| 0DTE $1K | 2 | 0% | −39.50 | −39.50 | next_day | −16.50 | −16.50 | Still a loss. n=2. |
| Sure Winner MAX/HIGH/SOLID | 0 | — | — | — | — | — | — | Not tagged in the log. Left on +80/−50. |
| swing, Beauty, Patterns, ML6 | 0 | — | — | — | — | — | — | No path. Left on +80/−50. |

`time` (45 minutes) was worse than the current rule on every desk that had a path.

## Second account

**Challenge** is the second sleeve.

It is the only other desk with a double-digit sample and a positive average that is not one symbol. Weeklies look better on the average only because two META calls are in the nine. 0DTE is about flat under the current stop. Sure Winner gold stays on the existing margin account because this study cannot show a better rule for it.

The recommendation is fragile. Challenge's median result is still about −$3 per contract. A few runners pay for a majority of losers. Treat the second paper account as a measured trial, not a proven edge.

## What the app now does

Configurable under `live_trading.exits` in `config.yaml`.

| Desk | Live rule | Why |
|---|---|---|
| sure_winner (gold) | premium +80% / −50%, 15:45 ET if the contract expires today | No path that justifies a change. Routes to `WEBULL_ACCOUNT_ID`. |
| challenge | premium +100% / −50%, no +40% sprint bank and no −25% early cut | Best average on the n=15 path. Routes to `WEBULL_ACCOUNT_ID_2`. |
| weeklies | premium +100% / −50% | Best average on n=9, with the META caveat. Not a second auto-trade account. |
| 0DTE action board | `eod`: premium target and stop off, 15:45 ET clock kept | Best *finished* rule on n=21. Median edge is $1 versus −$3. |
| RIP | +30% / −50% in config | n=3. Not auto-routed. |
| lottery, swing, Beauty, Patterns, ML6 | unchanged +80% / −50% | No better rule, or no path. |

`WEBULL_ACCOUNT_ID_2` must be the internal id from sandbox `get_account_list` for account number `DEA32RD3`. The account number itself is refused. If the variable is unset, challenge rows are not sent to the margin account.

The nine legacy sandbox lots are still not closed. `live_trading.close_legacy_positions` stays false.
