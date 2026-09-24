# Durable offline benchmark integration

`research.phase3_benchmark_store.record_benchmark_operation` accepts strictly typed JSON evidence, derives frozen benchmark targets through `phase3_benchmarks`, applies `phase3_benchmark_ledger` or `phase3_actions`, and records the transition atomically through the shared research store. This is modeled research state, not broker ownership, paper trading, prospective credit or an investment qualification decision.

## Public entry point

```python
record_benchmark_operation(
    database, run_id="separate-reviewed-run", strategy_id="E_DONCHIAN20_V1",
    control="matched", cost_scenario="base", operation_id="unique-operation",
    operation=operation_json, registry=frozen_registry_json,
)
```

Use a new database/run for different strategies, controls, cost scenarios, initial states, protocol or implementation identity. `control` is `matched` or `passive`; `cost_scenario` is explicitly `base` or `stress`. The initial state fixes that scenario for the entire run. Execution obtains its scenario from state; operation-level cost overrides are rejected. The canonical registry must match the shared supported protocol identity.

All prices, equity, gross exposure, quantities, steps and fee amounts must be finite **decimal strings**. Float/int substitutions are rejected at the wrapper boundary. Contract counts/multipliers require actual JSON integers, excluding booleans. Startup/completeness flags require actual booleans. Dates use aware ISO timestamps. Unknown operation fields fail instead of being silently ignored.

## Prepare operation

Required fields: `kind="prepare"`, `portfolio_batch_id`, `target_input`, `signal_prices`, `fees`, `quantity_steps`.

`target_input` contains `kind`, `timing`, `benchmark`, plus candidate evidence where appropriate:

- `timing`: `expected_as_of`, `decision_at`, `execution_at`, `calendar_sha256`, `startup`.
- `benchmark`: its own `equity` and `stamp`. A stamp contains `as_of`, `received_at`, `source_sha256`.
- Equity/crypto after startup: `kind="exposure"`, candidate with `candidate_equity`, `gross_dollars`, `stamp`.
- Equity/crypto startup: `kind="planned"`, candidate with complete symbol `weights` and `stamp`. These are candidate planned weights, not arbitrary benchmark target weights; the frozen equal-weight/capped benchmark function derives the control.
- Options matched control: `kind="delta"`, `expected_delta_model_sha256`, and candidate with `candidate_equity`, `exposures`, `stamp`, `complete`, `basis` (`planned` at startup, `actual` later). Each exposure has `underlying`, `contracts`, `delta`, `underlying_price`, `stamp`, `delta_model_sha256`, `multiplier`. Missing delta is never fabricated.
- Passive control: `kind="passive"`, startup timing and benchmark equity only. Candidate or delta-model fields are rejected. Frozen24%/25%/0.75% initial sleeves are derived by strategy.

Each `signal_prices[symbol]` contains `price` and `stamp`. Each `fees[symbol]` contains `rate`, `fixed_cash`, `currency`. Each `quantity_steps[symbol]` is an exact decimal string. Complete fixed universes are required by the underlying ledger, even for zero allocations. Target dollars must reconcile to benchmark cash, positions and receivables; caller candidate balances cannot replace own-equity accounting.

## Execute and corporate actions

Execute fields: `kind="execute"`, `portfolio_batch_id`, globally unique ledger `attempt_id`, `observations`, `now`, optionally `retry_execution_at` for original pending sells. Each observation is explicit `null` for missing evidence, or has `event_at`, `received_at`, `source`, `source_sha256` and applicable `raw_open`, `bid`, `ask`, `bid_size`, `ask_size`. Full absence is recorded, not dropped. Missing buys are not retried under a later target; retained tradable sells retry only through the explicit later boundary. Sub-step dust remains in durable ownership and valuation without indefinitely blocking rebalance.

Corporate-action fields: `kind="corporate_action"`, `event`, `now`, using the exact independently tested `phase3_actions` event schema. Entitlements, splits and payments share the same durable state chain. Unknown payment dates retain receivables; cash increases only after explicit payment evidence reconciles. The action ledger enforces ordering before same-boundary portfolio activity and refuses actions across unresolved prepared targets.

## Integrity and limits

Every operation and state transition is retained transactionally. Replaying the same operation ID/input returns its original result and current verified state without executing twice; changed inputs under the same ID fail. Preparing and executing use distinct operation IDs; attempt IDs are unique across the ledger, not merely within one target batch. Frozen-plan integrity, pending quantity bounds, original budgets and scenario identity survive process restart.

`phase3_identity.ImplementationGuard` binds the wrapper and its portfolio, target, fee, action, registry and storage dependencies to reviewed source bytes, checks loaded source-defined functions, and checks before/after transitions so detected changes roll back. It requires a clean interpreter and separately pinned Python/library runtime; it is not a defense against a hostile interpreter or coordinated file/database tampering. Supplied source hashes identify evidence but do not authenticate a data provider.

No HTTP calls, broker methods, credentials, service changes, backtest campaigns or paper-credit logic are present. Calendar correctness, point-in-time prices/chains/delta, verified fees/rounding/minimums, full replay/benchmark tracking, corporate-action provenance and independent prospective registration remain required. Synthetic tests demonstrate state consistency only.
