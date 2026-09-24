# trader-v0.2

The fixed ETF research program and five advisory specialists are documented in [NET_EDGE.md](docs/NET_EDGE.md). Start with local analysis or diagnostic shadow; no candidate is authorized for trading by these workflows.

trader-v0.2 is a clean-slate quantitative trading and research platform built around market-state modeling, Markov transition analysis, and expectancy-driven opportunity ranking.

Phase one is intentionally research-first:

- provider-swappable market data ingestion
- configurable OHLCV feature engineering with multi-horizon, volatility, distance, volume, and persistence descriptors
- pluggable discrete state classification
- first-order Markov transition and return conditioning
- walk-forward signal generation without lookahead leakage
- portfolio-level next-bar backtesting with costs and guardrails
- rolling walk-forward robustness analysis
- parameter sweeps ranked by out-of-sample performance
- side-by-side state-model comparison
- per-state frequency, persistence, entropy, and sparse-sample diagnostics
- tradability diagnostics and density-aware state-model ranking
- out-of-sample state and transition quality scoring
- state / transition quality gating and fold-consistency diagnostics
- ranking-based selection modes, percentile gates, and opportunity caps
- state-family filtering and opportunity-level gate reporting
- gate-rejection calibration diagnostics and gate-ablation research
- model-specific exit comparison research
- state, transition, volatility-regime, and asset-class attribution
- trade-level failure diagnostics
- artifact-driven dashboard for inspection

## Architecture

```text
trader-v0.2/
├── app.py
├── configs/
│   └── default.yaml
├── scripts/
│   └── run_research.py
├── src/trader_engine/
│   ├── analytics/
│   ├── backtest/
│   ├── core/
│   ├── data/
│   ├── execution/
│   ├── features/
│   ├── markov/
│   ├── research/
│   ├── risk/
│   ├── signals/
│   ├── states/
│   ├── ui/
│   └── workflows/
└── tests/
```

## Design Notes

- `data/` isolates providers, caching, and universe construction so Alpaca or websocket adapters can be added later without rewriting the research engine.
- `features/` computes reusable descriptors from OHLCV data instead of embedding indicator math inside strategy code.
- `states/` maps each observation into an explicit state label. The included implementations now cover coarse rule states, richer composite states, discretized feature bins, clustering-based states, hybrid regime-plus-cluster states, and contextual duration-aware variants behind the same interface.
- `markov/` estimates transition counts, transition probabilities, state-conditional forward returns, and prediction diagnostics.
- `signals/` converts state-conditioned expectancy into ranked long/short/flat decisions after cost, volatility, liquidity, confidence, optional state / transition quality gates, and cross-sectional opportunity caps.
- `backtest/` consumes walk-forward signals and enforces next-bar execution, stop logic, trailing stops, overlapping-position rules, and portfolio constraints.
- `analytics/` produces portfolio metrics, state-level breakdowns, transition breakdowns, and exportable artifacts.
- `research/` adds asset-class-aware config resolution, rolling walk-forward evaluation, parameter sweeps, state-model comparison, edge-quality scoring, gate-ablation, and cached exit-comparison workflows.
- `ui/` reads saved artifacts rather than re-running research inside the dashboard.

## Research Integrity

The historical signal pipeline is walk-forward. For each timestamp, the engine:

1. builds the Markov model using data available up to that bar
2. estimates state-conditional forward expectancy using only realized history
3. emits a signal for execution on the next bar

That keeps the backtest from using future transitions or future returns when scoring historical trades.

## Asset-Class Overrides

The config supports `asset_overrides.equity` and `asset_overrides.crypto` so you can separate:

- feature horizons and rolling windows
- state model type, thresholds, contextualization, and binning / clustering inputs
- state thresholds
- Markov lookback windows
- signal thresholds and penalties
- holding periods
- stop settings

The example configs already include equity and crypto overrides.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run Research

```bash
python scripts/run_research.py --config configs/default.yaml
```

For a faster smoke run against a smaller universe:

```bash
python scripts/run_research.py --config configs/smoke.yaml
```

Run rolling walk-forward robustness:

```bash
python scripts/run_walk_forward.py --config configs/smoke.yaml
```

Run a bounded out-of-sample parameter sweep:

```bash
python scripts/run_parameter_sweep.py --config configs/robustness.yaml
```

Run both together:

```bash
python scripts/run_robustness.py --config configs/robustness.yaml
```

Compare multiple state engines side by side:

