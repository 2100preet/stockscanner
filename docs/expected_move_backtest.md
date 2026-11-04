# Expected-move filter study

Yahoo Finance 5-minute bars, 2026-08-11 through 2026-11-03. VIX is the first regular 5-minute open (about 8:30 CT), and the daily open when that bar is missing. Not a paid feed.

Sessions with bars: 60. VIX days: 130. Train 2026-08-11 → 2026-10-08 (42 sessions). Out of sample 2026-10-09 → 2026-11-03 (18 sessions, the last 30%).

Live rules were not changed. The morning move is VIX / 15.87. Lunch still sells the ATM put and buys the put $2 lower, take profit at half the credit, stop at twice the credit, flat 2:45 CT, two names a day. Afternoon Puts still requires SPY and QQQ both, and still uses the live exits. Option dollars are the Black-Scholes stand-in, not a chain fill.

Research only. Webull stays paper. SPX is not an order. No filter from this study is enabled.

## Lunch-Break Credit

| Variant | All | Train | Out of sample |
| --- | --- | --- | --- |
| baseline | n=39 · 71.8% · $586 · avg $15.03 | n=28 · 78.6% · $635 · avg $22.67 | n=11 · 54.5% · $-49 · avg $-4.42 |
| skip if EM used > 50% | n=21 · 76.2% · $383 · avg $18.22 | n=14 · 85.7% · $394 · avg $28.13 | n=7 · 57.1% · $-11 · avg $-1.61 |
| skip if EM used > 75% | n=28 · 75.0% · $480 · avg $17.15 | n=17 · 88.2% · $529 · avg $31.10 | n=11 · 54.5% · $-49 · avg $-4.42 |
| skip if EM used > 100% | n=30 · 70.0% · $412 · avg $13.72 | n=19 · 79.0% · $460 · avg $24.23 | n=11 · 54.5% · $-49 · avg $-4.42 |
| only if EM used > 50% | n=18 · 66.7% · $204 · avg $11.31 | n=14 · 71.4% · $241 · avg $17.22 | n=4 · 50.0% · $-37 · avg $-9.36 |
| only if EM used > 75% | n=11 · 63.6% · $106 · avg $9.65 | n=11 · 63.6% · $106 · avg $9.65 | 0 trades |
| only if EM used > 100% | n=9 · 77.8% · $175 · avg $19.39 | n=9 · 77.8% · $175 · avg $19.39 | 0 trades |
| short strike 0.25σ below spot | n=39 · 97.4% · $882 · avg $22.62 | n=28 · 100.0% · $663 · avg $23.70 | n=11 · 90.9% · $219 · avg $19.89 |
| short strike 0.50σ below spot | n=38 · 100.0% · $611 · avg $16.08 | n=27 · 100.0% · $442 · avg $16.35 | n=11 · 100.0% · $170 · avg $15.42 |

Lunch-Break Credit: out of sample, short strike 0.25σ below spot is ahead of the baseline (11 trades, win rate 90.9%, P&L $219 vs baseline $-49). That is a research note. Do not turn it on. The live desk is unchanged.

## Afternoon Puts

| Variant | All | Train | Out of sample |
| --- | --- | --- | --- |
| baseline | n=46 · 17.4% · $-5,876 · avg $-127.75 | n=30 · 3.3% · $-9,071 · avg $-302.35 | n=16 · 43.8% · $3,194 · avg $199.64 |
| skip if EM used > 50% | n=18 · 22.2% · $-1,459 · avg $-81.04 | n=15 · 6.7% · $-4,035 · avg $-269.03 | n=3 · 100.0% · $2,577 · avg $858.93 |
| skip if EM used > 75% | n=27 · 14.8% · $-5,934 · avg $-219.76 | n=23 · 4.3% · $-7,932 · avg $-344.88 | n=4 · 75.0% · $1,999 · avg $499.66 |
| skip if EM used > 100% | n=33 · 12.1% · $-6,893 · avg $-208.89 | n=27 · 3.7% · $-8,799 · avg $-325.88 | n=6 · 50.0% · $1,906 · avg $317.59 |
| only if EM used > 50% | n=28 · 14.3% · $-4,418 · avg $-157.78 | n=15 · 0.0% · $-5,035 · avg $-335.68 | n=13 · 30.8% · $617 · avg $47.50 |
| only if EM used > 75% | n=19 · 21.1% · $57 · avg $3.01 | n=7 · 0.0% · $-1,138 · avg $-162.62 | n=12 · 33.3% · $1,196 · avg $99.63 |
| only if EM used > 100% | n=13 · 30.8% · $1,017 · avg $78.22 | n=3 · 0.0% · $-272 · avg $-90.61 | n=10 · 40.0% · $1,289 · avg $128.87 |
| put strike 0.25σ below spot | n=46 · 19.6% · $-2,856 · avg $-62.08 | n=30 · 3.3% · $-6,393 · avg $-213.10 | n=16 · 50.0% · $3,537 · avg $221.08 |
| put strike 0.50σ below spot | n=46 · 15.2% · $-4,211 · avg $-91.55 | n=30 · 3.3% · $-4,024 · avg $-134.12 | n=16 · 37.5% · $-188 · avg $-11.74 |

Afternoon Puts: out of sample, put strike 0.25σ below spot is ahead of the baseline (16 trades, win rate 50.0%, P&L $3,537 vs baseline $3,194). That is a research note. Do not turn it on. The live desk is unchanged.

## Recommendation

Leave the live rules as they are. Expected move is a pre-market print and an alert context line. It does not gate Lunch-Break Credit or Afternoon Puts.

The percent-used filters do not clear the bar. A filter needed at least 8 out-of-sample trades, a higher out-of-sample P&L, and a win rate that held up. On Lunch-Break Credit the 50% skip has 7 out-of-sample trades, and the 75% and 100% skips match the baseline's out-of-sample loss. The allow-only-above filters have an empty out-of-sample book. On Afternoon Puts the skip filters have 3 to 6 out-of-sample trades, under the minimum, and the allow-only-above filters make less out of sample than the unfiltered book.

The sigma strikes stay a hypothesis. Lunch-Break Credit with the short strike 0.25σ below spot finished 32 targets, 7 flats, and 0 stops across 39 modeled trades. Out of sample it made $219 against the baseline's $-49. The train win rate is 100.0% and the out-of-sample book is 11 trades. The further strike collects a smaller credit, and in this stand-in it does not hit the stop. The prices are Black-Scholes, not chain fills. That sample is too clean and too small to move the live short strike off the at-the-money put.

Afternoon Puts at 0.25σ is a small out-of-sample bump ($3,537 versus baseline $3,194 on the same 16 trades). The full-sample P&L on that strike is $-2,856. A few modeled winners are larger than the bump. The live contract stays one strike out of the money, and SPY and QQQ still enter together.

SPX stays an alert. Webull stays on the paper host. No filter from this table is enabled.
