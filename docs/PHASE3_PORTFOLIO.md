# Offline daily research portfolio transitions

`research/phase3_portfolio.py` composes the existing frozen daily adapters for **one separate equity or crypto track**, starting flat with $100,000. This is a pure synthetic/offline state machine, not a broker executor, an investment qualification system or a reported simulation campaign. It produces explicitly labeled **modeled fills** from adapter estimates; these never become real or paper broker fills by being persisted.

## State and public interface

- `initial_state(strategy_id, utc_day)` returns a JSON-compatible state with $100,000 cash, no holdings, no pending exits and no assumed prior-close baseline. Strategy must be `E_DONCHIAN20_V1` or `C_SMA200_CONFIRM_V1`; tracks cannot share state or be silently combined.
- `set_day_baseline(state, utc_day, prior_close_equity, *, boundary_at, provenance)` returns new state. It requires an exact UTC-midnight boundary and a nonempty reference to independently reconciled prior-close equity. It refuses backward dates and replacement of an already recorded same-day baseline. Only a genuinely new explicit UTC day resets the loss latch. A reference string is a caller assertion, not source authentication. The caller must reconcile cash flows rather than using deposits as recovered trading P&L. No cash-flow import or adjustment is implemented here.
- `prepare_batch(state, batch_id, decisions, marks, now, fees, quantity_steps)` returns a new state containing frozen original entry quantity/budget limits and decisions. Every frozen-universe symbol must have an explicit decision, fee schedule and quantity step. Current quantity and pending-exit ownership must match the supplied decision. Preparation records an integrity-bound `prepared_at` timestamp; application cannot precede it. An entry prepared after its scheduled execution receives a frozen zero-size plan (`entry_plan_prepared_after_execution`), preventing late marks from retrospectively sizing an earlier open. Late preparation preserves pending exits, but an equity exit prepared after its scheduled open cannot reuse that missed open: it is blocked with `missed_preparation_open` until a later correctly scheduled batch. A crypto exit prepared within the frozen 60-second window may use a genuinely fresh quote, subject to the existing adapter checks. One unresolved prepared batch is permitted at a time, preventing competing reservations. Identical preparation replay returns unchanged state; changed content under the same ID is rejected.
- `apply_batch(state, batch_id, observations, marks, now, stress=False)` returns `(new_state, report)`. Every universe symbol needs an explicit `PriceObservation` or `None`. It validates frozen content hashes, ownership and chronological execution on the scheduled UTC session date. It processes exits alphabetically, then entries alphabetically. Entry quantities and cash debit cannot exceed the original plan even if execution prices improve. New risk observations may reduce or block a plan, never enlarge it.
- `validate_state(state)` checks basic schema, quantities, universe, pending ownership, session guards and permanently false execution authority. JSON encode/decode preserves exact Decimal strings. Validation is not cryptographic authentication or proof that manually supplied starting ownership came from fills.

Use `phase3_daily.SignalDecision`, `PriceObservation`, `FeeSchedule` and Decimal quantity steps. No function computes an entry signal from outcomes or retunes the strategy. The caller must produce decisions using the causal frozen signal adapter, preserve its original dates and record data gaps.

State contains exact cash/owned quantities, persistent pending symbols, UTC day/baseline/reference, latched halt, prepared/completed batch receipts, modeled fill journal, per-symbol entry-attempt/closed-session guards and the last applied timestamp. Input dictionaries are not mutated. A failure before the returned transition leaves the original state unchanged; durable atomic storage belongs to the separate state-store wrapper.

## Valuation and exit priority

`Mark(price, event_at, received_at, source)` uses a positive Decimal price, aware timestamps and source `sip` or `alpaca_crypto` for the selected track. **The mark's event timestamp must equal the explicit research valuation boundary `now`; receipt must be causal and available by that boundary.** This deliberately strict synthetic research contract does not mean a real exchange quote arrives instantaneously. Do not relabel an older close/quote as a current mark to satisfy it. An eventual observed-data valuation layer needs separately reviewed boundary construction and receipt semantics.

Every owned instrument requires a valid mark for equity/gross. A prospective entry also needs its own mark before admission. Missing, old, future or unsupported marks block entry sizing and make reported equity/gross unavailable where holdings cannot be valued. Mark ages remain visible; a supplied source label is not verified source provenance. This avoids silently valuing a current portfolio at stale prices. There is no newly imposed two-second SIP *signal* rule: the boundary contract concerns this synthetic portfolio valuation, and execution observations retain the frozen daily adapter's own timing rules.

**Missing portfolio valuation never blocks a valid owned exit estimate.** A valid BTC exit can process even when an unrelated ETH holding lacks a mark; the remaining equity/gross then stays `null`, with `valuation_available=false`. Missing exit observations preserve both ownership and pending intent. If portfolio marks become available after exits, entries still respect their original frozen budgets, including any zero budget frozen when preparation evidence was absent.

The one-percent equity / three-percent crypto day-entry halt latches when a valid valuation crosses its baseline threshold. Later recovery during the same UTC day cannot clear it. Missing baseline or an unadvanced UTC-day boundary blocks entries without inventing a reference value. These conditions do not prevent risk-reducing exits with valid execution evidence.

## Modeled fills, fees and idempotency

All execution arithmetic comes from `phase3_daily.estimate_entry` / `estimate_exit`. Entry base-asset fees reduce acquired units; exit base-asset fees increase owned units debited in addition to the sold quantity. Precision dust remains owned and pending; it is never rounded away to pretend a position closed. A modeled flat exit clears only this research ledger's pending symbol. The primitive adapter's retained pending flag is preserved separately as `adapter_pending_exit`, distinguishing unexecuted estimates from this explicitly modeled state transition.

Entry attempts are recorded even if their observation is missing. A different batch ID cannot retry that symbol's entry on the same UTC session date or re-enter after a modeled exit that session. Existing holdings prohibit pyramiding. Pending exits may be retried in a later prepared batch with updated scheduled decisions, but their remaining ownership must match exactly.

Reapplying a completed batch with identical application content returns the current state unchanged and `replayed=true`; it never replays historical fills into newer state. Reusing that ID with changed marks, observations, time or scenario raises an error. Hashes catch content drift, not an attacker able to rewrite both state and hashes. The storage wrapper must bind and atomically commit the complete JSON state and result.

## Reuse and remaining gaps

This module reuses:

1. The existing `phase3_daily` fee normalization, original entry sizing and base/stress execution estimates.
2. The shared `execution.portfolio.Reservations` cash/notional reservation primitive, locally and without any broker object or live service state.

The general `backtest.engine.BacktestEngine` currently uses float-oriented ATR/strategy position accounting, while live `Reservations.release_flat` deliberately does not recycle unconfirmed positive P&L. Neither is silently claimed equivalent to this Decimal long-only modeled ledger. No generic-engine parity or broker-accounting parity is asserted. Research state is wholly separate from the live/paper ownership journal.

Explicit dividend/receivable/payment and whole-share split transitions are integrated through [PHASE3_CORPORATE_ACTIONS.md](PHASE3_CORPORATE_ACTIONS.md); unpaid receivables contribute equity but never cash.

Still missing: complete corporate-action corrections and cash-flow transitions, broker reconciliation, contractual partial-fill/cancellation lifecycle, independently reconstructed portfolio accounting, delayed-scenario scheduling, benchmark portfolios, options orchestration, portfolio concentration reporting and prospective qualification. Cash here is immediately reusable only because estimates are explicitly modeled as fills in an offline experiment; this must never authorize using unsettled or unconfirmed broker proceeds. No performance outcomes are produced by the tests.
