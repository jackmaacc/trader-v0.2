import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from trader_engine.operations.paper_trial import PaperTrial
from trader_engine.operations.trial_backup import BackupError, create_backup, restore_backup, verify_backup


def ledger(tmp_path):
    source = tmp_path/'source'
    trial = PaperTrial(source)
    trial.db.execute("INSERT INTO observations VALUES(?,?,?,?)", ('paper_crypto_service', '2026-09-24T12:00:00Z', '2026-09-24T12:00:01Z', '{"safe":true}'))
    trial.db.execute("INSERT INTO polls VALUES(?,?)", ('2026-09-24T12:00:01Z', '{"state":"observing"}'))
    trial.db.commit()
    return source, trial


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def test_online_backup_includes_committed_wal_rows_without_source_mutation(tmp_path):
    source, trial = ledger(tmp_path)
    assert (source/'observations.sqlite3-wal').stat().st_size > 0
    before = digest(source/'observations.sqlite3')
    manifest = create_backup(source, tmp_path/'backup')
    assert manifest['row_counts']['observations'] == 1
    assert manifest['observation_timestamps']['observations.heartbeat']['last'] == '2026-09-24T12:00:00Z'
    assert digest(source/'observations.sqlite3') == before
    assert trial.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 1
    assert verify_backup(tmp_path/'backup') == manifest
    assert (tmp_path/'backup').stat().st_mode & 0o077 == 0
    for path in (tmp_path/'backup').iterdir(): assert path.stat().st_mode & 0o077 == 0
    trial.close()


def test_snapshot_is_consistent_and_does_not_include_uncommitted_rows(tmp_path):
    source, trial = ledger(tmp_path)
    trial.db.execute("INSERT INTO polls VALUES(?,?)", ('2026-09-24T12:01:01Z', '{}'))
    manifest = create_backup(source, tmp_path/'backup')
    assert manifest['row_counts']['polls'] == 1
    trial.db.rollback(); trial.close()


def test_restore_defaults_to_dry_run_then_new_nonlive_filename(tmp_path):
    source, trial = ledger(tmp_path)
    backup = tmp_path/'backup'
    create_backup(source, backup)
    result = restore_backup(backup, tmp_path/'exercise')
    assert result['dry_run'] and not result['restored']
    assert not (tmp_path/'exercise').exists()
    result = restore_backup(backup, tmp_path/'exercise', dry_run=False)
    assert result['restored'] and result['not_live_state']
    assert (tmp_path/'exercise/restored.sqlite3').is_file()
    assert not (tmp_path/'exercise/observations.sqlite3').exists()
    assert digest(tmp_path/'exercise/restored.sqlite3') == digest(backup/'snapshot.sqlite3')
    trial.close()


def test_tampered_snapshot_rejected(tmp_path):
    source, trial = ledger(tmp_path); create_backup(source, tmp_path/'backup')
    with (tmp_path/'backup/snapshot.sqlite3').open('ab') as stream: stream.write(b'changed')
    with pytest.raises(BackupError, match='hash_or_size'): restore_backup(tmp_path/'backup', tmp_path/'exercise', dry_run=False)
    assert not (tmp_path/'exercise').exists()
    trial.close()


def test_tampered_manifest_counts_rejected(tmp_path):
    source, trial = ledger(tmp_path); create_backup(source, tmp_path/'backup')
    path = tmp_path/'backup/manifest.json'; payload = json.loads(path.read_text()); payload['row_counts']['polls'] = 42; path.write_text(json.dumps(payload))
    with pytest.raises(BackupError, match='evidence_mismatch'): verify_backup(tmp_path/'backup')
    trial.close()


def test_existing_destination_never_overwritten(tmp_path):
    source, trial = ledger(tmp_path)
    destination = tmp_path/'backup'; destination.mkdir(); (destination/'sentinel').write_text('keep')
    with pytest.raises(BackupError, match='already_exists'): create_backup(source, destination)
    assert (destination/'sentinel').read_text() == 'keep'
    create_backup(source, tmp_path/'valid')
    with pytest.raises(BackupError, match='already_exists'): restore_backup(tmp_path/'valid', source, dry_run=False)
    trial.close()


def test_wrong_database_and_extra_trigger_rejected_before_publish(tmp_path):
    source = tmp_path/'wrong'; source.mkdir()
    db = sqlite3.connect(source/'observations.sqlite3'); db.execute('CREATE TABLE unrelated(x)'); db.commit(); db.close()
    with pytest.raises(BackupError, match='unexpected_trial_schema'): create_backup(source, tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    source, trial = ledger(tmp_path)
    trial.db.execute('CREATE TRIGGER unexpected AFTER INSERT ON polls BEGIN SELECT 1; END'); trial.db.commit()
    with pytest.raises(BackupError, match='unexpected_trial_schema'): create_backup(source, tmp_path/'backup')
    trial.close()


def test_symlink_and_parent_traversal_and_source_nested_destination_rejected(tmp_path):
    source, trial = ledger(tmp_path)
    (tmp_path/'linked').symlink_to(source)
    for unsafe in [tmp_path/'linked', tmp_path/'other/../source']:
        with pytest.raises(BackupError): create_backup(unsafe, tmp_path/'backup')
    with pytest.raises(BackupError, match='isolated'): create_backup(source, source/'backup')
    (tmp_path/'target-link').symlink_to(tmp_path/'absent')
    with pytest.raises(BackupError, match='symlink'): create_backup(source, tmp_path/'target-link')
    trial.close()


def test_low_disk_blocks_before_destination_creation(tmp_path, monkeypatch):
    source, trial = ledger(tmp_path)
    monkeypatch.setattr('trader_engine.operations.trial_backup.shutil.disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(BackupError, match='free_disk'): create_backup(source, tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    trial.close()


def test_manifest_path_traversal_not_followed(tmp_path):
    source, trial = ledger(tmp_path); create_backup(source, tmp_path/'backup')
    path = tmp_path/'backup/manifest.json'; payload = json.loads(path.read_text()); payload['snapshot_file'] = '../source/observations.sqlite3'; path.write_text(json.dumps(payload))
    with pytest.raises(BackupError, match='unsupported_backup_manifest'): verify_backup(tmp_path/'backup')
    trial.close()


def test_unknown_schema_version_rejected(tmp_path):
    source, trial = ledger(tmp_path)
    trial.db.execute('PRAGMA user_version=99'); trial.db.commit()
    with pytest.raises(BackupError, match='schema_version'): create_backup(source, tmp_path/'backup')
    trial.close()


def test_restore_cannot_nest_within_live_ledger_directory(tmp_path):
    source, trial = ledger(tmp_path); create_backup(source, tmp_path/'backup')
    with pytest.raises(BackupError, match='inside_existing_runtime_state'):
        restore_backup(tmp_path/'backup', source/'isolated-looking', dry_run=False)
    assert not (source/'isolated-looking').exists()
    trial.close()
