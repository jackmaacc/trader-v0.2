# Reviewed release identity

`scripts/release_readiness.py` records a clean Git commit, dependency-file hashes, installed dependency versions, normalized host-manifest digest and planned worker arguments. It runs local preflight only. It does not install, restart, activate, contact a broker or read credentials.

Commit and review source changes first. With an existing private host manifest and an existing output parent directory:

```sh
.venv/bin/python scripts/release_readiness.py --manifest /absolute/path/runtime.json --output /absolute/path/new-receipt.json
.venv/bin/python scripts/release_readiness.py --manifest /absolute/path/runtime.json --verify /absolute/path/new-receipt.json
```

The output must not exist. Dirty source, a changed identity during checks, or failed preflight prevents staging eligibility. A successful verification compares the current source/configuration/dependencies with the receipt. Unverified clock synchronization, credential validity and single-writer ownership remain explicit limitations. Staging eligibility never authorizes execution or live capital.

Receipts are host-specific: Mac and WSL paths and environments differ, so generate and review a new receipt on the PC at the approved commit. The command digest covers generated planned arguments, not the contents of installed LaunchAgents/systemd units. Installed versions are evidence, not a reproducible dependency lockfile. Hashes detect differences; they are not signatures. Checks are point-in-time observations, not deployment locks; stage an isolated reviewed checkout and reverify immediately before a controlled deployment.

Retain receipts outside Git alongside deployment records. Never auto-pull into a running trading process. Rollback means selecting a tested compatible code version while retaining the latest ledger and ownership/pending-order state. The single-writer handoff and actual PC boot/network/remote-access acceptance in [PORTABLE_DEPLOYMENT.md](PORTABLE_DEPLOYMENT.md) still apply.

Operational ledger recovery is separately documented in [TRIAL_BACKUPS.md](TRIAL_BACKUPS.md). That backup does not cover the broker ownership journal, secrets or complete host recovery.
