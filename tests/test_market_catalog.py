from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError
from trader_engine.data.catalog import Instrument, discover, parse_nasdaq, parse_deribit, parse_kraken, save_catalog, import_instruments
NOW=datetime(2026,9,22,tzinfo=timezone.utc)
BASE=dict(provider='vendor',venue='X',symbol='ABC',name='ABC',kind='equity',observed_at=NOW)


def test_identity_preserves_venue_and_contract():
    equity=Instrument(**BASE)
    assert equity.instrument_id!=Instrument(**(BASE|{'venue':'Y'})).instrument_id
    option=BASE|dict(kind='option',expiry='2027-01-15',strike=100,option_right='call',contract_size=100)
    assert Instrument(**option).instrument_id!=Instrument(**(option|{'strike':110})).instrument_id
    assert Instrument(**option).instrument_id!=Instrument(**(option|{'option_right':'put'})).instrument_id


@pytest.mark.parametrize('fields',[
 {'kind':'option'}, {'kind':'future'}, {'kind':'perpetual'}, {'observed_at':datetime(2026,9,22)},
 {'kind':'option','expiry':'bad','strike':100,'option_right':'call','contract_size':100},
 {'kind':'option','expiry':'2027-01-15','strike':float('nan'),'option_right':'call','contract_size':100},
 {'history_status':'ready'}, {'symbol':'  '},
])
def test_invalid_or_falsely_ready_instrument_rejected(fields):
    with pytest.raises(ValidationError):Instrument(**(BASE|fields))


def test_nasdaq_filters_test_symbols_and_footer_without_dropping_etfs():
    raw='Symbol|Security Name|Test Issue|ETF\nABC|ABC shares|N|N\nFUND|Fund ETF|N|Y\nTEST|Test only|Y|N\nFile Creation Time: 0922202618:00||||\n'
    rows=parse_nasdaq(raw,'url',NOW)
    assert [(i.symbol,i.kind) for i in rows]==[('ABC','equity'),('FUND','etf')]
    with pytest.raises(ValueError):parse_nasdaq('<html>rate limit</html>','url',NOW)


def test_derivative_contract_details_and_halted_status_preserved():
    common=dict(instrument_name='BTC-25DEC26-100000-C',kind='option',expiration_timestamp=1798185600000,
                contract_size=1,option_type='call',strike=100000,state='halted',is_active=True)
    rows=parse_deribit({'result':[common,dict(instrument_name='BTC-PERPETUAL',kind='future',settlement_period='perpetual',contract_size=10,state='open')]},'url',NOW)
    assert rows[0].status=='halted' and rows[0].strike==100000 and rows[0].expiry
    assert rows[1].kind=='perpetual' and rows[1].expiry is None


def test_forex_not_confused_with_crypto_or_stablecoins():
    payload={'error':[],'result':{x:{'wsname':x,'aclass_base':'currency'} for x in ['EUR/USD','XBT/USD','USDT/USD']}}
    rows=parse_kraken(payload,'url',NOW)
    assert [r.kind for r in rows]==['forex','crypto','crypto']


def test_partial_failure_is_visible_and_empty_snapshot_never_overwrites(tmp_path):
    def fetch(url):
        if 'coinbase' in url:return json.dumps([dict(id='BTC-USD',base_currency='BTC',quote_currency='USD',status='online')])
        raise OSError('connection unavailable')
    payload=discover(['coinbase','kraken'],fetcher=fetch)
    assert [c['status'] for c in payload['coverage']]==['ok','failed']
    assert payload['worldwide_complete'] is False
    path=tmp_path/'catalog.json';save_catalog(payload,path);original=path.read_bytes()
    with pytest.raises(ValueError):save_catalog(dict(instruments=[]),path)
    assert path.read_bytes()==original


def test_import_accepts_worldwide_equity_and_rejects_incomplete_options(tmp_path):
    path=tmp_path/'vendor.json'
    path.write_text(json.dumps([BASE|{'observed_at':NOW.isoformat(),'venue':'Tokyo','currency':'JPY'}]))
    rows=import_instruments(path);assert rows[0].venue=='Tokyo'
    path.write_text(json.dumps([BASE|{'observed_at':NOW.isoformat(),'kind':'option'}]))
    with pytest.raises(ValidationError):import_instruments(path)


def test_market_catalog_renders_without_research_artifacts(tmp_path):
    from streamlit.testing.v1 import AppTest
    payload=discover(['coinbase'],fetcher=lambda _:json.dumps([dict(id='BTC-USD',base_currency='BTC',quote_currency='USD')]))
    path=tmp_path/'market_catalog'/'catalog.json';save_catalog(payload,path)
    source='from pathlib import Path\nfrom trader_engine.ui.dashboard import render_dashboard\nrender_dashboard(Path('+repr(str(tmp_path/'latest'))+'))'
    app=AppTest.from_string(source).run(timeout=20)
    assert not app.exception
    assert any('BTC-USD' in frame.value.to_string() for frame in app.dataframe)
    app.text_input(key='catalog_search').set_value('missing-symbol').run()
    assert not app.exception
    assert any('0 matching listings' in c.value for c in app.caption)
