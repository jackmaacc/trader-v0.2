# Persistent one-month paper-trial observation

`paper_trial_service.py` reads saved status from the five existing services and the paper crypto ownership state. It does not fetch credentials, contact Alpaca or submit/cancel orders. It writes an owner-only SQLite WAL database and a small dashboard status file under `artifacts/paper_trial/`.

## Meaning of the clock

The requirement is 30 consecutive days (2,592,000 seconds) of observed operational continuity. There is no credit before the first observation and no synthetic backfill. The observer checks once a minute. A collector gap over three minutes, crypto heartbeat over three minutes, other source heartbeat over ten minutes, regressed/future timestamps or a reported service error interrupts the streak. Prior incidents remain recorded when observation resumes.

Subscribed-idle streams outside market hours are normal operational states. Ordinary per-instrument stale/missing prices are recorded as coverage gaps separately. Explicit source errors, unknown ownership, missing state and stale pending reconciliation prevent a healthy operational assessment. This is sampled observability, not proof that no sub-minute outage ever occurred.

The current operational scope is the observer and existing crypto paper experiment. Equity and options services remain research-only; their paper execution qualification is false even if the general operational clock reaches 30 days. Investment qualification and live approval remain false at all durations. A separate prospective strategy record is still required.

## Evidence and storage

The database stores sanitized, allowlisted heartbeat observations, polling summaries and outage intervals. Repeated source heartbeats are deduplicated. Account cash/equity and owned quantities are observations; deposits, withdrawals, fees and fills still require the separate accounting workstream before calculating cash-flow-adjusted performance. Arbitrary payload text and credential fields are not retained.

Startup and each polling cycle require at least 1 GiB free space. The deployment preflight separately requires 5 GiB of operational headroom. A SQLite or input error stops the observer and reports failure without changing any trading worker. WAL checkpoints occur through SQLite; back up the database with SQLite's backup API or stop the observer cleanly before copying its complete database state. Do not copy a live database file alone while ignoring its WAL.

Use `--artifacts` for the common input state root and `--directory` for the ledger directory. `--once` takes one observation. A `SHUTDOWN` file in the ledger directory stops observation and leaves the execution worker untouched. One observer lock protects the ledger directory. A host migration must preserve the database and any observation gap; do not restart the clock using fabricated historical dates.


## Initial deployment evidence

The Mac observer began at **2026-09-24 04:17:45 UTC** with zero historical credit, zero recorded gaps, and five fresh operational sources. It reported `investment_qualified=false` and `live_approved=false`; equities/options remained research-only. The actual dashboard rendered without exceptions. The combined fast regression suite passed 176 tests; the separate 14,401-observation simulated 30-day boundary test also passed under independent review. These simulated checks are not credited as real paper-trading days.

Local runtime preflight found about 2.8 GB free, below the future deployment-readiness floor of 5 GiB. Current services retain their existing 1 GiB write guard. No cleanup was performed. PC boot, secrets, remote access and recovery remain unverified until tested on the actual machine.

## September 24 recovery milestone

The live observer timestamp race was fixed: acquisition completes before the observation clock is recorded. Genuine future timestamps and clock regressions remain invalid; acquisition over three minutes interrupts continuity. The earlier scanner `future_heartbeat` gap at 04:29:53–04:30:57 UTC remains in the ledger. It was not erased or credited back.

At 04:42:43 UTC, the online backup and isolated restore exercise verified 124 observations, 25 polls and the existing gap, with identical snapshot/restore SHA-256 and passing integrity/schema checks. These local artifacts are under `artifacts/operations_recovery/`; they are not off-machine disaster recovery or broker-state backups. See [TRIAL_BACKUPS.md](TRIAL_BACKUPS.md).

Only the observer was restarted to load the clock fix. At 04:42:54 UTC it resumed observing all five sources as operationally healthy, preserving its original start and interrupted streak. Freed space measured about 6.8 GiB and passed the 5 GiB local preflight. Credential validity, clock synchronization, cross-host ownership and actual PC operation remain unverified by that preflight.

The recovery milestone regression run passed 70 tests, including the full simulated 30-day continuity boundary, backup/restore, receipt identity, runtime preflight, portable credentials, dashboard operations and paper-crypto service checks. Independent review approved the observer, backup and receipt changes.
