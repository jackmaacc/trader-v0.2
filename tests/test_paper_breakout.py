from datetime import datetime,timezone,timedelta
from decimal import Decimal
from copy import deepcopy
from dataclasses import asdict
import pytest
from trader_engine.execution.breakout import candidate,hold_breakout
from trader_engine.execution.lifecycle import TradePlan,PaperExecutor,EntryBlocked
from trader_engine.execution.alpaca_paper import EntryNotSubmitted
from test_paper_lifecycle import FakeBroker,MemoryJournal

NOW=datetime(2026,9,23,19,15,tzinfo=timezone.utc)
def bars():
 rows=[]
 for i in range(16):
  rows.append({'t':(NOW-timedelta(minutes=16-i)).isoformat(),'o':100,'h':100.2,'l':99.8,'c':100,'v':1000})
 rows[-1].update(o=100,h=101.2,l=99.9,c=101,v=2000)
 return rows

def test_completed_breakout_and_volume_confirmation():
 s=candidate(bars(),NOW)
 assert s and s['breakout_level']=='100.2' and s['relative_volume']=='2'

@pytest.mark.parametrize('failure',['forming','gap','duplicate','volume','stale','no_breakout','nan'])
def test_bad_or_unconfirmed_signals_are_rejected(failure):
 b=bars();now=NOW
 if failure=='forming':b[-1]['t']=NOW.isoformat()
 if failure=='gap':b.pop(5)
 if failure=='duplicate':b[5]['t']=b[4]['t']
 if failure=='volume':b[-1]['v']=1100
 if failure=='stale':now=NOW+timedelta(minutes=2)
 if failure=='no_breakout':b[-1]['c']=100.1
 if failure=='nan':b[-1]['h']='NaN'
 assert candidate(b,now) is None

def test_future_bar_cannot_change_completed_signal():
 b=bars();s=candidate(b,NOW)
 b.append({'t':NOW.isoformat(),'o':999,'h':9999,'l':1,'c':999,'v':999999})
 assert candidate(b,NOW)==s

class StopBroker(FakeBroker):
 def submit(self,p):
  if p['type']=='stop':
   self.sent.append(deepcopy(p));self.counter+=1
   o={**p,'id':f'order-{self.counter}','status':'new','filled_qty':'0','filled_avg_price':None}
   self.orders[p['client_order_id']]=o;return deepcopy(o)
  return super().submit(p)

class Timer:
 def __init__(self):self.t=0
 def now(self):return NOW+timedelta(seconds=self.t)
 def mono(self):return self.t
 def sleep(self,s):self.t+=s

def run_hold(*,bid='100.2',stop_fill=0,cancel_race=False,fail_reporting=False,stop_requested=False,hold_seconds=9):
 b=StopBroker();j=MemoryJournal();timer=Timer();notes=[]
 original_cancel=b.cancel
 def cancel(id):
  o=next(x for x in b.orders.values() if x['id']==id)
  if cancel_race and o['type']=='stop':
   remaining=Decimal(o['qty'])-Decimal(o['filled_qty']);b.position-=remaining
   o.update(status='filled',filled_qty=o['qty'],filled_avg_price='99');return
  return original_cancel(id)
 b.cancel=cancel
 def quote(symbol):
  if stop_fill:
   stop=next(o for o in b.orders.values() if o['type']=='stop')
   if stop['filled_qty']=='0':
    b.position-=Decimal(stop_fill);stop.update(status='filled' if stop_fill==10 else 'partially_filled',filled_qty=str(stop_fill),filled_avg_price='99')
  return {'bp':bid,'ap':str(Decimal(bid)+Decimal('.01')),'t':timer.now().isoformat()}
 def note(row):
  notes.append(row)
  if fail_reporting:raise OSError(28,'disk full')
 def holding(plan,entry,owner):
  return hold_breakout(plan,entry,owner,get_quote=quote,close_at=NOW+timedelta(minutes=30),
    should_stop=lambda:stop_requested,notify=note,sleep=timer.sleep,monotonic=timer.mono,now=timer.now,max_hold_seconds=hold_seconds)
 e=PaperExecutor(b,j,polls=1,sleep=lambda _:None,hold_callback=holding)
 result=e.execute(TradePlan.create('SPY',10,'100.00',entry_tif='day'))
 return b,e,result,timer,notes

