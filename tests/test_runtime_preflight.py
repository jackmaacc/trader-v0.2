from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from trader_engine.operations.runtime import (ENV_KEYS, MIN_VERSIONS, SERVICES, normalize_manifest, preflight, render_service_files, service_commands)


def setup(tmp_path):
    repo = tmp_path/'repo with spaces'; repo.mkdir()
    (repo/'src/trader_engine').mkdir(parents=True)
    (repo/'scripts').mkdir()
    (repo/'pyproject.toml').write_text('[project]\n')
    for script, _ in SERVICES.values(): (repo/'scripts'/script).touch()
    python = repo/'.venv/bin/python'; python.parent.mkdir(parents=True); python.touch(); python.chmod(0o700)
    manifest = dict(repo_root=str(repo), python=str(python), state_root=str(tmp_path/'state'), expected_account_id='paper-account', stock_feed='sip')
    return manifest


def probe(_):
    return dict(version=[3, 12, 1], platform='linux', release='5.15-microsoft-standard-WSL2', fcntl=True, dependencies={name: '.'.join(map(str, version)) for name, version in MIN_VERSIONS.items()})


def inspect(config, **kwargs):
    return preflight(config, probe=kwargs.pop('probe', probe), environ=kwargs.pop('environ', {}), disk_usage=lambda _: SimpleNamespace(free=10*1024**3), **kwargs)


def test_preflight_does_not_create_state_and_marks_unverified_checks(tmp_path):
    config = setup(tmp_path)
    report = inspect(config)
    assert report['ready_for_render']
    assert not report['execution_authorized']
    assert not Path(config['state_root']).exists()
    assert next(c for c in report['checks'] if c['name']=='clock')['status'] == 'warning'
    assert next(c for c in report['checks'] if c['name']=='single_writer')['status'] == 'warning'


def test_secret_values_never_read_or_echoed(tmp_path):
    class KeysOnly:
        def __contains__(self, key): return key in ENV_KEYS
        def __getitem__(self, key): raise AssertionError('secret values must not be read')
    config = setup(tmp_path)
    secret = tmp_path/'secret.env'; secret.write_text('DO_NOT_READ_SECRET'); secret.chmod(0o600)
    config['secrets_file'] = str(secret)
    result = inspect(config, environ=KeysOnly())
    assert result['ready_for_render']
    assert 'DO_NOT_READ_SECRET' not in json.dumps(result)
    secret.chmod(0o644)
    assert not inspect(config)['ready_for_render']


def test_virtualenv_symlink_preserved(tmp_path):
    config = setup(tmp_path)
    target = tmp_path/'base-python'; target.touch()
    path = Path(config['python']); path.unlink(); path.symlink_to(target)
    assert normalize_manifest(config)['python'] == str(path)
    assert service_commands(config)['market-scanner'][0] == str(path)


@pytest.mark.parametrize('platform,fcntl', [('win32', False), ('linux', False)])
def test_unsupported_native_windows_and_missing_lock_primitive_fail(tmp_path, platform, fcntl):
    config = setup(tmp_path)
    def bad(path): return dict(probe(path), platform=platform, fcntl=fcntl)
    assert not inspect(config, probe=bad)['ready_for_render']


def test_missing_worker_and_dependency_versions_fail(tmp_path):
    config = setup(tmp_path)
    (Path(config['repo_root'])/'scripts/market_scanner_service.py').unlink()
    assert not inspect(config)['ready_for_render']
    def bad(path):
        value = probe(path); value['dependencies']['websockets'] = '17.0'; return value
    assert any(c['name']=='dependencies' and c['status']=='fail' for c in inspect(config, probe=bad)['checks'])


def test_supplied_clock_reference_cannot_claim_ntp_verification(tmp_path):
    config = setup(tmp_path); config['clock_reference_utc'] = '2026-09-24T12:00:00Z'
    now = datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc)
    report = inspect(config, now=now)
    clock = next(c for c in report['checks'] if c['name']=='clock')
    assert clock['status']=='fail'
    assert clock['detail']['reference']=='operator supplied; not network verified'


def test_systemd_templates_never_authorize_execution_or_expose_secrets(tmp_path):
    config = setup(tmp_path); config['secrets_file'] = str(tmp_path/'private.env')
    files = render_service_files(config, 'systemd')
    assert len(files)==6
    paper = files['trader-paper-crypto.service']
    assert '--observe-only' in paper and '--execute-paper' not in paper
    assert '[Install]' not in paper and 'Restart=no' in paper
    assert 'ConditionPathExists=' in paper
    assert 'EnvironmentFile=' in paper
    assert 'APCA_API_SECRET_KEY=' not in ''.join(files.values())
    assert '--artifacts' in files['trader-plus-research.service']
    assert 'repo with spaces' in files['trader-market-scanner.service']
    assert not Path(config['state_root']).exists()


def test_launch_agents_are_inert_and_contain_no_secret_values(tmp_path):
    config = setup(tmp_path)
    for name, text in render_service_files(config, 'launchd').items():
        payload = plistlib.loads(text.encode())
        assert payload['Disabled'] is True
        assert payload['RunAtLoad'] is False and payload['KeepAlive'] is False
        assert '--execute-paper' not in payload['ProgramArguments']
        assert not any(k in payload['EnvironmentVariables'] for k in ENV_KEYS)


@pytest.mark.parametrize('bad', ['relative/path', '/absolute/path\nExecStart=evil', '/absolute/%h', '/absolute/$HOME'])
def test_service_path_expansion_injection_rejected(tmp_path, bad):
    config = setup(tmp_path); config['repo_root'] = bad
    assert not inspect(config)['ready_for_render']
    with pytest.raises(ValueError): render_service_files(config, 'systemd')


def test_service_cli_paths_match_existing_names_and_feed_limits(tmp_path):
    config = setup(tmp_path)
    commands = service_commands(config)
    assert str(Path(config['state_root'])/'paper_crypto_service') in commands['paper-crypto']
    assert '--artifacts' in commands['paper-trial']
    config['stock_feed'] = 'iex'
    assert service_commands(config)['market-scanner'][-1] == '300'


def test_cli_rejects_service_install_directory_before_writing(tmp_path):
    import runpy
    script = Path(__file__).resolve().parents[1]/'scripts/runtime_preflight.py'
    namespace = runpy.run_path(str(script))
    config = setup(tmp_path)
    manifest = tmp_path/'runtime.json'; manifest.write_text(json.dumps(config))
    with pytest.raises(SystemExit) as exc:
        namespace['main'](['--manifest', str(manifest), '--render', 'systemd', '--output', '/etc/systemd/trader-do-not-create'])
    assert exc.value.code == 2
    assert not Path('/etc/systemd/trader-do-not-create').exists()


def test_linux_target_paths_are_not_resolved_against_mac_filesystem():
    config = dict(repo_root='/home/trader/repo', python='/home/trader/repo/.venv/bin/python', state_root='/home/trader/state')
    normalized = normalize_manifest(config)
    assert normalized['repo_root'] == '/home/trader/repo'
    for text in render_service_files(config, 'systemd').values():
        assert '/System/Volumes' not in text
        assert '"/home/trader/repo/.venv/bin/python"' in text
