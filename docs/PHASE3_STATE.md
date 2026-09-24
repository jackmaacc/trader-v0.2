# Durable offline portfolio state

`research.phase3_store.apply_batch` commits a research transition's input snapshot, result and new state in one SQLite transaction. It supports the frozen portfolio calculations; it is not a broker owner, distributed lock, scheduler or live account journal. Every store receipt states `execution_authorized=false` and `prospective_credit=false`.

The API requires a run identity, protocol and implementation SHA256 identities, the explicit initial JSON state, a unique batch ID, JSON input evidence and a pure transition callback. The callback receives isolated copies of state and inputs and returns exactly `{state: object, result: object}`. The portfolio transition is responsible for signal timing, valid state, modeled accounting and day/sequence semantics. The store does not infer a legitimate midnight baseline or validate an opaque state's financial content.

The initial state and both identity hashes remain fixed for the run. A changed implementation, initial state or protocol requires a separate run, not a silent continuation. Hash arguments are caller assertions; production integration must derive them from the actual implementation and retained protocol sources. No hash grants strategy approval.

`BEGIN IMMEDIATE` serializes local writers. Full synchronous SQLite transactions retain all input/state/result changes together, or roll them back on callback, validation or write failure. The callback must perform no I/O: this transaction cannot roll back an external effect or make a broker request exactly once. It must never wrap an order-submitting function.

Repeating an identical batch ID and input returns its original result without calling the transition. The receipt also returns the latest state, which may be later than that result, and explicitly labels the result historical. Changing inputs for a recorded ID is a conflict. All batches retain their input-state hash and previous-record hash, allowing restart verification of the chain. `read_state` uses one read-only SQLite snapshot and never calls the transition.

Files are owner-only; unrelated schemas and symlink paths are rejected. New private parent directories are created only when needed. Inputs must be known nonsecret research evidence. Keep original source responses and provenance separately; JSON snapshots are not original HTTP bytes.

Hashes are unsigned. They identify accidental corruption and internal linkage failures, not source authenticity or protection against an owner replacing the database, recomputing hashes or truncating its tail. Retain separately protected checkpoint identities/backups for rollback detection. This store alone does not satisfy off-host recovery or complete Phase 1 acceptance.

Offline tests exercise repeat/restart behavior, concurrent attempts at one batch, rollback after state mutation, conflicting identities, corruption, foreign schemas and invalid output. They do not count as paper observations or performance evidence. The current paper service and paper-trial ledger are unchanged.

## Daily portfolio integration

`phase3_portfolio_store.record_portfolio_operation(database, *, run_id, strategy_id, start_utc_day, operation_id, operation, registry)` connects this store to the daily equity/crypto portfolio adapter. Initial state comes from the adapter's flat $100,000 initializer, never an arbitrary supplied balance. The registry must match the implemented frozen design identity.

The JSON `operation` has one of four kinds:

- `baseline`: `utc_day`, exact-text `prior_close_equity`, aware `boundary_at` and a `provenance` reference.
- `prepare`: `portfolio_batch_id`, `now`, and complete `decisions`, `marks`, `fees`, `quantity_steps` mappings accepted by the portfolio adapter. Dates/times use ISO strings and Decimal values use strings. The wrapper constructs the adapter dataclasses; decisions must already come from the frozen signal calculation.
- `execute`: `portfolio_batch_id`, `now`, boolean `stress`, `marks` and `observations`. Each observation is null or contains event/receipt timestamps and the applicable raw open or bid/ask values.

- `corporate_action`: a source-bound `event` plus `now`, following [PHASE3_CORPORATE_ACTIONS.md](PHASE3_CORPORATE_ACTIONS.md).

Each outer `operation_id` identifies a single baseline/preparation/execution transaction. It is separate from the portfolio batch ID that pairs preparation with execution. Invalid kinds, malformed numeric inputs or failed transitions cannot commit partial state. These operations model fills only and do not submit broker orders. Source timestamps and baseline references remain caller assertions; storing them is not provenance verification.

The shared `phase3_identity.ImplementationGuard` derives hashes from ten implementation files for the daily wrapper, including the actual adapters, corporate-action bookkeeping, reservations, store, accounting/decision helpers, protocol validator and conversion layer. It compares source-defined loaded functions with compiled source and checks an import-time file inventory before operations, plus source identity before and after each transition within the transaction. Hot edits, including constant-only edits, cause rejection instead of being silently attributed to previously loaded code. Run in a clean interpreter without monkey patches; this guard is not protection against a hostile runtime or dependencies imported from already-modified files before this module loaded. Python and third-party dependency identities still belong in the separate release manifest.

Integration tests persist real adapter preparation and modeled fills, reopen saved state, reject replay conflicts, preserve a halt after recovery, and roll back simulated mid-transition source changes. An equity fixture also closes its modeled positions and independently normalizes its fill journal through the accounting/FIFO module, checking cash, quantity and P&L identities. That fixture has no corporate actions, base-asset crypto fees or broker lifecycle events; it is limited integration evidence, not complete generic-engine parity or actual account reconciliation.

## Other durable books

The [benchmark wrapper](PHASE3_BENCHMARK_STORE.md) derives passive, exposure or delta targets from input evidence and fixes base/stress costs per run. The [options wrapper](PHASE3_OPTIONS_STORE.md) persists base same-observation booking and pending ownership. Each uses the shared identity guard with its own dependency inventory and the same atomic store. Neither implements a coordinated candidate/control replay or makes supplied source assertions independently verified.
