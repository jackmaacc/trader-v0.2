"""Bounded local host evidence; never an execution or PC acceptance certificate."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import uuid

from .runtime import normalize_manifest

GIB = 1024 ** 3
PYTHON_PROBE = 'import sys,json,importlib.util; print(json.dumps({"version":list(sys.version_info[:3]),"platform":sys.platform,"fcntl":importlib.util.find_spec("fcntl") is not None}))'


def probe_host(config, *, runner=subprocess.run, system=None, release=None,
               read_text=None, disk_usage=shutil.disk_usage, now=None):
    """Collect local facts only. Inject probes to test without host dependencies.

    No manifest-supplied command string or shell is evaluated. Only the declared
    Python interpreter executes a fixed isolated probe; system commands are fixed.
    Environment values and credential files are never inspected.
    """
    config = normalize_manifest(config)
    system = system or platform.system()
    release = release or platform.release()
    read_text = read_text or (lambda path: Path(path).read_text())
    checks = []
    def add(name, status, detail):
        checks.append(dict(name=name, status=status, detail=detail))
    def command(args):
        try:
            result = runner(args, capture_output=True, text=True, timeout=5, check=False)
            return result.returncode, result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None, ''
    add('supported_os', 'pass' if system in ('Darwin', 'Linux') else 'fail', system)
    rc, output = command([config['python'], '-I', '-c', PYTHON_PROBE])
    try:
        runtime = json.loads(output) if rc == 0 else {}
        version = runtime.get('version', [])
        valid = (isinstance(version, list) and all(type(n) is int for n in version)
                 and tuple(version) >= (3, 11) and runtime.get('fcntl') is True
                 and runtime.get('platform') == {'Darwin': 'darwin', 'Linux': 'linux'}.get(system))
        add('configured_interpreter', 'pass' if valid else 'fail',
            {'version': version if isinstance(version, list) and all(type(n) is int for n in version) else None, 'fcntl': runtime.get('fcntl') is True})
    except (ValueError, AttributeError, TypeError):
        add('configured_interpreter', 'fail', 'fixed interpreter probe unavailable or invalid')
    wsl = system == 'Linux' and any(token in release.lower() for token in ('microsoft', 'wsl'))
    wsl2 = wsl and 'wsl2' in release.lower()
    add('wsl2_kernel', 'pass' if wsl2 else 'unverified',
        'WSL2 kernel observed; Windows boot behavior is unverified' if wsl2 else 'WSL2 kernel not established on this host')
    mounts = []
    if system == 'Linux':
        try:
            for line in read_text('/proc/self/mountinfo').splitlines():
                left, right = line.split(' - ', 1)
                mount = left.split()[4].replace('\\040', ' ').replace('\\134', '\\')
                mounts.append((Path(mount), right.split()[0]))
        except (OSError, ValueError, IndexError):
            mounts = []
    filesystems = {}
    for key in ('repo_root', 'python', 'state_root'):
        path = Path(config[key]).resolve()
        candidates = [(mount, fs) for mount, fs in mounts if path == mount or mount in path.parents]
        fs = max(candidates, key=lambda item: len(str(item[0])))[1] if candidates else None
        filesystems[key] = fs
        bad = str(path).startswith('/mnt/') or fs in ('9p', 'drvfs', 'ntfs', 'ntfs3', 'fuseblk', 'cifs', 'nfs', 'nfs4')
        status = 'fail' if system == 'Linux' and bad else ('pass' if system == 'Linux' and fs in ('ext4', 'ext3', 'xfs', 'btrfs') else 'unverified')
        add(key + '_linux_storage', status, {'filesystem': fs, 'windows_or_network_storage': bad if system == 'Linux' else False})
    ancestor = Path(config['state_root'])
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    try:
        free = disk_usage(ancestor).free
        add('disk_headroom', 'pass' if free >= 5 * GIB else 'fail', {'free_bytes': free, 'required_bytes': 5 * GIB})
    except OSError:
        add('disk_headroom', 'fail', 'state filesystem unavailable')
    boot_id = None
    if system == 'Linux':
        rc, result = command(['systemctl', 'is-system-running'])
        state = result if result in ('running', 'degraded', 'starting', 'initializing', 'maintenance', 'stopping', 'offline', 'unknown') else 'unknown'
        add('supervisor', 'pass' if rc == 0 and state == 'running' else ('fail' if state in ('degraded', 'maintenance', 'stopping', 'offline') else 'unverified'), {'kind': 'systemd', 'scope': 'system', 'state': state, 'worker_health_verified': False})
        rc, result = command(['timedatectl', 'show', '--property=NTPSynchronized', '--value'])
        add('time_sync', 'pass' if rc == 0 and result == 'yes' else ('fail' if rc == 0 and result == 'no' else 'unverified'), 'OS reports synchronization; independent clock offset not measured' if rc == 0 and result == 'yes' else 'clock synchronization not established')
        try:
            boot_id = str(uuid.UUID(read_text('/proc/sys/kernel/random/boot_id').strip()))
        except (OSError, ValueError):
            pass
    elif system == 'Darwin':
        rc, _ = command(['launchctl', 'print', 'gui/' + str(os.getuid())])
        add('supervisor', 'pass' if rc == 0 else 'unverified', {'kind': 'launchd', 'scope': 'gui', 'worker_health_verified': False})
        add('time_sync', 'unverified', 'Mac clock synchronization was not queried')
        rc, power = command(['pmset', '-g', 'batt'])
        source = ('battery' if "Now drawing from 'Battery Power'" in power else
                  'ac' if "Now drawing from 'AC Power'" in power else 'unknown') if rc == 0 else 'unknown'
        match = re.search(r'\b(\d{1,3})%;', power) if rc == 0 else None
        percent = int(match.group(1)) if match and int(match.group(1)) <= 100 else None
        add('continuous_host_power', 'fail' if source == 'battery' else 'unverified',
            {'source': source, 'battery_percent': percent,
             'reason': 'Battery operation cannot satisfy unattended host readiness' if source == 'battery'
             else 'Power alone does not verify lid-open operation or sleep prevention'})
    else:
        add('supervisor', 'unverified', 'unsupported host')
        add('time_sync', 'unverified', 'unsupported host')
    add('boot_identity', 'pass' if boot_id else 'unverified', 'Linux boot ID recorded' if boot_id else 'boot identity unavailable')
    return dict(schema_version=1, observed_at=(now or datetime.now(timezone.utc)).isoformat(),
                host={'system': system, 'kernel_release': release, 'wsl_detected': wsl,
                      'wsl2_detected': wsl2, 'boot_id': boot_id}, checks=checks,
                checks_failed=[c['name'] for c in checks if c['status'] == 'fail'],
                checks_unverified=[c['name'] for c in checks if c['status'] == 'unverified'],
                execution_authorized=False, live_approved=False, pc_acceptance_complete=False,
                limitations=['Local snapshot only; not proof of Windows restart, WSL termination, sleep or network recovery.',
                             'No broker, credential, remote-access, worker-liveness or cross-host fencing validation.'])


def save_report(path, report):
    """Create a private, exclusive report in an existing nonsymlink directory."""
    path = Path(path)
    if not path.is_absolute() or any(part == '..' for part in path.parts):
        raise ValueError('absolute output without traversal required')
    if any(parent.is_symlink() for parent in (path.parent, *path.parent.parents)):
        raise ValueError('symlink output parents rejected')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