def test_position_is_held_until_time_exit_with_broker_stop():
 b,e,r,t,notes=run_hold()
 assert r.status=='flat' and t.t==9 and notes[-1]['exit_reason']=='time_exit'
 assert [o['type'] for o in b.sent]==['limit','stop','market']
 assert b.sent[0]['time_in_force']=='day' and b.sent[1]['stop_price']=='99.00'
 assert b.position==0

def test_target_closes_only_after_protective_stop_cancelled():
 b,e,r,t,notes=run_hold(bid='102.1')
 assert r.status=='flat' and notes[-1]['exit_reason']=='profit_target'
 assert next(o for o in b.orders.values() if o['type']=='stop')['status']=='canceled'
 assert len(b.sent)==3 and b.position==0

def test_stop_fill_never_sends_additional_sell():
 b,e,r,t,notes=run_hold(stop_fill=10)
 assert r.status=='flat' and len(b.sent)==2 and b.position==0
 assert notes[-1]['exit_reason']=='protective_stop'

def test_partial_stop_fill_sells_only_remaining_shares():
 b,e,r,t,notes=run_hold(stop_fill=4)
 assert r.status=='flat' and b.sent[-1]['qty']=='6' and b.position==0

def test_stop_filling_during_cancel_never_oversells():
 b,e,r,t,notes=run_hold(bid='102.1',cancel_race=True)
 assert r.status=='flat' and len(b.sent)==2 and b.position==0

def test_failed_hold_reporting_still_exits():
 b,e,r,t,notes=run_hold(fail_reporting=True)
 assert r.status=='flat' and b.position==0 and e.entries_blocked

def test_operator_stop_closes_without_waiting_for_time_exit():
 b,e,r,t,notes=run_hold(stop_requested=True)
 assert r.status=='flat' and t.t==0 and b.position==0

def test_local_guard_rejection_is_not_an_ambiguous_broker_order():
 b=FakeBroker();j=MemoryJournal();e=PaperExecutor(b,j,polls=1,sleep=lambda _:None)
 calls=[]
 def blocked(payload):calls.append(payload);raise EntryNotSubmitted('local block')
 b.submit=blocked;p=TradePlan.create('SPY',10,'100')
 r=e.execute(p)
 assert r.status=='not_submitted' and r.residual=='0' and not e.uncertain and not e.attempted
 assert len(calls)==1 and b.position==0 and e.active is None
 assert any(x['kind']=='not_submitted' for x in j.rows)
 with pytest.raises(EntryBlocked):e.execute(p)

def test_invalid_entry_time_in_force_is_rejected():
 with pytest.raises(ValueError):TradePlan.create('SPY',1,'100',entry_tif='gtc')


def test_holding_data_failure_preserves_protection_until_same_process_cleanup():
 b=StopBroker();j=MemoryJournal();timer=Timer()
 def fail_quote(symbol):raise ConnectionError('feed interrupted')
 def holding(plan,entry,owner):
  hold_breakout(plan,entry,owner,get_quote=fail_quote,close_at=NOW+timedelta(minutes=30),
    sleep=timer.sleep,monotonic=timer.mono,now=timer.now)
 e=PaperExecutor(b,j,polls=1,sleep=lambda _:None,hold_callback=holding)
 r=e.execute(TradePlan.create('SPY',10,'100',entry_tif='day'))
 assert r.status=='needs_reconciliation' and len(b.sent)==2 and b.position==10
 assert next(o for o in b.orders.values() if o['type']=='stop')['status']=='new'
 r=e.resume_drain()
 assert r.status=='flat' and len(b.sent)==3 and b.position==0
