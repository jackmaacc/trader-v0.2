# MR30 / MR60 saved exit audit — 2026-09-23

The saved baseline trade tables are identical: 74 trades per candidate, 71 stops and 3 targets. All exit before 30 minutes (maximum 26); 61 exit within their entry minute. No baseline timeouts, session flattens, or risk-halt exits occur. Saved decisions match exactly after removing the expected differing strategy hash.

The timeout distinction works in the saved lower-cost scenarios: MR30 time exits occur at exactly 30 minutes; MR60 time exits occur at exactly 60 minutes. The 1 bps and 3 bps trade tables differ. StrategySpec.horizon parses the candidate suffix (src/trader_engine/research/strategy_spec.py:52); the engine compares elapsed timestamps to that horizon (src/trader_engine/backtest/engine.py:1179).

A separate stop-fill defect affects the saved baseline: 42 stops are already above the raw entry open because the stop is entry price including impact minus 1.5 times ATR. The saved results fill all 42 at this higher stop, instead of the available open; 23 reconstructed raw exit prices even exceed the exit bar high. The corrected shared research engine uses min(open, stop) and adjusts sizing. The old positive gross P&L therefore is not reliable. At fixed saved quantities the optimistic prices add $81.984145 gross / $81.926756 net. This arithmetic is not a corrected portfolio replay: later quantities can change with equity.

Each baseline row was joined to its entry and exit decisions and archived minute bars. Causal signal ATR/target was reconstructed from completed same-session history. All 74 entries qualify identically for MR30 and MR60; all exit timestamps and reasons match the first triggered price barrier; all saved stop/target prices match the original formulas. These checks validate the saved explanation, not absence of all possible execution defects.

## Saved cost and delay scenarios

| Scenario | Candidate | Trades | Reasons | Same minute | Max minutes | Identical pair |
|---|---|---:|---|---:|---:|---|
| cost_14bps_delay_0m | MR30 | 57 | stop: 57 | 56 | 17 | True |
| cost_14bps_delay_0m | MR60 | 57 | stop: 57 | 56 | 17 | True |
| cost_14bps_delay_1m | MR30 | 48 | stop: 48 | 47 | 4 | True |
| cost_14bps_delay_1m | MR60 | 48 | stop: 48 | 47 | 4 | True |
| cost_1bps_delay_0m | MR30 | 76 | stop: 48, target: 18, time_exit: 8, gap_stop: 2 | 5 | 30 | False |
| cost_1bps_delay_0m | MR60 | 76 | stop: 53, target: 19, gap_stop: 2, time_exit: 2 | 5 | 60 | False |
| cost_28bps_delay_0m | MR30 | 29 | stop: 29 | 28 | 16 | True |
| cost_28bps_delay_0m | MR60 | 29 | stop: 29 | 28 | 16 | True |
| cost_3bps_delay_0m | MR30 | 76 | stop: 63, target: 9, time_exit: 4 | 25 | 30 | False |
| cost_3bps_delay_0m | MR60 | 76 | stop: 65, target: 10, time_exit: 1 | 25 | 60 | False |
| cost_7bps_delay_0m | MR30 | 74 | stop: 71, target: 3 | 61 | 26 | True |
| cost_7bps_delay_0m | MR60 | 74 | stop: 71, target: 3 | 61 | 26 | True |

## Every baseline trade

One row represents the same saved trade in both MR30 and MR60. Times are New York EDT. Zero minutes means same one-minute bar; precise intraminute duration and ordering are unavailable. An asterisk flags the optimistic saved stop-fill defect.

