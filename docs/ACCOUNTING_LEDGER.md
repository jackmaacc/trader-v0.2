# Offline cash and position accounting

`scripts/accounting_ledger.py` imports saved activity evidence into a transactional SQLite ledger. It never contacts a broker or loads credentials. This verifies cash and signed quantities for one explicitly identified USD account and time window; it does not establish execution quality, profitability, or authorization.

```sh
.venv/bin/python scripts/accounting_ledger.py --database artifacts/accounting/example.sqlite --activities saved-activities.json --context saved-context.json
.venv/bin/python scripts/accounting_ledger.py --database artifacts/accounting/example.sqlite
```

The second command opens the database read-only. Exit code 0 means reconciliation with declared complete evidence; 2 means mismatch or incomplete evidence. Invalid or conflicting evidence fails with an exception and rolls back the import. New ledger files are owner-only (0600), new parent directories are private (0700), symlink paths are rejected, and existing ledgers must already be owner-only regular files. Existing parent permissions are not changed. Treat databases as private account evidence and store under a private state directory.

The context format is:

```json
{
  "account_reference": "paper-account-local-alias",
  "currency": "USD",
  "start": "2026-09-23T00:00:00Z",
  "end": "2026-09-24T00:00:00Z",
  "opening": {"timestamp": "2026-09-23T00:00:00Z", "cash": "1000", "positions": {}},
  "ending": {"timestamp": "2026-09-24T00:00:00Z", "cash": "979.90", "positions": {"ETF": "2"}},
  "instruments": {"ETF": {"multiplier": "1"}},
  "activities_complete": true,
  "cashflows_complete": true,
  "fees_complete": true,
  "corporate_actions_complete": true
}
```

Completeness flags are evidence declarations, not discovery. Only set them after checking export coverage/pagination, all relevant cash movements, fee reporting and corporate actions. Missing declarations or snapshots yield `inconclusive`, including empty activity lists. A complete explicitly flat unchanged interval can reconcile with no events. Opening/ending snapshots must be observed at the declared exact boundaries; do not fabricate opening cash from ending cash minus fills. The event window is strictly `(start, end]`. Account aliases must identify the same real account throughout; the tool cannot authenticate an offline export's ownership.

Activities are a JSON array of Alpaca-style `FILL` records with stable `id`, `transaction_time`, `symbol`, `side`, `qty`, `price` and optional nonnegative `fee`. Each incremental partial fill counts separately. `cum_qty`, canceled order status and order IDs never replace activity quantities/identities. Multipliers are required explicitly for every fill symbol, including equity/crypto 1 and the actual documented option contract multiplier. Never assume every option contract is 100; adjusted contracts require verified metadata. All snapshots use signed position quantities, including shorts.

Supported cash events are `CSD`, `CSW`, `DIV`, `INT`, `FEE`, and explicit `CASH_ADJUSTMENT`, with signed `net_amount`: deposits positive, withdrawals negative, fees negative. Fill fees reduce cash separately; never include the same fee both inline and as a fee event. `POSITION_ADJUSTMENT` accepts `symbol` and signed `quantity_delta` for reviewed non-fill quantity changes. Cash and position adjustments must carry independent stable IDs and timestamp evidence. Date-only non-trade records require a documented boundary attribution before conversion to timezone-aware timestamps; the importer does not invent an intraday time. Unsupported broker activity types fail instead of being silently dropped.

Amounts use decimal strings (JSON fractional numbers are parsed as Decimal). Input amounts support magnitude up to 1e12 and at most 12 fractional decimal places; larger or finer input is rejected explicitly. Derived products and differences use 256-digit Decimal arithmetic with separate wider validation, so accepted quantity/price/multiplier products retain tiny fees exactly. No binary floating-point arithmetic or SQLite numeric aggregation is used. The reported cash difference is calculated cash minus observed ending cash; position differences have the same convention. No tolerance silently forgives a difference. External cashflow and fee totals are reported separately. Total return, realized profit and unrealized profit are deliberately not inferred from this evidence.

SQLite commits batches atomically, deduplicates stable activity IDs, and rejects conflicting repeats including changed raw evidence. Each import retains SHA256 hashes for the exact source/context bytes and links event IDs to source hashes. Import ordering does not affect sums. A ledger's context is immutable: corrected snapshots, another account, or a new interval require a separate ledger, retaining the earlier evidence. The tool does not automatically detect omitted broker events, corporate-action completeness, account authenticity, multi-currency FX, or ledger tampering. Hashes identify source bytes; they are not signatures. The source files are read only and must be retained separately.

Existing historical candidate: `artifacts/alpaca_orders_2026-09-23/raw/activities.json` contains 406 fill activities, including option contracts. Review its contract metadata and opening/closing snapshot timestamps before constructing context. It is useful import evidence, but its presence alone does not supply a complete accounting interval. Never claim reconciliation by deriving missing boundaries from the same fills under test.
