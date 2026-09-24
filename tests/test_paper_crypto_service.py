import importlib.util
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
import json
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('paper_crypto_service',ROOT/'scripts/paper_crypto_service.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Broker:
    def __init__(self):self.orders={};self.posts=[];self.fail=False
    def get_order(self,cid):return self.orders.get(cid)
    def submit(self,payload):
        self.posts.append(payload.copy())
        if self.fail:raise TimeoutError('secret should never be printed')
        return {'status':'new'}

def buy():return {'order':{'symbol':'BTC/USD','side':'buy','qty':'1','type':'limit','time_in_force':'ioc','limit_price':'100'}}
def position(qty):return [{'symbol':'BTCUSD','qty':str(qty)}] if Decimal(str(qty)) else []
def terminal(s,qty,status='filled'):
    p=s.state['pending'];return dict(client_order_id=p['client_order_id'],symbol='BTCUSD',side=p['payload']['side'],filled_qty=str(qty),status=status,**{k:v for k,v in p['payload'].items() if k in ('type','time_in_force','qty','limit_price')})

def test_ambiguous_post_is_durable_and_never_resubmitted_after_restart(tmp_path):
    b=Broker();b.fail=True;s=m.Service(b,None,tmp_path,'account')
    s.submit(buy(),'2026-09-23',[])
    assert len(b.posts)==1
    restarted=m.Service(b,None,tmp_path,'account')
    assert restarted.state['pending']
    assert not restarted.reconcile([])
    with pytest.raises(ValueError,match='Pending'):restarted.submit(buy(),'2026-09-23',[])
    assert len(b.posts)==1

def test_fee_deducted_terminal_fill_owns_net_and_prevents_same_day_repeat(tmp_path):
    b=Broker();s=m.Service(b,None,tmp_path,'account');s.submit(buy(),'2026-09-23',[])
    cid=s.state['pending']['client_order_id'];b.orders[cid]=terminal(s,'1')
    assert s.reconcile(position('.9975'))
    assert s.state['ownership']=={'BTC/USD':'0.9975'}
    with pytest.raises(ValueError,match='already attempted'):s.submit(buy(),'2026-09-23',position('.9975'))

def test_partial_terminal_sale_preserves_exact_residual(tmp_path):
    b=Broker();s=m.Service(b,None,tmp_path,'account');s.state['ownership']={'BTC/USD':'1'}
    sell={'order':{'symbol':'BTC/USD','side':'sell','qty':'1','type':'market','time_in_force':'ioc'}}
    s.submit(sell,'2026-09-23',position(1));cid=s.state['pending']['client_order_id'];b.orders[cid]=terminal(s,'.4','canceled')
    assert s.reconcile(position('.6'))
    assert s.state['ownership']=={'BTC/USD':'0.6'}
    assert m.Service(b,None,tmp_path,'account').state['ownership']==s.state['ownership']

def test_foreign_position_delta_blocks_ownership(tmp_path):
    b=Broker();s=m.Service(b,None,tmp_path,'account');s.submit(buy(),'2026-09-23',[])
    cid=s.state['pending']['client_order_id'];b.orders[cid]=terminal(s,'1')
    with pytest.raises(ValueError,match='foreign'):s.reconcile(position('2'))
    assert s.state['pending'] and not s.state['ownership']

def test_intent_saved_before_post(tmp_path):
    class Check(Broker):
        def submit(self,payload):
            saved=json.loads((tmp_path/'state.json').read_text())
            assert saved['pending']['client_order_id']==payload['client_order_id']
            assert saved['attempted']['2026-09-23']==['BTC/USD']
            return super().submit(payload)
    b=Check();m.Service(b,None,tmp_path,'account').submit(buy(),'2026-09-23',[])
    assert len(b.posts)==1

def test_observe_only_never_submits(tmp_path):
    b=Broker();s=m.Service(b,None,tmp_path,'account',observe_only=True)
    with pytest.raises(ValueError,match='Observation'):s.submit(buy(),'2026-09-23',[])
    assert not b.posts and not (tmp_path/'state.json').exists()

def test_seed_verifies_order_and_preserves_prior_eth_attempt(tmp_path):
    b=Broker();b.orders[m.SEED_CLIENT]=dict(id=m.SEED_ID,status='filled',side='buy',symbol='BTCUSD',filled_qty='0.14699704')
    eth='cx-20260924-sma200-eth-01';b.orders[eth]=dict(status='canceled',side='buy',symbol='ETHUSD',filled_qty='0')
    seed=tmp_path/'seed.json';seed.write_text(json.dumps(dict(account_id='account',client_order_id=m.SEED_CLIENT,order_id=m.SEED_ID,owned_net_qty=str(m.SEED_QTY),prior_entry_attempts=[dict(client_order_id=eth,symbol='ETH/USD',signal_day='2026-09-23')])))
    s=m.Service(b,None,tmp_path/'service','account',seed=seed)
    s.seed_existing({'id':'account'},position(m.SEED_QTY))
    assert s.state['ownership']['BTC/USD']==str(m.SEED_QTY)
    assert s.state['attempted']['2026-09-23']==['BTC/USD','ETH/USD']

def test_market_quote_outage_retains_exit_bars():
    market=m.Market('fixture','fixture')
    def get(path,params):
        if path=='bars':return {'bars':{'BTC/USD':[{'t':'fixture','c':1}]}}
        raise m.ServiceError('Market data unavailable')
    market.get=get
    bars,quotes=market.snapshot(datetime(2026,9,24,tzinfo=timezone.utc))
    assert bars['BTC/USD'] and quotes=={}

def test_exit_priority_stop_and_asset_failure_do_not_trigger_entry(tmp_path,monkeypatch):
    class Full(Broker):
        def account(self):return dict(id='account',equity='97000',cash='90000',status='ACTIVE',trading_blocked=False,account_blocked=False)
        def positions(self):return position(1)
        def open_orders(self):return []
        def asset(self,s):
            if s=='ETH/USD':raise TimeoutError()
            return {}
    class Data:
        def snapshot(self,now):return {},{}
    b=Full();s=m.Service(b,Data(),tmp_path,'account');s.state['ownership']={'BTC/USD':'1'}
    s.state['daily']=dict(date='2026-09-24',start_equity='100000',entry_halted=False)
    (tmp_path/'STOP').touch()
    def planner(snapshot,owned,attempted):
        assert snapshot['entries_enabled'] is False
        assert snapshot['quotes']=={}
        return {'signal_day':'2026-09-23','plans':[dict(symbol='ETH/USD',action='entry',**buy()),dict(symbol='BTC/USD',action='exit',order=dict(symbol='BTC/USD',side='sell',qty='1',type='market',time_in_force='ioc'))]}
    monkeypatch.setattr(m,'plan_crypto_actions',planner)
    status=s.tick(datetime(2026,9,24,tzinfo=timezone.utc))
    assert status['error'] is None
    assert len(b.posts)==1 and b.posts[0]['side']=='sell'
    assert s.state['daily']['entry_halted'] is True

def test_failed_state_write_prevents_post_and_latches(tmp_path,monkeypatch):
    b=Broker();s=m.Service(b,None,tmp_path,'account')
    original=m.atomic
    def fail_state(path,value):
        if path.name=='state.json':raise OSError('disk full')
        return original(path,value)
    monkeypatch.setattr(m,'atomic',fail_state)
    with pytest.raises(m.ServiceError,match='Durable'):s.submit(buy(),'2026-09-23',[])
    assert not b.posts and s.storage_failed
    assert s.tick()['error']=='Durable state write previously failed; restart reconciliation required'

def test_terminal_order_terms_must_match_intent(tmp_path):
    b=Broker();s=m.Service(b,None,tmp_path,'account');s.submit(buy(),'2026-09-23',[])
    cid=s.state['pending']['client_order_id'];order=terminal(s,1);order['qty']='2';b.orders[cid]=order
    with pytest.raises(m.ServiceError,match='quantity mismatch'):s.reconcile(position('.9975'))
    assert s.state['pending']

def test_blocked_owned_symbol_prevents_other_entry(tmp_path,monkeypatch):
    class Full(Broker):
        def account(self):return dict(id='account',equity='100000',cash='90000',status='ACTIVE',trading_blocked=False,account_blocked=False)
        def positions(self):return position(1)
        def open_orders(self):return []
        def asset(self,s):return {}
    class Data:
        def snapshot(self,now):return {},{}
    b=Full();s=m.Service(b,Data(),tmp_path,'account');s.state['ownership']={'BTC/USD':'1'}
    monkeypatch.setattr(m,'plan_crypto_actions',lambda *args:dict(signal_day='2026-09-23',plans=[dict(symbol='BTC/USD',action='blocked',reason='missing bars'),dict(symbol='ETH/USD',action='entry',**buy())]))
    status=s.tick(datetime(2026,9,24,tzinfo=timezone.utc))
    assert status['status']=='needs_attention'
    assert status['error']=='owned_position_management_blocked'
    assert not b.posts
