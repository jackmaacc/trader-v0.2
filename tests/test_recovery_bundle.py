import json
from pathlib import Path
import pytest
from trader_engine.operations import recovery_bundle as bundle


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'runtime'; root.mkdir()
    (root / 'state.json').write_text(json.dumps({'ownership': {'BTC/USD': '0.1'}, 'pending': None}))
    (root / 'journal.jsonl').write_text(json.dumps({'event': 'intent', 'qty': '0.1'}) + '\n')
    return root


def selection():
    return [{'path': 'state.json', 'role': 'ownership'}, {'path': 'journal.jsonl', 'role': 'pending_journal'}]


def test_create_verify_isolated_restore_preserves_bytes_and_no_authority(source, tmp_path):
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    output = tmp_path / 'bundle'
    report = bundle.create_bundle(source, selection(), output)
    assert report == bundle.verify_bundle(output)
    assert not report['stopped_writer_verified'] and not report['cross_file_atomicity_verified']
    assert not report['recovery_authorized'] and not report['execution_authorized']
    restored = tmp_path / 'restore'
    result = bundle.restore_bundle(output, restored)
    assert result['isolated_restore_verified'] is True
    assert (restored / 'isolated-files/0000.json').read_bytes() == original['state.json']
    assert not (restored / 'state.json').exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == original
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / 'files/0000.json').stat().st_mode & 0o777 == 0o600


def test_missing_source_and_existing_output(source, tmp_path):
    output = tmp_path / 'bundle'
    with pytest.raises(bundle.BundleError, match='source_missing'):
        bundle.create_bundle(source, [{'path': 'absent.json', 'role': 'ownership'}], output)
    assert not output.exists()
    bundle.create_bundle(source, selection(), output)
    with pytest.raises(bundle.BundleError, match='already_exists'):
        bundle.create_bundle(source, selection(), output)
    with pytest.raises(bundle.BundleError, match='already_exists'):
        bundle.restore_bundle(output, source)


@pytest.mark.parametrize('unsafe', ['../state.json', '/state.json', 'foo\\state.json', '.env', 'api_key.json'])
def test_unsafe_selection_paths_refused(source, tmp_path, unsafe):
    with pytest.raises(bundle.BundleError):
        bundle.create_bundle(source, [{'path': unsafe, 'role': 'ownership'}], tmp_path / 'bundle')


def test_symlinks_hardlinks_and_runtime_destination_refused(source, tmp_path):
    (source / 'link.json').symlink_to(source / 'state.json')
    with pytest.raises(bundle.BundleError, match='symlink'):
        bundle.create_bundle(source, [{'path': 'link.json', 'role': 'ownership'}], tmp_path / 'bundle')
    with pytest.raises(bundle.BundleError, match='isolated'):
        bundle.create_bundle(source, selection(), source / 'bundle')
    (source / 'hard.json').hardlink_to(source / 'state.json')
    with pytest.raises(bundle.BundleError, match='hardlinked'):
        bundle.create_bundle(source, selection(), tmp_path / 'bundle')


@pytest.mark.parametrize('data', [b'SQLite format 3\0random', b'{"nested":{"APCA_API_SECRET_KEY":"do not copy"}}', b'{"private_key":"no"}', b'not json'])
def test_sqlite_and_secret_content_not_copied(source, tmp_path, data):
    (source / 'state.json').write_bytes(data)
    output = tmp_path / 'bundle'
    with pytest.raises(bundle.BundleError):
        bundle.create_bundle(source, selection(), output)
    assert not output.exists()


def test_corruption_and_unexpected_files(source, tmp_path):
    output = tmp_path / 'bundle'; bundle.create_bundle(source, selection(), output)
    payload = output / 'files/0000.json'; original = payload.read_bytes()
    payload.write_text('{"ownership":{}}')
    with pytest.raises(bundle.BundleError, match='hash_or_size'):
        bundle.verify_bundle(output)
    payload.write_bytes(original)
    (output / 'extra.json').write_text('{}')
    with pytest.raises(bundle.BundleError, match='unexpected'):
        bundle.verify_bundle(output)


def test_manifest_traversal_and_authority_rejected(source, tmp_path):
    output = tmp_path / 'bundle'; bundle.create_bundle(source, selection(), output)
    path = output / 'manifest.json'; original = json.loads(path.read_text())
    changed = json.loads(path.read_text()); changed['files'][0]['payload'] = '../state.json'
    path.write_text(json.dumps(changed))
    with pytest.raises(bundle.BundleError, match='payload_path'):
        bundle.verify_bundle(output)
    original['recovery_authorized'] = True; path.write_text(json.dumps(original))
    with pytest.raises(bundle.BundleError, match='authority_flags'):
        bundle.verify_bundle(output)


def test_source_changes_between_passes_never_publish_manifest(source, tmp_path, monkeypatch):
    actual = bundle._write
    def change_after_copy(path, data):
        actual(path, data)
        if path.name == '0001.jsonl':
            (source / 'state.json').write_text('{"ownership":{},"pending":null}')
    monkeypatch.setattr(bundle, '_write', change_after_copy)
    output = tmp_path / 'bundle'
    with pytest.raises(bundle.BundleError, match='source_changed'):
        bundle.create_bundle(source, selection(), output)
    assert not (output / 'manifest.json').exists()
    with pytest.raises(bundle.BundleError):
        bundle.verify_bundle(output)


def test_restore_rechecks_bundle_change(source, tmp_path, monkeypatch):
    output = tmp_path / 'bundle'; bundle.create_bundle(source, selection(), output)
    actual = bundle._write
    def mutate(path, data):
        actual(path, data)
        if path.name == '0000.json':
            (output / 'files/0001.jsonl').write_text('{"changed":true}\n')
    monkeypatch.setattr(bundle, '_write', mutate)
    restored = tmp_path / 'restored'
    with pytest.raises(bundle.BundleError):
        bundle.restore_bundle(output, restored)
    assert not (restored / 'restore-verification.json').exists()


def test_restore_low_disk_refuses_before_creating_output(source, tmp_path, monkeypatch):
    from types import SimpleNamespace
    output = tmp_path / 'bundle'; bundle.create_bundle(source, selection(), output)
    monkeypatch.setattr(bundle.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    restored = tmp_path / 'restored'
    with pytest.raises(bundle.BundleError, match='insufficient_disk_headroom'):
        bundle.restore_bundle(output, restored)
    assert not restored.exists()
