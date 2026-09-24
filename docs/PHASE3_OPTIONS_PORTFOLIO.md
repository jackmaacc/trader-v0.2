# Base options portfolio booking from pure adapter evidence

`research/phase3_options_portfolio.py` adds an offline cash/ownership layer for unchanged `O_ETF_TREND_CALL_V1` **base-cost, same-observation modeled fills**. It consumes actual result dictionaries from `phase3_options.plan_entries` and `plan_exit`, alongside their selected contract metadata and OPRA quote marks. It does not select a different contract, run a historical campaign, place orders or activate the strategy. Every state/report denies execution, live approval and investment qualification.

This bounded component does **not** implement stress or delayed booking, delta-matched benchmarks, complete lifecycle accounting, a broker writer, or prospective qualification. It is separate from the running paper account.

## Public interface

- `initial_state(utc_day)` starts flat with $100,000, no invented prior-close reference.
- `set_day_baseline(state, utc_day, prior_close_equity, *, boundary_at, provenance)` supplies an explicit reconciled UTC-boundary reference. It refuses backward dates, dates before recorded execution, and changed same-day references. Only a new day resets the latched one-percent entry halt. Cash-flow treatment and provenance authentication remain caller responsibilities.
- `adapter_positions(state)` returns the existing adapter's `Position` objects, preserving original premium, contract, entry date, one-contract quantity and any pending-exit reason.
- `prepare_batch(state, batch_id, calendar, session_date, now, entry_outputs, exit_outputs, contracts, marks)` freezes asserted adapter outputs, full contract metadata, calendar hash, preparation time and original budget ceilings. `contracts` maps selected symbols to `phase3_options.Contract`; `marks` maps option symbols to `phase3_options.Quote`. Nonempty entry outputs must contain the complete ordered IWM/QQQ/SPY list from `plan_entries`; later exit-only batches can use an empty list. Every currently owned underlying needs an explicit hold/pending/exit output. No pending exit may disappear into a hold.
- `apply_batch(state, batch_id, now, marks)` returns `(new_state, report)` with exact string amounts and explicit modeled fills. Inputs are not mutated. JSON roundtrips preserve state. A separate durable wrapper must persist each complete transition atomically.

Only one prepared batch may be unresolved. Exact content replay is idempotent; changed input under a reused ID is rejected. The complete frozen body is hash-bound. These hashes detect accidental changes, not an attacker rewriting hashes or proof that an asserted output was produced by authenticated market data. The caller must retain the underlying adapter inputs, source provenance, fee schedule and ownership history.

## Causal booking and limits

Entry candidates retain their original selected contract, quantity one, base modeled premium, verified fee amount and total debit from the adapter. Arithmetic is checked at the 100-share multiplier. The budget ceiling freezes from preparation-time state valuation: at most 0.25% equity including fees, available cash, and remaining 0.75% aggregate original premium allowance. It never increases from later prices or proceeds. Reservation is deliberately conservative: preparation does not spend proceeds from exits that have not yet been modeled.

Entries can book only at their recorded 09:35 decision time. Late preparation produces zero entry ceilings; late application skips missed candidates. No contract reselection, cheaper-strike fallback, sizing inflation or later quote repricing occurs. Current valuation, cash, halt and aggregate premium limits can only reduce/skip admission. One position per underlying and three total positions are enforced. Ownership carries the original total premium, excluding separately recorded fees.

Exits book before entries in a same-observation batch. An exit candidate must be prepared and booked at its own recorded observation timestamp. A noon exit output cannot be included in the morning batch or used to fund earlier entries. Use separate chronological batches for later exits. Missing exit quotes/fees preserve ownership and pending reason. Expired contracts stay in lifecycle-reconciliation state; they never receive invented liquidation proceeds. Even valid modeled exits are not broker confirmations.

Entry attempts, including explicit adapter skips, and closed positions have session guards. A different batch ID cannot retry the same underlying's skipped entry or re-enter after an exit that session. No incomplete or expired position is silently dropped.

## Valuation, halt and cost scope

Held contracts are valued at causally available OPRA bid quotes using the original adapter's positive, uncrossed, size/spread and two-second freshness checks. Missing held marks produce `equity: null`, blocking entries. Valid exit candidates can still book without unrelated portfolio marks. A newly admitted entry also needs its own current mark.

The one-percent UTC-day entry halt is stored and remains latched after recovered equity. No known prior-close baseline means no new entries. There is no liquidation triggered merely by this entry halt. Marks and source labels remain asserted offline evidence; the module never fetches quotes or authenticates fee/account eligibility.

Base prices and fees are taken unchanged from the pure adapter's candidate, where base entry is ask plus one-percent adverse impact and exit is bid minus one percent. The ledger records cash debit/credit, premium, fee, quantity and contract separately. It does not calculate investment returns or claim FIFO/broker reconciliation. Stress/delayed scenarios need a separately bound original budget plus later contract-specific quote revalidation; they cannot be synthesized by running contract selection again.

Calendar provenance, forward corporate actions, complete chains, weekly signal ownership, deterministic delta controls, exercise/assignment events, actual fees, business overhead, independent accounting reconstruction and untouched prospective registration remain required. This component's tests are synthetic software checks, not paper observations or evidence of edge.
