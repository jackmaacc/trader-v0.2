# Mac development, Windows-hosted runtime: phase 1

This is a deployment foundation, not a completed PC installation or an unattended-uptime guarantee. The existing Mac workers are unchanged by this work. No services, credentials, remote listeners or network settings are installed by the preflight or renderer. The Windows PC has not been tested.

## Runtime contract

Keep developing on the Mac; deploy a reviewed, explicit Git commit to the runtime machine. Use Ubuntu under WSL2 on the Windows PC, with the repository, virtual environment and state inside Linux storage such as `/home/trader`. Native Windows Python is unsupported because the current workers require `fcntl`. Microsoft recommends Linux filesystem storage for Linux workloads and documents systemd support in WSL. [Filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems), [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd).

`deploy/runtime.example.json` declares the repository, interpreter, state root, optional environment-file path and expected paper-account identifier. It never contains API keys. All paths are absolute; `%`, `$` and control characters are rejected to avoid service-file expansion. Spaces are supported. The Python path deliberately preserves a virtual-environment symlink instead of resolving it to its base interpreter.

The current worker CLI mapping is:

| Worker | State location beneath state root | Additional arguments |
|---|---|---|
| Market scanner | `continuous_market_scan` | `--feed sip --poll-seconds 60` (IEX uses 300 seconds) |
| SIP stream | `plus_stream` | `--feed sip` |
| OPRA stream | `plus_options_stream` | `--feed opra --options-file STATE/plus_research/options.json` |
| Plus research | `plus_research` | `--artifacts STATE` for scanner/stream inputs |
| Paper trial | `paper_trial` | `--artifacts STATE`; local artifacts only, no credentials |
| Paper crypto | `paper_crypto_service` | expected account ID, `--observe-only` |

State directory names are deployment choices, not permission to create a second ownership history. During cutover, point at or migrate the **actual** current paper state directory in its entirety and reconcile it; do not substitute an empty example directory for existing account ownership. Additional strategy services are not covered by these templates.

## Read-only inspection and rendering

Copy and edit the manifest outside Git. Create/rebuild the environment on each host; do not copy a Mac `.venv` onto Linux. From the repository, run:

```sh
.venv/bin/python scripts/runtime_preflight.py --manifest /absolute/path/runtime.json
.venv/bin/python scripts/runtime_preflight.py --manifest /absolute/path/runtime.json --render systemd --output /absolute/path/new-template-directory
```

`--render launchd` produces Mac examples. The destination must be new. This only writes text files; it never calls systemctl, launchctl, a broker, package installation or Git pull. Review every output before installation. Do not render directly into a service manager's installation directories.

Preflight checks repository/worker paths, executable Python >=3.11, supported OS and `fcntl`, declared dependency version bounds, at least 5 GiB free space, state-parent permissions, secret-file existence/private permissions, and **environment variable names only**. It does not read credential file contents or environment values, query Keychain, authenticate, or establish entitlement. Credentials present as empty variables remain unverified. The optional `clock_reference_utc` compares a supplied reference within five seconds; without one, clock sync is explicitly unverified. A supplied time is not independent NTP evidence.

