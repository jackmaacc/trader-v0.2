# Crypto baseline diagnostic — September 23, 2026

Completed **12 distinct historical replays** using the shared `BacktestEngine.run`, starting each with **$100,000**. No orders were submitted. Public BTC/USD and ETH/USD daily data were refreshed through **September 22, 2026**, the latest completed UTC day. Each asset has 1,361 consecutive daily bars beginning January 1, 2023, with no missing dates, duplicates or invalid OHLCV detected.

The single frozen strategy holds each asset when its completed daily close is above its trailing 200-day average, and otherwise holds cash. Signals execute at the next daily open. Each entry targets 12.5% of current equity, subject to the shared engine's 25% gross entry cap and execution-cost adjustments. Holdings are not rebalanced; appreciation can push exposure above 25%. This research allocation changes no live or paper policy. There are no shorts, leverage or intraday stops. The generic engine's 3% daily-loss halt blocks entries; it does not liquidate holdings. Both strategies liquidate at the final close with costs.

The comparison is an approximately 25% initial BTC/ETH buy-and-hold basket, using the same engine, entry timing, terminal liquidation and costs. Cash earns no interest. Development evaluation begins July 20, 2023, with first possible execution July 21; it ends December 31, 2024 (531 daily rows). The later reserved diagnostic covers January 1, 2025–September 22, 2026 (630 daily rows), resetting flat with $100,000. Earlier crypto research already exists in this project, so this later split is **not pristine out-of-sample confirmation**.

## All results

“Before costs” adds explicit fees and modeled impact back to each run's actual quantities and execution times. It is a fixed-fill accounting decomposition; the separate zero-cost replay can have different sizing. Dollar figures below are total P&L, not annualized returns.

| Window | Strategy | Costs | Before costs | Fees + impact | Net P&L | Maximum drawdown |
|---|---|---|---:|---:|---:|---:|
| Development | SMA200 | Zero | $23,229.39 | $0.00 | $23,229.39 | 10.07% |
| Development | Buy and hold | Zero | $36,205.51 | $0.00 | $36,205.51 | 14.99% |
| Development | SMA200 | Base | $23,100.91 | $1,159.12 | $21,941.78 | 10.56% |
| Development | Buy and hold | Base | $36,182.66 | $258.39 | $35,924.26 | 14.99% |
| Development | SMA200 | Stress | $23,057.10 | $1,542.13 | $21,514.96 | 10.72% |
| Development | Buy and hold | Stress | $36,146.53 | $344.10 | $35,802.43 | 14.98% |
| Later diagnostic | SMA200 | Zero | $3,458.41 | $0.00 | $3,458.41 | 9.08% |
| Later diagnostic | Buy and hold | Zero | −$3,330.88 | $0.00 | −$3,330.88 | 18.76% |
| Later diagnostic | SMA200 | Base | $3,381.73 | $1,235.70 | $2,146.02 | 9.50% |
| Later diagnostic | Buy and hold | Base | −$3,328.09 | $139.91 | −$3,468.00 | 18.76% |
| Later diagnostic | SMA200 | Stress | $3,359.32 | $1,643.13 | $1,716.18 | 9.64% |
| Later diagnostic | Buy and hold | Stress | −$3,324.77 | $186.36 | −$3,511.14 | 18.74% |

Base means 25 basis points of commission plus 5 basis points of modeled impact **per side**; stress raises impact to 15 basis points. The current [Alpaca crypto fee documentation](https://docs.alpaca.markets/us/docs/crypto-trading), checked September 23, gives a 25-basis-point lowest-volume taker fee. Holding that rate fixed ignores volume discounts and does not reconstruct historical fee schedules. The generic engine deducts fees in dollars, approximating Alpaca's actual buy-side crypto fee deduction. Spreads and slippage are combined in assumed impact, not independently measured. Yahoo daily prices are indicative aggregate data rather than executable Alpaca quotes.

## Interpretation

The baseline made money in both windows after modeled costs. It underperformed buy and hold during development, but held less exposure during the later period and finished ahead in the later diagnostic. At base costs it completed 14 trades in development and 18 in the later window. Average exposure was 22.37% and 11.00%, respectively, versus 34.42% and 22.31% for buy and hold. Maximum exposure reached 41.90% in development and 32.74% later, illustrating the drift above the entry cap.

These are unequal-risk comparisons, not evidence of independent alpha or a consistent daily income stream. The later base result is a 2.15% total account gain over 630 calendar days, with a 9.50% maximum drawdown. No parameter was retuned after seeing the results. No strategy promotion follows from this diagnostic; prospective and risk-matched evidence remains necessary. Business overhead is unknown, so net trading P&L does not establish business profit.

Futures were not simulated: the shared model supports equity and crypto assets but has no contract-specific multipliers, tick values, margin, settlement, expiration or roll accounting. No ETF or crypto proxy is presented as a futures test.

## Evidence and validation

Frozen protocol: [CRYPTO_BASELINE_PROTOCOL.md](CRYPTO_BASELINE_PROTOCOL.md). Driver: [run_crypto_baseline.py](../scripts/run_crypto_baseline.py). Local evidence lives in `artifacts/crypto_diagnostic_20260923/results/`: frozen `protocol.json`, source snapshots and input hashes, all 12 per-scenario daily ledgers, orders, trades and cash/holdings reconstructions, `summary.csv`, and `evidence_manifest.json`. Raw public download snapshots are in the adjacent `refresh_attempt/` directory. These local artifacts are excluded from Git; this report and the reproducible driver are tracked.

There are 12 unique configurations and 12 distinct final equities, totaling 6,966 retained daily rows. Every day's cash and marked holdings reconciled to engine equity; terminal positions were flat, terminal realized P&L matched final equity minus $100,000, and gross less fees and impact equaled net. Source hashes matched before and after execution. Evidence totals approximately 1.86 MB, with one sequential worker and disk headroom checks.

Three focused offline checks passed: future-price changes cannot affect earlier signals; missing calendar days fail validation; and next-open/terminal execution, fees, slippage and cash reconciliation behave as expected, including deliberate corruption detection. The coordinator also ran 26 existing engine/correlation/stop-risk tests successfully. A separate reviewer approved the frozen driver before execution and independently audited all 12 completed scenarios: 6,966 daily rows, 108 closed trades and 71 evidence-file hashes. The reviewer rebuilt cash and positions from trades rather than this driver’s order reconstruction, checked every signal against the previous completed bar and every fill against the raw open or terminal close, and reproduced returns, drawdowns, costs and exposure. Maximum cash/equity discrepancy was $0.000000000073. This audit validates the saved accounting and causal rules, not market-data accuracy or future profitability.

Reproduce using the retained raw input snapshots and an unused output directory:

```sh
.venv/bin/python scripts/run_crypto_baseline.py freeze --inputs artifacts/crypto_diagnostic_20260923/refresh_attempt --output artifacts/crypto_diagnostic_reproduction
.venv/bin/python scripts/run_crypto_baseline.py run --output artifacts/crypto_diagnostic_reproduction
.venv/bin/python -m pytest -q tests/test_crypto_baseline.py
```