| # | Date | ETF | Entry | Exit | Minutes | Exit reason | Stop at/above raw open |
|---:|---|---|---|---|---:|---|---|
| 1 | 2026-08-26 | GLD | 10:49 | 10:49 | 0 | stop | No |
| 2 | 2026-08-26 | IWM | 11:02 | 11:02 | 0 | stop | Yes * |
| 3 | 2026-08-26 | SPY | 12:03 | 12:03 | 0 | stop | Yes * |
| 4 | 2026-08-26 | TLT | 12:07 | 12:07 | 0 | stop | Yes * |
| 5 | 2026-08-26 | QQQ | 12:22 | 12:22 | 0 | stop | Yes * |
| 6 | 2026-08-27 | IWM | 10:32 | 10:36 | 4 | target | No |
| 7 | 2026-08-27 | TLT | 12:51 | 12:51 | 0 | stop | Yes * |
| 8 | 2026-08-28 | IWM | 10:30 | 10:30 | 0 | stop | No |
| 9 | 2026-08-28 | TLT | 11:37 | 11:37 | 0 | stop | Yes * |
| 10 | 2026-08-28 | GLD | 11:48 | 11:48 | 0 | stop | No |
| 11 | 2026-08-28 | QQQ | 12:22 | 12:22 | 0 | stop | No |
| 12 | 2026-08-28 | SPY | 12:28 | 12:28 | 0 | stop | No |
| 13 | 2026-08-31 | IWM | 10:31 | 10:31 | 0 | stop | No |
| 14 | 2026-08-31 | SPY | 10:31 | 10:31 | 0 | stop | Yes * |
| 15 | 2026-08-31 | QQQ | 10:33 | 10:33 | 0 | stop | No |
| 16 | 2026-09-01 | TLT | 11:27 | 11:27 | 0 | stop | Yes * |
| 17 | 2026-09-01 | IWM | 12:49 | 12:49 | 0 | stop | Yes * |
| 18 | 2026-09-01 | SPY | 12:49 | 12:49 | 0 | stop | Yes * |
| 19 | 2026-09-01 | GLD | 12:55 | 12:56 | 1 | stop | No |
| 20 | 2026-09-01 | QQQ | 13:18 | 13:18 | 0 | stop | Yes * |
| 21 | 2026-09-02 | TLT | 10:53 | 10:53 | 0 | stop | Yes * |
| 22 | 2026-09-02 | GLD | 11:17 | 11:18 | 1 | stop | No |
| 23 | 2026-09-03 | IWM | 10:31 | 10:31 | 0 | stop | No |
| 24 | 2026-09-03 | SPY | 10:45 | 10:45 | 0 | stop | Yes * |
| 25 | 2026-09-03 | TLT | 13:38 | 13:38 | 0 | stop | Yes * |
| 26 | 2026-09-04 | SPY | 10:33 | 10:33 | 0 | stop | Yes * |
| 27 | 2026-09-04 | QQQ | 10:51 | 10:52 | 1 | stop | No |
| 28 | 2026-09-04 | TLT | 11:30 | 11:30 | 0 | stop | Yes * |
| 29 | 2026-09-04 | GLD | 14:10 | 14:10 | 0 | stop | Yes * |
| 30 | 2026-09-08 | SPY | 10:30 | 10:30 | 0 | stop | Yes * |
| 31 | 2026-09-08 | TLT | 10:33 | 10:33 | 0 | stop | Yes * |
| 32 | 2026-09-08 | GLD | 10:43 | 10:48 | 5 | stop | No |
| 33 | 2026-09-09 | IWM | 10:40 | 10:40 | 0 | stop | No |
| 34 | 2026-09-09 | TLT | 10:55 | 10:55 | 0 | stop | Yes * |
| 35 | 2026-09-09 | SPY | 11:04 | 11:04 | 0 | stop | No |
| 36 | 2026-09-09 | GLD | 11:59 | 11:59 | 0 | stop | No |
| 37 | 2026-09-09 | QQQ | 11:43 | 12:09 | 26 | stop | No |
| 38 | 2026-09-10 | GLD | 11:14 | 11:17 | 3 | target | No |
| 39 | 2026-09-10 | IWM | 12:09 | 12:09 | 0 | stop | Yes * |
| 40 | 2026-09-10 | TLT | 12:09 | 12:09 | 0 | stop | Yes * |
| 41 | 2026-09-10 | SPY | 12:37 | 12:37 | 0 | stop | Yes * |
| 42 | 2026-09-10 | QQQ | 14:05 | 14:05 | 0 | stop | Yes * |
| 43 | 2026-09-11 | TLT | 10:30 | 10:30 | 0 | stop | Yes * |
| 44 | 2026-09-11 | IWM | 10:31 | 10:31 | 0 | stop | No |
| 45 | 2026-09-11 | GLD | 10:33 | 10:34 | 1 | stop | No |
| 46 | 2026-09-11 | SPY | 11:03 | 11:03 | 0 | stop | Yes * |
| 47 | 2026-09-14 | SPY | 10:52 | 10:52 | 0 | stop | Yes * |
| 48 | 2026-09-14 | IWM | 10:55 | 10:55 | 0 | stop | Yes * |
| 49 | 2026-09-15 | IWM | 10:32 | 10:32 | 0 | stop | No |
| 50 | 2026-09-15 | SPY | 10:42 | 10:42 | 0 | stop | Yes * |
| 51 | 2026-09-15 | QQQ | 10:45 | 10:45 | 0 | stop | No |
| 52 | 2026-09-15 | GLD | 10:53 | 10:53 | 0 | stop | No |
| 53 | 2026-09-15 | TLT | 13:45 | 13:45 | 0 | stop | Yes * |
| 54 | 2026-09-16 | SPY | 13:05 | 13:05 | 0 | stop | Yes * |
| 55 | 2026-09-16 | QQQ | 13:52 | 13:52 | 0 | stop | Yes * |
| 56 | 2026-09-16 | GLD | 14:15 | 14:32 | 17 | stop | No |
| 57 | 2026-09-17 | GLD | 11:03 | 11:12 | 9 | stop | No |
| 58 | 2026-09-17 | IWM | 11:26 | 11:26 | 0 | stop | Yes * |
| 59 | 2026-09-18 | QQQ | 10:31 | 10:31 | 0 | stop | Yes * |
| 60 | 2026-09-18 | IWM | 10:31 | 10:31 | 0 | stop | No |
| 61 | 2026-09-18 | GLD | 10:31 | 10:32 | 1 | stop | No |
| 62 | 2026-09-18 | SPY | 10:33 | 10:33 | 0 | stop | Yes * |
| 63 | 2026-09-18 | TLT | 11:02 | 11:02 | 0 | stop | Yes * |
| 64 | 2026-09-21 | GLD | 10:41 | 10:43 | 2 | stop | No |
| 65 | 2026-09-21 | TLT | 14:11 | 14:11 | 0 | stop | Yes * |
| 66 | 2026-09-22 | GLD | 10:30 | 10:30 | 0 | stop | No |
| 67 | 2026-09-22 | TLT | 10:37 | 10:37 | 0 | stop | Yes * |
| 68 | 2026-09-22 | SPY | 10:37 | 10:37 | 0 | stop | Yes * |
| 69 | 2026-09-22 | IWM | 10:58 | 10:58 | 0 | stop | No |
| 70 | 2026-09-23 | IWM | 10:33 | 10:33 | 0 | stop | No |
| 71 | 2026-09-23 | SPY | 10:41 | 10:41 | 0 | stop | Yes * |
| 72 | 2026-09-23 | TLT | 10:50 | 10:50 | 0 | stop | Yes * |
| 73 | 2026-09-23 | QQQ | 10:40 | 10:53 | 13 | target | No |
| 74 | 2026-09-23 | GLD | 11:58 | 11:58 | 0 | stop | Yes * |

## Limits and reproduction

This is the saved 20-session August 26–September 23, 2026 engineering diagnostic, not a full-history evaluation. The 7 bps assumption is per side. Minute OHLC cannot establish actual executable quotes, real stop trigger sequence, fills, or latency. Barrier ordering is the simulator convention. This audit does not establish profitability, absence of other bugs, or corrected aggregate performance.

No explicit market-regime label is saved in these MR trades or decisions. Cost/delay scenarios are matched above; no regime classification has been invented. All 12 saved MR cost/delay trade tables, with every trade reason and duration, are included in artifacts/mr_exit_audit_20260923/all_saved_mr_trades.json. The same artifact directory contains baseline_trades.json and input_hashes.json.

Run the Python block in artifacts/mr_exit_audit_20260923/REPRODUCE.md with PYTHONPATH=src .venv/bin/python. The code performs bounded offline analysis only. The original research artifacts are unmodified.
