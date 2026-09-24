"""Offline feed wiring tests; runner run() functions must never be invoked."""
import importlib.util
import io
import json
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from trader_engine.data.market_feed import StockFeedConfig
from trader_engine.data.quote_validation import QuotePolicy, validate_quote
from trader_engine.execution.shadow import run_shadow
from test_net_edge_shadow import inputs

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
NOW = datetime(2026, 9, 23, 15, tzinfo=timezone.utc)


@pytest.fixture
def runners(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    modules = []
    for name in ('paper_breakout_until_close', 'paper_portfolio_until_close'):
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, 'utc', lambda: NOW)
        modules.append(module)
    # Fixtures supply fake values without accessing real credentials.
    for module in modules:
        monkeypatch.setattr(module, 'os', type('FakeOS', (), {'environ': {
            'APCA_API_KEY_ID': 'offline-test', 'APCA_API_SECRET_KEY': 'offline-test'}}))
    return modules


@pytest.mark.parametrize('bad', ['', 'SIP', 'unknown', None, ['iex']])
def test_invalid_feed_fails_explicitly(bad):
    with pytest.raises(ValueError, match='Stock feed'):
        StockFeedConfig(bad)


def test_default_feed_and_coverage():
    default = StockFeedConfig().to_record()
    assert default['feed'] == default['expected_feed'] == 'iex'
    assert default['restricted_market_data'] is True
    assert default['market_data_coverage'] == 'single_exchange_iex'
    sip = StockFeedConfig('sip').to_record()
    assert sip['restricted_market_data'] is False
    assert sip['market_data_coverage'] == 'consolidated_sip'
    assert default['feed_fallback'] is sip['feed_fallback'] is False


@pytest.mark.parametrize('feed', ['iex', 'sip'])
def test_both_http_clients_use_selected_feed(runners, monkeypatch, feed):
    breakout, portfolio = runners
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request.full_url)
            path = urlsplit(request.full_url).path
            payload = {'quotes': {}} if path.endswith('/quotes/latest') else {'bars': {}}
            if path.endswith('/snapshots'):
                payload = {}
            return io.BytesIO(json.dumps(payload).encode())

    for module in runners:
        monkeypatch.setattr(module, 'build_opener', lambda *args: Opener())
    breakout.latest_quotes(feed=feed)
    breakout.latest_bars(feed=feed)
    market = portfolio.Market(feed=feed)
    monkeypatch.setattr(market.limiter, 'acquire', lambda **kwargs: None)
    market.quotes(['SPY'])
    market.bars(['SPY'])
    market.shortlist(['SPY'], threading.Event(), NOW + timedelta(minutes=1), lambda *args: None)
    assert len(requests) == 5
    assert all(parse_qs(urlsplit(url).query)['feed'] == [feed] for url in requests)


@pytest.mark.parametrize('feed', ['iex', 'sip'])
def test_quote_evidence_preserves_feed_and_entry_thresholds(runners, feed):
    breakout, _ = runners
    raw = {'bp': 100, 'ap': 100.01, 't': NOW.isoformat()}
    records = []
    assert breakout.qualified_quote(raw, NOW, feed=feed, evidence=records.append)
    assert not breakout.qualified_quote(raw, NOW + timedelta(seconds=5.001), feed=feed,
                                        evidence=records.append)
    assert not breakout.qualified_quote(raw | {'ap': 101}, NOW, feed=feed,
                                        evidence=records.append)
    for record in records:
        assert record['envelope']['feed'] == record['policy']['expected_feed'] == feed
        assert record['policy']['max_source_age_seconds'] == 5
        assert record['policy']['max_spread_bps'] == 10
        assert record['policy']['require_sizes'] is False
    assert 'stale_source' in records[1]['reasons']
    assert 'excessive_spread' in records[2]['reasons']


@pytest.mark.parametrize('status', [403, 429])
def test_failed_sip_request_records_failure_without_fallback(runners, monkeypatch, status):
    breakout, _ = runners
    requests, records = [], []

    class Opener:
        def open(self, request, timeout):
            requests.append(request.full_url)
            raise HTTPError(request.full_url, status, 'offline fixture', {}, None)

    monkeypatch.setattr(breakout, 'build_opener', lambda *args: Opener())
    with pytest.raises(HTTPError):
        breakout.fetch_entry_quotes(lambda: breakout.latest_quotes(feed='sip'), ['SPY'],
                                    evidence=records.append, now=lambda: NOW, feed='sip')
    assert len(requests) == 1
    assert parse_qs(urlsplit(requests[0]).query)['feed'] == ['sip']
    assert records[0]['retrieval_status'] == status
    assert records[0]['policy']['expected_feed'] == records[0]['envelope']['feed'] == 'sip'
    assert 'quote_retrieval_failed' in records[0]['reasons']


