"""Saved-evidence capability inventory. No network, broker methods or authority."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path

SOURCE_PATHS = {
    'catalog': 'continuous_market_scan/instruments.json',
    'scanner': 'continuous_market_scan/status.json',
    'sip': 'plus_stream/status.json', 'opra': 'plus_options_stream/status.json',
    'research': 'plus_research/status.json', 'options': 'plus_research/options.json',
    'crypto_status': 'paper_crypto_service/status.json',
    'crypto_state': 'paper_crypto_service/state.json',
}
STATES = {'running', 'healthy', 'scanning', 'evaluating', 'degraded', 'streaming',
          'subscribed_idle', 'error', 'shutdown', 'needs_attention', 'connecting',
          'disconnected', 'stopped', 'authenticating'}
CLASSES = ('equities_etfs', 'options', 'crypto', 'futures', 'forex')


def utc(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone required')
    return stamp.astimezone(timezone.utc)


def _read(path):
    try:
        if path.is_symlink() or not path.is_file():
            return {}, {'availability': 'missing_or_not_regular'}
        if path.stat().st_size > 40_000_000:
            return {}, {'availability': 'oversized'}
        data = path.read_bytes()
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError('object required')
        return raw, {'availability': 'present', 'sha256': hashlib.sha256(data).hexdigest()}
    except (OSError, ValueError, UnicodeError):
        return {}, {'availability': 'unreadable_or_malformed'}


def _stamp(value, now, threshold=600):
    try:
        stamp = utc(value)
        age = (now - stamp).total_seconds()
        return {'timestamp': stamp.isoformat(), 'age_seconds': round(age, 3),
                'state': 'future' if age < 0 else 'stale' if age > threshold else 'recent'}
    except (ValueError, TypeError, OverflowError):
        return {'timestamp': None, 'age_seconds': None, 'state': 'missing_or_invalid'}


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _source(name, raw, evidence, now):
    result = dict(evidence)
    result['issues'] = []
    result['service_state'] = raw.get('status') if isinstance(raw.get('status'), str) and raw['status'] in STATES else 'unknown'
    if name == 'crypto_state':
        result['heartbeat'] = {'state': 'not_recorded'}
    else:
        result['heartbeat'] = _stamp(raw.get('checked_at'), now, 180 if name == 'crypto_status' else 600)
    if raw.get('error') or raw.get('errors'):
        result['issues'].append('reported_errors')
    if (name in {'scanner', 'sip', 'opra', 'research', 'crypto_status'}
            and result['service_state'] not in {'running', 'healthy', 'scanning', 'evaluating', 'degraded', 'streaming', 'subscribed_idle'}):
        result['issues'].append('service_not_healthy')
    if evidence['availability'] != 'present':
        result['issues'].append('source_unavailable')
    if result['heartbeat']['state'] not in {'recent', 'not_recorded'}:
        result['issues'].append('heartbeat_' + result['heartbeat']['state'])
    return result


def _stream(raw, source, expected_feed, now):
    authenticated = raw.get('authenticated') is True
    active = all(raw.get(k) is True for k in ('connected', 'authenticated', 'subscribed'))
    recent = source['heartbeat']['state'] == 'recent' and not source['issues']
    correct_feed = raw.get('feed') == expected_feed
    fresh = raw.get('fresh_counts') if isinstance(raw.get('fresh_counts'), dict) else {}
    return {
        'feed': expected_feed, 'feed_matches': correct_feed,
        'authenticated_observed': authenticated,
        'subscription_observed': active and correct_feed,
        'current_subscription_evidence': bool(recent and active and correct_feed),
        'last_market_data': _stamp(raw.get('last_data_at'), now),
        'fresh_bars_reported': _count(fresh.get('bars')),
        'fresh_quotes_reported': _count(fresh.get('quotes')),
        'complete_market_coverage': False,
        'note': 'Authentication and idle subscriptions do not prove fresh quotes or account trading permission.',
    }


def _catalog(raw, now):
    result = {name: {'listed_count': 0, 'tradable_flag_count': 0} for name in CLASSES}
    rows = raw.get('records')
    invalid = 0
    seen = set()
    if not isinstance(rows, list):
        rows = []; invalid = 1
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1; continue
        asset = row.get('asset_class')
        group = {'us_equity': 'equities_etfs', 'crypto': 'crypto', 'futures': 'futures'}.get(asset) if isinstance(asset, str) else None
        symbol = row.get('symbol')
        if group is None or not isinstance(symbol, str) or not symbol.strip() or type(row.get('tradable')) is not bool or (group, symbol) in seen:
            invalid += 1; continue
        seen.add((group, symbol))
        result[group]['listed_count'] += 1
        result[group]['tradable_flag_count'] += int(row['tradable'])
    last_success = raw.get('last_success_at') if isinstance(raw.get('last_success_at'), dict) else {}
    for group, key in [('equities_etfs', 'us_equity'), ('crypto', 'crypto')]:
        result[group]['last_success'] = _stamp(last_success.get(key), now, 86400)
    return result, invalid


def _permission(account, source, envelope):
    current = (source['heartbeat']['state'] == 'recent' and not source['issues']
               and envelope.get('mode') == 'paper'
               and envelope.get('account_binding_verified') is True
               and all(type(account.get(k)) is bool for k in ('trading_blocked', 'account_blocked', 'trade_suspended_by_user')))
    config = envelope.get('configuration') if isinstance(envelope.get('configuration'), dict) else {}
    blocked = any(account.get(key) is True for key in ('trading_blocked', 'account_blocked', 'trade_suspended_by_user'))
    blocked = blocked or any(config.get(key) is True for key in ('closing_transactions_only', 'suspend_trade'))
    active = account.get('status') == 'ACTIVE'
    return {'evidence_current': current, 'reported_account_active': active,
            'reported_account_blocked': blocked,
            'general_account_state': 'blocked' if blocked and current else 'active_observed' if active and current else 'unknown'}


def _ownership(status, state):
    if not isinstance(status.get('ownership'), dict) or not isinstance(state.get('ownership'), dict):
        return 'missing_or_invalid'
    def normalized(raw):
        parsed = {}
        for symbol, value in raw.items():
            if symbol not in {'BTC/USD', 'ETH/USD'}:
                raise ValueError('unexpected ownership')
            qty = Decimal(str(value))
            if not qty.is_finite() or qty < 0:
                raise ValueError('invalid quantity')
            parsed[symbol] = qty
        return parsed
    try:
        matching = normalized(status['ownership']) == normalized(state['ownership'])
    except (InvalidOperation, ValueError, TypeError):
        return 'missing_or_invalid'
    return 'matching_saved_evidence' if matching else 'disagreement'


def build_capability_audit(artifacts, *, now=None, account_snapshot=None):
    """Read allowlisted files only; optional account envelope: checked_at, mode, account.

    A mode of paper/live records provenance only. Permissions require explicit fields;
    OPRA access never infers options trading approval. Reads are not an atomic snapshot.
    """
    artifacts = Path(artifacts)
    acquired = {name: _read(artifacts / path) for name, path in SOURCE_PATHS.items()}
    if account_snapshot is not None:
        acquired['account_snapshot'] = _read(Path(account_snapshot))
    # Capture after acquisition: fresh publishers must not look future-dated merely
    # because they published during our read interval.
    now = utc(now) if now is not None else datetime.now(timezone.utc)
    raw = {name: pair[0] for name, pair in acquired.items()}
    sources = {name: _source(name, value, evidence, now) for name, (value, evidence) in acquired.items()}
    counts, invalid_rows = _catalog(raw['catalog'], now)
    sources['catalog']['invalid_rows'] = invalid_rows
    if invalid_rows:
        sources['catalog']['issues'].append('invalid_catalog_rows')
    if raw['catalog'].get('stale') is not False:
        sources['catalog']['issues'].append('catalog_not_confirmed_current')
    account_name = 'account_snapshot' if account_snapshot is not None else 'crypto_status'
    envelope = raw[account_name]
    account = envelope.get('account') if isinstance(envelope.get('account'), dict) else {}
    account_health = _permission(account, sources[account_name], envelope)
    account_health['source'] = account_name
    account_health['mode'] = envelope.get('mode') if envelope.get('mode') in ('paper', 'live') else 'unknown'
    assets = {}
    for name in CLASSES:
        blockers = ['no_live_approval']
        if name != 'crypto':
            blockers.append('no_approved_strategy')
        if name in ('equities_etfs', 'crypto'):
            if sources['catalog']['issues'] or counts[name]['last_success']['state'] != 'recent':
                blockers.append('catalog_evidence_incomplete_or_stale')
        permission = 'unknown'
        if account_health['reported_account_blocked'] and account_health['evidence_current']:
            permission = 'blocked'
        elif name == 'options' and account_health['evidence_current'] and account_health['reported_account_active']:
            approved, enabled = account.get('options_approved_level'), account.get('options_trading_level')
            if type(approved) is int and type(enabled) is int and min(approved, enabled) >= 0:
                permission = 'not_enabled' if min(approved, enabled) == 0 else 'paper_level_reported_scope_requires_validation'
        elif account_health['evidence_current'] and account_health['reported_account_active'] and name == 'crypto':
            permission = 'paper_crypto_active_observed' if account.get('crypto_status') == 'ACTIVE' else 'unknown'
        elif account_health['evidence_current'] and account_health['reported_account_active'] and name == 'equities_etfs':
            permission = 'paper_account_active_instrument_eligibility_unverified'
        if permission == 'unknown':
            blockers.append('account_permission_unverified')
        elif permission in {'blocked', 'not_enabled'}:
            blockers.append('account_permission_denied')
        assets[name] = {'catalog': counts[name], 'account_permission': permission,
                        'execution_adapter': 'not_configured', 'strategy_approval': 'none',
                        'market_data': {}, 'instrument_flags': 'fractional_short_margin_session_flags_not_retained', 'blockers': blockers, 'ready_for_live': False}
    for name, stream, feed in [('equities_etfs', 'sip', 'sip'), ('options', 'opra', 'opra')]:
        assets[name]['market_data'] = _stream(raw[stream], sources[stream], feed, now)
        if not assets[name]['market_data']['current_subscription_evidence']:
            assets[name]['blockers'].append('current_stream_subscription_unverified')
    assets['equities_etfs']['execution_adapter'] = 'legacy_equity_adapter_present_not_enabled_in_continuous_pipeline'
    assets['options']['execution_adapter'] = 'lifecycle_adapter_not_enabled'
    levels = {k: _count(account.get(k)) for k in ('options_approved_level', 'options_trading_level')}
    assets['options']['reported_permission_levels'] = levels
    contracts = raw['options'].get('contracts')
    valid_contracts = [x for x in contracts if isinstance(x, dict) and isinstance(x.get('symbol'), str) and type(x.get('tradable')) is bool] if isinstance(contracts, list) else []
    assets['options']['catalog'] = {'listed_count': len(valid_contracts), 'tradable_flag_count': sum(x['tradable'] for x in valid_contracts), 'scope': 'saved_research_sample_only', 'complete_market_catalog': False}
    if not isinstance(contracts, list) or len(valid_contracts) != len(contracts):
        sources['options']['issues'].append('invalid_contract_catalog')
    if sources['options']['issues']:
        assets['options']['blockers'].append('contract_research_evidence_incomplete_or_stale')
    sources['research']['execution_disabled_observed'] = raw['research'].get('broker_execution_enabled') is False
    if not sources['research']['execution_disabled_observed']:
        sources['research']['issues'].append('research_execution_boundary_unverified')
    crypto = assets['crypto']
    crypto['execution_adapter'] = 'existing_single_paper_owner_btc_eth_only'
    crypto['strategy_approval'] = 'existing_btc_eth_sma200_paper_experiment_only'
    crypto['market_data'] = {'scope': 'scanner_snapshots_and_daily_strategy_evidence', 'complete_market_coverage': False,
                             'data_issues_reported': bool(raw['crypto_status'].get('data_issues'))}
    ownership = _ownership(raw['crypto_status'], raw['crypto_state'])
    crypto['ownership_evidence'] = ownership
    crypto['pending_order_observed'] = bool(raw['crypto_status'].get('pending') or raw['crypto_state'].get('pending'))
    paper_account = raw['crypto_status'].get('account') if isinstance(raw['crypto_status'].get('account'), dict) else {}
    crypto['saved_paper_owner_healthy'] = bool(
        not sources['crypto_status']['issues'] and not sources['crypto_state']['issues']
        and raw['crypto_status'].get('status') == 'running' and raw['crypto_status'].get('mode') == 'paper'
        and paper_account.get('status') == 'ACTIVE'
        and all(paper_account.get(k) is False for k in ('trading_blocked', 'account_blocked'))
        and ownership == 'matching_saved_evidence' and not crypto['pending_order_observed']
        and not crypto['market_data']['data_issues_reported'])
    if not crypto['saved_paper_owner_healthy']:
        crypto['blockers'].append('paper_owner_evidence_needs_review')
    for name in ('futures', 'forex'):
        assets[name]['account_permission'] = 'unsupported_by_current_integration'
        assets[name]['market_data'] = {'scope': 'indicative_continuous_proxies_only' if name == 'futures' else 'not_configured', 'executable_feed': False}
        assets[name]['blockers'].append('no_executable_feed_or_adapter')
    scanner = raw['scanner']
    coverage = scanner.get('coverage') if isinstance(scanner.get('coverage'), dict) else {}
    states = coverage.get('by_state') if isinstance(coverage.get('by_state'), dict) else {}
    sources['scanner']['coverage_counts'] = {k: _count(states.get(k)) for k in ('fresh', 'stale', 'missing', 'pending', 'closed_last_observation', 'indicative_unknown_latency')}
    return {'schema_version': 1, 'observed_at': now.isoformat(), 'sources': sources,
            'account_health': account_health, 'asset_classes': assets,
            'execution_authorized': False, 'live_approved': False,
            'limitations': ['Saved local evidence only; no broker verification or new subscriptions.',
                           'Reads across files are not atomic; discrepancies require review.',
                           'Catalog tradable flags are not account eligibility or strategy approval.',
                           'The us_equity catalog includes other securities and OTC listings; ETF metadata and fractional/short/margin/session flags are not retained.',
                           'BOATS overnight routing is not configured; account flags cannot prove instrument session eligibility.',
                           'Existing paper authority is documented scope, not approval granted by this report.',
                           'Service heartbeats are not market-data timestamps; closed-market idle is not an outage.']}


def render_markdown(report):
    lines = ['# Saved market capability audit', '', 'Observed: ' + report['observed_at'], '',
             'Read-only evidence. No execution authority or live approval.', '',
             '| Asset class | Listed / tradable flags | Account permission | Execution adapter | Strategy scope |',
             '| --- | --- | --- | --- | --- |']
    for name, row in report['asset_classes'].items():
        catalog = row['catalog']
        lines.append(f"| {name} | {catalog['listed_count']} / {catalog['tradable_flag_count']} | {row['account_permission']} | {row['execution_adapter']} | {row['strategy_approval']} |")
    lines += ['', '## Source evidence', '']
    for name, source in report['sources'].items():
        lines.append(f"- {name}: {source['availability']}; heartbeat {source['heartbeat']['state']}; issues: {', '.join(source['issues']) or 'none recorded'}.")
    lines += ['', '## Remaining gates', '']
    for name, row in report['asset_classes'].items():
        lines.append(f"- {name}: {', '.join(row['blockers'])}.")
    lines += ['', '## Limitations', ''] + ['- ' + note for note in report['limitations']]
    return '\n'.join(lines) + '\n'


def write_capability_report(report, destination):
    """Create new output only. Never overwrite or update source/runtime artifacts."""
    destination = Path(destination)
    # Parents must already exist; no traversing symlinked output ancestors.
    if any(part in {Path(value).parts[0] for value in SOURCE_PATHS.values()} for part in destination.parts):
        raise ValueError('runtime source directories cannot contain reports')
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError('symlink output refused')
    destination.mkdir(mode=0o700)
    for name, value in [('report.json', json.dumps(report, indent=2, sort_keys=True) + '\n'), ('REPORT.md', render_markdown(report))]:
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
    return destination
