from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from trader_engine.data.plus_rest import PlusRESTClient, assess_option_candidates, _NoRedirect


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.responses.pop(0)


def client(responses, **kwargs):
    transport = Transport(responses)
    sleeps = []
    result = PlusRESTClient('dummy-key', 'dummy-secret', transport=transport, sleep=sleeps.append, monotonic=lambda: 100, **kwargs)
    return result, transport, sleeps


def test_paginated_bars_keep_feed_adjustment_dates_and_symbol_completeness():
    c, t, sleeps = client([(200, {}, {'bars': {'AAA': [{'t': '2026-01-01', 'c': 10}]}, 'next_page_token': 'page2'}), (200, {}, {'bars': {'BBB': [{'t': '2026-01-01', 'c': 20}]}})])
    result = c.bars(['AAA', 'BBB', 'CCC'], '2026-01-01', '2026-02-01', adjustment='all')
    assert result['complete'] and result['pages'] == 2
    assert result['missing_symbols'] == ['CCC']
    assert [r['symbol'] for r in result['records']] == ['AAA', 'BBB']
    params = parse_qs(urlsplit(t.calls[1][0]).query)
    assert params['feed'] == ['sip'] and params['adjustment'] == ['all']
    assert params['page_token'] == ['page2'] and params['asof'] == ['-']
    assert sleeps == [.2]
    assert 'dummy-secret' not in str(result)


@pytest.mark.parametrize('code', [301, 302, 307, 308, 401, 403])
def test_no_redirect_or_entitlement_fallback(code):
    c, t, _ = client([(code, {'Location': 'https://evil.invalid'}, {'message': 'dummy-secret'})])
    result = c.option_chain('SPY')
    assert not result['complete'] and result['errors'] == [f'http_{code}']
    assert len(t.calls) == 1
    assert 'feed=opra' in t.calls[0][0]
    assert 'dummy-secret' not in str(result)
    assert _NoRedirect().redirect_request(None, None, code, '', {}, 'https://evil.invalid') is None


def test_origin_allowlist_rejects_credentials_before_transport():
    c, t, _ = client([])
    for endpoint in ['https://evil.invalid/v2/stocks/bars', 'https://data.alpaca.markets.evil.invalid/v2/stocks/bars', 'http://data.alpaca.markets/v2/stocks/bars', 'https://data.alpaca.markets:443/v2/stocks/bars', 'https://paper-api.alpaca.markets/v2/orders']:
        assert c._get(endpoint, {}) == (None, 'endpoint_not_allowed')
    assert not t.calls


def test_bounded_retries_and_rate_limit():
    c, t, sleeps = client([(429, {'Retry-After': '2'}, {}), (503, {}, {}), (200, {}, {'quotes': {}})])
    result = c.latest_quotes(['SPY'])
    assert result['complete'] and len(t.calls) == 3
    assert sleeps == [2.0, .2, 2, .2]
    c, t, _ = client([(429, {'Retry-After': '3600'}, {})])
    assert c.latest_quotes(['SPY'])['errors'] == ['retry_after_exceeds_bound']
    assert len(t.calls) == 1


def test_partial_error_and_page_limit_remain_incomplete():
    first = (200, {}, {'snapshots': {'OPTION': {'latestQuote': {}}}, 'next_page_token': 'next'})
    c, _, _ = client([first, (403, {}, {})])
    result = c.option_chain('SPY')
    assert len(result['records']) == 1 and not result['complete']
    assert result['errors'] == ['http_403']
    c, _, _ = client([first], max_pages=1)
    assert c.option_chain('SPY')['errors'] == ['pagination_limit']


def test_repeated_page_token_and_bad_schema():
    c, _, _ = client([(200, {}, {'snapshots': {}, 'next_page_token': 'same'}), (200, {}, {'snapshots': {}, 'next_page_token': 'same'})])
    assert c.option_chain('SPY')['errors'] == ['invalid_or_repeated_page_token']
    c, _, _ = client([(200, {}, {'snapshots': []})])
    assert c.option_chain('SPY')['errors'] == ['invalid_snapshots_schema']


def test_get_metadata_explicit_expiry_avoids_next_week_default():
    c, t, _ = client([(200, {}, {'option_contracts': [{'symbol': 'SPYOPT', 'size': '100'}]})])
    r = c.option_contracts('SPY', '2026-10-01', '2026-12-31')
    assert r['complete']
    assert t.calls[0][0].startswith('https://paper-api.alpaca.markets/v2/options/contracts?')
    assert parse_qs(urlsplit(t.calls[0][0]).query)['expiration_date_lte'] == ['2026-12-31']


