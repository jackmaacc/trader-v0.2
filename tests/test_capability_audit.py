from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import pytest
from trader_engine.operations.capability_audit import build_capability_audit, write_capability_report, SOURCE_PATHS

NOW = datetime(2026, 9, 24, 5, tzinfo=timezone.utc)


def save(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def artifacts(tmp_path):
    for name, relative in SOURCE_PATHS.items():
        value = {'checked_at': NOW.isoformat(), 'status': 'running', 'errors': []}
        if name == 'catalog':
            value.update(stale=False, records=[{'symbol': 'SPY', 'asset_class': 'us_equity', 'tradable': True}, {'symbol': 'BTC/USD', 'asset_class': 'crypto', 'tradable': True}], last_success_at={'us_equity': NOW.isoformat(), 'crypto': NOW.isoformat()})
        if name in {'sip', 'opra'}:
            value.update(status='subscribed_idle', feed=name, connected=True, authenticated=True, subscribed=True, last_data_at=None, fresh_counts={'bars': 0, 'quotes': 0})
        if name == 'research':
            value['broker_execution_enabled'] = False
        if name == 'options':
            value['contracts'] = [{'symbol': 'SPY260924C00100000', 'tradable': True}]
        if name in {'crypto_status', 'crypto_state'}:
            value.update(mode='paper', ownership={'BTC/USD': '0.1'}, pending=None, account={'status': 'ACTIVE', 'trading_blocked': False, 'account_blocked': False})
        save(tmp_path, relative, value)
    return tmp_path


def test_idle_authentication_is_not_data_or_account_permission(artifacts):
    report = build_capability_audit(artifacts, now=NOW)
    options = report['asset_classes']['options']
    assert options['market_data']['current_subscription_evidence'] is True
    assert options['market_data']['last_market_data']['state'] == 'missing_or_invalid'
    assert options['account_permission'] == 'unknown'
    assert not report['execution_authorized'] and not report['live_approved']
    assert report['asset_classes']['crypto']['saved_paper_owner_healthy'] is True


def test_missing_and_malformed_sources_never_green(tmp_path):
    save(tmp_path, SOURCE_PATHS['catalog'], {'records': ['wrong', {'symbol': 'X', 'asset_class': []}]})
    save(tmp_path, SOURCE_PATHS['sip'], [])
    report = build_capability_audit(tmp_path, now=NOW)
    assert report['sources']['catalog']['invalid_rows'] == 2
    assert report['sources']['sip']['availability'] == 'unreadable_or_malformed'
    assert not report['asset_classes']['crypto']['saved_paper_owner_healthy']
    assert all(not row['ready_for_live'] for row in report['asset_classes'].values())


@pytest.mark.parametrize('seconds,state', [(601, 'stale'), (-1, 'future')])
def test_source_time_validation(artifacts, seconds, state):
    path = artifacts / SOURCE_PATHS['sip']
    raw = json.loads(path.read_text()); raw['checked_at'] = (NOW-timedelta(seconds=seconds)).isoformat()
    path.write_text(json.dumps(raw))
    report = build_capability_audit(artifacts, now=NOW)
    assert report['sources']['sip']['heartbeat']['state'] == state
    assert not report['asset_classes']['equities_etfs']['market_data']['current_subscription_evidence']


def test_catalog_real_discovery_age_and_duplicates(artifacts):
    path = artifacts / SOURCE_PATHS['catalog']
    raw = json.loads(path.read_text()); raw['last_success_at']['us_equity'] = (NOW-timedelta(days=2)).isoformat()
    raw['records'].append(raw['records'][0]); path.write_text(json.dumps(raw))
    report = build_capability_audit(artifacts, now=NOW)
    row = report['asset_classes']['equities_etfs']
    assert row['catalog']['listed_count'] == 1
    assert 'catalog_evidence_incomplete_or_stale' in row['blockers']


def account_file(root, **updates):
    data = {'checked_at': NOW.isoformat(), 'mode': 'paper', 'account_binding_verified': True,
            'account': {'status': 'ACTIVE', 'options_approved_level': 3, 'options_trading_level': 3, 'crypto_status': 'ACTIVE', 'trading_blocked': False, 'account_blocked': False, 'trade_suspended_by_user': False},
            'configuration': {}}
    data.update(updates)
    return save(root, 'account.json', data)


def test_explicit_paper_permissions_remain_separate_from_strategy(artifacts):
    report = build_capability_audit(artifacts, now=NOW, account_snapshot=account_file(artifacts))
    assert report['asset_classes']['options']['account_permission'] == 'paper_level_reported_scope_requires_validation'
    assert report['asset_classes']['options']['strategy_approval'] == 'none'
    assert report['asset_classes']['crypto']['account_permission'] == 'paper_crypto_active_observed'


@pytest.mark.parametrize('updates', [{'account_binding_verified': False}, {'mode': 'live'}, {'checked_at': (NOW-timedelta(minutes=11)).isoformat()}])
def test_unbound_stale_or_other_account_not_permissions(artifacts, updates):
    report = build_capability_audit(artifacts, now=NOW, account_snapshot=account_file(artifacts, **updates))
    assert report['asset_classes']['options']['account_permission'] == 'unknown'


@pytest.mark.parametrize('config', [{'suspend_trade': True}, {'closing_transactions_only': True}])
def test_config_veto(artifacts, config):
    report = build_capability_audit(artifacts, now=NOW, account_snapshot=account_file(artifacts, configuration=config))
    assert report['asset_classes']['options']['account_permission'] == 'blocked'


def test_zero_options_permission_and_secret_allowlist(artifacts):
    path = account_file(artifacts, account={'status': 'ACTIVE', 'options_approved_level': 0, 'options_trading_level': 0, 'id': 'PRIVATE_ID', 'secret_key': 'SECRET_KEY', 'trading_blocked': False, 'account_blocked': False, 'trade_suspended_by_user': False}, arbitrary='PRIVATE_DATA')
    report = build_capability_audit(artifacts, now=NOW, account_snapshot=path)
    assert report['asset_classes']['options']['account_permission'] == 'not_enabled'
    payload = json.dumps(report)
    assert all(text not in payload for text in ['PRIVATE_ID', 'SECRET_KEY', 'PRIVATE_DATA'])


def test_ownership_disagreement_and_pending_block_health(artifacts):
    path = artifacts / SOURCE_PATHS['crypto_state']
    raw = json.loads(path.read_text()); raw['ownership']['BTC/USD'] = '0.2'; raw['pending'] = {'secret': 'not emitted'}
    path.write_text(json.dumps(raw))
    report = build_capability_audit(artifacts, now=NOW)
    assert not report['asset_classes']['crypto']['saved_paper_owner_healthy']
    assert report['asset_classes']['crypto']['ownership_evidence'] == 'disagreement'
    assert 'not emitted' not in json.dumps(report)


def test_report_new_output_only_and_sources_unchanged(artifacts):
    source = artifacts / SOURCE_PATHS['catalog']; before = source.read_bytes()
    report = build_capability_audit(artifacts, now=NOW)
    output = artifacts / 'audit'
    write_capability_report(report, output)
    assert (output / 'report.json').stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == before
    with pytest.raises(FileExistsError):
        write_capability_report(report, output)
    link = artifacts / 'link'; link.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError):
        write_capability_report(report, link / 'nested')


@pytest.mark.parametrize('account', [None, [], {}, {'status': 'INACTIVE', 'trading_blocked': False, 'account_blocked': False}])
def test_malformed_or_inactive_paper_owner_account_fails_closed(artifacts, account):
    path = artifacts / SOURCE_PATHS['crypto_status']
    raw = json.loads(path.read_text()); raw['account'] = account; path.write_text(json.dumps(raw))
    report = build_capability_audit(artifacts, now=NOW)
    assert not report['asset_classes']['crypto']['saved_paper_owner_healthy']


def test_options_inactive_account_not_positive_permission(artifacts):
    path = account_file(artifacts)
    raw = json.loads(path.read_text()); raw['account']['status'] = 'INACTIVE'; path.write_text(json.dumps(raw))
    report = build_capability_audit(artifacts, now=NOW, account_snapshot=path)
    assert report['asset_classes']['options']['account_permission'] == 'unknown'
