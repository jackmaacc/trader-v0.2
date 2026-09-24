from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from trader_engine.research.protocol_registry import freeze_registry, registry_hash, validate_registry, verify_registry

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


@pytest.fixture
def registry():
    return json.loads((ROOT / 'config/research/phase3_protocols.json').read_text())


@pytest.fixture
def sources(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('immutable model definition\n')
    return {'model': source}


def test_registry_and_key_order_hash(registry):
    validate_registry(registry)
    assert registry_hash(registry) == registry_hash(dict(reversed(list(registry.items()))))


def test_freeze_verify_private_and_exclusive(tmp_path, registry, sources):
    target = tmp_path / 'freeze.json'
    freeze_registry(registry, target, sources, now=NOW)
    assert target.stat().st_mode & 0o777 == 0o600
    result = verify_registry(target, registry, sources)
    assert result['verified'] and not result['prospective_registered'] and not result['execution_authorized']
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        freeze_registry(registry, target, sources, now=NOW)
    assert before == target.read_bytes()


def test_tamper_receipt(tmp_path, registry, sources):
    target = tmp_path / 'freeze.json'
    freeze_registry(registry, target, sources, now=NOW)
    receipt = json.loads(target.read_text())
    receipt['live_authorized'] = True
    target.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_registry(target, registry, sources)


def test_changed_source_or_inventory(tmp_path, registry, sources):
    target = tmp_path / 'freeze.json'
    freeze_registry(registry, target, sources, now=NOW)
    with pytest.raises(ValueError, match='Source'):
        verify_registry(target, registry, {'other': sources['model']})
    sources['model'].write_text('changed')
    with pytest.raises(ValueError, match='Source'):
        verify_registry(target, registry, sources)


def test_changed_rules(tmp_path, registry, sources):
    target = tmp_path / 'freeze.json'
    freeze_registry(registry, target, sources, now=NOW)
    registry['tracks'][0]['rules']['signal'] += ' Retune.'
    with pytest.raises(ValueError, match='Registry changed'):
        verify_registry(target, registry, sources)


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(execution_authorized=True),
    lambda r: r.update(live_authorized=None),
    lambda r: r.update(minimum_observations=125),
    lambda r: r.update(minimum_closed_episodes=29),
    lambda r: r.update(retired=[]),
    lambda r: r['inference'].update(alpha=.05),
    lambda r: r['inference'].update(resamples=100),
    lambda r: r['tracks'].append(deepcopy(r['tracks'][0])),
    lambda r: r['tracks'][0].update(id='MR30'),
    lambda r: r['tracks'][0]['costs'].update(stress=''),
    lambda r: r['tracks'][0]['benchmarks'].update(primary=''),
    lambda r: r['tracks'][0].update(universe=[]),
    lambda r: r['tracks'][0]['risk'].update(bad=float('nan')),
])
def test_invalid_registry(registry, mutation):
    mutation(registry)
    with pytest.raises(ValueError):
        validate_registry(registry)


def test_late_naive_and_symlink_freeze(tmp_path, registry, sources):
    target = tmp_path / 'freeze.json'
    for stamp in (datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 9, 24)):
        with pytest.raises(ValueError):
            freeze_registry(registry, target, sources, now=stamp)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        freeze_registry(registry, link / 'freeze.json', sources, now=NOW)
    assert not target.exists()


@pytest.mark.parametrize('field,value', [
    ('rules', True), ('rules', {'signal': True, 'sizing': 'fixed'}),
    ('rules', {'signal': 'fixed'}), ('risk', 'anything'),
    ('risk', {'research_capital': True, 'drawdown_reject': .05, 'policy': 'fixed'}),
    ('risk', {'research_capital': 100000, 'drawdown_reject': 'none', 'policy': 'fixed'}),
    ('costs', 1), ('benchmarks', ['primary']), ('hypothesis', True),
    ('rejection_gates', True), ('data_prerequisites', 1),
    ('data_prerequisites', ['']), ('rejection_gates', ['valid', None]),
    ('universe', [['SPY']]), ('asset_class', {}),
])
def test_malformed_track_types_fail_before_freeze(tmp_path, registry, sources, field, value):
    registry['tracks'][0][field] = value
    target = tmp_path / 'malformed.json'
    with pytest.raises(ValueError):
        freeze_registry(registry, target, sources, now=NOW)
    assert not target.exists()


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('minimum_observations', 126.0),
    ('minimum_closed_episodes', '30'), ('tracks', [True]), ('tracks', 'equities'),
    ('reserved_window', True), ('reserved_window', {'start': 1}),
    ('inference', True), ('retired', [{'MR30': True}]), ('activation_gate', True),
])
def test_malformed_top_level_types(registry, field, value):
    registry[field] = value
    with pytest.raises(ValueError):
        validate_registry(registry)


@pytest.mark.parametrize('field,value', [
    ('alpha', '.01'), ('resamples', 100000.0), ('block_lengths', [5.0, 10]),
    ('seed', True), ('family_slots', 6.0), ('method', 123),
])
def test_malformed_inference_types(registry, field, value):
    registry['inference'][field] = value
    with pytest.raises(ValueError):
        validate_registry(registry)


@pytest.mark.parametrize('value', [None, True, [], 'registry', 123])
def test_malformed_root(value):
    with pytest.raises(ValueError):
        validate_registry(value)