```bash
python scripts/run_state_model_comparison.py --config configs/state_models.yaml
```

Compare filtered dense / hybrid models and exit profiles:

```bash
python scripts/run_state_model_comparison.py --config configs/edge_quality.yaml
python scripts/run_exit_comparison.py --config configs/edge_quality.yaml
```

Run calibrated gate ablation across the selected dense / hybrid models:

```bash
python scripts/run_gate_ablation.py --config configs/edge_quality.yaml
```

Artifacts are written to `artifacts/latest/` by default:

- `opportunities.csv`
- `symbols/*.parquet`
- `markov/*`
- `backtest/*`
- `analytics/*`
- `state_diagnostics/*`
- `research/walk_forward/*`
- `research/parameter_sweep/*`
- `research/state_model_comparison/*`
- `run_summary.json`

Walk-forward artifacts include:

- `research/walk_forward/folds.csv`
- `research/walk_forward/fold_metrics.csv`
- `research/walk_forward/aggregate_metrics.csv`
- `research/walk_forward/trades.csv`
- `research/walk_forward/attribution/*`
- `research/walk_forward/diagnostics/*`
- `research/walk_forward/quality/*`

Parameter sweep artifacts include:

- `research/parameter_sweep/summary.csv`
- `research/parameter_sweep/fold_metrics.csv`
- `research/parameter_sweep/parameter_definitions.json`

State diagnostics artifacts include:

- `state_diagnostics/*_state_frequency.csv`
- `state_diagnostics/*_state_persistence.csv`
- `state_diagnostics/*_state_quality.csv`
- `state_diagnostics/*_transition_quality.csv`
- `state_diagnostics/*_state_tradability.csv`
- `state_diagnostics/*_summary.json`
- model-specific tables such as `*_model_settings.csv`, `*_bin_edges.csv`, `*_cluster_centers.csv`, and `*_context_settings.csv`

State-model comparison artifacts include:

- `research/state_model_comparison/summary.csv`
- `research/state_model_comparison/fold_metrics.csv`
- `research/state_model_comparison/sensitivity_summary.csv`
- `research/state_model_comparison/<model_name>/*.csv`

Exit-comparison artifacts include:

- `research/exit_comparison/summary.csv`
- `research/exit_comparison/fold_metrics.csv`
- `research/exit_comparison/<model_name>__<exit_profile>/*.csv`

Gate-ablation artifacts include:

- `research/gate_ablation/summary.csv`
- `research/gate_ablation/fold_metrics.csv`
- `research/gate_ablation/<model_name>__<ablation_name>/*.csv`

## Launch Dashboard

```bash
streamlit run app.py
```

## Current Defaults

- historical data provider: `yfinance`
- interval: daily bars
- state model: composite rule-based regime labels
- Markov model: first-order transitions
- execution model: backtest only for the current research phase

The richer state pipeline now supports:

- simultaneous short / medium / long return context
- return acceleration and deceleration
- realized volatility level and expansion / compression
- ATR-normalized move magnitude
- distance from SMA20 / SMA50 / SMA200
- RSI zone and rolling return z-scores
- volume expansion / contraction
- trend persistence and state age
- sparse-state merging and density controls for richer state models
- cluster pruning / merging and optional PCA before clustering
- hybrid regime-first state engines that cluster locally inside a coarse trend or volatility bucket

## Robustness Workflow

The robustness layer is designed to answer whether the current state model has edge and where that edge survives out of sample.

Walk-forward flow:

1. Define rolling train / validation / test windows.
2. Fit state transition statistics on the training window.
3. Score validation on frozen training statistics.
4. Build validation-derived state and transition quality tables.
5. Optionally refit on train + validation and score the forward test window with quality-gated signals.
6. Aggregate fold metrics, attribution tables, edge-quality tables, and failure diagnostics.

Sweep flow:

1. Enumerate dotted-path parameter overrides from the configured search space.
2. Rebuild the research stack for each parameter set.
3. Run the walk-forward evaluation for that parameter set.
4. Rank parameter sets by out-of-sample metrics such as `mean_test_sharpe`.

State-model comparison flow:

1. Define alternative state engines under `research.state_model_comparison.models`.
2. Apply per-model overrides without disturbing the shared research stack.
3. Re-run the walk-forward evaluation for each model definition.
4. Compare out-of-sample return, Sharpe, drawdown, trade count, sparsity, state coverage, transition stability, and tradability score side by side.
5. Inspect per-model state frequency, persistence, state quality, state tradability, transition quality, cluster diagnostics, and model-specific diagnostic tables in the dashboard.

