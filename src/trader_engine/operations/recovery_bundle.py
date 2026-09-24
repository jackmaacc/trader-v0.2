"""Private offline file evidence bundles; never adopts or activates runtime state."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

ROLES = {'configuration', 'ownership', 'pending_journal', 'seed'}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
SECRET_KEY = re.compile(r'(secret|password|passwd|api.?key|access.?token|refresh.?token|private.?key|authorization|credential)', re.I)
FORBIDDEN_NAMES = {'.env', '.git', '.ssh', '.aws', '.config', 'credentials', 'keychain'}


class BundleError(ValueError):
    """Fixed diagnostic codes; no file payloads or credentials in exception text."""


def _path(value, *, exists=False):
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise BundleError('parent_traversal_refused')
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise BundleError('symlink_refused_use_canonical_paths')
    if exists and not path.exists():
        raise BundleError('source_missing')
    return path


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise BundleError('invalid_relative_path')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or any(part in FORBIDDEN_NAMES or SECRET_KEY.search(part) for part in path.parts):
        raise BundleError('unsafe_or_credential_path')
    if path.suffix not in {'.json', '.jsonl'}:
        raise BundleError('only_explicit_json_or_jsonl_supported')
    return path.as_posix()


def _fingerprint(path):
    value = path.stat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise BundleError('regular_non_hardlinked_file_required')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _safe_payload(data, suffix):
    if data.startswith(b'SQLite format 3'):
        raise BundleError('sqlite_requires_online_backup_tool')
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if SECRET_KEY.search(key):
                    raise BundleError('possible_credential_content_refused')
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str) and ('-----BEGIN ' in value or 'Bearer ' in value):
            raise BundleError('possible_credential_content_refused')
    try:
        text = data.decode('utf-8')
        values = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == '.jsonl' else [json.loads(text)]
        for value in values:
            if not isinstance(value, (dict, list)):
                raise BundleError('structured_json_required')
            inspect(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError('invalid_structured_payload') from None


def _read(path):
    path = _path(path, exists=True)
    before = _fingerprint(path)
    if before[2] > MAX_FILE_BYTES:
        raise BundleError('file_too_large')
    with path.open('rb') as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise BundleError('file_too_large')
    after = _fingerprint(path)
    if before != after or len(data) != before[2]:
        raise BundleError('source_changed_during_read')
    return data, after


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _destination(value, *, forbidden=()):
    path = _path(value)
    if path.exists():
        raise BundleError('destination_already_exists')
    if not _path(path.parent, exists=True).is_dir():
        raise BundleError('destination_parent_not_directory')
    for source in forbidden:
        if path == source or source in path.parents:
            raise BundleError('destination_not_isolated')
    # Refuse nesting an exercise in a live state or installed-service directory.
    for parent in path.parents:
        if any((parent / marker).exists() for marker in ('state.json', 'observations.sqlite3')):
            raise BundleError('runtime_directory_refused')
    if any(part in {'LaunchAgents', 'LaunchDaemons', 'systemd', '.git'} for part in path.parts):
        raise BundleError('service_or_repository_metadata_directory_refused')
    return path


def create_bundle(source_root, selections, destination):
    """Copy explicit files, then re-read all sources before publishing manifest.

    A matching second pass cannot prove cross-file transactional consistency or
    writer fencing. Every bundle therefore remains unapproved recovery evidence.
    """
    root = _path(source_root, exists=True)
    if not root.is_dir() or not isinstance(selections, list) or not 1 <= len(selections) <= 128:
        raise BundleError('invalid_selection')
    selected = []; seen = set(); total = 0
    for item in selections:
        if not isinstance(item, dict) or set(item) != {'path', 'role'} or not isinstance(item['role'], str) or item['role'] not in ROLES:
            raise BundleError('invalid_selection')
        relative = _relative(item['path'])
        if relative in seen:
            raise BundleError('duplicate_selection')
        seen.add(relative)
        source = _path(root / relative, exists=True)
        data, identity = _read(source)
        _safe_payload(data, source.suffix)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError('bundle_too_large')
        selected.append((source, relative, item['role'], data, identity))
    output = _destination(destination, forbidden=[source.parent for source, *_ in selected])
    if shutil.disk_usage(output.parent).free < max(64 * 1024 * 1024, total * 2):
        raise BundleError('insufficient_disk_headroom')
    output.mkdir(mode=0o700)
    files = output / 'files'; files.mkdir(mode=0o700)
    inventory = []
    for index, (source, relative, role, data, identity) in enumerate(selected):
        name = f'{index:04d}{source.suffix}'
        _write(files / name, data)
        inventory.append({'path': relative, 'role': role, 'payload': 'files/' + name,
                          'bytes': len(data), 'sha256': _digest(data)})
    for source, relative, role, data, identity in selected:
        latest, current = _read(source)
        if current != identity or _digest(latest) != _digest(data):
            raise BundleError('source_changed_during_capture')
    manifest = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': inventory, 'source_recheck_passed': True,
                'stopped_writer_verified': False, 'cross_file_atomicity_verified': False,
                'recovery_authorized': False, 'execution_authorized': False,
                'credential_screen': 'heuristic_only_operator_must_select_nonsecret_files'}
    _sync(files)
    _write(output / 'manifest.json', _json(manifest))
    _sync(output); _sync(output.parent)
    verify_bundle(output)
    return manifest


def verify_bundle(bundle):
    root = _path(bundle, exists=True)
    manifest_data, _ = _read(root / 'manifest.json')
    try:
        manifest = json.loads(manifest_data)
    except (ValueError, UnicodeError):
        raise BundleError('invalid_manifest') from None
    required = {'format_version', 'created_at', 'files', 'source_recheck_passed', 'stopped_writer_verified',
                'cross_file_atomicity_verified', 'recovery_authorized', 'execution_authorized', 'credential_screen'}
    if not isinstance(manifest, dict) or set(manifest) != required or type(manifest['format_version']) is not int or manifest['format_version'] != 1:
        raise BundleError('unsupported_manifest')
    if manifest['credential_screen'] != 'heuristic_only_operator_must_select_nonsecret_files':
        raise BundleError('invalid_credential_screen')
    if manifest['source_recheck_passed'] is not True or any(manifest[k] is not False for k in ('stopped_writer_verified', 'cross_file_atomicity_verified', 'recovery_authorized', 'execution_authorized')):
        raise BundleError('invalid_authority_flags')
    try:
        stamp = datetime.fromisoformat(manifest['created_at'])
        if stamp.tzinfo is None: raise ValueError()
    except (TypeError, ValueError):
        raise BundleError('invalid_manifest_time') from None
    rows = manifest['files']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise BundleError('invalid_inventory')
    expected = {'manifest.json', 'files'}; payloads = set(); originals = set(); total = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {'path', 'role', 'payload', 'bytes', 'sha256'}:
            raise BundleError('invalid_inventory')
        relative = _relative(row['path'])
        if relative in originals or not isinstance(row['role'], str) or row['role'] not in ROLES:
            raise BundleError('invalid_inventory')
        originals.add(relative)
        payload = f'files/{index:04d}{Path(relative).suffix}'
        if row['payload'] != payload:
            raise BundleError('invalid_payload_path')
        data, _ = _read(root / payload)
        _safe_payload(data, Path(relative).suffix)
        if type(row['bytes']) is not int or len(data) != row['bytes'] or _digest(data) != row['sha256']:
            raise BundleError('payload_hash_or_size_mismatch')
        total += len(data); payloads.add(Path(payload).name)
        if total > MAX_TOTAL_BYTES: raise BundleError('bundle_too_large')
    if {p.name for p in root.iterdir()} != expected or {p.name for p in (root / 'files').iterdir()} != payloads:
        raise BundleError('unexpected_bundle_files')
    return manifest


def restore_bundle(bundle, destination):
    """Materialize numbered exercise files only; never restores operational names."""
    root = _path(bundle, exists=True)
    manifest = verify_bundle(root)
    output = _destination(destination, forbidden=[root])
    total = sum(row['bytes'] for row in manifest['files'])
    if shutil.disk_usage(output.parent).free < max(64 * 1024 * 1024, total * 2):
        raise BundleError('insufficient_disk_headroom')
    output.mkdir(mode=0o700)
    files = output / 'isolated-files'; files.mkdir(mode=0o700)
    for row in manifest['files']:
        data, _ = _read(root / row['payload'])
        if len(data) != row['bytes'] or _digest(data) != row['sha256']:
            raise BundleError('bundle_changed_during_restore')
        _write(files / Path(row['payload']).name, data)
    for row in manifest['files']:
        copied, _ = _read(files / Path(row['payload']).name)
        if len(copied) != row['bytes'] or _digest(copied) != row['sha256']:
            raise BundleError('restored_payload_mismatch')
    # Validate manifest and full source again; changes invalidate this exercise.
    if verify_bundle(root) != manifest:
        raise BundleError('bundle_changed_during_restore')
    result = {'format_version': 1, 'isolated_restore_verified': True,
              'manifest_sha256': _digest(_json(manifest)), 'file_count': len(manifest['files']),
              'recovery_authorized': False, 'execution_authorized': False,
              'stopped_writer_verified': False}
    _sync(files)
    _write(output / 'source-manifest.json', _json(manifest))
    _write(output / 'restore-verification.json', _json(result))
    _sync(output); _sync(output.parent)
    return result
