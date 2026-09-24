# Portable deployment templates

These files are examples, not installed services. Review `docs/PORTABLE_DEPLOYMENT.md` first. Copy the manifest outside Git, replace all example paths and run the read-only preflight. The runtime renderer generates six systemd or LaunchAgent files from the actual worker CLI contracts.

- `systemd/`: Ubuntu/WSL2 user-service examples; read-only units may be enabled only after host checks. The paper unit has no install target, requires an absent-by-default observation marker, and contains `--observe-only` rather than execution permission.
- `launchd/`: all examples are disabled, have no automatic run or keep-alive behavior, and contain no secrets.
- All credentials, ownership journals and mutable runtime artifacts belong outside versioned files. Do not copy credential values into a manifest or service file.

No command here installs, enables, starts or restarts anything automatically. Do not use these alongside an existing writer for the same paper account.