Selective edge flow:

1. Score out-of-sample states and transition setups on trade returns rather than raw occurrence counts.
2. Measure fold activity, positive-fold fraction, expectancy variance, and Sharpe variance for each tradable state or transition.
3. Apply one of three selection modes: `hard_gate_only`, `soft_scoring_only`, or `hybrid`.
4. In soft or hybrid mode, rank setups with a composite score built from base signal strength, state quality, transition quality, fold consistency, sample size, and tradability.
5. Use absolute, percentile, or rank-based filters for state quality, transition quality, and consistency.
6. Optionally restrict trading to the best state families and cap opportunities per rebalance date, asset class, or model.
7. Inspect gate-rejection breakdowns, score distributions, and top-N summaries to see which filters are starving trades.
8. Run gate-ablation comparisons to isolate whether state, transition, or consistency filters are helping out of sample.
9. Run exit-profile comparisons on the filtered models to test whether the edge prefers time, state, ATR, or hybrid exits.

Exit-comparison runtime is now optimized for repeated research passes by preparing each model once, caching the walk-forward fold plan, and reusing those prepared artifacts across exit profiles. The summary artifacts include `prepare_seconds`, `run_seconds`, and `total_runtime_seconds` so you can see where the CPU time is going.

## Selection Calibration

The quality layer is no longer limited to brittle hard cutoffs. The calibrated selection stack supports:

- `hard_gate_only`: trade only if every active quality gate passes.
- `soft_scoring_only`: rank all non-family-rejected candidates by composite quality score without hard state / transition cutoffs.
- `hybrid`: remove clearly bad setups with minimal hard filters, then rank the survivors.

Quality filtering can be expressed as:

- absolute thresholds such as `min_state_quality_score`
- percentile thresholds such as `min_state_quality_percentile`
- within-table rank thresholds such as `max_state_quality_rank`
- fold-consistency requirements such as minimum active folds, positive-fold fraction, and consistency score
- state-family allow / exclude rules
- top-N opportunity caps per rebalance date, asset class, or model

The selection diagnostics now report:

- percent rejected by each gate
- cumulative rejection after each gate
- remaining trade count after each gate
- most trade-starving gate by fold or asset class
- quality-score distributions for candidate setups
- pre-cap vs post-cap top-N selection counts

## State Models

The current `states/` module supports five interchangeable model families:

- `composite_rule`: the original coarse trend / volatility / momentum / extension state.
- `rich_composite_rule`: a multi-horizon rule engine using trend, volatility regime, acceleration, extension, volume, and persistence components.
- `feature_bins`: discretized feature-bin states on configurable normalized inputs.
- `kmeans`: clustering-based states on normalized feature vectors.
- `hybrid_regime_kmeans`: a coarse trend or volatility regime first, then local K-means inside each regime.

Context augmentation can be layered onto any of them by appending previous-state group, state-age bucket, recent path, and entry-bias metadata directly into the state label. That keeps the current first-order Markov engine intact while adding transition context.

The denser-state controls are designed to keep richer models tradable rather than maximally expressive. They include:

- per-feature bin counts and disabled bin features
- grouped composite scores before binning
- automatic sparse-state merging for feature-bin and rich-rule models
- collapsed RSI / volatility / momentum branches for rich-rule states
- minimum cluster sizes, small-cluster merging, and optional PCA for clustering models
- hybrid regime-local clustering to reduce global fragmentation

Use `configs/state_models.yaml` as the starting point for state research. It demonstrates:

- different state models evaluated in one run
- separate equity and crypto overrides
- state-model-specific feature inputs for binning and clustering
- sparse-state merging and cluster-density controls
- a hybrid trend-regime-plus-clustering model

Use `configs/edge_quality.yaml` when the question shifts from state usability to selective edge isolation. It demonstrates:

- filtered vs unfiltered dense feature-bin and hybrid regime models
- state and transition quality gates built from validation-window trades
- fold-consistency thresholds and sample-size guards
- model-specific exit comparison profiles
- separate equity and crypto gate thresholds

## Extension Path

The current layout is designed so later additions slot into existing module boundaries:

- hidden Markov models in `states/` or `markov/`
- higher-order chains in `markov/`
- cross-sectional ranking and portfolio optimization in `signals/` and `risk/`
- Alpaca execution adapters in `execution/` and `data/`
- intraday or websocket feeds through new `data/` providers
- multi-strategy ensembles through workflow composition

