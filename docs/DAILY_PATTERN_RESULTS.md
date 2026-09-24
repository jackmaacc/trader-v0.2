# Daily pattern research results — September 23, 2026

Implemented four concrete daily algorithms and completed the frozen experiment. All 32 candidate replays earned positive modeled net P&L, including doubled costs and an additional execution-session delay. **None met the frozen buy-and-hold return hurdle; no champion qualifies and no trading is enabled.**

The most useful development lead is H1, patient monthly trend following: fewer, larger winning episodes outweighed more frequent losses. This is a descriptive research lead, not a passed selection decision or demonstrated independent alpha. The results use provisional historical accounting inputs and already-consumed history.

## What was built

The existing `BacktestEngine` now exposes `run_daily_etf_replay`. Pure signal rules are in `src/trader_engine/research/daily_hypotheses.py`; daily cash, positions, corporate actions and execution are in `src/trader_engine/backtest/daily_etf.py`. Existing minute replay is unchanged. The algorithms use completed closes, plan quantities before execution, fill at later raw opens, retain unpaid dividends, preserve open-position P&L, and never assume an intraday stop fill from daily bars.

- H1: at month-end, enter qualifying ETFs above their 200-session mean; hold until a later month-end trend failure or a protective/risk exit.
- H2: weekly two-slot rotation using positive medium-term relative strength, a retention buffer and a minimum ordinary holding period.
- H3: a 63-close breakout with a 21-close exit, 10-session ordinary minimum and 63-session maximum.
- H4: monthly inverse-volatility allocation with a 2-percentage-point rebalance band.

Each retains the fixed research risk limits, including 0.1% equity modeled risk per position. Actual average invested exposure was only 3.54%–10.25%, much less than the nominal 25% portfolio maximum. Neither live/paper risk settings nor feed subscriptions changed.

## Fixed comparison

Each window starts separately with $100,000. These are total period profits, not annual returns, and the two windows are not a single continuous account. Development starts January 3, 2017 after 252 archived warm-up sessions and ends December 29, 2023 (1,760 evaluated sessions). The consumed diagnostic window is January 2, 2024–September 23, 2026 (684 sessions).

Base cost is an assumed 7 basis points per side. Gross P&L adds the actual modeled drag back to the same fills; it is distinct from a zero-cost replay that changes quantities and decisions. Both baskets hold the same SPY/QQQ/IWM/TLT/GLD universe on identical evaluation dates. The initially 25%-invested basket can drift upward; it is not a continuously exposure-matched portfolio. Paid dividends stay cash, unknown payment dates remain receivables, and cash earns zero.

### Development: 2017–2023

| Algorithm | Gross P&L | Net P&L | Average invested | Max drawdown | Closed episodes |
|---|---:|---:|---:|---:|---:|
| Monthly trend | $5,768.08 | $5,700.16 | 6.90% | 1.54% | 21 |
| Weekly rotation | $2,982.77 | $2,843.48 | 3.54% | 0.94% | 45 |
| Daily breakout | $3,313.73 | $2,995.52 | 4.46% | 1.28% | 88 |
| Monthly inverse volatility | $3,900.33 | $3,864.35 | 7.17% | 2.57% | 10 |
| Buy-and-hold, 100% initially | $102,484.56 | $102,414.92 | 96.61% | 26.05% | 0 |
| Buy-and-hold, 25% initially | $25,313.64 | $25,296.38 | 31.77% | 10.54% | 0 |

### Consumed diagnostic: 2024–September 23, 2026

| Algorithm | Gross P&L | Net P&L | Average invested | Max drawdown | Closed episodes |
|---|---:|---:|---:|---:|---:|
| Monthly trend | $4,487.04 | $4,449.52 | 7.08% | 1.21% | 10 |
| Weekly rotation | $1,100.72 | $1,028.13 | 3.67% | 0.53% | 25 |
| Daily breakout | $1,208.61 | $1,102.26 | 4.24% | 0.55% | 35 |
| Monthly inverse volatility | $6,118.61 | $6,106.58 | 10.25% | 1.56% | 2 |
| Buy-and-hold, 100% initially | $58,440.67 | $58,371.01 | 98.30% | 12.49% | 0 |
| Buy-and-hold, 25% initially | $14,372.22 | $14,355.09 | 28.92% | 3.64% | 0 |

## Before-cost expectancy

Average daily gross return includes all cash and post-halt days and uses prior-day gross equity. Values below are basis points per day (100 basis points = 1%). Excess is paired against the same-date fully invested basket. These descriptive averages are not forecasts.

| Window | Algorithm | Mean gross daily return, bps | Excess over full basket, bps/day | Mean gross closed episode |
|---|---|---:|---:|---:|
| development | Monthly trend | 0.3207 | -4.0399 | $241.47 |
| development | Weekly rotation | 0.1676 | -4.1929 | $51.47 |
| development | Daily breakout | 0.1860 | -4.1746 | $31.17 |
| development | Monthly inverse volatility | 0.2197 | -4.1409 | $258.13 |
| development | Buy-and-hold, 100% initially | 4.3606 | 0.0000 | N/A |
| development | Buy-and-hold, 25% initially | 1.3223 | -3.0382 | N/A |
| consumed_diagnostic | Monthly trend | 0.6448 | -6.4465 | $408.22 |
| consumed_diagnostic | Weekly rotation | 0.1608 | -6.9305 | $40.89 |
| consumed_diagnostic | Daily breakout | 0.1765 | -6.9147 | $34.51 |
| consumed_diagnostic | Monthly inverse volatility | 0.8730 | -6.2183 | $-115.82 |
| consumed_diagnostic | Buy-and-hold, 100% initially | 7.0912 | 0.0000 | N/A |
| consumed_diagnostic | Buy-and-hold, 25% initially | 1.9960 | -5.0952 | N/A |

