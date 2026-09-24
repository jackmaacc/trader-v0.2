# Private recovery evidence bundles

This offline tool captures explicitly selected **nonsecret JSON configuration, ownership state, seed records and pending-order journals**. It never reads credential stores, contacts a broker, stops a writer, installs services or adopts restored state. The paper-trial observer database uses the separate [online trial backup tool](TRIAL_BACKUPS.md), which validates that observer-specific schema. Other SQLite ledgers require a schema-appropriate backup process; this tool rejects SQLite and supports only JSON/JSONL files.

A bundle made while a writer runs is useful evidence, **not a certified recovery point**. Files are checked twice for content and filesystem-identity changes, but matching reads cannot prove an atomic transaction across files, absence of intervening changes, a stopped process, or broker reconciliation. Every manifest permanently records:

- `source_recheck_passed: true` only after the second source pass.
- `stopped_writer_verified: false` and `cross_file_atomicity_verified: false`.
- `recovery_authorized: false` and `execution_authorized: false`.

No CLI option changes those boundaries. A stopped-writer cutover still requires the separately reviewed fencing, broker reconciliation and latest-state handoff in [PORTABLE_DEPLOYMENT.md](PORTABLE_DEPLOYMENT.md).

## Explicit selection

Create a private selection JSON outside Git. Paths are relative to the supplied source root. Roles are descriptive, not authority. For the existing paper manager, an example selection is:

```json
[
  {"path": "artifacts/paper_crypto_service/state.json", "role": "ownership"},
  {"path": "artifacts/paper_crypto_service/journal.jsonl", "role": "pending_journal"},
  {"path": "artifacts/paper_crypto_service/seed.json", "role": "seed"},
  {"path": "configs/project_mandate.json", "role": "configuration"}
]
```

The project mandate is planning configuration; it is not the executor's complete runtime policy. An explicit selection is not automatic proof that every recovery dependency was included. Review the actual running configuration and source commit before a future cutover. Mutable state must not go into Git.

The tool preserves exact bytes, including account/order identifiers that may occur in private journals. Known credential-like paths, keys, PEM markers and bearer strings are rejected, but this is a heuristic screen, not a secret detector or a substitute for selecting known nonsecret files. Do not include environment files, Keychain exports or credentials under disguised names. Reports remain private. File payloads are never printed by the CLI.

## Create and verify

Use canonical absolute paths and new destinations whose parent already exists. Avoid symlink aliases such as `/tmp`; use `/private/tmp` on macOS. Directory permissions are `0700`, file permissions `0600`.

```sh
.venv/bin/python scripts/recovery_bundle.py create \
  --source-root /absolute/path/trader-v0.2 \
  --selection /absolute/private/recovery-selection.json \
  --destination /absolute/private/bundle-20260924T120000Z

.venv/bin/python scripts/recovery_bundle.py verify \
  --bundle /absolute/private/bundle-20260924T120000Z
```

The private `files/` directory contains numbered payload files. `manifest.json` maps those files to original relative paths and roles, with byte counts and SHA-256 hashes. The manifest is published last after source rechecks and output flushes. A partial directory without a valid manifest is incomplete; verification refuses it. New attempts require a different destination; this tool never overwrites an existing report or cleans up a failed attempt automatically.

Paths reject traversal, symlinks and source hardlinks. Reports may not be nested beneath selected source directories, recognized live state directories or service-manager directories. Files are capped at 32 MiB each, 128 MiB total, and selections at 128 files. Minimum disk headroom is 64 MiB or twice selected byte size, whichever is larger. Exceeding a size limit needs a reviewed extension, not silently truncated evidence.

Checksums detect corruption; they do not authenticate a bundle against an attacker able to replace both payloads and the manifest. Create-once behavior is not filesystem-enforced immutability against the owner. The protections assume trusted operator-owned directories, not concurrent hostile filesystem manipulation. Encryption, off-machine transport, scheduled retention and recovery after total host loss are not implemented.

## Isolated restore exercise

```sh
.venv/bin/python scripts/recovery_bundle.py restore \
  --bundle /absolute/private/bundle-20260924T120000Z \
  --destination /absolute/private/restore-exercise-20260924T120000Z
```

Restored bytes go under **`isolated-files/0000.json`**, not back to `state.json` or any live service path. The exercise rechecks both restored hashes and the source bundle, then publishes `source-manifest.json` and `restore-verification.json`. It cannot overwrite existing files or launch a process. A failed exercise has no verification marker. Successful verification demonstrates readable byte-equivalent isolated copies, not permission to start an executor with those bytes.

Never point a service at an exercise or rename it into runtime state as an implicit recovery. Ownership and pending submissions require current broker reconciliation. Do not roll mutable journals back with a code rollback. A functioning local backup does not establish PC boot/recovery, cross-host fencing or investment qualification.
