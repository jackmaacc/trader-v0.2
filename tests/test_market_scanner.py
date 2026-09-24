from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
import pytest
from trader_engine.research.market_scanner import observe,futures_snapshot,summarize
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('scanner_service',ROOT/'scripts/market_scanner_service.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NOW=datetime(2026,9,24,3,tzinfo=timezone.utc)
def snap(age=0,price=100):
    return dict(latestTrade={'p':price,'t':(NOW-timedelta(seconds=age)).isoformat()},dailyBar={'c':price,'v':1000,'t':NOW.isoformat()},prevDailyBar={'c':90,'t':(NOW-timedelta(days=1)).isoformat()},latestQuote={'ap':100.1,'bp':99.9,'t':NOW.isoformat()})

def test_fresh_stale_closed_and_future_semantics():
    assert observe('X','us_equity',snap(),NOW,market_open=True)['state']=='fresh'
    assert observe('X','us_equity',snap(301),NOW,market_open=True)['state']=='stale'
    assert observe('X','us_equity',snap(10000),NOW,market_open=False)['state']=='closed_last_observation'
    assert observe('BTC/USD','crypto',snap(181),NOW)['state']=='stale'
    assert observe('X','us_equity',snap(-1),NOW,market_open=True)['state']=='invalid'
    assert observe('X','us_equity',snap(),NOW,market_open=None)['state']=='unavailable'

def test_query_receipt_does_not_create_observation_and_failure_retains_label():
    first=observe('X','us_equity',snap(),NOW,market_open=True)
    second=observe('X','us_equity',snap(),NOW+timedelta(seconds=1),market_open=True,previous=first)
    assert second['observation_count']==1 and second['scan_count']==2
    error=observe('X','us_equity',None,NOW+timedelta(seconds=2),market_open=True,previous=second,failure='batch_failed')
    assert error['state']=='unavailable' and error['retained'] and error['price']==100
    assert error['data_asof']==first['data_asof']
    assert not summarize([error])[1]

@pytest.mark.parametrize('bad',[{'latestTrade':[]},{'latestTrade':{'p':'NaN','t':NOW.isoformat()}},{'latestTrade':{'p':100,'t':'bad'}},{'latestTrade':{'p':0,'t':NOW.isoformat()}},{'dailyBar':{'c':100,'t':NOW.isoformat(),'v':-1}}])
def test_malformed_bars_invalid(bad):assert observe('X','us_equity',bad,NOW,market_open=True)['state']=='invalid'

def test_futures_never_rank_as_live_execution_data():
    payload={'chart':{'result':[{'timestamp':[NOW.timestamp()],'indicators':{'quote':[{'close':[100],'volume':[10]}]},'meta':{'chartPreviousClose':90}}]}}
    row=observe('ES=F','futures',futures_snapshot(payload),NOW)
    assert row['state']=='indicative_unknown_latency' and not row['execution_eligible']
    assert row['volume'] is None and not summarize([row])[1]
    with pytest.raises(ValueError):futures_snapshot({'chart':{'result':[]}})

class Response:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return b'{}'
class Opener:
    def __init__(self):self.requests=[]
    def open(self,r,timeout):self.requests.append(r);return Response()

def test_transport_get_allowlist_and_no_yahoo_credential_forwarding():
    opener=Opener();transport=m.ReadOnlyTransport('fixturekey','fixturesecret',opener=opener,sleep=lambda _:None)
    transport.get('data','/v2/stocks/snapshots',{'symbols':'SPY','feed':'iex'})
    transport.get('yahoo','/v8/finance/chart/ES%3DF',{'range':'1d'})
    assert all(r.get_method()=='GET' for r in opener.requests)
    assert 'fixturesecret' not in str(opener.requests[-1].headers)
    assert 'fixturesecret' in str(opener.requests[0].headers)
    for source,path in [('paper','/v2/orders'),('paper','/v2/account'),('data','https://evil.test'),('yahoo','/v8/finance/chart/OTHER')]:
        with pytest.raises(m.ScanError):transport.get(source,path)
    assert len(opener.requests)==2

class Fake:
    def __init__(self):self.fail_stock=False;self.fail_assets=False
    def get(self,source,path,params=None):
        if path=='/v2/assets':
            if self.fail_assets:raise m.ScanError('http_503')
            kind=params['asset_class'];names=['SPY','MISSING'] if kind=='us_equity' else ['BTC/USD']
            return [dict(symbol=s,status='active',tradable=True,asset_class=kind) for s in names]
        if path=='/v2/clock':return {'is_open':True}
        if path=='/v2/stocks/snapshots':
            if self.fail_stock:raise m.ScanError('http_503')
            return {'SPY':snap()}
        if path=='/v1beta3/crypto/us/snapshots':return {'snapshots':{'BTC/USD':snap()}}
        raise AssertionError(path)

def test_cycles_restart_counts_partial_failures_and_history_bound(tmp_path):
    fake=Fake();scanner=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW)
    history=tmp_path/'history';history.mkdir()
    for offset in range(10):m.atomic(history/((NOW-timedelta(days=offset)).date().isoformat()+'.json'),{})
    status=scanner.cycle()
    assert status['status']=='degraded' and status['coverage']['by_state']['missing']==1
    assert len(list(history.glob('*.json')))==7
    scanner=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW)
    scanner.cycle();rows=json.loads((tmp_path/'latest.json').read_text())['records']
    spy=next(r for r in rows if r['symbol']=='SPY');assert spy['observation_count']==1 and spy['scan_count']==2
    fake.fail_stock=True;status=scanner.cycle()
    rows=json.loads((tmp_path/'latest.json').read_text())['records']
    assert next(r for r in rows if r['symbol']=='SPY')['state']=='unavailable'
    assert next(r for r in rows if r['symbol']=='BTC/USD')['state']=='fresh'
    assert status['sources']['equities']['status']=='degraded'