## Tests

```bash
pytest
```

## Important Caveats

- `yfinance` is suitable for research bootstrapping, not institutional-grade production data.
- The included state models are research tools, not claims of durable alpha. Sparse states, unstable transitions, or weak OOS metrics should be treated as evidence against a specification, not optimized away blindly.
- State and transition quality scores are only as credible as their validation-window sample sizes and fold consistency. Empty or weak gate tables should lead to fewer trades, not forced trades.
- The dashboard is artifact-driven so the research run stays deterministic and inspectable.

### Risk-controlled research profile

Use `configs/risk_controlled.yaml` for the new conservative research setup:

```sh
.venv/bin/python scripts/run_research.py --config configs/risk_controlled.yaml
```

This separate profile uses the 95-stock research universe, excludes the five
quarantined names, disables paper execution, and writes to
`artifacts/risk_controlled`. It caps modeled initial-stop loss at 0.25% of equity
per trade and limits new entries to a 1% combined mark-to-stop risk budget, with
50% gross exposure and a 0.60 pairwise correlation entry limit. These thresholds
are engineering settings, not optimized profit targets or guaranteed loss limits.

Existing default settings remain available for reproducibility. Stop-risk budgets
require ATR stops; incompatible configurations now fail rather than silently
simulating a risk budget without stops. Sizing uses the modeled fill price,
including short-side slippage. Combined risk sums both long and short stop losses
without netting them; missing current held prices prevent additional entries.
The strictest configured portfolio budget covers the shared account.

The budget restricts **new entries**. It does not forcibly rebalance existing
positions when markets move. Gaps, costs and price changes can exceed the budget.
Exits remain available. Trade exports include initial modeled stop risk; equity
exports include current modeled stop risk. Missing or inactive-stop estimates
are displayed as unavailable rather than safe.

Normal research and walk-forward workflows now pass prior price history to
correlation checks. The risk manager reads only prices at or before each signal,
so supplying warmup history does not authorize future-price access.

The dashboard labels historical results as research. It is not a broker-level
approval gate, and a profitable historical ranking does not authorize trading.
The profile's universe is a dated research snapshot; data quality and prospective
validation still require separate checks before any trading decision.

To view this profile's research output after running it:

```sh
TRADER_ARTIFACTS=artifacts/risk_controlled .venv/bin/streamlit run app.py
```

### Minute-scale intraday research

The separate `intraday` module implements a selective, long-only opening-range
breakout and VWAP-recovery candidate. It is a research simulator, not a production
broker executor or high-frequency trading system. The first real-data diagnostic
lost money after costs; do not activate this candidate for trading.

```sh
.venv/bin/python scripts/run_intraday.py --config configs/intraday.yaml \
  --download --start 2026-08-24 --end 2026-09-22 \
  --data-dir artifacts/minute_data_new --output-dir artifacts/intraday_run_new
```

