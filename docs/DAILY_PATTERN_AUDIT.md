# Independent daily-pattern evidence audit

Audit of `artifacts/daily_hypotheses_20260923/results`, conducted separately from the strategy implementation and campaign execution. No strategy rules, inputs or results were changed during this evidence audit. The independent reconstruction used existing execution records and raw input data; it did not rerun strategies, search parameters or contact a broker.

## Conclusion

**Accounting and evidence integrity pass the checks below. No candidate qualifies for promotion.** All four candidates have positive modeled returns but trail both frozen buy-and-hold benchmarks in both evaluated windows. The prepared input manifest explicitly sets `qualification_allowed=false`; the campaign also sets `promotion_authorized=false`. Passing engineering/accounting checks does not establish a repeatable trading edge.

## Independent checks and exact scope

- Reconstructed all **48 recorded cases**, comprising 32 candidate runs and 16 benchmark/reference runs. There are **44 effective configurations**: four delayed-scenario benchmark references deliberately repeat the corresponding unchanged static benchmark. These are not 48 independent market histories.
- Rebuilt **58,656 daily ledger records** and checked **2,038 executed transaction records** from the corrected raw daily OHLC, corporate actions and exported decision fills. This includes 1,760 sessions per development case, January 3, 2017–December 29, 2023, and 684 sessions per consumed-diagnostic case, January 2, 2024–September 23, 2026.
- Independently maintained quantities, cash, distribution receivables, documented payments and cumulative execution drag. Matched every daily cash balance, marked holding value, receivable balance, portfolio equity and cumulative modeled cost. Maximum observed absolute dollar discrepancy was **8.731149137020111e-11**, consistent with floating-point rounding.
- Checked that every exported execution occurs at that session's actual opening timestamp, its raw price matches the input raw open, and its modeled fill has the correct adverse side and cost rate. Every candidate buy/add has an earlier recorded plan. This checks observed transaction timing and records; it is not an independent reimplementation of every signal rule.
- Independently recomputed average and maximum exposure, annualized turnover, terminal net return and same-transaction gross return. Verified complete/valid daily valuation flags for all cases.
- Verified all **299 SHA-256 entries** in the completed campaign evidence manifest. Verified 48 summary rows and labels H1, H2, H3, H4, BH100 and BH25. Benchmark rows are not mislabeled as the H1 adapter identifier.
- Compared each static benchmark's base and delayed-reference daily files for exact equality. Their first-open initialization remains unchanged as frozen in the protocol.

The initial eight-case intermediate reconstruction checked 477 transactions with maximum discrepancy 4.3655745685100555e-11 dollars. The final all-case reconstruction supersedes that narrower check.

Independent pre-campaign synthetic verification used:

```text
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_daily_hypotheses.py tests/test_daily_etf_replay.py tests/test_daily_pattern_driver.py tests/test_daily_hypothesis_inputs.py
27 passed in 8.82s
```

An earlier invocation used a nonexistent driver-test filename and ran zero tests; the corrected command above completed successfully. The implementation writer subsequently reported 59 focused/legacy tests passing; the coordinator reported 716 full-suite tests passing. Those broader runs are separately owned verification, not represented as this auditor's independent executions. Harmless PyArrow host-cache detection warnings appeared during sandboxed data reads; all reads and reconstruction assertions completed.

## Review findings corrected before historical execution

The pre-outcome review found and the implementation corrected delayed risk exits whose due date could be moved forward repeatedly, cap reductions that could replace stronger pending exits, and missing-entry opens that could retain stale entry orders. Genuine earlier reductions retain their due date; pending additions cannot accelerate a newly requested risk exit. Missing entries expire while missing exits remain economically pending.

The implementation also rejects duplicate same-symbol/ex-date actions and serializes open-position session dates correctly. A draft delayed-halt test only canceled an entry before a holding existed; it was replaced with a test that opens holdings and verifies actual delayed liquidation. These corrections preceded the frozen campaign rather than being outcome-driven strategy changes.

## H4 zero-cost/base divergence: a threshold effect

The consumed-diagnostic H4 result is sensitive to small position-size changes. The zero-cost counterfactual earns **$2,342.22**, while the 7-bps base case earns **$6,106.58**. This does not show that paying execution costs improves an edge.

The first positions on February 1, 2024 already differ because risk/cost sizing and integer quantities interact:

| ETF | Zero-cost shares | Base-cost shares |
|---|---:|---:|
| GLD | 15 | 15 |
| IWM | 8 | 7 |
| QQQ | 5 | 5 |
| SPY | 7 | 6 |
| TLT | 23 | 22 |

On April 4, 2025, the zero-cost portfolio falls about **0.5181%** in one session. Its closing equity of $101,394.437916 is **$18.457460 below** the fixed 0.5% daily-loss threshold. The base-cost portfolio falls about **0.4793%**; its $101,370.909995 closing equity is **$21.105500 above** its threshold.

Consequently, the zero-cost case liquidates GLD, IWM, QQQ and SPY at the April 7 open. The base case exits only IWM for its protective stop, retaining the other holdings. Subsequent allocation and exit paths differ. This is consistent with the frozen rules and independent accounting reconstruction, but it demonstrates **brittleness around the daily-loss threshold**, not robust performance superiority.

For the same actual base-case transactions, gross P&L is **$6,118.61**, execution drag is **$12.03**, and net P&L is **$6,106.58**. Costs reduce same-transaction P&L. The separate zero-cost run changes trades and must not be used as fixed-transaction cost attribution.

The base H4 consumed window has only **two closed episodes**, with mean gross closed-episode P&L **−$115.82**. Its positive portfolio result includes open holdings and distributions; it does not imply a positive mean realized trade or a large closed-trade sample.

## Interpretation and remaining limitations

All 32 candidate scenarios have positive modeled net P&L and none triggers the permanent 3% drawdown halt. Across those scenarios, maximum annualized turnover is 0.6779303 and maximum drawdown is 2.9532237%. This is descriptive historical evidence. It does not erase failure of the frozen primary-benchmark hurdle or input qualification.

Base candidate average exposures range from about 3.54% to 10.25%. The primary basket is initially 100% invested. The secondary basket is initially 25% invested and then drifts; its average exposure is about 31.77% in development and 28.92% in the consumed window. Neither benchmark is continuously exposure-matched to each candidate. Raw-return underperformance is clear; reduced drawdown alone does not establish risk-adjusted alpha.

Material unresolved data limits include the explicitly provisional QQQ 2022 action correction, 90 unknown distribution payment dates, missing historical publication/arrival/revision provenance and uncalibrated execution costs. The original evidence remains preserved. Reconstruction confirms consistency with the chosen diagnostic inputs, not independent factual completeness of those inputs.

The historical windows are development and consumed diagnostics, never untouched confirmation. The proposed future confirmation interval remains untouched and unscheduled. No broker orders, promotion, subscription or live-service restart follows from this audit.
