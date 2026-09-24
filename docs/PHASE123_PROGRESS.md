# Phases 1–3 implementation and acceptance evidence

September 24, 2026. The user authorized continued Phases 1–3 work on the Mac, with PC transfer last. This changes sequencing, not the acceptance requirements or live-trading boundary.

## Phase 1: operations

The read-only host probe now records Mac power conditions. Actual power logs identify repeated lid-closed and maintenance sleep on battery during the service-heartbeat gaps. An awake powered host is required for continuity; a running process or a brief maintenance wake cannot establish continuous management.

Private evidence is retained under `artifacts/phase123_20260924T154714Z`: `host.json` and `sleep-evidence.json`. No power setting, service or order policy was changed.

The recovery bundle tool captured four actual files: crypto ownership state, pending-order journal, seed and project mandate. Its isolated restore passed byte/hash verification. The source worker stayed running, so this is not a transactionally consistent stopped-writer recovery point. Local restore does not prove off-host disaster recovery or complete dependency coverage. See [RECOVERY_BUNDLES.md](RECOVERY_BUNDLES.md).

**Remaining acceptance:** actual PC boot/recovery, user-service startup, private access, source fencing, broker reconciliation at handoff, complete configuration/dependency recovery, off-host protected storage and controlled alert drills. These are deferred until the target host is available for transfer; no successful migration is claimed. Mac alert/failure/recovery observations remain useful evidence but are not Windows acceptance.

## Phase 2: durable evidence and accounting

The new transactional SQLite ledger imports saved fill/cash/position evidence with stable-ID deduplication, conflict rejection, source hashes and exact decimal arithmetic. Opening and ending boundaries are explicit. It does not infer return from account equity or silently forgive differences. See [ACCOUNTING_LEDGER.md](ACCOUNTING_LEDGER.md).

Actual exercise: all **406** saved September 23 fills imported into `sept23-accounting.sqlite3`; an identical second import added **zero** events. Twenty-three instrument symbols use verified saved order asset classes; the two option contracts use their saved explicit multiplier metadata. Context and source hashes are retained in the private evidence directory.

Result: **inconclusive**, with missing exact opening/ending snapshots and unverified activities, cashflow, fee and corporate-action completeness. No balances were reconstructed from the same fills to force a match. This demonstrates working durable import and honest evidence gates, not completed account reconciliation.

The market-data recorder now refuses a second finalization and requests after finalization. A verifier detects changed/missing files, unexpected files, unsafe inventory paths and symlinks. Existing archive bytes are not rewritten. Hash inventories are not signatures, source accuracy proof or filesystem-enforced immutability against an owner. Historical acquisition timestamps remain distinct from original publication/availability timestamps.

**Remaining acceptance:** complete actual account boundary evidence, supported mappings for all encountered activity/lifecycle types, decision-to-order-to-fill lineage, independently reconstructed realized/unrealized performance and cashflow-adjusted equity, corporate-action corrections and independently checked daily reconciliations. An offline ledger alone does not continuously ingest future account activity.

## Phase 3: frozen research

Three designs and a content/source-bound design registry are implemented. See [PHASE3_RESEARCH_PROTOCOL.md](PHASE3_RESEARCH_PROTOCOL.md). They cover equities, options and crypto, retain failed candidates, and specify execution/cost/benchmark/sample rules before new outcomes. Design receipts never grant order authority or claim prospective registration.

**Remaining acceptance:** executable adapters and parity tests, exact exchange calendar and source availability, calibrated costs/known overhead, independent design/code/data review, then sufficient genuinely untouched prospective observations and organic exits. New simulations, trade throughput and preexisting consumed results cannot substitute for that record. The future evidence gate cannot be completed today.

## Work order

1. Complete and independently review recovery, accounting and registry implementation; preserve real drill evidence.
2. Connect the new research definitions to tested causal adapters and durable decision/data lineage, without changing the current account writer.
3. Resolve actual data/accounting prerequisites; record unresolved differences explicitly.
4. Register reviewed prospective studies before their frozen start; collect rather than backfill evidence.
5. Perform the PC transfer and its actual recovery/fencing/alert tests last, as requested.

The project goal remains active. No phase is marked complete solely because tooling or fixtures pass, and no stock/options executor is activated by this work.

## Reviewed checkpoint

Combined focused regression: **165 passed**, covering host/preflight, evidence finalization/data, observer backup, release receipts, recovery bundles, accounting ledger and research registry. Independent reviewers approved root evidence/power changes, recovery behavior and research design tooling after schema validation repairs. Root reviewed ledger arithmetic/privacy and exercised actual import/deduplication.

`research-design-freeze.json` in the private evidence directory verifies canonical registry hash `b33d9064bb674f3948ac4b004a3ec88ad4553e08236ad42e5278741a6f4bedf0` and the design document/module sources. This is a design-only freeze: adapters are absent, prospective registration is false and no research outcome has been computed.
