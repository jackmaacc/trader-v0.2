from datetime import datetime, timedelta, timezone
import json

import pytest
from streamlit.testing.v1 import AppTest

from trader_engine.ui.market_scanner import scanner_view

NOW = datetime(2026, 9, 24, 3, tzinfo=timezone.utc)


def save(root, name, payload):
    (root/name).write_text(json.dumps(payload))


def fixture(root, now=NOW):
    stamp = now.isoformat()
    save(root, 'status.json', {'checked_at': stamp, 'status': 'healthy', 'progress': {'processed': 2, 'total': 2}, 'sources': {'equities': {'status': 'closed', 'market_open': False, 'feed': 'iex'}}, 'coverage': {'total': 2, 'by_state': {'closed': 1, 'fresh': 1}}})
    save(root, 'latest.json', {'checked_at': stamp, 'records': [
        {'symbol': 'SPY', 'asset_class': 'equity', 'source': 'IEX', 'state': 'closed', 'data_asof': (now-timedelta(hours=8)).isoformat(), 'change_pct': 2.5},
        {'symbol': 'BTC/USD', 'asset_class': 'crypto', 'source': 'Alpaca', 'state': 'fresh', 'data_asof': stamp, 'change_pct': -3}]})
    save(root, 'instruments.json', {'checked_at': stamp, 'last_success_at': stamp, 'stale': False, 'records': []})


def app(root):
    return AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.market_scanner import render_market_scanner\nrender_market_scanner(Path('+repr(str(root))+'))').run(timeout=20)


def test_missing_artifacts_are_visible_not_blank(tmp_path):
    result = app(tmp_path)
    assert not result.exception
    assert any('Unavailable' in item.value for item in result.warning)
    assert any('No instrument observations' in item.value for item in result.info)


@pytest.mark.parametrize('stamp,expected', [(NOW-timedelta(minutes=11), 'Stale'), (NOW+timedelta(seconds=1), 'Stale'), (NOW, 'Healthy')])
def test_timestamp_health(tmp_path, stamp, expected):
    fixture(tmp_path)
    payload = json.loads((tmp_path/'status.json').read_text())
    payload['checked_at'] = stamp.isoformat()
    save(tmp_path, 'status.json', payload)
    assert scanner_view(tmp_path, NOW)['health'] == expected


def test_partial_unreadable_latest_is_degraded(tmp_path):
    fixture(tmp_path)
    (tmp_path/'latest.json').write_text('{')
    view = scanner_view(tmp_path, NOW)
    assert view['health'] == 'Degraded'
    assert view['rows'] == []


def test_recent_heartbeat_preserves_closed_and_stale_observations(tmp_path):
    fixture(tmp_path)
    view = scanner_view(tmp_path, NOW)
    spy = view['rows'][0]
    assert spy['state'] == 'closed'
    assert spy['freshness'] == 'Stale (>10 min)'
    assert spy['data_age_minutes'] == 480


def test_errors_override_healthy_label(tmp_path):
    fixture(tmp_path)
    payload = json.loads((tmp_path/'status.json').read_text())
    payload['sources']['crypto'] = {'status': 'failed', 'error': 'timeout'}
    save(tmp_path, 'status.json', payload)
    assert scanner_view(tmp_path, NOW)['health'] == 'Degraded'


def test_scanning_partial_ui_and_search(tmp_path):
    now = datetime.now(timezone.utc)
    fixture(tmp_path, now)
    payload = json.loads((tmp_path/'status.json').read_text())
    payload.update(status='scanning', stage='crypto batch', progress={'processed': 1, 'total': 3})
    save(tmp_path, 'status.json', payload)
    result = app(tmp_path)
    assert not result.exception
    assert any('Scanning' in item.value for item in result.info)
    assert any('IEX-only' in item.value for item in result.caption)
    assert any('Index membership is not verified' in item.value for item in result.caption)
    assert len(result.dataframe[-1].value) == 2
    result.text_input(key='continuous_scanner_search').set_value('BTC/USD').run()
    assert not result.exception
    assert list(result.dataframe[-1].value['symbol']) == ['BTC/USD']
    result.text_input(key='continuous_scanner_search').set_value('missing').run()
    assert not result.exception
    assert len(result.dataframe[-1].value) == 0


