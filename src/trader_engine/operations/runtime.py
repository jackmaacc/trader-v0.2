"""Read-only deployment checks and inactive service-file rendering.

This module never starts services, reads credential contents, contacts a broker,
or grants execution permission. Rendered paper service is observation-only and
has no automatic install target. Locks remain local, never a cross-host fence.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys

SERVICES = {
    "market-scanner": ("market_scanner_service.py", "continuous_market_scan"),
    "sip-stream": ("plus_stream_service.py", "plus_stream"),
    "opra-stream": ("plus_stream_service.py", "plus_options_stream"),
    "plus-research": ("plus_research_service.py", "plus_research"),
    "paper-crypto": ("paper_crypto_service.py", "paper_crypto_service"),
    "paper-trial": ("paper_trial_service.py", "paper_trial"),
}
MIN_VERSIONS = {"numpy": (2, 0), "pandas": (2, 2), "pyarrow": (16, 0), "pydantic": (2, 7), "PyYAML": (6, 0), "streamlit": (1, 44), "plotly": (6, 0), "scikit-learn": (1, 5), "yfinance": (0, 2, 54), "websockets": (15,), "msgpack": (1, 0)}
PACKAGES = ("numpy", "pandas", "pyarrow", "pydantic", "PyYAML", "streamlit", "plotly", "scikit-learn", "yfinance", "websockets", "msgpack")
ENV_KEYS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def _path(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\r\n\x00%$'):
        raise ValueError("paths must be absolute and contain no control, percent or dollar characters")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("absolute path required")
    # Target paths may belong to another OS; never resolve host symlinks here.
    return Path(os.path.abspath(path))


def normalize_manifest(config):
    if not isinstance(config, dict):
        raise ValueError("manifest must be an object")
    result = {key: str(_path(config[key])) for key in ("repo_root", "python", "state_root")}
    result["secrets_file"] = str(_path(config["secrets_file"])) if config.get("secrets_file") else None
    if config.get("stock_feed", "sip") not in ("sip", "iex"):
        raise ValueError("stock_feed must be explicit sip or iex")
    result["stock_feed"] = config.get("stock_feed", "sip")
    account = config.get("expected_account_id")
    if account is not None and (not isinstance(account, str) or not account or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in account)):
        raise ValueError("invalid expected account identifier")
    result["expected_account_id"] = account
    result["clock_reference_utc"] = config.get("clock_reference_utc")
    return result


def service_commands(config):
    """Use only actual worker CLI flags. No command authorizes paper orders."""
    config = normalize_manifest(config)
    repo, state = Path(config["repo_root"]), Path(config["state_root"])
    commands = {}
    for name, (script, dirname) in SERVICES.items():
        command = [config["python"], str(repo/"scripts"/script), "--directory", str(state/dirname)]
        if name == "market-scanner": command += ["--feed", config["stock_feed"], "--poll-seconds", "60" if config["stock_feed"] == "sip" else "300"]
        elif name in ("sip-stream", "opra-stream"):
            command += ["--feed", "sip" if name == "sip-stream" else "opra"]
            if name == "opra-stream": command += ["--options-file", str(state/"plus_research"/"options.json"), "--max-option-quotes", "100"]
        elif name == "plus-research": command += ["--artifacts", str(state)]
        elif name == "paper-trial": command += ["--artifacts", str(state)]
        elif name == "paper-crypto": command += ["--expected-account-id", config.get("expected_account_id") or "UNCONFIGURED", "--observe-only", "--poll-seconds", "60"]
        commands[name] = command
    return commands


def _probe_python(executable):
    code = '''import sys,json,platform,importlib.util,importlib.metadata
names=json.loads(sys.argv[1]);deps={}
for name in names:
 try: deps[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError: deps[name]=None
print(json.dumps(dict(version=list(sys.version_info[:3]),platform=sys.platform,release=platform.release(),fcntl=importlib.util.find_spec("fcntl") is not None,dependencies=deps)))'''
    process = subprocess.run([str(executable), "-I", "-c", code, json.dumps(PACKAGES)], capture_output=True, text=True, timeout=20, check=False)
    if process.returncode:
        raise ValueError("interpreter probe failed")
    return json.loads(process.stdout)


def preflight(config, *, environ=None, probe=None, now=None, disk_usage=None):
    """Inspect only; environment VALUES and credential-file CONTENTS are never read.

    Env key existence cannot verify a nonempty/valid credential. Clock comparison
    accepts an operator reference but does not independently verify NTP accuracy.
    """
    checks = []
    def add(name, status, detail): checks.append(dict(name=name, status=status, detail=detail))
    try: config = normalize_manifest(config)
    except (KeyError, TypeError, ValueError):
        return dict(ready_for_render=False, execution_authorized=False, checks=[dict(name="manifest", status="fail", detail="invalid required fields or unsafe path")])
    repo, state, executable = (Path(config[k]) for k in ("repo_root", "state_root", "python"))
    add("repository", "pass" if (repo/"pyproject.toml").is_file() and (repo/"src/trader_engine").is_dir() else "fail", str(repo))
    scripts = sorted({script for script, _ in SERVICES.values()})
    missing = [script for script in scripts if not (repo/"scripts"/script).is_file()]
    add("worker_paths", "fail" if missing else "pass", {"missing": missing})
    runtime = {}
    if not executable.is_file() or not os.access(executable, os.X_OK):
        add("interpreter", "fail", "configured Python is missing or not executable")
    else:
        try:
            runtime = (probe or _probe_python)(executable)
            supported = runtime.get("platform") in ("darwin", "linux") and runtime.get("fcntl") is True
            add("platform", "pass" if supported else "fail", "macOS/Linux fcntl required; use WSL2 Linux rather than native Windows")
            add("python_version", "pass" if tuple(runtime.get("version", [])) >= (3, 11) else "fail", runtime.get("version"))
            dependencies = runtime.get("dependencies", {})
            def supported_version(name):
                try:
                    version = tuple(int(part) for part in str(dependencies.get(name, "")).split("."))
                    upper = {"websockets": (17,), "msgpack": (2,)}.get(name)
                    return version >= MIN_VERSIONS[name] and (upper is None or version < upper)
                except ValueError:
                    return False
            add("dependencies", "pass" if all(supported_version(name) for name in PACKAGES) else "fail", dependencies)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            add("interpreter", "fail", "read-only interpreter probe failed")
    release = str(runtime.get("release", "")).lower()
    if runtime.get("platform") == "linux" and ("microsoft" in release or "wsl" in release):
        windows_mount = any(str(p).startswith('/mnt/') for p in (repo, state, executable))
        add("wsl_filesystem", "fail" if windows_mount else "pass", "keep repository, environment and state in WSL Linux filesystem, not /mnt drive mounts")
        add("wsl_boot", "warning", "Windows boot launch, WSL uptime and systemd service persistence need host acceptance testing")
    if state.exists() and not state.is_dir():
        add("state_directory", "fail", "configured state path is not a directory")
    ancestor = state
    while not ancestor.exists() and ancestor != ancestor.parent: ancestor = ancestor.parent
    try:
        free = (disk_usage or shutil.disk_usage)(ancestor).free
        add("disk_headroom", "pass" if free >= 5*1024**3 else "fail", {"free_bytes": free, "required_bytes": 5*1024**3})
        add("state_parent_access", "pass" if os.access(ancestor, os.W_OK|os.X_OK) else "fail", str(ancestor))
    except OSError:
        add("disk_headroom", "fail", "cannot inspect state filesystem")
    env = os.environ if environ is None else environ
    presence = {key: key in env for key in ENV_KEYS}
    add("credential_environment_presence", "pass" if all(presence.values()) else "warning", presence)
    secrets_file = config.get("secrets_file")
    if secrets_file:
        path = Path(secrets_file)
        try:
            info = path.stat()
            private = stat.S_ISREG(info.st_mode) and info.st_mode & 0o077 == 0
            add("credential_file_permissions", "pass" if private else "fail", "file presence and owner-only permissions checked; contents and credential validity not checked")
        except OSError:
            add("credential_file_permissions", "fail", "configured credential file missing or inaccessible")
    elif not all(presence.values()):
        add("credentials", "warning", "credentials not verified; Mac Keychain is deliberately not queried; Linux requires APCA environment provisioning")
    current = now or datetime.now(timezone.utc)
    reference = config.get("clock_reference_utc")
    if reference:
        try:
            reference = datetime.fromisoformat(str(reference).replace('Z', '+00:00'))
            if reference.tzinfo is None: raise ValueError()
            skew = abs((current-reference).total_seconds())
            add("clock", "pass" if skew <= 5 else "fail", {"reference": "operator supplied; not network verified", "difference_seconds": skew})
        except (TypeError, ValueError): add("clock", "fail", "invalid timezone-aware clock reference")
    else:
        add("clock", "warning", "absolute clock synchronization unverified; verify host time sync before activation")
    add("single_writer", "warning", "local key-hash locks do not fence another host; deliberate shutdown and broker reconciliation required before cutover")
    add("paper_execution", "disabled", "templates are observation-only; missing account id remains UNCONFIGURED; execution requires separate approved cutover")
    return dict(ready_for_render=not any(c["status"] == "fail" for c in checks), execution_authorized=False, manifest=config, checks=checks)


def render_service_files(config, target):
    """Return {filename: text} only; does not write, install, enable or start."""
    config = normalize_manifest(config)
    if target not in ("systemd", "launchd"): raise ValueError("target must be systemd or launchd")
    commands = service_commands(config)
    files = {}
    for name, args in commands.items():
        paper = name in ("paper-crypto", "paper-trial")
        if target == "systemd":
            quote = lambda text: '"'+str(text).replace('\\', '\\\\').replace('"', '\\"')+'"'
            lines = ["# Rendered only; installation and activation require operator review.", "[Unit]", "Description=trader-v0.2 "+name, "After=network-online.target", "Wants=network-online.target"]
            if paper: lines += ["# Deliberately disabled; no execution flag and no install target.", "ConditionPathExists="+quote(Path(config["state_root"])/("PAPER_TRIAL_OBSERVATION_ENABLED" if name == "paper-trial" else "PAPER_OBSERVATION_ENABLED"))]
            lines += ["", "[Service]", "Type=simple", "WorkingDirectory="+quote(config["repo_root"]), "UMask=0077", "Environment=PYTHONUNBUFFERED=1"]
            if config.get("secrets_file") and name != "paper-trial": lines += ["EnvironmentFile="+quote(config["secrets_file"])]
            lines += ["ExecStart="+' '.join(quote(arg) for arg in args), "Restart="+("no" if paper else "on-failure"), "RestartSec=10", "TimeoutStopSec=90", "KillMode=control-group", "NoNewPrivileges=true"]
            if not paper: lines += ["", "[Install]", "WantedBy=default.target"]
            files["trader-"+name+".service"] = '\n'.join(lines)+'\n'
        else:
            payload = {"Label": "com.trader-v02."+name, "ProgramArguments": args, "WorkingDirectory": config["repo_root"], "RunAtLoad": False, "KeepAlive": False, "Disabled": True, "Umask": 63, "ProcessType": "Background", "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"}}
            # No secrets in plist, no shell source; existing Mac Keychain lookup can
            # provision the worker after deliberate activation if already configured.
            files["com.trader-v02."+name+".plist"] = plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode()
    return files
