# Phase 1 acceptance: portable operations

Status September 24, 2026: **in progress, not accepted**. The user reports a PC with WSL2 and remote access. Its private connection target, login and repository path are still needed before host inspection. No PC connection or service cutover has occurred.

## Completed engineering evidence

- Read-only host probe and exclusive private JSON reports implemented. Interpreter, disk, Linux mount types, bounded supervisor/clock queries and Linux boot identity are inspected without credentials or broker access.
- Focused regression: `tests/test_host_probe.py`, `tests/test_runtime_preflight.py`, `tests/test_release_receipt.py`, `tests/test_trial_backup.py`: **46 passed**. Independent review found no blocking issues in the host collector and required explicit supervisor scope labeling.
- Actual Mac report: `artifacts/phase1_host_20260924T052935Z/mac.json`. Python 3.12.2, POSIX locking import and GUI launchd availability passed; 6,264,569,856 bytes free exceeded the 5 GiB guard. Linux storage, WSL2, boot identity and clock synchronization remained unverified. This is a local snapshot, not worker-health or PC evidence.
- Existing local observer-ledger backup and isolated restore evidence remains in `artifacts/operations_recovery`; it does not cover broker ownership/pending-order recovery.

## Required remaining evidence

| Gate | Required observation | Current status |
|---|---|---|
| Private access | Verified SSH host identity, intended unprivileged user, private path and loopback dashboard tunnel | User reports configured; unverified |
| Target build | Clean reviewed commit, rebuilt Linux environment, dependency record and target-specific release receipt | Pending |
| Host baseline | WSL2, Linux storage, disk, synchronized time, actual user manager and planned unit contents | Pending |
| Windows startup | Windows LastBootUpTime before/after controlled reboot plus post-reboot remote and worker observations without manual login | Pending |
| Recovery | Dedicated nontrading acceptance worker survives intended WSL termination/restart, network loss/reconnect and host sleep policy | Pending |
| Alerts | Deliberately induced nontrading heartbeat failure produces a delivered alert and subsequent recovery notice | Pending |
| Backup/restore | Off-host protected backup, isolated restore and verified hashes; complete ownership/pending-order/config recovery scope | Pending; observer ledger only tested locally |
| Single writer | Disabled source restart paths, old process absent, broker/state reconciliation, explicit designated destination and rollback evidence | Pending; Mac remains sole writer |

A systemd system-manager probe does not validate the user manager used by the templates. A Linux boot UUID does not prove a Windows reboot. Authentication does not prove fresh data. An observed heartbeat does not establish recovery after interruption.

Use a separate no-broker acceptance worker for failure drills. Do not interrupt the current Mac trading owner to test infrastructure. Controlled Windows reboot, WSL shutdown and network interruption must be scheduled for the actual host after identifying other workloads. Review the concrete handoff before stopping risk management or transferring writer authority.

Phase 1 closes only when its required host, recovery, backup, fencing and alert evidence is recorded and independently reviewed. Live trading and investment qualification remain separate gates.
