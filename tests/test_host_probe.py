import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from trader_engine.operations.host_probe import probe_host, save_report


def config(tmp_path):
    return {'repo_root': str(tmp_path), 'python': '/usr/bin/python3', 'state_root': str(tmp_path / 'state')}


def runner(args, **kwargs):
    assert kwargs == dict(capture_output=True, text=True, timeout=5, check=False)
    if args[0] == '/usr/bin/python3':
        return SimpleNamespace(returncode=0, stdout=json.dumps({'version': [3, 12, 1], 'platform': 'linux', 'fcntl': True}))
    values = {'systemctl': 'running', 'timedatectl': 'yes'}
    assert args[0] in values
    return SimpleNamespace(returncode=0, stdout=values[args[0]])


def read_text(path):
    return {'/proc/self/mountinfo': '1 0 1:0 / / rw - ext4 /dev/sda rw\n2 1 2:0 / /mnt/c rw - 9p C: rw',
            '/proc/sys/kernel/random/boot_id': '550e8400-e29b-41d4-a716-446655440000'}[path]


def probe(tmp_path, **kwargs):
    return probe_host(config(tmp_path), runner=kwargs.pop('runner', runner),
                      system='Linux', release=kwargs.pop('release', '6.6-microsoft-standard-WSL2'),
                      read_text=kwargs.pop('read_text', read_text),
                      disk_usage=lambda _: SimpleNamespace(free=6 * 1024 ** 3), **kwargs)


def test_wsl_success_never_grants_acceptance(tmp_path):
    result = probe(tmp_path)
    assert not result['checks_failed']
    assert result['host']['wsl2_detected']
    assert result['host']['boot_id'] == '550e8400-e29b-41d4-a716-446655440000'
    assert result['execution_authorized'] is result['live_approved'] is result['pc_acceptance_complete'] is False
    assert 'restart' in result['limitations'][0]


def test_plain_linux_not_wsl2(tmp_path):
    result = probe(tmp_path, release='6.8.0-generic')
    assert not result['host']['wsl2_detected']
    assert 'wsl2_kernel' in result['checks_unverified']


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.TimeoutExpired('probe', 5)])
def test_missing_and_timeout_sanitized(tmp_path, error):
    def broken(args, **kwargs):
        raise error
    result = probe(tmp_path, runner=broken)
    assert 'configured_interpreter' in result['checks_failed']
    assert 'time_sync' in result['checks_unverified']
    assert 'supervisor' in result['checks_unverified']


def test_unknown_clock_output_not_saved(tmp_path):
    def changed(args, **kwargs):
        if args[0] == 'timedatectl':
            return SimpleNamespace(returncode=0, stdout='PRIVATE_ARBITRARY_OUTPUT')
        return runner(args, **kwargs)
    result = probe(tmp_path, runner=changed)
    assert 'time_sync' in result['checks_unverified']
    assert 'PRIVATE_ARBITRARY_OUTPUT' not in json.dumps(result)


def test_actual_mount_detects_windows_storage_outside_mnt(tmp_path):
    def windows_mount(path):
        if path == '/proc/self/mountinfo':
            return '1 0 1:0 / / rw - 9p C: rw'
        return read_text(path)
    result = probe(tmp_path, read_text=windows_mount)
    assert 'state_root_linux_storage' in result['checks_failed']


def test_failed_clock_and_degraded_supervisor(tmp_path):
    def degraded(args, **kwargs):
        if args[0] == 'systemctl':
            return SimpleNamespace(returncode=1, stdout='degraded')
        if args[0] == 'timedatectl':
            return SimpleNamespace(returncode=0, stdout='no')
        return runner(args, **kwargs)
    result = probe(tmp_path, runner=degraded)
    assert {'time_sync', 'supervisor'} <= set(result['checks_failed'])


def test_private_output_no_overwrite_or_symlink(tmp_path):
    # macOS pytest roots often resolve through /var -> /private/var.
    tmp_path = tmp_path.resolve()
    output = tmp_path / 'report.json'
    save_report(output, {'safe': True})
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_report(output, {})
    assert json.loads(output.read_text()) == {'safe': True}
    (tmp_path / 'link').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        save_report(tmp_path / 'link' / 'report2.json', {})
    with pytest.raises(ValueError):
        save_report(tmp_path / '..' / 'report3.json', {})
    with pytest.raises(ValueError):
        save_report(Path('relative.json'), {})


def test_malformed_interpreter_and_missing_mounts(tmp_path):
    def malformed(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout='[]')
    def missing(path):
        raise FileNotFoundError(path)
    result = probe(tmp_path, runner=malformed, read_text=missing)
    assert 'configured_interpreter' in result['checks_failed']
    assert {'state_root_linux_storage', 'boot_identity'} <= set(result['checks_unverified'])


def test_disk_guard_is_failure(tmp_path):
    result = probe_host(config(tmp_path), runner=runner, system='Linux', release='WSL2',
                        read_text=read_text, disk_usage=lambda _: SimpleNamespace(free=4 * 1024 ** 3))
    assert 'disk_headroom' in result['checks_failed']


def test_mnt_path_rejected_even_missing_mountinfo(tmp_path):
    values = config(tmp_path)
    values['state_root'] = '/mnt/c/trader-state'
    result = probe_host(values, runner=runner, system='Linux', release='WSL2',
                        read_text=lambda _: '', disk_usage=lambda _: SimpleNamespace(free=6 * 1024 ** 3))
    assert 'state_root_linux_storage' in result['checks_failed']


def test_malformed_version_not_republished(tmp_path):
    def malformed(args, **kwargs):
        if args[0] == '/usr/bin/python3':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'version': 'PRIVATE_VALUE'}))
        return runner(args, **kwargs)
    result = probe(tmp_path, runner=malformed)
    assert 'PRIVATE_VALUE' not in json.dumps(result)

@pytest.mark.parametrize('power,status,source,percent', [
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 21%; discharging", 'fail', 'battery', 21),
    ("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged", 'unverified', 'ac', 100),
    ('PRIVATE_ARBITRARY_OUTPUT', 'unverified', 'unknown', None),
])
def test_mac_power_cannot_certify_uptime(tmp_path, power, status, source, percent):
    def mac_runner(args, **kwargs):
        if args[0] == '/usr/bin/python3':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'version': [3,12,1], 'platform':'darwin', 'fcntl':True}))
        if args[0] == 'launchctl':
            return SimpleNamespace(returncode=0, stdout='')
        assert args == ['pmset', '-g', 'batt']
        return SimpleNamespace(returncode=0, stdout=power)
    report = probe_host(config(tmp_path), system='Darwin', release='test', runner=mac_runner)
    check = next(c for c in report['checks'] if c['name']=='continuous_host_power')
    assert (check['status'],check['detail']['source'],check['detail']['battery_percent']) == (status,source,percent)
    assert 'PRIVATE_ARBITRARY_OUTPUT' not in json.dumps(report)
    assert report['execution_authorized'] is False