For downloads, configure `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the local
process environment. Credentials never belong in YAML or source files. The
connector only reads historical bars and the paper-endpoint exchange calendar;
it has no order-submission endpoint. It refuses redirects, checks pagination, and
fingerprints immutable raw data. The requested historical end is capped before
the latest 15 minutes to avoid requiring real-time feed access. Remove `--download`
to replay an existing fingerprinted dataset; always use a new output directory.

Defaults: $100,000, two-minute timestamp delay from the signal bar's start,
1.5-ATR stop, 2R target, 30-minute hold, and flatten five minutes before the actual
exchange close. Signals wait for completed bars and session warmup. A crossing
above the opening range or a recovery above session VWAP requires trend and
relative-volume confirmation. These are hypotheses, not proven predictors.

Entry limits: 0.10% initial-stop risk per trade, 0.50% combined stop risk, 10%
notional per stock, 50% gross exposure, five positions, 1% daily-loss halt,
50 entries/day and three/name/day, five-minute cooldown, 1% of the signal bar's
known volume, and 0.60 pairwise correlation over 30 completed session minutes.
They are caps, not trading quotas or guaranteed loss bounds. Orders use whole
shares. A daily-loss breach blocks more entries and liquidates at the next
modeled open; price gaps can exceed the threshold.

The simulator charges explicit one-way costs of 7, 14 and 28 basis points.
It rejects entries whose target is too small relative to assumed costs, applies
stop-first handling when both stop and target are touched, and reports actual
net expectancy rather than inferring profitability from the target ratio.
Cost scenarios can take different trades because entry eligibility depends on
cost. Daily metrics use session equity, not minute returns treated as daily.

Missing minute bars stop strict replay and produce a coverage report; bars are
never fabricated. Supplied exchange sessions handle holidays and early closes.
Historical minute OHLC does not establish bid/ask spreads, queue priority, market
impact, late bar revisions, or live fills. No broker order lifecycle, unattended
scheduler, or live trading authorization is included. A 1% daily gain is tracked,
not promised. This implementation supports U.S. stocks only.

### Research methodology and execution evidence

The next research direction emphasizes fair-value hypotheses, execution economics,
calibration and fresh evaluation. See [RESEARCH_METHOD.md](RESEARCH_METHOD.md) for
what is implemented, what remains a research proposal, and the promotion sequence.

`audit_execution_evidence.py` compares identical recorded fills at several cost
levels. `QuoteEvidence` rejects unusable or unavailable quotes and computes
explicitly descriptive quote features. `evaluate_expected_edge` is a fail-closed
research filter for externally supplied, calibrated forecasts; it does not train
a model, replace the existing intraday rule, or authorize broker orders.

## Daily relative-value research

The separate research engine fits forecasts using only matured historical labels, simulates both hedge legs at the next session open, charges trading and calendar-day borrow costs, and applies portfolio exposure and loss controls. Adjusted fractional units are research proxies, not executable shares. No broker orders are submitted.

Replay the frozen six-scenario experiment from the archived dataset:

```sh
.venv/bin/python scripts/run_relative_value.py --config configs/relative_value.yaml --data-dir artifacts/relative_value_build_2026-09-22/data --output-dir artifacts/relative_value_replay_new
```

Use a new output folder. Add `--download` with a new data folder to request read-only Alpaca data using environment credentials. The evaluation periods are explicit in the configuration; extending a download does not silently change them. Results, source fingerprints, the frozen protocol and limitations are retained in `artifacts/relative_value_build_2026-09-22`. Eight historical trades do not establish profitability or readiness for paper/live execution.

## Year-to-date daily simulation

```sh
.venv/bin/python scripts/run_daily_ytd.py --data-dir artifacts/relative_value_build_2026-09-22/data --output-dir artifacts/daily_ytd_replay_new --start 2026-01-01 --end 2026-09-22
```

This compares a prior-close forecast of next-session open-to-close returns with a separately trained next-open exit forecast. It includes a deliberately forced daily-trade diagnostic, separate from cost-filtered runs. All variants retain daily/monthly ledgers, cost stress and frozen data fingerprints. Daily bars cannot simulate intraday stops or guarantee daily loss caps. See `artifacts/daily_ytd_2026-09-22/results/REPORT.md` for the 181-session result and limitations. No broker orders are sent.

## Diversification study: 10,000 YTD simulations

```sh
.venv/bin/python scripts/run_diversification_10000.py --data-dir artifacts/relative_value_build_2026-09-22/data --output-dir artifacts/diversification_10000_replay_new
```

The reproducible seed generates 5,000 distinct rule sets and evaluates each at base and doubled costs. Candidate screening uses 2024 and 2025 and is locked before 2026 results. The report distinguishes unique parameter sets from repeated trading outcomes, retains every YTD daily path, and measures correlations with SPY and QQQ. These are proxies for diversification; the user's personal holdings are unknown. This six-ETF daily study does not establish worldwide coverage or authorize trading. The completed run is in `artifacts/diversification_10000_2026-09-23`.

## Guarded paper execution repair

The September 23 rapid paper test exposed logging failures that interrupted exits. The new paper-only adapter, bounded durable journal and explicit order lifecycle separate persistence from risk reduction and reconcile ambiguous orders without blind retries. The earlier backtest PaperBroker remains unchanged.

`python scripts/paper_execution.py --symbol SPY --limit-price 100 --stop-price 99` previews configurable whole-share sizing offline (75 shares / $7,500 for the example account). It does not connect to a broker by default. The optional manual execution flag performs one immediate-exit paper experiment only; it is not an automated strategy or unattended trading service. Larger settings remain dormant. See [the repair notes](docs/PAPER_EXECUTION_FIXES.md) for test coverage, requirements and unresolved hard-crash limitations. Old one-off burst scripts are retained as evidence and must not be reused for trading.
