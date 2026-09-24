# Frozen Phase 3 benchmark target calculations

`research.phase3_benchmarks` calculates causal research target weights and the equity/crypto exposure-matching validity gate. It implements no price execution, return calculation, ledger, broker action or qualification decision. Options matching remains explicitly diagnostic because the frozen protocol does not specify how to aggregate per-underlying errors into its gate. The original registry/receipt is unchanged.

## Inputs and causal boundary

Every financial value is a finite `Decimal`; timestamps are timezone-aware. `SourceStamp(as_of, received_at, source_sha256)` identifies each archived input. `Timing(expected_as_of, decision_at, execution_at, calendar_sha256, startup=False)` names the independently archived actual-calendar boundary. The module requires the source as-of time equal this exact boundary, source receipt no earlier than as-of and no later than decision, and execution no earlier than decision. Post-startup as-of must precede decision; startup permits contemporaneous planned inputs. It never infers exchange sessions from weekdays.

The calendar/source hash identifies supplied evidence, not its authenticity or completeness. The caller must derive `expected_as_of` from the actual preceding exchange close/UTC-day end and select next actual open,00:05UTC or09:35Eastern as frozen for the track. The module checks ordering and identity presence, not that an arbitrarily asserted timestamp is the actual exchange close. Include the full archived calendar in upstream decision evidence.

`EquitySnapshot(equity, stamp)` always means the **benchmark's own equity**. `ExposureSnapshot(candidate_equity, gross_dollars, stamp)` means actual candidate prior-close exposure. Candidate equity determines its risky fraction; benchmark equity independently converts target fractions into dollars. Benchmark returns are never copied from the candidate.

## Passive initialization

`passive_initial_target(strategy_id, benchmark, timing)` requires explicit startup and uses the benchmark's own initial equity. It allocates24% equally across SPY/QQQ/IWM for equities,25% equally across BTC/ETH for crypto, or0.75% equally across SPY/QQQ/IWM for the options descriptive control. Output `control=passive_initial` distinguishes this from a matched target. Post-startup calls fail: the upstream passive ledger must retain its actual holdings and cash without rebalance. The helper cannot determine whether another startup call has already been booked; the ledger must enforce one initialization.

## Equity and crypto controls

`exposure_target(strategy_id, evidence, benchmark, timing)` supports E_DONCHIAN20_V1 and C_SMA200_CONFIRM_V1. After startup, use actual `ExposureSnapshot`; fraction equals candidate gross dollars divided by candidate equity. Cap at24% for equities or25% for crypto and equal-weight the fixed basket, leaving the remainder in cash. The original uncapped reference and clipping amount remain in `BenchmarkTarget`, so appreciation above the cap cannot be hidden by comparing against a retrospectively capped denominator.

For startup only, use `PlannedWeights(weights, stamp)` with every universe symbol explicitly represented, including zeros. Sum the candidate's planned weights, then apply the same cap/equal allocation. Passing planned weights after startup, or actual holdings for startup, fails. Deterministic Decimal division assigns any rounding remainder to the final alphabetic component to keep the gross sum exact.

## Options delta-notional control

`delta_target(evidence, benchmark, timing, expected_delta_model_sha256=...)` requires `DeltaSnapshot(candidate_equity, exposures, stamp, complete, basis='actual')`. Each immutable `DeltaExposure` specifies underlying, exactly one contract, archived delta, underlying price, source stamp, model SHA256 and multiplier100. A complete empty tuple explicitly denotes flat ownership. Missing delta/completeness or a different model identity fails. No delta is inferred or calibrated here.

Compute each underlying's delta ×100 ×contracts ×underlying price /candidate equity, clip negative weights to zero, and proportionally scale all positive weights to sum1 when necessary. Delta is constrained to[-1,1]; negative input is retained through the prescribed zero clip, not treated as short exposure. Startup requires `basis='planned'` and planned option quantities/decision-time delta. Later periods require actual prior-close ownership and delta. The benchmark's own equity converts these fractions to target dollars.

This matches delta-notional magnitude only; gamma, vega, theta, realized volatility and tail risk require separate recorded diagnostics. An aggregate match can hide incorrect allocation across underlyings. This module therefore does not declare an options matching gate passed.

## Tracking coverage and denominators

`tracking_gate(strategy_id, expected_dates, observations)` receives the exact ordered expected evaluation dates and `TrackingObservation(day, reference_gross, planned_benchmark_gross, reference_by_underlying=None, benchmark_by_underlying=None)`. Values are fractions on the same exposure basis, **not dollar amounts divided by different portfolios interchangeably**. Reference is uncapped candidate gross for equity/crypto, or the sum of positive candidate delta-notional fractions for options. Use actual planned benchmark exposure after downstream cash/quantity constraints, not merely unconstrained desired targets, when reporting execution-aware matching validity.

Equity/crypto require at least95% of positive-reference dates within10% relative error, inclusive at both boundaries. Zero-reference dates are reported separately and excluded from that ratio; all-zero windows cannot pass. Zero-reference dates with residual benchmark exposure are explicitly counted. Missing rows or values remain dated missing evidence and force inconclusive status; duplicate or extra dates fail. The helper calculates the matching subgate for the supplied schedule, not the separate126-observation or30-episode investment requirements.

For options, supply complete per-underlying maps when available. Their sums must reconcile to aggregate fractions; aggregate and individual absolute/relative errors are reported. Status remains `inconclusive`, reason `gate_aggregation_not_frozen`, even if aggregate matching is perfect. Before outcomes, independently clarify whether the gate is aggregate, component-wise or another defined measure and register that clarification prospectively. Never choose the favorable aggregation after results. Missing component diagnostics cannot be manufactured from totals.

## Remaining integration

`BenchmarkTarget` reports weights, dollar targets, cash fraction, uncapped reference, capped target, clipping, timing and source hashes; authority/qualification are always false. Upstream accounting must compute whole-share/venue precision, next-open or quote execution, sell-before-buy ordering, fees, spread/impact, dividends, cash, ownership drift and terminal marks separately for each control. Rebalancing turnover must be charged. Passive buy-and-hold control ledgers, delayed scenarios, stationary bootstrap, benchmark profitability and qualification are not implemented by this bounded module. Tests use synthetic evidence; no market outcomes are generated.
