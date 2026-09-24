# Durable base-only options research operations

`research.phase3_options_store.record_options_operation` converts explicit JSON evidence into the existing options portfolio's pure transitions, then commits the input, output and complete state through the shared hash-linked SQLite research store. It performs no broker, credential, service or market-data calls. This is offline modeled state; successful persistence never grants paper/prospective credit, live approval or investment qualification.

```python
record_options_operation(
    database,
    run_id="independent-options-research-book",
    start_utc_day="2026-10-01",
    operation_id="unique-operation-id",
    operation=operation_json,
    registry=frozen_registry_json,
    scenario="base",
)
```

`scenario` accepts **only `base`**. Stress/delayed fills and contract reselection are not implemented or implied. There is no argument for arbitrary initial cash, initial positions, ownership seeds or an alternative strategy: initialization is the existing options portfolio's flat $100,000 state. Run identity and start day are immutable for an existing database.

## Operations and JSON contracts

Each operation has an exact field set; unexpected fields are rejected.

- **Baseline:** `kind: "baseline"`, `utc_day`, `prior_close_equity`, `boundary_at`, `provenance`. The amount is an exact decimal string. The existing portfolio enforces the true UTC-boundary timestamp, nonrewinding day and immutable same-day reference. The caller must independently reconcile the baseline and cash flows; a reference label alone authenticates nothing.
- **Prepare:** `kind: "prepare"`, `portfolio_batch_id`, `calendar`, `session_date`, `now`, `entry_outputs`, `exit_outputs`, `contracts`, `marks`. This calls the existing `prepare_batch`; it does not generate new strategy signals. Supply actual result dictionaries from `phase3_options.plan_entries` and `plan_exit`. The wrapper verifies their supported schema, timestamps, exact numeric types, false authority flags and one-contract quantities. The existing portfolio binds prices, fees, selected metadata and original budgets.
- **Execute:** `kind: "execute"`, `portfolio_batch_id`, `now`, `marks`. This calls the existing base/same-observation `apply_batch`. Original contract and budget are retained. Missed observations cannot be repriced later; pending exits require a new chronological prepared batch with new actual adapter evidence.

`calendar` contains `sessions`, an ordered list of `{day, close_at}`, and `archived_sha256`. `ArchivedCalendar` verifies its exact canonical hash, session ordering and close-date consistency. The archive still must come from a verified exchange calendar; generating a hash does not prove its provenance.

`contracts` maps each option symbol to all `Contract` fields: `symbol`, `underlying`, `expiry`, `strike`, `listed_at`, `metadata_received_at`, `option_type`, `multiplier`, `standard_unadjusted`, `physically_delivered`, `active`. Mapping key must match symbol; multiplier is integer 100; flags are JSON booleans. Strike is an exact decimal string. Existing adapter/portfolio checks enforce selected point-in-time contract scope.

`marks` maps an option symbol to all `Quote` fields: `symbol`, `feed`, `bid`, `ask`, `bid_size`, `ask_size`, `event_at`, `received_at`. Symbol must match the mapping key, feed must be `opra`, financial values must be decimal strings and timestamps must include timezone offsets. Actual freshness and causality are checked by the existing options portfolio. Missing marks remain missing evidence; the wrapper does not fill them from cached prices.

Entry/exit candidate `modeled_prices`, premiums, fees and cash debit/proceeds remain exact strings. Floats, integer substitutions for financial strings, booleans substituted for quantity, nonfinite numbers, naive timestamps, unknown operations and injected initial holdings are rejected. State and outputs are JSON-compatible, with amounts retained as strings.

## Durability and code identity

The common `phase3_identity.ImplementationGuard` binds the wrapper, options adapter/portfolio and their implementation dependencies, including the common store, registry validator and accounting/hash helpers. It checks loaded source consistency, the import-time source identity, and pre/post transition identities. The supported frozen registry must match exactly. A reviewed source change requires a fresh interpreter and separate research run; this wrapper does not migrate an existing book across code changes.

Each `operation_id` is an idempotent outer transaction identity. Identical input replay returns the stored result without rerunning booking. A conflicting input under the same ID fails. This is separate from the inner `portfolio_batch_id` linking preparation to execution. Missing-quote pending state, halt latches, original ceilings, attempted-entry guards and selected contracts survive database close/reopen because the complete portfolio state is persisted.

The shared store atomically commits one transition or rolls it back. Read with `phase3_store.read_state(database)` to verify the saved chain without executing a transition. Source/registry hashes detect accidental drift; they are not signatures or protection against a hostile interpreter or an attacker rewriting the entire database. External library/runtime reproducibility still needs the reviewed release manifest.

## Acceptance and limits

Offline tests use actual pure-adapter outputs from synthetic calendars/quotes. They cover restart after preparation, duplicate/conflicting operations, missing-exit persistence followed by valid recovery, changed run start/registry/scenario, backwards application time and malformed-input rollback. They do not establish host reboot recovery, broker reconciliation, authentic market evidence or profitability.

Remaining work includes complete research-accounting integration, verified actual calendars/chains/fees, exercise and assignment lifecycle, delta-matched benchmark execution, stress and delayed observation booking, independent reconstruction and untouched prospective evidence. The wrapper does not run a backtest or start an automated paper service.