def test_directory_refresh_failure_retains_universe_but_labels_stale(tmp_path):
    fake=Fake();scanner=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW);scanner.cycle()
    fake.fail_assets=True;scanner.now=lambda:NOW+timedelta(days=1);status=scanner.cycle()
    directory=json.loads((tmp_path/'instruments.json').read_text())
    assert directory['stale'] and len(directory['records'])==3
    assert status['directory_stale'] and status['status']=='degraded'

def test_low_disk_preserves_previous_latest(tmp_path,monkeypatch):
    fake=Fake();scanner=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW)
    m.atomic(tmp_path/'latest.json',{'sentinel':'preserved'})
    class Usage:free=10
    monkeypatch.setattr(m.shutil,'disk_usage',lambda _:Usage())
    assert scanner.cycle()['error']=='disk_headroom_below_1GiB'
    assert json.loads((tmp_path/'latest.json').read_text())=={'sentinel':'preserved'}

def test_derived_metrics_require_comparison_and_volume_timestamps():
    payload=snap();payload['prevDailyBar']['t']=(NOW+timedelta(days=1)).isoformat();payload['dailyBar']['t']='bad'
    row=observe('SPY','us_equity',payload,NOW,market_open=True)
    assert row['state']=='fresh' and row['change_pct'] is None and row['volume'] is None
    assert row['comparison_asof'] is None and row['volume_asof'] is None
    assert not summarize([row])[1]
    assert observe('SPY','us_equity',snap(8*86400),NOW,market_open=False)['state']=='stale'
    assert observe('ES=F','futures',snap(1801),NOW)['state']=='stale'

@pytest.mark.parametrize('payload',[{'chart':[]},{'chart':{'result':[None]}},{'chart':{'result':[{'indicators':[]}]}},{'chart':{'result':[{'timestamp':[1],'indicators':{'quote':[{'close':None}]}}]}}])
def test_malformed_futures_nested_data(payload):
    with pytest.raises(ValueError):futures_snapshot(payload)

def test_progress_publish_does_not_overwrite_last_completed_snapshot(tmp_path):
    scanner=m.Scanner(Fake(),tmp_path,futures=False,now=lambda:NOW)
    m.atomic(tmp_path/'latest.json',{'sentinel':'last-complete'})
    scanner.publish({'errors':[]},[observe('SPY','us_equity',snap(),NOW,market_open=True)])
    assert json.loads((tmp_path/'latest.json').read_text())=={'sentinel':'last-complete'}

def test_explicit_sip_metadata_and_no_fallback(tmp_path):
    class SIP(Fake):
        def get(self,source,path,params=None):
            if path=='/v2/stocks/snapshots':
                assert params['feed']=='sip'
                raise m.ScanError('http_403')
            return super().get(source,path,params)
    scanner=m.Scanner(SIP(),tmp_path,futures=False,now=lambda:NOW,feed='sip')
    status=scanner.cycle()
    assert status['stock_feed']=='sip' and status['feed_fallback'] is False
    assert status['sources']['equities']['error']=='restricted_or_unavailable_symbols'
    assert any('http_403' in error for error in status['errors'])
    rows=json.loads((tmp_path/'latest.json').read_text())['records']
    assert all(r['source']=='alpaca_sip' and r['state']=='unavailable' for r in rows if r['asset_class']=='us_equity')

