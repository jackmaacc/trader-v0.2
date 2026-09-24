# Research direction: fair value, execution and evidence

We are adopting publicly described research principles, not reproducing Jane Street's or Citadel Securities' proprietary models. Citadel Securities describes forming and testing hypotheses for market-making models; Jane Street describes data preparation, model validation, and studying how models behave in trading.

Sources: [Citadel Securities quantitative research](https://www.citadelsecurities.com/careers/quantitative-research/), [Jane Street real-world machine learning](https://blog.janestreet.com/real-world-machine-learning-part-1/).

## The objective

Find a repeatable positive net expectancy with controlled account risk. Win rate, trade count, a 2R target and 1% daily growth are not substitutes for measured expectancy. We will not assume that earning spreads or submitting limit orders makes market making profitable: adverse selection, hedging costs, inventory exposure and uncertain fills must be measured.

## Implemented foundation

1. **Fixed-trade cost attribution.** Compare the exact same trades and quantities at several costs. Keep this separate from adaptive strategy replays, which can change entries and position sizes. The attribution tool validates dollar reconciliation and reports break-even costs.
2. **Quote-quality checks.** Validate event and availability timestamps separately; reject future, stale, crossed and invalid quotes. Report spread, midpoint, displayed-size imbalance and a size-weighted quote price. These are descriptive features, not trained fair-value forecasts. Quote sizes are in shares, and displayed liquidity is not a fill guarantee.
3. **Research edge filter.** Require a calibrated expected gross advantage to exceed estimated round-trip costs, uncertainty and an inventory penalty. Missing or unverified estimates reject eligibility by default. This is a research helper, not a broker authorization gate. The caller's calibration flag does not independently prove calibration.

These components are in `trader_engine.research.execution_evidence`. They do not replace the current intraday strategy's entry rule or automatically route orders. The subsequent relative-value build adds a trained daily forecast in a separate research module; there is still no production market maker or live quote connection.

## Next hypothesis: relative value, not an arbitrary price threshold

**Question:** within an economically related stock/ETF group, does a deviation from a causally estimated fair value predict a subsequent hedged return large enough to cover both legs' costs?

Before testing, specify the economic relationship, universe membership history, fair-value model, hedge ratio, one primary horizon, latency assumptions and all costs. Estimate parameters only on earlier data. Include borrow/short availability where needed; a stock-versus-basket residual alone is not proof of a tradable arbitrage.

Compare a simple model against zero forecast, market exposure and an explicitly priced hedge. Record forecast calibration, forecast error, net returns, inventory, turnover and adverse price movement after entry. Separate forecast evaluation from execution simulation. Do not declare the size-weighted quote price to be fair value without testing it.

**Current state:** implemented as a daily adjusted-price research proxy in `trader_engine.research.relative_value`. Three preselected ETF relationships and six chronological cost scenarios were evaluated through September 22, 2026. The eight simulated trades are insufficient evidence for promotion; the semiconductor forecast underperformed a zero forecast in 2025 and 2026. Historical executable prices, point-in-time adjustments, short availability, distributions and quote/fill evidence remain unverified. See `artifacts/relative_value_build_2026-09-22/results/REPORT.md`.

## Experiment discipline

Each experiment must freeze its hypothesis, source fingerprints, feature availability times, training/validation dates, costs, selection rule and rejection criteria before results. Record every attempted configuration. Use chronological evaluation and purge training labels that extend into evaluation periods. Treat overlapping observations as dependent when estimating uncertainty.

All previously inspected dates remain consumed history. A new architecture does not turn them into an untouched holdout. Retrospective results can debug a hypothesis; new prospective evidence is still required before promotion. The new workflow must retain the original-data coverage failures rather than select good days silently.

## Execution evidence still needed

- Historical and prospective bid/ask observations with actual availability times.
- Signal-to-order and order-to-fill timing, partial fills, rejects, cancellations and reconciliation.
- Side-aware effective spreads and post-fill price changes; separate spread, fees, impact and missed fills where observable.
- Conservative passive-order assumptions: touching a limit price does not establish a fill or queue priority.
- Inventory and hedge risk across related positions, beyond an isolated trade stop.

The current simulator's combined cost assumptions are not measurements of actual execution. The quote helper validates supplied observations; it does not download or authenticate their provenance.

## Promotion sequence

Data validity → evidence of a predictive relationship → realistic execution economics → constrained portfolio test → fresh observation/shadow evaluation → review of readiness for broker paper execution.

Each stage can fail. Neither a historical profit nor the new research helper authorizes trading. No production deployment or return guarantee is implied.

## Run the fixed-fill audit

```sh
.venv/bin/python scripts/audit_execution_evidence.py \
  --trades artifacts/intraday_build_2026-09-22/diagnostic_results/cost_1x/trades.csv \
  --output-dir artifacts/execution_audit_new
```

Use a new output directory for each audit. The recorded fills and their source fingerprint are preserved. This command reads local artifacts and does not contact a broker.

## Completed daily-pattern implementation and evaluation

The September 23 daily build implements four frozen slower ETF rules through the shared engine's explicit daily path. Completed 32 candidate replays and 16 benchmark executions (four benchmark references repeat, for 44 effective configurations). All candidate scenarios were net-positive under the fixed assumptions; none passed the frozen buy-and-hold raw-return hurdle. Average exposure was only 3.54%–10.25%. Monthly trend following was the strongest development lead, but sparse episodes, recent GLD concentration and data limitations prevent an edge or promotion claim.

The new path separates close decisions from later raw-open fills and preserves split/dividend/receivable accounting, pending exits and open-position P&L. Independent reconstruction verified 58,656 daily records and 2,038 transactions. Provisional QQQ dividend correction, unknown payment/availability provenance and uncalibrated costs remain explicit. All artifacts remain development-only. See `docs/DAILY_PATTERN_PROTOCOL.md`, `docs/DAILY_PATTERN_RESULTS.md` and `docs/DAILY_PATTERN_AUDIT.md`. No additional tuning, new role, recurring service or broker action followed from these results.
