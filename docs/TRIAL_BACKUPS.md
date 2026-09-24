# Operational trial backups and restore exercises

This tool protects the **operational trial observation ledger only**: `paper_trial/observations.sqlite3`. It does not back up the paper broker account, ownership or pending-order journals, API credentials, runtime configuration, the entire repository, or any account recovery material. It does not install a schedule, change a service, submit orders or create encrypted off-machine storage. Those remain separate work.

The collector already sanitizes observation payloads before persistence. The backup preserves that ledger, including its stored payloads; it does not retroactively redact arbitrary content added by another writer. Keep backups private even though the manifest contains only structural evidence and timestamp ranges.

## Create a consistent snapshot

Use canonical directory paths, avoiding symlinks such as macOS `/tmp` (use `/private/tmp` instead). The destination's parent must already exist; the destination itself must be new and outside the source directory.

```sh
.venv/bin/python scripts/paper_trial_backup.py create \
  --source /absolute/runtime/paper_trial \
  --destination /absolute/private-backups/trial-20260924T120000Z
```

The source is opened with SQLite `mode=ro`, then SQLite's online backup API produces a consistent database while the observer may continue writing. It includes committed WAL data; copying only the live main database file would not reliably do that. No source checkpoint or SQL mutation is issued. SQLite may maintain its normal shared-memory reader coordination while the source connection is open.

The tool validates the exact four-table schema (`meta`, `observations`, `polls`, `gaps`), current SQLite user schema version 0, column definitions, absence of additional tables/views/triggers, and `PRAGMA integrity_check`. A new private destination is reserved exclusively. Only after the snapshot passes verification are these files published:

- `snapshot.sqlite3`: standalone database, no WAL dependency, permissions `0600`.
- `manifest.json`: format version, snapshot SHA-256/byte count, SQLite library version, schema evidence, table counts and observation/poll/gap timestamp ranges. It contains no row payloads or credential values.

The destination directory uses `0700`. The manifest is written last and is the completed-backup marker. A directory without a valid manifest is incomplete; do not treat it as a usable backup. Backup attempts have a bounded online-copy duration (30 seconds by default, maximum 300 through the Python API). Disk headroom must exceed the larger of 64 MiB or twice the current database-plus-WAL size plus 16 MiB. Growth during copying or device errors can still fail the attempt; no disk check guarantees success.

## Verify first; restore only into an isolated exercise directory

Verification is the default operation and creates no restored files:

```sh
.venv/bin/python scripts/paper_trial_backup.py restore \
  --source /absolute/private-backups/trial-20260924T120000Z
```

A deliberate restore exercise requires both `--apply` and a new destination:

```sh
.venv/bin/python scripts/paper_trial_backup.py restore \
  --source /absolute/private-backups/trial-20260924T120000Z \
  --destination /absolute/private-restore-exercises/trial-20260924T120000Z \
  --apply
```

The tool validates SHA-256, size, supported schema, SQLite integrity, row counts and timestamp ranges against the manifest before copying. The isolated output is intentionally named `restored.sqlite3`, **not** the worker's `observations.sqlite3`, and includes `restore-verification.json`. No existing destination is replaced, no live state is adopted, and no worker is started. A new destination nested beneath an existing `observations.sqlite3` or `state.json` runtime directory is rejected. Never rename or move the exercise into live state without a separately reviewed recovery procedure.

The verifier rejects snapshot sidecars, unsupported manifests, unexpected schemas, tampering detected by the hash, parent traversal and symlink paths. These controls prevent routine path mistakes; they do not defend against an attacker who can concurrently rewrite the operator-owned directories. Checksums detect accidental corruption and inconsistent evidence, not an attacker who can replace both snapshot and manifest. Cryptographic authenticity and off-machine encryption are not implemented.

## Acceptance evidence and limits

Offline tests cover committed WAL rows, exclusion of uncommitted writes, unchanged source data, private output permissions, default dry-run behavior, isolated restore equivalence, corruption/manifest mismatch, wrong schema/version, existing-destination refusal, symlink/traversal guards, live-directory isolation and disk-headroom failure.

A successful local exercise establishes that this snapshot can be read and independently verified. It does not prove future recovery from a lost computer, preserve 30-day operational continuity after a gap, qualify a trading strategy, restore positions, or establish profitability. Keep the live collector running only under its existing authorization; this tool neither changes nor renews that authorization.
