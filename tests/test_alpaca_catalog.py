from datetime import datetime, timezone, timedelta
import pytest
from trader_engine.data.alpaca_catalog import discover_alpaca, request_directory


def asset_reply(params):
    crypto=params['asset_class']=='crypto'
    return [dict(symbol='BTC/USD' if crypto else 'XYZ',name='Bitcoin' if crypto else 'Example ETF',exchange='CRYPTO' if crypto else 'ARCA',status='active',tradable=True)]


def option(symbol):
    return dict(symbol=symbol,expiration_date=(datetime.now(timezone.utc).date()+timedelta(days=10)).isoformat(),
        strike_price='100',type='call',size='100',underlying_symbol='XYZ',status='active')


def test_alpaca_paginates_and_sends_explicit_expiry_bounds():
    requests=[]
    def fetch(path,params):
        requests.append((path,params.copy()))
        if path=='/v2/assets':return asset_reply(params)
        if params.get('page_token')=='second':return dict(option_contracts=[option('CONTRACT2')],next_page_token=None)
        return dict(option_contracts=[option('CONTRACT1')],next_page_token='second')
    end=datetime.now(timezone.utc).date()+timedelta(days=20)
    rows,coverage=discover_alpaca(end,fetcher=fetch,sleeper=lambda _:None)
    assert len(rows)==4
    assert [r.kind for r in rows]==['etf','crypto','option','option']
    assert all(r.history_status=='unverified' and r.execution_status=='not_connected' for r in rows)
    option_calls=[p for path,p in requests if path.endswith('contracts')]
    assert len(option_calls)==2 and option_calls[1]['page_token']=='second'
    assert all(p['expiration_date_lte']==end.isoformat() and p['expiration_date_gte'] for p in option_calls)
    assert coverage[-1]['instrument_count']==2


@pytest.mark.parametrize('max_pages, expected',[(1,'page limit'),(3,'Repeated')])
def test_alpaca_refuses_incomplete_or_looping_contract_pages(max_pages,expected):
    def fetch(path,params):
        if path=='/v2/assets':return asset_reply(params)
        return dict(option_contracts=[option('C')],next_page_token='same')
    with pytest.raises(ValueError,match=expected):
        discover_alpaca(datetime.now(timezone.utc).date()+timedelta(days=20),fetcher=fetch,max_pages=max_pages,sleeper=lambda _:None)


def test_missing_credentials_and_non_directory_calls_rejected(monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID',raising=False);monkeypatch.delenv('APCA_API_SECRET_KEY',raising=False)
    with pytest.raises(RuntimeError,match='not connected'):request_directory('/v2/assets',{})
    with pytest.raises(ValueError,match='Only instrument'):request_directory('/v2/orders',{})


def test_options_not_silently_requested_without_horizon():
    def fetch(path,params):
        assert path=='/v2/assets'
        return asset_reply(params)
    rows,coverage=discover_alpaca(fetcher=fetch)
    assert len(rows)==2 and coverage[-1]['status']=='not_requested'
