"""Offline design registry: a hash records intent, never qualification or authority."""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping


RETIRED = {'MR30', 'MR60', 'MOM20', 'MOM60'}


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON; reject NaN/infinity rather than hashing invalid JSON."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value or any(not isinstance(k, str) or not k.strip() for k in value):
        raise ValueError(f'{label} must be a nonempty string-keyed mapping')
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')
    return value


def _texts(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{label} must be a nonempty list of text')
    for item in value:
        _text(item, label)
    return value


def _text_mapping(value: Any, label: str, required: tuple[str, ...]) -> Mapping[str, Any]:
    fields = _mapping(value, label)
    for key in set(fields) | set(required):
        _text(fields.get(key), f'{label}.{key}')
    return fields


def validate_registry(registry: Mapping[str, Any]) -> None:
    """Validate registration structure, not executable semantics or data readiness."""
    registry = _mapping(registry, 'registry')
    try:
        canonical_bytes(registry)
    except (TypeError, ValueError) as exc:
        raise ValueError('Registry must contain finite JSON values') from exc
    if type(registry.get('schema_version')) is not int or registry['schema_version'] != 1 or registry.get('status') != 'design_only':
        raise ValueError('Only schema 1 design-only registries are supported')
    if any(registry.get(key) is not False for key in ('execution_authorized', 'live_authorized')):
        raise ValueError('Designs cannot authorize execution')
    if (type(registry.get('minimum_observations')) is not int or registry['minimum_observations'] != 126
            or type(registry.get('minimum_closed_episodes')) is not int or registry['minimum_closed_episodes'] < 30):
        raise ValueError('126 observations and at least 30 closed episodes required')
    retired = _texts(registry.get('retired'), 'retired')
    if len(retired) != len(RETIRED) or set(retired) != RETIRED:
        raise ValueError('Retired candidates must remain recorded')
    window = _mapping(registry.get('reserved_window'), 'reserved_window')
    start = date.fromisoformat(_text(window.get('start'), 'window.start'))
    end = date.fromisoformat(_text(window.get('end'), 'window.end'))
    _text(window.get('selection'), 'window.selection')
    if start >= end:
        raise ValueError('Invalid reserved window')
    inference = _mapping(registry.get('inference'), 'inference')
    if (type(inference.get('family_slots')) is not int or inference['family_slots'] != 6
            or type(inference.get('alpha')) not in (int, float) or not 0 < inference['alpha'] <= .05 / 6
            or type(inference.get('resamples')) is not int or inference['resamples'] < 100000
            or not isinstance(inference.get('block_lengths'), list)
            or any(type(n) is not int for n in inference['block_lengths'])
            or inference['block_lengths'] != [5, 10]
            or type(inference.get('seed')) is not int):
        raise ValueError('Invalid multiple-testing or dependence protocol')
    _text(inference.get('method'), 'inference.method')
    tracks = registry.get('tracks')
    if not isinstance(tracks, list) or not tracks:
        raise ValueError('Tracks must be a nonempty list')
    counts = dict.fromkeys(('equities', 'options', 'crypto'), 0)
    seen = set()
    for track in tracks:
        track = _mapping(track, 'track')
        ident = _text(track.get('id'), 'track.id')
        asset = _text(track.get('asset_class'), 'track.asset_class')
        if ident in seen or ident in RETIRED:
            raise ValueError('Duplicate or retired hypothesis ID')
        if asset not in counts:
            raise ValueError('Unsupported asset class')
        seen.add(ident)
        counts[asset] += 1
        universe = _texts(track.get('universe'), 'universe')
        if len(set(universe)) != len(universe):
            raise ValueError('Unique universe required')
        _text(track.get('hypothesis'), 'hypothesis')
        _text_mapping(track.get('rules'), 'rules', ('signal', 'sizing'))
        _text_mapping(track.get('costs'), 'costs', ('base', 'stress'))
        _text_mapping(track.get('benchmarks'), 'benchmarks', ('passive', 'primary', 'match_gate'))
        risk = _mapping(track.get('risk'), 'risk')
        if type(risk.get('research_capital')) not in (int, float) or risk['research_capital'] <= 0:
            raise ValueError('Positive numeric research capital required')
        if type(risk.get('drawdown_reject')) not in (int, float) or not 0 < risk['drawdown_reject'] <= 1:
            raise ValueError('Drawdown rejection fraction must be in (0,1]')
        _text(risk.get('policy'), 'risk.policy')
        _texts(track.get('data_prerequisites'), 'data_prerequisites')
        _texts(track.get('rejection_gates'), 'rejection_gates')
    if any(not 1 <= count <= 2 for count in counts.values()):
        raise ValueError('Each track requires one or two hypotheses')
    _text(registry.get('legacy_contract'), 'legacy_contract')
    _text(registry.get('activation_gate'), 'activation_gate')


def registry_hash(registry: Mapping[str, Any]) -> str:
    validate_registry(registry)
    return sha256(canonical_bytes(registry)).hexdigest()


def _source_hashes(sources: Mapping[str, str | Path]) -> dict[str, str]:
    if not sources:
        raise ValueError('Source files must be bound to the design freeze')
    result = {}
    for name, path in sources.items():
        if not isinstance(name, str) or not name:
            raise ValueError('Named sources required')
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            raise ValueError(f'Not a regular source: {name}')
        result[name] = sha256(p.read_bytes()).hexdigest()
    return result


def freeze_registry(registry: Mapping[str, Any], destination: str | Path,
                    sources: Mapping[str, str | Path], *, now: datetime | None = None) -> dict:
    """Create one exclusive 0600 receipt before start; never overwrite existing evidence.

    ``now`` is injectable for tests, not a trusted timestamp/signature. Calendar/data
    readiness and independently verified prospective registration remain separate.
    """
    digest = registry_hash(registry)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError('Timezone-aware freeze timestamp required')
    stamp = stamp.astimezone(timezone.utc)
    if stamp.date() >= date.fromisoformat(registry['reserved_window']['start']):
        raise ValueError('Cannot freeze this prospective design at or after its start')
    body = {'schema_version': 1, 'status': 'design_only', 'frozen_at': stamp.isoformat(),
            'registry': registry, 'registry_sha256': digest, 'sources': _source_hashes(sources),
            'prospective_registered': False, 'execution_authorized': False, 'live_authorized': False}
    receipt = {**body, 'receipt_sha256': sha256(canonical_bytes(body)).hexdigest()}
    target = Path(destination).absolute()
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError('Symlink paths are not valid freeze destinations')
    # Parent must already exist: no hidden directory creation or existing-file writes.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(canonical_bytes(receipt) + b'\n')
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return receipt


def verify_registry(receipt_path: str | Path, registry: Mapping[str, Any],
                    sources: Mapping[str, str | Path]) -> dict:
    """Verify content/source integrity only, never infer investment qualification."""
    receipt = json.loads(Path(receipt_path).read_text())
    claimed = receipt.pop('receipt_sha256', None)
    if claimed != sha256(canonical_bytes(receipt)).hexdigest():
        raise ValueError('Receipt hash mismatch')
    if receipt.get('schema_version') != 1 or receipt.get('status') != 'design_only':
        raise ValueError('Invalid receipt scope')
    if any(receipt.get(k) is not False for k in ('prospective_registered', 'execution_authorized', 'live_authorized')):
        raise ValueError('Freeze does not confer authority')
    stamp = datetime.fromisoformat(receipt['frozen_at'])
    if stamp.tzinfo is None or stamp.astimezone(timezone.utc).date() >= date.fromisoformat(registry['reserved_window']['start']):
        raise ValueError('Invalid prospective freeze timestamp')
    if receipt.get('registry_sha256') != registry_hash(registry) or canonical_bytes(receipt.get('registry')) != canonical_bytes(registry):
        raise ValueError('Registry changed after freeze')
    if receipt.get('sources') != _source_hashes(sources):
        raise ValueError('Source content or inventory changed after freeze')
    return {'verified': True, 'status': 'design_only', 'registry_sha256': registry_hash(registry),
            'prospective_registered': False, 'execution_authorized': False, 'live_authorized': False}
