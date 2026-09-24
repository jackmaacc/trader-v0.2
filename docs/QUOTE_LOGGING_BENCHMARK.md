# Offline quote logging latency and per-trade record budget

Measured 2026-09-23 on the project volume. This is synthetic local filesystem
evidence, not a strategy backtest or a broker timing test. No trading runner,
network transport, live credentials, orders, or cancellations were used.

## Measured latency

The benchmark calls the actual `qualified_quote` helper with its default IEX
policy and an accepted synthetic XOM quote. It compares validation/evidence
construction alone, validation plus `record_quote_decision` JSONL append, and
validation plus `ReservedJournal.append` with the portfolio capacity of 2048.
Each durable path performs its real `os.fsync`. Journal construction/reservation
is outside the timer. All three paths run in seeded interleaved order, with 20
warmup calls and 300 measured calls each. Percentiles use nearest rank.

| Path | p50 ms | p95 ms | p99 ms | Maximum ms |
| --- | ---: | ---: | ---: | ---: |
| Validation, no evidence sink | 0.076875 | 0.137917 | 0.173209 | 0.280333 |
| Validation + JSONL append + fsync | 0.298084 | 0.454625 | 0.613084 | 1.711791 |
| Validation + reserved journal append + fsync | 6.819958 | 10.926000 | 14.348000 | 22.817416 |

The median additional helper time was **0.221209 ms for JSONL** and
**6.743083 ms for the reserved journal**, compared with validation alone.
These differences of sample percentiles are descriptive, not the percentiles of
a paired causal latency distribution.

The benchmark also times the real fsync call within each total:

| fsync system call only | p50 ms | p95 ms | p99 ms | Maximum ms |
| --- | ---: | ---: | ---: | ---: |
| JSONL | 0.027333 | 0.057833 | 0.069958 | 0.075542 |
| Reserved journal | 0.060833 | 0.088583 | 0.104834 | 0.130042 |

The journal path does substantially more work than a sync: every append calls
`_load()`, rereads all 2048 reserved 4096-byte slots (8 MiB of record slots),
validates existing framing/checksums, encodes the new record, writes it, and
syncs. The measured fsync call explains only a small fraction of its total
latency. Do not attribute the whole 6.74 ms median increase to fsync itself.

The portfolio worker uses the reserved journal for the submission guard. Its
two preceding accepted entry checks write JSONL records in the coordinator.
Sizing, plan/intent durability, order responses, and holding evidence add other
journal operations; this benchmark is per quote helper call, not total order
submission latency. The breakout runner uses JSONL for its submission quote.

## Reproduction and limits

```sh
.venv/bin/python scripts/benchmark_quote_logging.py \
  --samples 300 --warmup 20 \
  --output artifacts/quote_logging_benchmark_20260923_repeat
```

Use a new output directory. The script creates temporary evidence in the project
directory and removes it afterward. Results are in
`artifacts/quote_logging_benchmark_20260923/results.json`, including all 900 raw
samples, source SHA-256 hashes, the exact fixture, instrumentation details and
environment metadata. The benchmark checks source hashes again after timing
and rejects runs whose measured helper sources changed.

This run used Python 3.12.2, macOS 15.5 arm64, eight reported logical CPUs,
`mach_absolute_time()` timing, and filesystem device 16777231. Measured journal
occupancy was 20 through 319 records before each append; each quote record was
775 bytes as ordinary JSON. Higher occupancy increases the amount of populated
evidence decoded, although every append already reads every reserved slot.
Initialization, broker/network latency, rate-limiter waits, cold cache,
concurrent portfolio workers, near-full storage and prolonged load are excluded.
Timing the fsync adds two timer reads per durable sample. Local fsync completion
does not establish hardware power-loss durability. These observations do not
bound future latency, and no latency SLA is inferred from 300 samples.

## Entry guard and capacity audit

The reviewed source has no `_check_entry_guard` method. The relevant call chain
is `PaperExecutor.execute` -> `_send` -> `SymbolBroker.submit` ->
`AlpacaPaperClient.submit` -> `_request`. `_request` invokes `entry_guard` once
after limiter admission and only for buys. Limiter waiting does not invoke the
guard. Sells and GET requests do not invoke it.

`_send` records an attempted client ID before submission. An ambiguous POST
causes bounded lookup retries, never another POST. A later `_send` for the same
attempted ID also performs lookup only. `_read`, delayed visibility handling,
cancel reconciliation and the runner's three `resume_drain` attempts cannot
repeat the buy guard. `EntryNotSubmitted` clears the attempted marker but
`execute` completes that plan; the runner does not retry it. A later trade has
a new journal. For this worker, a guard can therefore produce **at most one
submission quote record per trade**, and zero if earlier checks short-circuit.

Offline tests use the real executor, `SymbolBroker`, client `submit` and
`_request`, with an in-memory transport replacing all network I/O. They count
one guard invocation and one quote record for successful submission, ambiguous
POST recovery, delayed visibility plus read failures, HTTP 429, local rejection
and guard exceptions, including all three configured drain retries when needed.

The normal 900-second holding fixture produces **615 total records**: 300
holding quote records, 307 order records, one submission quote record, and seven
other lifecycle/runner records. This is an observed fixture count, not a
universal worst-case bound.

The audit also found a separate capacity defect: when a tiny future quote
remained future-dated after revalidation, `hold_breakout` continued without its
normal three-second poll interval. A deliberately frozen wall clock with an
advancing monotonic clock exhausted a simulated 2048-record budget in under ten
seconds, despite only one entry guard record. The coordinator's fix restores
the existing holding poll interval before this retry. It changes neither quote
eligibility nor permission to act on a future quote. The regression requires
that same adverse clock fixture to finish its 900-second hold flat with fewer
than 1000 total records and without a storage failure.

With that cadence fix, a conservative record-count budget for the current
portfolio defaults is **1521 slots**, leaving **527 of 2048 slots**:

| Source | Conservative record allowance |
| --- | ---: |
| At most 300 holding iterations, two quote records each | 600 |
| Holding protective-order lookups, including final deadline check | 301 |
| Four drain calls, four lookups and four terminal reconciliations per call | 592 |
| Four mutation intents and four successful acknowledgment/ambiguity lookups | 8 |
| Initial partially filled entry terminal reconciliation | 12 |
| Sizing, plan, entry guard and hold-end records | 4 |
| Execute result plus three resume results | 4 |
| Total | 1521 |

Each `_terminal` reconciliation has at most 36 successful lookup records with
`polls=12`: at most two 12-poll phases, plus at most one additional 12-poll
partial-entry recursion. Each `_drain` checks the entry plus three reserved exit
IDs, so its loose bound is `4 + 4*36 = 148`; four drain calls give 592. Mutation
attempts/acknowledgments are counted separately. `_lookup` records at most one
successful result regardless of nested read/absence retries. Only four client
IDs can be submitted. This deliberately overcounts some mutually exclusive
paths, including holding after a fully consumed entry polling budget.

This bound is conditional on the current runner's 900-second hold, three exit
IDs, `polls=12`, at most three resume calls, no optional quote-recovery policy,
and functioning monotonic time/sleep. It is not a universal property of
`ReservedJournal` or a guarantee against disk errors. Repeated external
`resume_drain`, `recover`, or `verify_protection` calls can append indefinitely.
A single oversized evidence payload can also exceed the 4024-byte framed-record
payload limit independently of free slots; existing tests cover that failure.
Changing these defaults or adding telemetry records requires a new budget.
