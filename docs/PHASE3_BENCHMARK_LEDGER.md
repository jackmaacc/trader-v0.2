# Offline benchmark cash and quantity ledger

`research.phase3_benchmark_ledger` turns the frozen benchmark target definitions into **modeled**, separate cash/holdings transitions. It does not fetch data, create broker orders, prove actual fills, produce investment outcomes or authorize execution. All fees/precision/source assertions still require verified evidence. Root research storage can retain these pure JSON transitions; this module itself performs no I/O.

## Shared state and API

`initial_state(strategy_id, control)` accepts `matched` or `passive`, starts with$100,000 cash and no holdings, and returns exact decimal strings under `cash`/`positions`, immutable-input batch/attempt journals and modeled fill history. The shared action ledger can attach `corporate_actions`; `receivable_value(state)` delegates to that ledger. Unpaid distributions enter equity at their recorded face amount but never fund buys. Positions continue to include base-fee/precision dust.

`SignalPrice(price, SourceStamp)` carries raw decision-scale prices available at the target's exact expected boundary. `ExecutionPrice(event_at, received_at, source, source_sha256, raw_open, bid, ask, bid_size, ask_size)` carries executable-price evidence. Sources are `sip` for ETF controls, `alpaca_crypto` for crypto controls. Source labels/hashes identify caller-supplied evidence without authenticating it.

`prepare_target(state, batch_id, target, signal_prices, fees, quantity_steps)` freezes desired quantities and maximum buy amounts from the supplied `BenchmarkTarget`. It requires every universe symbol, even zero weights. Target dollar values must equal target weights times the **benchmark's own cash + marked positions + receivables**. No candidate cash or return can be substituted. Equity/options-underlying controls require whole shares; crypto requires explicitly verified venue steps. Fees use `phase3_daily.FeeSchedule`, with crypto rate at least25bps and explicit base/quote currency.

Signal-valued projected sell proceeds create only a frozen upper bound on possible buy reservations. They are not available cash. At execution, actual modeled cash after successful sells is the binding constraint. Frozen buy quantity and budget can only shrink, even if prices fall or sell proceeds rise. Sells and buys run in alphabetical order, all sells before any buy. Controls have no candidate entry halt, trend filter, stop or extra exposure ceiling; exposure targets come solely from the frozen benchmark definitions.

`execute_target(state, batch_id, attempt_id, observations, now, stress=False, retry_execution_at=None)` returns `(new_state, report)`. Every symbol must have an observation or explicit `None`. Equal attempt IDs/evidence replay the existing report without another fill; conflicting IDs fail. An immutable plan hash binds target, original buy/sell quantities, cash budgets, fees, steps, pre-plan ownership/cash and corporate-action identity; every transition verifies it before use. Remaining sell quantities are separate mutable state. Unsigned hashes detect inconsistency, not coordinated malicious rewriting; the durable outer journal remains necessary. Cost scenario is fixed on the first attempt and cannot change on retries. New target batches cannot rewind or reuse an executed boundary. The module returns modeled cash/quantity changes and fees, not completed strategy episodes.

## Cost and timing

- Equity benchmark: actual scheduled09:30Eastern raw open,7bps adverse impact or14bps stress plus specified applicable fees.
- Crypto benchmark: first supplied valid execution within60seconds of00:05UTC, positive noncrossed venue bid/ask no older than2seconds,5/15bps adverse impact plus at least25bps fee. The caller must prove this was the first valid eligible observation; a later favorable quote cannot silently replace it.
- Options **underlying ETF** control:09:35Eastern SIP ask for buys/bid for sells, age at most2seconds, positive noncrossed quotes, bid/ask sizes at least1 and spread at most10% of midpoint;7bps adverse impact (14bps stress) plus ETF fees. These are ETF controls, not option premiums or option broker fills.

The actual archived calendar remains a prerequisite; fixed clock checks do not establish that a supplied date is an exchange session. Timestamp order rejects future/unavailable evidence. Each target retains its archived calendar identity. Default timezones follow New York DST and UTC crypto boundaries.

Base-asset buy fees reduce acquired units; base-asset sell fees reserve additional owned units so a sell cannot overspend inventory. Quote fees debit cash/proceeds. All calculations use a local50-digit Decimal context. Actual venue rounding/minimum notional rules beyond supplied step/fee remain a qualification dependency.

## Missing evidence, retries and actions

Missing buys are skipped for that target and explicitly recorded, never retried on a later date. Missing tradable sells remain pending and block new target preparation. A subsequent attempt may retry **only the original pending sells**, with an explicit later scheduled execution boundary. It cannot refill skipped buys or increase original quantities. The caller must verify the retry boundary against the actual calendar; no holiday inference occurs here.

Sub-step sell residuals remain economically owned and marked. They appear in `dust_owned`, but do not indefinitely block every later rebalance: once no quantity at least one verified step can be submitted after reserving base fees, the executable batch completes. No cash or units are silently written off. Missing quotes or fees exceeding proceeds for still-tradable quantity remain pending.

Passive initialization can be prepared once. After that, no target rebalance is permitted, including retrying an initially skipped buy under a new target. Only outstanding sell retries could remain if the supplied passive state legitimately contained a pending sell; ordinary passive startup begins flat. Dividend income, splits and valuation continue through the shared action/mark layer.

A prepared batch has `apply_hash=None` until executable pending sells are resolved; completed batches receive an input hash. The shared action ledger can use this to block actions during unresolved plans. `last_portfolio_at` advances on each applied benchmark attempt, allowing the action ledger to require ex-date entitlement and split events before portfolio processing at the same boundary. If action state changes between planning and execution, execution rejects and requires explicit action-aware handling. This module does not rescale a queued order after a split or guess entitlement timing. Direct out-of-band cash/position edits are not authenticated; upstream durable transition ownership must prevent them.

Every report retains issue reasons and pending quantities; nothing drops a date to improve apparent results. Required remaining work includes source provenance, production calendar verification, event-to-ledger integration, independent cash/quantity reconstruction, fees/rounding/minimums, delayed scenarios, matched-control tracking, passive performance accounting and prospective registration. Unit fixtures are not backtests or paper evidence.
