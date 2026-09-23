import pytest
from trader_engine.execution.recovery import QuoteRecoveryPolicy
from trader_engine.execution.broker_priority import lookup_order


def test_fifteen_second_protected_retry_then_exit_intent():
    p=QuoteRecoveryPolicy()
    a=p.update('A',category='data',protection_verified=True,now=0)
    assert a.retry_quote and a.freeze_entries and a.preserve_protection and a.deadline==15
    assert p.update('A',category='data',protection_verified=True,now=14.99).retry_quote
    d=p.update('A',category='data',protection_verified=True,now=15)
    assert d.escalate and d.drain_scope=='symbol' and not d.retry_quote and d.preserve_protection
    assert p.update('A',category='healthy',protection_verified=True,now=16).freeze_entries


def test_repeated_errors_do_not_extend_deadline():
    p=QuoteRecoveryPolicy()
    for t in [0,3,6,9,12]:
        assert p.update('A',category='data',protection_verified=True,now=t).deadline==15


def test_global_freeze_until_every_symbol_recovers():
    p=QuoteRecoveryPolicy()
    p.update('A',category='data',protection_verified=True,now=0)
    p.update('B',category='data',protection_verified=True,now=1)
    assert p.update('A',category='healthy',protection_verified=True,now=2).freeze_entries
    assert not p.update('B',category='healthy',protection_verified=True,now=3).freeze_entries


def test_unknown_protection_immediately_reconciles_never_cancels():
    p=QuoteRecoveryPolicy()
    d=p.update('A',category='data',protection_verified=False,now=0)
    assert d.escalate and d.reconcile_orders and d.preserve_protection and d.drain_scope is None
    assert p.update('A',category='healthy',protection_verified=True,now=1).freeze_entries
    assert not p.reconcile('A',ownership_verified=True,protection_verified=True,now=2).freeze_entries


def test_account_incident_is_not_cleared_by_symbol_quote():
    p=QuoteRecoveryPolicy()
    p.update('A',category='account',protection_verified=True,now=0)
    assert p.update('A',category='healthy',protection_verified=True,now=1).freeze_entries
    assert not p.reconcile('__account__',ownership_verified=True,protection_verified=True,now=2).freeze_entries


def test_deadline_edge_and_invalid_clock():
    p=QuoteRecoveryPolicy()
    p.update('A',category='data',protection_verified=True,now=0)
    assert p.update('A',category='healthy',protection_verified=True,now=15).escalate
    with pytest.raises(ValueError):p.update('A',category='healthy',protection_verified=True,now=14)
    with pytest.raises(ValueError):QuoteRecoveryPolicy(16)


def test_order_lookup_compatibility_and_no_typeerror_retry():
    class Legacy:
        def get_order(self,cid):return cid
    class New:
        def get_order(self,cid,*,priority):return cid,priority
    assert lookup_order(Legacy(),'id')=='id'
    assert lookup_order(New(),'id')==('id','emergency')
    class Broken:
        calls=0
        def get_order(self,cid,*,priority):
            self.calls+=1;raise TypeError('inside method')
    b=Broken()
    with pytest.raises(TypeError):lookup_order(b,'id')
    assert b.calls==1


def test_holding_integration_isolates_two_symbol_incidents():
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from trader_engine.execution.breakout import hold_breakout
    base=datetime(2026,9,23,15,tzinfo=timezone.utc)
    class Clock:
        t=0.
        def now(self):return base+timedelta(seconds=self.t)
        def sleep(self,n):self.t+=n
    class Engine:
        stop_requested=entries_blocked=storage_failed=False
        def __init__(self):self.events=[]
        def _send(self,*args,**kwargs):return self._lookup(None,None)
        def _lookup(self,*args,**kwargs):return dict(status='new',qty='10',filled_qty='0',stop_price='99.00')
        def _event(self,row):self.events.append(row)
        def stop(self):self.stop_requested=True
    c=Clock();p=QuoteRecoveryPolicy();a=Engine();b=Engine()
    p.update('B',category='data',protection_verified=True,now=c.t)
    attempts=[]
    def qa(symbol):
        attempts.append(symbol)
        if len(attempts)==1:return dict(t=(c.now()-timedelta(seconds=20)).isoformat(),bp=100,ap=100.01,bs=1,**{'as':1})
        return dict(t=c.now().isoformat(),bp=102,ap=102.01,bs=1,**{'as':1})
    def run(symbol,engine,quote):
        return hold_breakout(SimpleNamespace(symbol=symbol,exit_ids=('stop',)),dict(filled_avg_price='100',filled_qty='10'),engine,
          get_quote=quote,close_at=base+timedelta(hours=1),now=c.now,sleep=c.sleep,monotonic=lambda:c.t,recovery_policy=p)
    assert run('A',a,qa)=='profit_target'
    assert p.freeze_entries and not a.stop_requested and not b.stop_requested
    assert a.events[0]['kind']=='holding_quote_rejected'
    assert 'stale_source' in a.events[0]['reasons']
    assert run('B',b,lambda _:dict(t=c.now().isoformat(),bp=102,ap=102.01,bs=1,**{'as':1}))=='profit_target'
    assert not p.freeze_entries


def test_lifecycle_lookup_passes_emergency_without_breaking_legacy():
    from trader_engine.execution.lifecycle import PaperExecutor
    class Broker:
        def __init__(self):self.priority=None
        def get_order(self,cid,*,priority):self.priority=priority;return None
    class Journal:
        def append(self,record):pass
    b=Broker();e=PaperExecutor(b,Journal())
    assert e._lookup(None,'never-attempted',priority='emergency') is None
    assert b.priority=='emergency'