The environment interface is `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. On Linux provision them privately into the service environment; the example systemd units consume the configured owner-only `EnvironmentFile`. Do not store that file in the repository. Launchd examples do **not** read environment files; use existing approved Mac Keychain provisioning or a separately reviewed secure service environment. Missing Linux credentials must fail clearly instead of attempting Mac Keychain access.

The checked dependency ranges follow current `pyproject.toml`; this is not a cross-platform lockfile or reproducible build. Freeze and review exact resolved versions before a production cutover. Clock/network/Alpaca access and filesystem durability need host acceptance tests. A successful preflight means local configuration is renderable, never that trading is safe or active.

## Defaults that prevent an accidental second writer

Every LaunchAgent example is `Disabled=true`, `RunAtLoad=false`, `KeepAlive=false`. Read-only systemd examples include an install target but are not installed/enabled by this project. The paper crypto and paper trial units have **no install target**, no restart loop, an absent-by-default `PAPER_OBSERVATION_ENABLED` condition, and, for paper crypto, only `--observe-only`. The trial unit requires its separate `PAPER_TRIAL_OBSERVATION_ENABLED` marker and receives no credentials. Creating that marker permits observation only; it does not permit orders. Paper execution requires a separately approved, reviewed cutover and deliberate command change. No renderer flag enables it.

A key-hash lock in a host's temporary directory protects only that host. Identical keys on two machines do not share a lock. A local `SHUTDOWN` or entry-stop marker also does not fence the other machine. Do not rotate or copy keys as an implicit handoff mechanism, and do not assume another process is off because the dashboard is unreachable.

## Single-writer cutover checklist

1. Record the current deployed commit, dependency versions, service configuration, account ID and active state-directory paths. Prepare the PC at that same reviewed version with all trading services disabled.
2. Verify Windows/WSL startup, systemd configuration, time synchronization, disk space, connectivity, private credentials and read-only data freshness on the PC. Test a Windows restart, WSL termination, network interruption and sleep/wake behavior. A Windows host that sleeps is not a 24/7 runtime; WSL systemd alone does not prove boot or uptime behavior.
3. Stop new entries on the Mac through the existing supported control, reconcile outstanding orders, and choose a controlled handoff window. Stopping a worker does not liquidate positions.
4. Stop the Mac execution service and disable all restart paths: LaunchAgent, watchdog/heartbeat and manual runner. Verify the old process is gone and broker open orders/positions are reconciled. Do not stop risk management without a planned handoff for open holdings.
5. Transfer the complete durable ownership/pending-intent journals and configuration over a private authenticated channel, including unresolved submissions. Do not Git-commit mutable state. Keep an encrypted backup of the stopped source. Do not manufacture a new seed from displayed quantities.
6. On the PC, start observation only and compare broker account, available quantities, pending orders and restored journals. An ambiguous or inconsistent submission blocks activation until resolved. Keep the Mac execution paths disabled.
7. After explicit execution approval, activate exactly one PC execution owner with the same intended policy. Confirm its fresh heartbeat and broker reconciliation, then perform a bounded monitored acceptance period. Preserve an accessible operator shutdown path.
8. For rollback, stop and reconcile the PC first, then transfer its latest durable state back before reactivating the Mac. A code rollback alone must not roll back the ownership journal or silently change state schema. Never overlap the two writers.

## Private remote viewing and control

Keep the dashboard bound to `127.0.0.1`; do not publish a broker dashboard or an unauthenticated control API to the Internet. Use an SSH key and an already configured private VPN path to the host. From a Mac with authorized SSH access, a local tunnel can expose only a local browser port:

```sh
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 trader@PRIVATE_HOST
```

For a relocated state root, start the dashboard from the repository with its research path beneath that root, so all operational panels resolve the same sibling service directories:

```sh
TRADER_ARTIFACTS=/home/trader/trader-state/latest .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

The `latest` subdirectory is the research-output path; its parent is the shared state root. The dashboard is a separate read-only process and this command does not start any account executor. It requires its own supervised startup before relying on remote availability.

SSH's `-L` supports an explicit loopback bind address. [OpenSSH manual](https://man.openbsd.org/ssh). Validate host keys rather than disabling host verification. Restrict the VPN/SSH service to authorized users; do not expose public port forwards. Mobile access requires a compatible private VPN plus authenticated SSH/tunnel client, or a separately designed authenticated private web proxy. None is installed here. Viewing the dashboard is not proof of execution health and does not itself authorize changes.

## Deliberate deployment and rollback

Deploy an explicit reviewed commit; never run an automatic `git pull` inside an execution service. Keep a version/dependency manifest with each deployment. Update and test a separate checkout/environment while the running version remains stable. Preserve state schema compatibility, stop/reconcile before changing the execution owner, and restart only after approval. Logs, environment files, deployment manifests with local identifiers, downloaded datasets and account state stay outside Git. Existing ignore rules must be checked before staging; ignore rules do not protect already tracked files.

Phase 1 does not provide cross-host fencing, high availability, automatic failover, remote command authorization, native Windows support, guaranteed PC wake/boot operation or completed PC verification. Those require explicit implementation and host testing before claiming unattended reliability.

## Release and recovery tools

[RELEASE_WORKFLOW.md](RELEASE_WORKFLOW.md) describes clean-commit receipts and host-specific revalidation. [TRIAL_BACKUPS.md](TRIAL_BACKUPS.md) provides online observation-ledger snapshots and isolated restore exercises. Both are local tools; neither deploys services or activates orders. The September 24 Mac exercise verified a real ledger snapshot and isolated copy while the original observer retained its history. Scheduled/off-machine backups and full execution-state recovery remain pending.