@pytest.mark.parametrize('script_index', [0, 1])
@pytest.mark.parametrize('feed', [None, 'sip'])
def test_preview_discloses_feed_without_starting_runner(runners, monkeypatch, capsys, script_index, feed):
    module = runners[script_index]
    monkeypatch.setattr(module, 'run', lambda *args, **kwargs: pytest.fail('Runner must not start'))
    monkeypatch.setattr(sys, 'argv', ['offline-preview'] + (['--feed', feed] if feed else []))
    module.main()
    record = json.loads(capsys.readouterr().out)
    assert record['mode'] == 'preview_no_orders'
    assert record['feed'] == record['expected_feed'] == (feed or 'iex')
    assert record['restricted_market_data'] is (feed is None)


@pytest.mark.parametrize('selected,actual', [('sip','iex'), ('iex','iex'), ('iex','sip')])
def test_shadow_keeps_default_sip_gate_and_labels_explicit_iex(tmp_path, selected, actual):
    kw = inputs(tmp_path)
    kw['quotes'] = {symbol: validate_quote(replace(quote.envelope, feed=actual),
                      QuotePolicy(expected_feed=actual)) for symbol, quote in kw['quotes'].items()}
    # Omit the argument to prove the existing default still requires SIP.
    if selected == 'iex':
        kw['expected_feed'] = selected
    result = run_shadow(**kw)
    assert result['expected_feed'] == selected
    assert result['restricted_market_data'] is (selected == 'iex')
    manifest = json.loads((kw['output_dir'] / 'manifest.json').read_text())
    assert manifest['expected_feed'] == selected
    assert manifest['restricted_market_data'] is (selected == 'iex')
    for row in result['decisions']:
        assert row['quote']['policy']['expected_feed'] == selected
        assert row['quote']['envelope']['feed'] == actual
        assert row['restricted_market_data'] is (selected == 'iex')
        assert ('unexpected_feed' in row['quote']['reasons']) is (selected != actual)
    if selected != actual:
        assert not any(row['eligible'] for row in result['decisions'])
    else:
        assert any(row['eligible'] for row in result['decisions'])
    assert not result['formal_forward_run']


@pytest.mark.parametrize('feed', [None, 'iex'])
def test_shadow_cli_passes_explicit_selection(monkeypatch, capsys, feed):
    spec = importlib.util.spec_from_file_location('offline_net_edge_cli', SCRIPTS / 'run_net_edge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, 'run_shadow_snapshot', lambda *args, **kwargs: calls.append((args, kwargs)))
    argv = ['shadow', '--input', 'snapshot.json', '--registry', 'registry.json',
            '--candidate', 'MR30', '--output', 'out']
    if feed:
        argv += ['--expected-feed', feed]
    assert module.main(argv) == 0
    assert calls == [(('snapshot.json', 'registry.json', 'MR30', 'out'),
                      {'expected_feed': feed or 'sip'})]
    assert json.loads(capsys.readouterr().out)['broker_orders_submitted'] == 0


@pytest.mark.parametrize('feed', [None, 'iex'])
def test_snapshot_preserves_actual_quote_feed_and_selected_policy(tmp_path, feed):
    from trader_engine.workflows.net_edge import freeze_registry, run_shadow_snapshot

    kw = inputs(tmp_path)
    registry = tmp_path / 'registry.json'
    freeze_registry(registry)
    histories = {}
    for symbol, frame in kw['histories'].items():
        histories[symbol] = symbol + '.parquet'
        frame.to_parquet(tmp_path / histories[symbol])
    actual_quotes = {symbol: {'feed': 'iex', 'received_at': quote.envelope.received_at,
                     'raw_payload': quote.envelope.raw_payload} for symbol, quote in kw['quotes'].items()}
    raw = dict(context=dict(as_of=kw['context'].as_of.isoformat(), strategy_hash=kw['spec'].spec_hash,
                            symbols=list(kw['context'].symbols), evidence=[]),
               histories=histories, quotes=actual_quotes, risk_state='risk.json', positions=[], cash=100000,
               session_open=kw['session_open'].isoformat(), session_close=kw['session_close'].isoformat(),
               previous_session_close=kw['previous_session_close'].isoformat(),
               history_available_at={s: at.isoformat() for s, at in kw['history_available_at'].items()})
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(raw))
    before = (tmp_path / 'risk.json').read_bytes()
    selection = {'expected_feed': feed} if feed else {}
    output = tmp_path / 'snapshot-output'
    result = run_shadow_snapshot(snapshot, registry, 'MOM20', output, **selection)
    assert (tmp_path / 'risk.json').read_bytes() == before
    assert result['expected_feed'] == (feed or 'sip')
    assert result['restricted_market_data'] is (feed == 'iex')
    assert result['broker_orders_submitted'] == 0 and result['approved_for_trading'] is False
    for row in result['decisions']:
        assert row['quote']['envelope']['feed'] == 'iex'
        assert row['quote']['policy']['expected_feed'] == (feed or 'sip')
        assert ('unexpected_feed' in row['quote']['reasons']) is (feed is None)
    for filename in ('manifest.json', 'inputs.json'):
        saved = json.loads((output / filename).read_text())
        assert saved['expected_feed'] == (feed or 'sip')
        assert saved['restricted_market_data'] is (feed == 'iex')
        assert saved['formal_forward_run'] is False