def test_interrupted_cycle_preserves_all_completed_profiles(tmp_path):
    fake=Fake();scanner=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW);scanner.cycle()
    old=(tmp_path/'latest.json').read_bytes()
    class Interrupt(Fake):
        def get(self,source,path,params=None):
            if path=='/v1beta3/crypto/us/snapshots':raise KeyboardInterrupt()
            return super().get(source,path,params)
    restarted=m.Scanner(Interrupt(),tmp_path,futures=False,now=lambda:NOW)
    with pytest.raises(KeyboardInterrupt):restarted.cycle()
    assert (tmp_path/'latest.json').read_bytes()==old
    loaded=m.Scanner(fake,tmp_path,futures=False,now=lambda:NOW)
    assert loaded.previous[('crypto','BTC/USD')]['observation_count']==1

def test_retained_iex_observation_never_relabeled_sip_on_failed_upgrade():
    previous=observe('SPY','us_equity',snap(),NOW,market_open=True,feed='iex')
    failed=observe('SPY','us_equity',None,NOW,market_open=True,previous=previous,failure='http_403',feed='sip')
    assert failed['source']=='alpaca_iex' and failed['feed']=='iex'
    assert failed['requested_source']=='alpaca_sip' and failed['requested_feed']=='sip'
    assert failed['state']=='unavailable'
    successful=observe('SPY','us_equity',snap(),NOW,market_open=True,previous=failed,feed='sip')
    assert successful['source']=='alpaca_sip' and successful['feed']=='sip'

def test_extreme_finite_inputs_do_not_emit_infinite_derived_values():
    payload=snap(price=1e308);payload['prevDailyBar']['c']=1e-300
    row=observe('SPY','us_equity',payload,NOW,market_open=True)
    assert row['change_pct'] is None
    json.dumps(row,allow_nan=False)

def test_forbidden_symbol_isolation_retains_accessible_siblings(tmp_path):
    class Restricted:
        def __init__(self):self.calls=[]
        def get(self,source,path,params):
            assert params['feed']=='sip'
            symbols=params['symbols'].split(',');self.calls.append(symbols)
            if 'BAD' in symbols:raise m.ScanError('http_403')
            return {s:snap() for s in symbols}
    transport=Restricted();scanner=m.Scanner(transport,tmp_path,futures=False,feed='sip')
    data,errors=scanner.stock_batch(['AAA','BAD','CCC','DDD'],[32])
    assert set(data)=={'AAA','CCC','DDD'} and errors=={'BAD':'http_403'}
    assert len(transport.calls)==6

def test_all_forbidden_requests_have_shared_bounded_isolation_budget(tmp_path):
    class Forbidden:
        def __init__(self):self.calls=0
        def get(self,*args,**kwargs):self.calls+=1;raise m.ScanError('http_403')
    transport=Forbidden();scanner=m.Scanner(transport,tmp_path,futures=False,feed='sip');budget=[32]
    data,errors=scanner.stock_batch([f'S{i}' for i in range(200)],budget)
    assert not data and len(errors)==200 and transport.calls==33 and budget==[0]
    assert 'http_403_isolation_budget_exhausted' in errors.values()
    scanner.stock_batch(['NEXT','BATCH'],budget)
    assert transport.calls==34

def test_non403_failure_is_not_bisected_and_futures_disabled_is_not_error(tmp_path):
    class Failed:
        def __init__(self):self.calls=0
        def get(self,*args,**kwargs):self.calls+=1;raise m.ScanError('http_429')
    transport=Failed();scanner=m.Scanner(transport,tmp_path,futures=False)
    data,errors=scanner.stock_batch(['A','B'],[32])
    assert transport.calls==1 and errors=={'A':'http_429','B':'http_429'}
    status=m.Scanner(Fake(),tmp_path/'normal',futures=False,now=lambda:NOW).cycle()
    assert status['sources']['futures']['status']=='disabled'
    assert not any(error.startswith('futures:') for error in status['errors'])

def test_transient403_recovers_once_before_bisection(tmp_path):
    class Transient:
        def __init__(self):self.calls=0
        def get(self,source,path,params):
            assert params['feed']=='sip';self.calls+=1
            if self.calls==1:raise m.ScanError('http_403')
            return {s:snap() for s in params['symbols'].split(',')}
    transport=Transient();scanner=m.Scanner(transport,tmp_path,feed='sip');budget=[32]
    data,errors=scanner.stock_batch(['A','B'],budget)
    assert set(data)=={'A','B'} and errors=={} and transport.calls==2 and budget==[31]