## Cost and timing robustness

All candidate scenarios stayed net-positive and avoided the persistent 3% drawdown termination. The largest modeled drawdown across them was 2.9532%; maximum annualized turnover was 0.67793. This is robustness over four specified assumptions, not a probability of future profit.

| Window | Algorithm | Zero-cost counterfactual | Base net | Double-cost net | Extra-session-delay net |
|---|---|---:|---:|---:|---:|
| development | Monthly trend | $6,001.01 | $5,700.16 | $5,340.93 | $5,193.21 |
| development | Weekly rotation | $3,128.97 | $2,843.48 | $2,574.50 | $2,331.24 |
| development | Daily breakout | $3,588.15 | $2,995.52 | $2,586.48 | $2,406.65 |
| development | Monthly inverse volatility | $4,037.67 | $3,864.35 | $4,205.72 | $2,818.52 |
| consumed_diagnostic | Monthly trend | $4,627.27 | $4,449.52 | $4,177.27 | $5,337.35 |
| consumed_diagnostic | Weekly rotation | $1,207.95 | $1,028.13 | $919.37 | $1,386.56 |
| consumed_diagnostic | Daily breakout | $1,635.58 | $1,102.26 | $934.94 | $1,472.68 |
| consumed_diagnostic | Monthly inverse volatility | $2,342.22 | $6,106.58 | $6,051.54 | $6,061.28 |

## What the apparent pattern actually consists of

H1 had 9 winning and 12 losing closed episodes in development: mean net winner $660.85 versus mean net loser −$78.47. In the consumed window it had 4 winners and 6 losers: mean winner $1,140.91 versus mean loser −$85.78. That is the desired asymmetric payoff shape, but only 31 closed episodes across the two separately initialized windows. It does not establish a stable future expectancy.

H1's development net profit was $5,700.16 on average 6.90% invested, with 1.54% maximum drawdown. Its consumed-window net profit was $4,449.52 on average 7.08% invested, with 1.21% maximum drawdown. GLD contributed $3,497.44, or about 78.6% of the latter profit; this result is heavily dependent on one asset's trend. Closed-episode gains and remaining marked holdings together reconcile to total portfolio P&L.

H4 had the largest consumed-window base profit, but no candidate qualified. Selecting H4 from the consumed window would violate the frozen selection rule. It closed only two episodes in that window, both losses; positive portfolio P&L largely remained in open holdings/distributions. Its large zero-cost/base divergence shows path dependence, not an economic benefit from paying costs. Slightly different initial whole-share quantities caused an April 4, 2025 daily loss of −0.5181% in the zero-cost replay versus −0.4793% at base cost. Only the former crossed the fixed 0.5% daily-loss threshold and liquidated the broader portfolio at the next open. This is a brittle threshold effect; fixed base-case fills still lost $12.03 to modeled costs. Do not present its $6,106.58 as evidence of a high-frequency or reliably winning trade engine.

No candidate beat the fully invested or initially 25%-invested basket in either base-cost window. Since exposure differs greatly, this does not by itself rule out a risk-adjusted benefit. It does fail the frozen raw-return hurdle. We have implemented profitable historical prototypes under the stated assumptions, not found a qualified consistent edge or anything approaching a supported 1% daily target.

## Data and independent verification

The input audit found complete raw daily bars/calendar for 2,696 sessions. It also found a conflicting QQQ dividend record and 90 unknown historical dividend payment dates. Original inputs remain untouched; a separate explicitly provisional correction and forward total-return reconstruction were fixed before strategy outcomes. Historical action publication times, arrival/revision provenance and execution-cost calibration remain unverified. Every input manifest retains `qualification_allowed=false`. See `docs/DAILY_DATA_AUDIT.md` for exact records and evidence.

Completed 48 executions: 32 candidate replays, 12 distinct benchmark configurations and four repeated static benchmark references. No signal parameter search or retuning occurred. The separate auditor reconstructed 58,656 daily records and 2,038 executed transactions from raw opens, action events and decision fills; maximum ledger discrepancy was $0.0000000000873. All 299 recorded evidence-file hashes matched. Full software suite: 716 passed, with one existing joblib physical-core detection warning. See `docs/DAILY_PATTERN_AUDIT.md`.

## Reproduce and inspect

```sh
.venv/bin/python scripts/run_daily_pattern_research.py \
  --inputs artifacts/daily_hypotheses_20260923/corrected_inputs \
  --output artifacts/daily_hypotheses_20260923/results_repeat
```

Use a new directory; the driver will not overwrite saved evidence. The input builder defaults to preserving the ambiguous original records and requires `--provisional-qqq-correction` to reproduce the explicitly labeled corrected diagnostic bundle.

Evidence: `artifacts/daily_hypotheses_20260923/results/summary.csv`, `summary.json`, `protocol.json`, `evidence_manifest.json`, source snapshots and per-scenario daily/trade/decision/contribution ledgers. These local artifacts occupy about 13 MB and remain outside Git; the code, tests and review reports are committed.

The next research requirement is better data provenance and a genuinely future evaluation. A risk-matched comparison would be a proposed future protocol change requiring agreement before fresh outcomes; it does not replace this experiment's frozen primary hurdle. The proposed October 2026–September 2027 confirmation remains unscheduled and unregistered. No candidate passed promotion, no old failed MR/MOM family was retuned, and no broker orders were placed.
