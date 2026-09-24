# Offline FIFO P&L attribution

`trader_engine.operations.pnl_attribution.attribute(context, events, evidence)` accepts the immutable accounting ledger's context and normalized events. `attribute_database(database, evidence)` reads those inputs and source hashes from an existing ledger through a read-only SQLite transaction. Neither API requests broker data, changes accounting evidence, or authorizes execution.

This is attribution, not a strategy backtest or a claim of edge. It calculates dollar P&L, not a percentage return or annualized performance.

Evidence supplies:

```json
{
  "opening_lots": [
    {"symbol": "SPY", "quantity": "2", "price": "500", "entry_fee_remaining": "0.20", "acquired_at": "2026-09-22T14:00:00Z"}
  ],
  "opening_marks": {"SPY": {"price": "510", "timestamp": "2026-09-23T00:00:00Z"}},
  "terminal_marks": {"SPY": {"price": "515", "timestamp": "2026-09-24T00:00:00Z"}}
}
```

Opening lots must explicitly cover the signed opening snapshot, with known original unit execution price and remaining allocable entry fees. A flat opening requires an explicit empty `opening_lots` list. Acquisition timestamps cannot follow the opening boundary. Lots sort chronologically; their supplied list order resolves equal acquisition timestamps and therefore must reflect established FIFO order. Never infer basis from marks, closing snapshots, or the fills being tested.

Opening and terminal marks are required for every corresponding nonzero position. Each mark must have exactly the boundary timestamp, with known valuation provenance retained by the caller. The tool neither fetches marks nor pretends a previous last-trade timestamp is an exact-boundary observation. Prices and other inputs share the ledger bounds: magnitude at most 1e12 and at most 12 fractional places. Multipliers are explicitly supplied in ledger context and must be positive; adjusted options must use their actual documented multiplier.

Fill execution prices are recovered from normalized signed fill cash, incremental quantity, explicit multiplier and inline fee, checked for exact reversal of that identity. Fills process chronologically. Same-symbol fills with identical timestamps require `event_order`, a complete unique list of event IDs supplying the broker's actual tie ordering. Arbitrary lexical activity-ID order is not assumed to be execution order.

FIFO handles long and short partial exits, several opening lots, and reversals. A crossing fill closes the old direction before opening the residual in the new direction. Entry and exit fees are allocated in proportion to matched quantities to 12 decimal places, rounding toward zero. The final portion receives the entire remaining fee. This documented allocation convention preserves fees exactly; it does not assert that repeating fractional allocations have finite exact decimal representations. Arithmetic uses 256-digit Decimal precision, never floating-point market arithmetic.

Outputs separate realized gross/net P&L, terminal unrealized gross/net P&L, opening unrealized net P&L, period fill fees, standalone fees, dividend/interest income and external deposits/withdrawals. Realized net may include entry fees incurred before this window. Period attribution therefore subtracts opening unrealized net, preventing historical basis gains or historical entry fees from being mislabeled as current-period performance:

`period net P&L = realized net + terminal unrealized net − opening unrealized net + income − standalone fees`

An independent cash-and-marked-quantity calculation checks:

`ending marked equity − opening marked equity − external cashflows = period net P&L`

Cash reconciliation and signed ending quantities are also checked without hidden tolerances. Any mismatch produces `mismatch`. Missing activity, cashflow, fee or corporate-action completeness declarations keep the result `inconclusive`, even where arithmetic is computable. Missing basis, marks, boundaries or ordering prevents complete numeric attribution. Raw cash or position adjustments are unsupported because their basis and P&L meaning cannot safely be invented. Option expiration/assignment, stock splits, wash-sale accounting, multicurrency FX and broker-specific tax-lot elections need separately reviewed mappings. This is economic FIFO accounting, not tax reporting.

`attributed` means the supplied evidence reconciles under these explicit assumptions; it does not authenticate source ownership or prove export completeness independently. The caller must retain source evidence and marks/basis provenance. No account equity figure alone establishes return. The September 23 historical import remains incomplete unless exact boundary balances, lot basis, marks and completeness are independently established.
