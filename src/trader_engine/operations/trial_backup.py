"""Consistent local backups of the operational trial, never broker ownership state.

Only the SQLite online backup API reads a live ledger. No credentials, broker
requests, checkpointing, scheduling, or service operations are performed here.
A manifest is the publication marker and is written only after verification.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

FORMAT_VERSION = 1
SCHEMA = {
    'meta': [('key', 'TEXT', 0, 1), ('value', 'TEXT', 1, 0)],
    'observations': [('source', 'TEXT', 1, 1), ('heartbeat', 'TEXT', 1, 2), ('observed_at', 'TEXT', 1, 0), ('payload', 'TEXT', 1, 0)],
    'polls': [('observed_at', 'TEXT', 0, 1), ('summary', 'TEXT', 1, 0)],
    'gaps': [('id', 'INTEGER', 0, 1), ('source', 'TEXT', 1, 0), ('started_at', 'TEXT', 1, 0), ('ended_at', 'TEXT', 0, 0), ('reason', 'TEXT', 1, 0)],
}


class BackupError(ValueError):
    """Sanitized error suitable for a CLI; never includes row payloads."""


def _path(value, *, exists=False):
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise BackupError('parent_traversal_not_allowed')
    path = path.absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise BackupError('symlink_path_not_allowed_use_canonical_path')
    if exists and not path.exists():
        raise BackupError('required_path_missing')
    return path


def _regular(path):
    path = _path(path, exists=True)
    if not stat.S_ISREG(path.stat().st_mode): raise BackupError('regular_file_required')
    return path


def _new_destination(value, source):
    destination = _path(value)
    if destination.exists(): raise BackupError('destination_already_exists')
    if destination == source or source in destination.parents or destination in source.parents:
        raise BackupError('destination_must_be_isolated_from_source')
    parent = _path(destination.parent, exists=True)
    if not parent.is_dir(): raise BackupError('destination_parent_not_directory')
    return destination


def _connect(path):
    return sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)


def _summary(db):
    # Exact known schema avoids blessing a different database, virtual tables,
    # unexpected triggers/views or extensions as a valid trial ledger.
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')").fetchall()
    if set(objects) != {('table', name) for name in SCHEMA}:
        raise BackupError('unexpected_trial_schema')
    for table, expected in SCHEMA.items():
        columns = [(row[1], row[2].upper(), row[3], row[5]) for row in db.execute('PRAGMA table_info('+table+')')]
        if columns != expected: raise BackupError('unexpected_trial_columns')
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise BackupError('sqlite_integrity_check_failed')
    if db.execute('PRAGMA user_version').fetchone()[0] != 0:
        raise BackupError('unsupported_trial_schema_version')
    counts = {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in SCHEMA}
    dates = {}
    for table, column in [('observations', 'observed_at'), ('observations', 'heartbeat'), ('polls', 'observed_at'), ('gaps', 'started_at'), ('gaps', 'ended_at')]:
        first, last = db.execute('SELECT MIN('+column+'),MAX('+column+') FROM '+table).fetchone()
        for value in (first, last):
            if value is not None:
                try:
                    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                    if parsed.tzinfo is None: raise ValueError()
                except (ValueError, TypeError): raise BackupError('invalid_observation_timestamp') from None
        dates[table+'.'+column] = {'first': first, 'last': last}
    return {'schema_version': db.execute('PRAGMA user_version').fetchone()[0], 'schema': {table: [list(column) for column in cols] for table, cols in SCHEMA.items()}, 'row_counts': counts, 'observation_timestamps': dates}


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def _headroom(source, destination_parent):
    # WAL size is included because committed data may not be in the main file.
    total = source.stat().st_size
    wal = source.with_name(source.name+'-wal')
    if wal.exists(): total += _regular(wal).stat().st_size
    required = max(64*1024**2, 2*total+16*1024**2)
    if shutil.disk_usage(destination_parent).free < required:
        raise BackupError('insufficient_free_disk_space')


def _write_json(path, payload):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def create_backup(source_directory, destination_directory, *, timeout_seconds=30):
    """Reserve NEW private destination, then publish verified snapshot+manifest."""
    source_dir = _path(source_directory, exists=True)
    source = _regular(source_dir/'observations.sqlite3')
    for suffix in ('-wal', '-shm', '-journal'):
        _path(source.with_name(source.name+suffix))
    destination = _new_destination(destination_directory, source_dir)
    _headroom(source, destination.parent)
    if not isinstance(timeout_seconds, (float, int)) or not 0 < timeout_seconds <= 300:
        raise BackupError('invalid_backup_timeout')
    # Validate before allocating any destination, but do not compare live counts
    # afterward: the observer is allowed to commit while the backup runs.
    try:
        with _connect(source) as live:
            _summary(live)
            destination.mkdir(mode=0o700, exist_ok=False)
            building = destination/'.building.sqlite3'
            fd = os.open(building, os.O_CREAT|os.O_EXCL|os.O_RDWR, 0o600); os.close(fd)
            try:
                started = time.monotonic()
                def progress(status, remaining, total):
                    if time.monotonic()-started > timeout_seconds: raise BackupError('backup_timeout')
                with sqlite3.connect(building) as saved:
                    live.backup(saved, pages=128, progress=progress, sleep=.05)
                    saved.execute('PRAGMA journal_mode=DELETE')
                    summary = _summary(saved)
                # sqlite3 context managers commit but do not close connections.
                saved.close()
                os.chmod(building, 0o600)
                with building.open('rb') as stream: os.fsync(stream.fileno())
                digest = _hash(building)
                manifest = dict(format_version=FORMAT_VERSION, kind='operational_trial_backup', created_at=datetime.now(timezone.utc).isoformat(), snapshot_file='snapshot.sqlite3', sha256=digest, snapshot_bytes=building.stat().st_size, sqlite_version=sqlite3.sqlite_version, **summary)
                building.rename(destination/'snapshot.sqlite3')
                _write_json(destination/'manifest.json', manifest)
                _sync_directory(destination); _sync_directory(destination.parent)
                return manifest
            except Exception:
                shutil.rmtree(destination)
                raise
            finally:
                if 'saved' in locals(): saved.close()
    except BackupError: raise
    except (OSError, sqlite3.Error): raise BackupError('backup_io_or_sqlite_failure') from None
    finally:
        if 'live' in locals(): live.close()


def verify_backup(backup_directory):
    """Verify hash, size, strict schema, integrity, counts and date ranges."""
    directory = _path(backup_directory, exists=True)
    manifest_path = _regular(directory/'manifest.json')
    snapshot = _regular(directory/'snapshot.sqlite3')
    # A published snapshot is standalone; sidecars would make its hash ambiguous.
    if any((directory/name).exists() or (directory/name).is_symlink() for name in ('snapshot.sqlite3-wal','snapshot.sqlite3-shm','snapshot.sqlite3-journal')):
        raise BackupError('snapshot_has_unexpected_sidecars')
    try:
        if manifest_path.stat().st_size > 1024*1024: raise BackupError('manifest_too_large')
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict) or manifest.get('format_version') != FORMAT_VERSION or manifest.get('kind') != 'operational_trial_backup' or manifest.get('snapshot_file') != 'snapshot.sqlite3':
            raise BackupError('unsupported_backup_manifest')
        if snapshot.stat().st_size != manifest.get('snapshot_bytes') or _hash(snapshot) != manifest.get('sha256'):
            raise BackupError('snapshot_hash_or_size_mismatch')
        with _connect(snapshot) as saved:
            summary = _summary(saved)
        saved.close()
        if any(manifest.get(key) != value for key, value in summary.items()):
            raise BackupError('snapshot_manifest_evidence_mismatch')
        return manifest
    except BackupError: raise
    except (OSError, ValueError, sqlite3.Error): raise BackupError('backup_verification_failed') from None
    finally:
        if 'saved' in locals(): saved.close()


def restore_backup(backup_directory, destination_directory=None, *, dry_run=True):
    """Dry run by default. Apply creates a NEW isolated directory, never live state.

    Deliberately writes restored.sqlite3, not observations.sqlite3: workers cannot
    adopt it by simply pointing their existing state directory at this output.
    """
    source = _path(backup_directory, exists=True)
    manifest = verify_backup(source)
    destination = _new_destination(destination_directory, source) if destination_directory is not None else None
    if destination is not None and any((parent/'observations.sqlite3').exists() or (parent/'state.json').exists() for parent in destination.parents):
        raise BackupError('restore_destination_is_inside_existing_runtime_state')
    if dry_run:
        return {'verified': True, 'dry_run': True, 'restored': False, 'manifest': manifest}
    if destination is None: raise BackupError('new_isolated_restore_destination_required')
    _headroom(source/'snapshot.sqlite3', destination.parent)
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        restored = destination/'restored.sqlite3'
        fd = os.open(restored, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output, (source/'snapshot.sqlite3').open('rb') as stream:
            shutil.copyfileobj(stream, output); output.flush(); os.fsync(output.fileno())
        if _hash(restored) != manifest['sha256']: raise BackupError('restore_copy_hash_mismatch')
        with _connect(restored) as db:
            _summary(db)
        db.close()
        _write_json(destination/'restore-verification.json', {'kind': 'isolated_trial_restore_exercise', 'verified_at': datetime.now(timezone.utc).isoformat(), 'sha256': manifest['sha256'], 'row_counts': manifest['row_counts'], 'not_live_state': True})
        _sync_directory(destination); _sync_directory(destination.parent)
        return {'verified': True, 'dry_run': False, 'restored': True, 'not_live_state': True, 'sha256': manifest['sha256'], 'row_counts': manifest['row_counts']}
    except Exception:
        shutil.rmtree(destination)
        raise
    finally:
        if 'db' in locals(): db.close()
