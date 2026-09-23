# Audit repair status — September 15, 2026

This is the first implementation milestone from [the audit](AUDIT_2026-09-14.md). Historical research results remain provisional: learned-state leakage, model identity, experiment precedence and independent model selection are still open. Existing market-data and performance artifacts have not been regenerated. The audit is preserved as a historical snapshot; its line references identify the pre-repair source.

## Plan review

Repair accounting and event ordering before changing strategy definitions or rerunning parameter searches. Preserve regression examples and make risk rules explicit. Treat broker integration, exchange-session calendars, and an insolvency/margin model as separate design work. In particular, do not disable the validation bootstrap when later separating it from evidence-required evaluation gates.

## Implemented

- **2:** opening size and initial stop now use ATR captured when the signal was generated, not the execution bar's later high/low.
- **3:** the final equity record reflects liquidation and both sides' fees; flat cash and equity reconcile to realized P&L.
- **4:** existing positions are marked at the open, opening protective stops execute before discretionary orders, and opening gap losses are checked before entries.
- **5 (date-based simulation contract):** a portfolio entry halt latches through the current timestamp date, using the strictest daily-loss threshold among the configured instruments. Queued entries are cancelled on breach; exits remain allowed. The baseline is prior valuation equity at the new date. Exchange-specific sessions, intrabar mark-to-market loss paths, and liquidation-on-breach are not implemented.
- **13:** historical inference includes the tail; only unrealized training labels are excluded by the analyzer.
- **14:** RSI handles uninterrupted gains, uninterrupted losses, and flat prices, with flat-price RSI defined as 50 after warm-up.
- **24:** gap/scheduled opening exits include only observed opening excursions. Intrabar stops use opening/stop-price bounds and carry `excursion_censored=True`; their held bar is counted. Exact intrabar extrema still require finer data.
- **25:** opening/stop exits carry the last observed state and state family, rather than the later closing state.
- **29:** the dashboard safely reads columnless empty CSV artifacts.

## Partially implemented

- **12:** unknown configuration keys and nonfinite numbers are rejected. Positive bounds now cover main risk, execution, feature, Markov and walk-forward settings. Feature horizon/window lists cannot be empty or nonpositive. All five shipped configs resolve successfully. Full cross-field validation, policy references, interval support, and overlap policy remain.
- **19:** entry quantity reserves commission/slippage cost and checks projected exposure/equity. Unleveraged long entries cannot spend more cash than available. Nonfinite risk inputs fail closed; insolvent holdings no longer report zero gross exposure. Maintenance margin and a complete insolvency lifecycle remain.
- **22:** Sortino now uses zero-target downside deviation over actual observed return intervals, without an artificial initial zero return. Trade-statistic naming and portfolio frequency conventions remain.
- **23:** sequential-trade drawdown includes the initial capital baseline. It is still a synthetic compounded-trade statistic, not capital-weighted portfolio drawdown attribution.
- **27:** normalization rejects invalid OHLC ranges, nonpositive/nonfinite prices, negative/nonfinite volume, duplicate timestamps, and missing numeric values. The backtest independently validates bars. Numeric strings normalize to numeric columns. Calendar-gap analysis, stale feeds, and per-instrument quarantine remain.
- **36:** added 35 deterministic regression cases, including long/short variants. Dependency locking, CI and performance optimization remain.

## Verification

The first 28 regression cases produced **27 failures and 1 pass on the original source**, then all passed after repair. Seven additional boundary/regression cases were added. The pre-existing suite contains 21 tests. Final combined verification: **56 passed**, with one pre-existing joblib physical-core detection warning. See `tests/test_audit_regressions.py` for the causal and accounting invariants.

Tests run without network access or broker orders, using the existing project virtual environment and an isolated source copy. Test caches/bytecode writing are disabled for the check. All five shipped YAML configurations were loaded and resolved for both asset classes.

## Remaining work, in order

1. Complete causal state fitting, feature-readiness masks, and fitted-model identity; separate historical/latest entry selection from position exits (**1, 6, 11, 15–18**).
2. Fix policy/experiment precedence, effective-config export, ablation behavior, explicit-null overrides, cached-profile override restrictions and instrument identity (**9, 10, 33–35**).
3. Refresh open-ended data, fail visibly on inadequate datasets, isolate inner model selection from untouched outer evaluation, and define non-overlapping portfolio reporting (**7, 8, 20, 21, 26, 28**).
4. Publish immutable run artifacts, improve metrics/provenance and decision-trace explanations, and finish CI/reproducibility (**22, 23, 30–32, 36**).
5. Rerun research only after those validity gates pass. Durable paper execution and a broker-specific instrument contract follow separately.