@pytest.mark.parametrize('symbol', ['SPY/../../orders', 'SPY?feed=indicative', 'https://evil.invalid', 'spy'])
def test_bad_underlying_not_sent(symbol):
    c, t, _ = client([])
    with pytest.raises(ValueError): c.option_chain(symbol)
    assert not t.calls


def test_input_dates_adjustment_required_and_chronological():
    c, t, _ = client([])
    with pytest.raises(TypeError): c.bars(['SPY'], '2026-01-01', '2026-02-01')
    with pytest.raises(ValueError): c.bars(['SPY'], '2026-02-01', '2026-01-01', adjustment='raw')
    with pytest.raises(ValueError): c.bars(['SPY'], '2026-01-01', '2026-02-01', adjustment='unknown')
    assert not t.calls


def test_transport_exception_is_sanitized():
    def fail(*args): raise RuntimeError('dummy-secret')
    c = PlusRESTClient('dummy-key', 'dummy-secret', transport=fail)
    assert c.latest_quotes(['SPY'])['errors'] == ['transport_failure']


def option_input():
    chain = {'complete': True, 'provenance': {'feed': 'opra'}, 'records': [{'symbol': 'OPTION', 'latestQuote': {'t': '2026-09-24T14:00:00Z', 'bp': 1, 'ap': 1.05, 'bs': 10, 'as': 20}, 'impliedVolatility': .25, 'greeks': {'delta': .5, 'gamma': .01, 'theta': -.1, 'vega': .05, 'rho': .01}}]}
    contracts = {'complete': True, 'records': [{'symbol': 'OPTION', 'expiration_date': '2026-10-01', 'strike_price': '100', 'size': '100', 'type': 'call', 'style': 'american'}]}
    return chain, contracts


def test_options_quality_is_not_execution_or_profitability():
    chain, contracts = option_input()
    result = assess_option_candidates(chain, '2026-09-24T14:00:30Z', contracts)
    row = result['records'][0]
    assert row['quality_status'] == 'passes_quality_screen'
    assert not row['execution_eligible'] and not row['profitable_edge_established']


@pytest.mark.parametrize('stamp', ['2026-09-24T13:00:00Z', '2026-09-24T15:00:00Z'])
def test_stale_future_options_quotes_rejected(stamp):
    chain, contracts = option_input()
    chain['records'][0]['latestQuote']['t'] = stamp
    result = assess_option_candidates(chain, '2026-09-24T14:00:30Z', contracts)
    assert 'stale_or_future_quote' in result['records'][0]['reasons']


def test_missing_greeks_metadata_and_partial_chain_are_explicit():
    chain, _ = option_input()
    chain['complete'] = False
    chain['records'][0]['greeks'] = None
    chain['records'][0]['latestQuote']['bp'] = 2
    row = assess_option_candidates(chain, '2026-09-24T14:00:30Z')['records'][0]
    assert 'greek_delta' in row['missing'] and 'contract_size' in row['missing']
    assert 'incomplete_chain' in row['reasons'] and 'nonpositive_or_crossed_quote' in row['reasons']
    assert 'incomplete_contract_metadata' in row['reasons']


@pytest.mark.parametrize('field,value,reason', [
    ('expiration_date', '2026-01-01', 'expired_contract'),
    ('expiration_date', 'invalid', 'invalid_contract_expiration'),
    ('strike_price', '-1', 'invalid_contract_strike'),
    ('strike_price', 'NaN', 'invalid_contract_strike'),
    ('size', '100.5', 'invalid_contract_size'),
    ('size', '0', 'invalid_contract_size'),
    ('type', 'unknown', 'invalid_contract_type'),
    ('style', 'unknown', 'invalid_contract_style'),
])
def test_invalid_metadata_fails_quality_screen(field, value, reason):
    chain, contracts = option_input()
    contracts['records'][0][field] = value
    row = assess_option_candidates(chain, '2026-09-24T14:00:30Z', contracts)['records'][0]
    assert reason in row['reasons']
    assert row['quality_status'] != 'passes_quality_screen'


@pytest.mark.parametrize('name,value', [('delta', 1.1), ('delta', -1.1), ('gamma', -.01), ('vega', -.1)])
def test_invalid_greek_ranges_fail_quality(name, value):
    chain, contracts = option_input()
    chain['records'][0]['greeks'][name] = value
    row = assess_option_candidates(chain, '2026-09-24T14:00:30Z', contracts)['records'][0]
    assert 'invalid_greek_'+name in row['reasons']


def test_indicative_or_missing_provenance_never_passes_opra_screen():
    chain, contracts = option_input()
    chain['provenance']['feed'] = 'indicative'
    row = assess_option_candidates(chain, '2026-09-24T14:00:30Z', contracts)['records'][0]
    assert 'opra_provenance_required' in row['reasons']