def test_stopped_and_invalid_schema_never_healthy(tmp_path):
    fixture(tmp_path)
    save(tmp_path, 'status.json', {'checked_at': NOW.isoformat(), 'status': 'shutdown', 'sources': [], 'progress': None})
    assert scanner_view(tmp_path, NOW)['health'] == 'Stopped'
    save(tmp_path, 'latest.json', {'records': [None, {}, {'symbol': 'ABC', 'data_asof': 'invalid'}]})
    result = app(tmp_path)
    assert not result.exception
    assert 'saved observation' in result.dataframe[-1].value['freshness'].iloc[0]


def test_stale_catalog_is_not_hidden_by_recent_worker_heartbeat(tmp_path):
    fixture(tmp_path)
    save(tmp_path, 'instruments.json', {'stale': False, 'last_success_at': (NOW-timedelta(days=2)).isoformat()})
    view = scanner_view(tmp_path, NOW)
    assert view['catalog_stale']
    assert view['health'] == 'Degraded'


def test_catalog_per_class_timestamps(tmp_path):
    fixture(tmp_path)
    save(tmp_path, 'instruments.json', {'stale': False, 'last_success_at': {'us_equity': NOW.isoformat(), 'crypto': NOW.isoformat()}})
    assert scanner_view(tmp_path, NOW)['health'] == 'Healthy'
    save(tmp_path, 'instruments.json', {'stale': False, 'last_success_at': {'us_equity': NOW.isoformat()}})
    assert scanner_view(tmp_path, NOW)['catalog_stale']
    save(tmp_path, 'instruments.json', {'stale': False, 'last_success_at': {'us_equity': NOW.isoformat(), 'crypto': (NOW-timedelta(days=2)).isoformat()}})
    assert scanner_view(tmp_path, NOW)['catalog_stale']


def test_sip_status_preserves_retained_iex_record_and_intervals(tmp_path):
    now = datetime.now(timezone.utc)
    fixture(tmp_path, now)
    status = json.loads((tmp_path/'status.json').read_text())
    status['sources']['equities']['feed'] = 'sip'
    status['stock_feed'] = 'sip'
    save(tmp_path, 'status.json', status)
    latest = json.loads((tmp_path/'latest.json').read_text())
    latest['records'][0].update(source='alpaca_iex', feed='iex', requested_source='alpaca_sip', requested_feed='sip', comparison_asof='2026-09-22T20:00:00Z', volume_asof='2026-09-23T20:00:00Z')
    save(tmp_path, 'latest.json', latest)
    result = app(tmp_path)
    assert not result.exception
    assert any('SIP consolidated' in c.value for c in result.caption)
    frame = result.dataframe[-1].value
    row = frame[frame.symbol == 'SPY'].iloc[0]
    assert row['source'] == 'alpaca_iex' and row['feed'] == 'iex'
    assert row['requested_feed'] == 'sip'
    assert row['comparison_asof'] == '2026-09-22T20:00:00Z'
    assert row['volume_asof'] == '2026-09-23T20:00:00Z'


def test_requested_sip_does_not_claim_verified_feed(tmp_path):
    fixture(tmp_path, datetime.now(timezone.utc))
    status = json.loads((tmp_path/'status.json').read_text())
    del status['sources']['equities']['feed']
    status['stock_feed'] = 'sip'
    save(tmp_path, 'status.json', status)
    result = app(tmp_path)
    assert not result.exception
    assert any('feed is not verified' in c.value for c in result.caption)
    assert not any('SIP consolidated' in c.value for c in result.caption)


def test_failed_sip_requests_do_not_claim_available_coverage(tmp_path):
    fixture(tmp_path, datetime.now(timezone.utc))
    status = json.loads((tmp_path/'status.json').read_text())
    status['sources']['equities'] = {'feed': 'sip', 'status': 'failed', 'error': 'HTTP 403'}
    save(tmp_path, 'status.json', status)
    result = app(tmp_path)
    assert not result.exception
    assert any('Degraded' in w.value for w in result.warning)
    assert any('request SIP consolidated data' in c.value for c in result.caption)
    assert not any('reports SIP consolidated coverage' in c.value for c in result.caption)
    assert 'HTTP 403' in result.dataframe[0].value.to_string()
