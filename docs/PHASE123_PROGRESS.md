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

**Remaining acceptance:** shared portfolio orchestration and reference parity, exact exchange calendar and source availability, calibrated costs/known overhead, independent design/code/data review, then sufficient genuinely untouched prospective observations and organic exits. New simulations, trade throughput and preexisting consumed results cannot substitute for that record. The future evidence gate cannot be completed today.

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

## Causal adapters and attribution checkpoint

The frozen daily equity/crypto and weekly options rules now have pure research adapters. They check information availability, preserve pending exits, enforce explicit latched UTC-day entry halts, and calculate conservative sizing/cost estimates without broker I/O. Later corrections cannot change an earlier signal. Planned entry quantities cannot grow from later price information. See [PHASE3_DAILY_ADAPTERS.md](PHASE3_DAILY_ADAPTERS.md) and [PHASE3_OPTIONS_ADAPTER.md](PHASE3_OPTIONS_ADAPTER.md). These adapters do not replace the running BTC/ETH worker.

The private transactional [decision ledger](DECISION_LEDGER.md) retains causal input snapshots, protocol/code hashes, corrections and order-evidence associations without claiming broker verification. An offline daily-signal command integrates the actual adapter; its synthetic CLI drill verified one decision/22 input records and idempotent replay. No prospective observations were backfilled. The original design-only freeze remains unchanged and does not bind these new adapter sources.

[FIFO attribution](PNL_ATTRIBUTION.md) now separates realized/unrealized P&L, fees, income and external flows, with exact cash/quantity and marked-equity identity checks. Its read-only run against the 406-fill historical ledger retained `inconclusive`, `metrics=null`, and six missing-evidence conditions in `artifacts/phase123_20260924T154714Z/sept23-pnl-attribution.json`. It does not invent boundaries or certify source completeness.

Combined focused regression: **172 passed** across daily/options adapters, recording/decision ledger, FIFO attribution, accounting import and protocol registry. Independent reviewers confirmed repaired decision-before-execution timing, persistent-halt input enforcement, options causal revision selection and P&L arithmetic. A separate reviewer also exercised 200 synthetic FIFO/cash/quantity identity cases. These are software checks, not market simulations or qualifying returns.

Remaining implementation includes persistent portfolio orchestration (day identity, halt reset, ownership/reservations and exit-before-entry ordering), delay scenarios retaining original plans, shared-engine reference parity, passive/exposure/delta benchmark ledgers, verified calendar/corporate-action/fee evidence and prospective collection/registration. All three asset tracks remain research work. Actual PC recovery/cutover and the untouched future sample remain required.

## Portfolio persistence and benchmark integration

The daily equity/crypto adapters now compose into a separate flat-$100,000 modeled portfolio with shared cash reservations, alphabetical exits before entries, preserved original plan limits, base-fee/dust ownership, pending exits and persistent UTC-day entry halts. Cross-batch guards prevent duplicate entry attempts or same-session reentry. Missing valuation blocks new entries while valid owned exits remain available. See [PHASE3_PORTFOLIO.md](PHASE3_PORTFOLIO.md).

The [transactional research store](PHASE3_STATE.md) commits inputs, results and portfolio state atomically, with stable batch identities and hash-linked history. The concrete JSON integration derives implementation identities, rejects changed/hot-edited sources, and survives restart/replay without duplicating modeled fills. An independent FIFO reconstruction checks a synthetic equity round trip; actual September 23 reconciliation is still incomplete. These records never become broker fills or prospective credit.

[Benchmark target calculations](PHASE3_BENCHMARKS.md) cover fixed passive sleeves, candidate-lagged equal-weight exposure and archived options delta-notional controls using each benchmark's own equity. Missing expected dates remain inconclusive; caps retain the uncapped comparison. The options tracking gate's aggregation rule is unspecified in the frozen design, so both aggregate and per-underlying errors are reported without declaring that gate passed. It needs a prospective clarification before outcomes, not favorable selection afterward.

Remaining work is broader than these components: benchmark fill/cash/turnover ledgers, options portfolio orchestration and lifecycle, delay scenarios, corporate-action/receivable handling, observed-data valuation boundary construction, full reference parity and verified source/fee/overhead evidence. The daily portfolio's strict synthetic boundary marks are not a deployed real-market valuation collector. Prospective registration/sample and deferred PC acceptance remain open. Existing services and strategy policies are unchanged.


Portfolio checkpoint validation: **238 combined tests passed**, including 22 portfolio, 22 benchmark, 14 transactional-store and 8 concrete store-integration checks alongside the prior 172 tests. Independent review caught and confirmed repairs for Python bool/number replay identity, source hot edits, preparation-before-execution timing and missed equity opening prices. Synthetic restart/replay and SQLite isolated restore produced identical verified state (three operations, three explicitly modeled fills) under `artifacts/phase3_portfolio_drill_20260924T162550Z`; repeat execution added no fill. This is local synthetic recovery evidence, not an off-host restore or paper-trading record.


## Benchmark books, options booking and corporate actions

The frozen benchmark targets now have their own modeled cash/quantity ledger. It freezes quantities and cash ceilings, charges spread/impact and applicable fees, executes sales before purchases, retains missing tradable exits and owned precision dust, and prevents passive rebalancing. Source-bound plan hashes and remaining-sale bounds reject altered quantities, budgets, fees or symbol sets. See [PHASE3_BENCHMARK_LEDGER.md](PHASE3_BENCHMARK_LEDGER.md).

A separate options portfolio component books the original pure-adapter candidates at their same observation time, with one standard call per underlying, full fee/premium bookkeeping, frozen budget ceilings and a persistent entry halt. Pending/expired contracts remain owned until supported lifecycle evidence exists. Skip retries and contract/metadata symbol substitution are rejected. This is base same-observation booking; stress/delay and complete exercise/assignment accounting remain open. See [PHASE3_OPTIONS_PORTFOLIO.md](PHASE3_OPTIONS_PORTFOLIO.md).

[Corporate-action bookkeeping](PHASE3_CORPORATE_ACTIONS.md) now accrues cash dividends as nonspendable receivables and credits cash only against explicit reconciled payment evidence, including after the shares were sold. Payments do not add profit twice. Integer splits preserve ownership/pending exits; fractional cash-in-lieu and basis corrections remain unsupported. Entitlement precedes trading at the same boundary. The daily durable wrapper records these transitions and binds their source; missing historical QQQ/SPY/payment-date evidence remains unresolved.

Combined focused regression: **286 passed**. Independent reviewers checked all three components and confirmed the repairs for options session guards/symbol identity and immutable benchmark plans/pending-sale bounds. The durable dividend test verifies receivable/payment restart and replay without duplicate cash. These are synthetic software checks, not backtests, broker reconciliation, paper activity or prospective credit.

Remaining acceptance includes observed-data valuation construction, complete corporate-action and option lifecycle mappings, integrated stress/delay scenarios, benchmark/candidate accounting and reference parity, precise prospective registration/inference, source/fee/overhead verification and genuinely untouched observations. Generic transactional storage exists, but benchmark/options records still need a reviewed concrete durable integration. PC boot/cutover/recovery and off-host protected backups remain deferred until transfer. The goal stays active; no phase or live-trading gate is declared complete.
