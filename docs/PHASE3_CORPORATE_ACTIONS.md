# Modeled equity corporate actions

`research.phase3_actions.apply_corporate_action(state, event, now)` adds explicit dividend receivables, payments and whole-share splits to the offline equity candidate or ETF-control ledger. It returns a new JSON state and receipt; it never changes the input, requests provider data or touches the paper account. This implements bookkeeping, not verification of the historical QQQ/SPY corrections or missing payment dates.

Every event requires a unique `id`, `kind`, timezone-aware `effective_at` and `received_at`, and a retained `source_sha256`. Amounts use exact decimal strings. The caller must supply the real event boundary and ordering from archived evidence. Receipt must be no later than processing; `now` must equal the effective boundary. Events cannot alter an earlier processed state. Historical evidence learned later requires a separate reconstruction with its late availability retained, not backdating this state machine.

Supported events:

| Kind | Additional evidence | Ledger effect |
| --- | --- | --- |
| `cash_dividend` | `symbol`, `quantity_before`, `amount_per_share`, `currency="USD"`, optional `payment_at` | Amount equals owned eligible shares times per-share distribution; creates a nonspendable receivable |
| `dividend_schedule` | Original `dividend_id`, verified `payment_at`, USD currency | Records a previously unknown date; creates no cash |
| `dividend_payment` | Original `dividend_id`, exact `amount`, USD currency | Requires payment evidence and exact entitlement reconciliation; moves the amount from receivable to cash |
| `split` | `symbol`, `quantity_before`, positive `ratio` | Adjusts owned share quantity; no cash or dividend entitlement change |

An unknown payment date stays unknown and unpaid. A scheduled date alone never credits cash automatically. Payment evidence may arrive after the stock was sold: the prior entitlement remains payable. A payment cannot precede a known scheduled date; a different known schedule, net withholding or unmatched credit requires a separately reviewed correction rather than a forced match. Only USD cash distributions are supported.

`quantity_before` must match actual state at entitlement/split time. Entitlements and splits must precede portfolio processing at that same boundary, tracked separately through `last_portfolio_at`; a purchase at the ex-dividend open cannot acquire the earlier entitlement. Multiple actions at a boundary need authoritative ordering and share basis. The module does not infer simultaneous dividend/split ordering from filenames or IDs.

Receivables live under `corporate_actions.receivables`, alongside the retained event receipts. `receivable_value(state)` contributes their unpaid face value to equity, not cash or market-exposure gross. On payment, cash rises by exactly the amount by which receivables fall, so payment itself does not add profit again. The daily candidate's valuation and benchmark preparation use that shared helper.

Whole-share splits preserve pending-exit symbols but change their owned quantity; future decisions must use the new quantity and correctly adjusted raw market prices. Fractional split entitlements are rejected until cash-in-lieu handling exists. Split cost-basis/tax-lot/FIFO amendments, mergers, spinoffs, option deliverable adjustments, withholding, multicurrency and changed-entitlement corrections are not implemented. Do not pass this equity-quantity interface an options-contract position object.

Actions are rejected while a prepared portfolio/benchmark batch remains unresolved. Its original quantities cannot silently survive a split or receive new spending capacity. An action-aware rescheduling/cancellation transition remains a prerequisite for those cases. Normal source gaps remain visible; callers must not discard failed dates.

The daily durable wrapper accepts `operation={"kind":"corporate_action","event":...,"now":...}` and binds this module in its source inventory. Repeated identical event IDs are idempotent; changed evidence for an existing ID fails. Independent accounting should consume unique event IDs, not sum a replay receipt's deltas again. Hashes identify bytes and detect drift; they do not authenticate a provider or prove entitlement completeness.

Arithmetic uses a local 128-digit context. Inputs and derived amounts are bounded; extreme valid-sized inputs whose product exceeds the supported range fail rather than truncate. Normal-sized fixture tests verify ex-date price-drop offsets, nonspendable receivables, payment after sale, no duplicate income, split invariants, missing/late evidence, conflicting IDs and restart persistence. These are synthetic checks, not actual reconciled income or investment evidence.
