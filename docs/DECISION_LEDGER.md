# Durable research decision lineage

`trader_engine.operations.decision_ledger` stores one research decision and its input snapshots atomically in an owner-only SQLite database. It has no broker client, credential access, scheduler or execution-authority path.

`record_decision` takes an explicit stable decision ID, strategy ID, UTC-aware decision time, protocol/source-code SHA256 identities, a nonempty list of input evidence, and a JSON adapter result. Each input carries `role`, `event_at`, `received_at` and `payload`. Event time must not exceed receipt time, and receipt must not exceed decision time. Completed-bar semantics, exchange calendars and source correctness remain the adapter's responsibility. Caller-supplied protocol/code hashes identify inputs; the ledger does not certify approval of that code or protocol.

Canonical JSON input snapshots are retained in the database, deduplicated by hash and linked in order to the decision. This is not an exact-byte HTTP archive. Preserve original raw responses separately and include their acquisition hashes/paths in the payload when available. Sources must be known nonsecret market/research evidence; do not pass credentials or arbitrary environment/configuration dumps.

Repeating the exact decision is idempotent. Reusing its ID for changed inputs or results fails. A correction requires a new ID with `amends` naming the earlier decision. Both remain visible; a correction cannot erase the original or make later information available earlier. Successful storage does not qualify any period as prospective or credit a simulated observation as actual trading.

`record_execution_link` adds an offline association between an existing decision, a client order ID, incremental activity IDs and the SHA256 of retained order/fill evidence. It cannot prove that the broker accepted an order, validate account identity, or reconcile the activity itself. `broker_execution_verified` remains false. Use the accounting ledger and independently retained exports to check quantities/cash; do not count links as trades or P&L.

`verify_decisions` opens the database read-only and checks hashes, source presence/order, causal source timing and parent references. It rejects corrupted evidence and dangling links. Hashes are unsigned and cannot detect coordinated replacement of payloads and hashes. SQLite is private durable storage, not filesystem-enforced immutability against its owner. The schema is dedicated: writes refuse a database containing unrelated tables, including the running paper-trial ledger.

This module is not installed into the current execution worker. The offline daily adapter integration below is available; a reviewed prospective collector still needs to record each decision at the actual decision time. Existing status summaries cannot reconstruct missing historical input availability, so prior decisions are not backfilled into a purported prospective record.

## Offline daily signal recording

Run from the repository root:

```sh
.venv/bin/python scripts/record_phase3_daily.py --snapshot /private/path/snapshot.json --registry config/research/phase3_protocols.json --database /private/path/decisions.sqlite3
```

The snapshot supplies `decision_id`, `strategy_id`, `symbol`, `signal_day`, `decision_at`, `metadata_received_at`, exact-text `held_quantity`, boolean `pending_exit`, `calendar` and `bars`. Calendar entries contain `day`, `open_at`, `close_at`; bars contain `symbol`, `day`, exact-text `raw_close`, nullable exact-text `total_return_close`, and `received_at`. Use timezone-aware ISO timestamps. See the synthetic fixture in `tests/test_phase3_recording.py` for the structure, not a production calendar.

The command calls the actual daily signal adapter, binds its source bytes and the conversion layer by hash, and stores the result with all supplied bars and calendar/position metadata. It refuses inputs received after the stated decision or before bar completion, changed protocol definitions unsupported by the adapter, and a database path equal to either input path. Caller receipt timestamps remain assertions: this command does not authenticate them. Every result is labeled `offline_reconstruction` with `prospective_credit=false`; writing it today never establishes an earlier live decision. Entry estimates, reservations, option decisions and future automatic collection are not wired into this command.

September 24 integrated CLI drill: a synthetic fixture stored one decision linked to 22 inputs; a second identical invocation retained one decision and returned `inserted=false`. Evidence is private under `artifacts/phase3_adapter_drill_20260924T160822Z`. This tests recording/idempotence, not market performance.
