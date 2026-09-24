import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from copy import deepcopy
import pytest
from trader_engine.execution.portfolio import Reservations,SymbolBroker
from trader_engine.execution.lifecycle import PaperExecutor,TradePlan,EntryBlocked
from test_paper_lifecycle import FakeBroker,MemoryJournal

def test_multiple_reservations_share_cash_without_count_cap():
 r=Reservations('100000')
 for i in range(6):r.reserve(str(i),'15000','15003')
 assert r.snapshot()==(Decimal('90000'),Decimal('9982'))
 r.reserve('7','9900','9902')
 with pytest.raises(ValueError):r.reserve('8','100','100')
 with pytest.raises(ValueError):r.reserve('0','1','1')
 r.release_flat('0','-150')
 assert r.snapshot()==(Decimal('84900'),Decimal('14933'))

def test_unknown_outcome_keeps_funds_reserved():
 r=Reservations('1000');r.reserve('A','900','901')
 with pytest.raises(ValueError):r.release_flat('A','NaN')
 assert r.snapshot()==(Decimal('900'),Decimal('99'))

def test_simultaneous_reservations_cannot_double_spend():
 r=Reservations('1000');barrier=threading.Barrier(2)
 def reserve(s):
  barrier.wait()
  try:r.reserve(s,'700','701');return True
  except ValueError:return False
 with ThreadPoolExecutor(2) as pool:results=list(pool.map(reserve,['A','B']))
 assert sum(results)==1 and r.snapshot()[1]==299

def test_scope_keeps_foreign_same_symbol_orders_visible():
 b=FakeBroker();b.foreign_orders=[{'symbol':'SPY','client_order_id':'foreign'},{'symbol':'QQQ','client_order_id':'other'}]
 scoped=SymbolBroker(b,'SPY')
 assert scoped.open_orders()==[b.foreign_orders[0]]
 with pytest.raises(EntryBlocked):PaperExecutor(scoped,MemoryJournal()).execute(TradePlan.create('SPY',1,'100'))
 with pytest.raises(ValueError):scoped.submit({'symbol':'QQQ'})
 with pytest.raises(ValueError):scoped.position_qty('QQQ')

class MultiBroker:
 telemetry_failed=False
 def __init__(self):self.orders={};self.positions={};self.lock=threading.RLock();self.sent=[]
 def open_orders(self):
  with self.lock:return [deepcopy(o) for o in self.orders.values() if o['status']=='new']
 def get_order(self,cid):
  with self.lock:return deepcopy(self.orders.get(cid))
 def position_qty(self,s):
  with self.lock:return self.positions.get(s,Decimal(0))
 def submit(self,p):
  with self.lock:
   self.sent.append(deepcopy(p));stop=p['type']=='stop';qty=Decimal(p['qty'])
   o={**p,'id':str(len(self.sent)),'status':'new' if stop else 'filled','filled_qty':'0' if stop else str(qty),'filled_avg_price':None if stop else '100'}
   if not stop:self.positions[p['symbol']]=self.positions.get(p['symbol'],Decimal(0))+(qty if p['side']=='buy' else -qty)
   self.orders[p['client_order_id']]=o;return deepcopy(o)
 def cancel(self,oid):
  with self.lock:
   for o in self.orders.values():
    if o['id']==oid:o['status']='canceled'

def test_two_live_positions_with_stops_exit_independently():
 b=MultiBroker();barrier=threading.Barrier(2);simultaneous=[]
 def run(symbol):
  def hold(plan,entry,engine):
   engine._send(plan,plan.exit_ids[0],Decimal(entry['filled_qty']),stop_price=Decimal('99'))
   barrier.wait(timeout=3)
   with b.lock:simultaneous.append(sum(v>0 for v in b.positions.values()))
   barrier.wait(timeout=3)
  e=PaperExecutor(SymbolBroker(b,symbol),MemoryJournal(),polls=1,sleep=lambda _:None,hold_callback=hold)
  return e.execute(TradePlan.create(symbol,10,'100',entry_tif='day'))
 with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['SPY','QQQ']))
 assert simultaneous==[2,2]
 assert all(r.status=='flat' for r in results)
 assert all(v==0 for v in b.positions.values()) and not b.open_orders()
 assert len(b.sent)==6

def test_other_symbols_stop_does_not_block_new_entry():
 b=MultiBroker();b.orders['existing']={'id':'outside','symbol':'QQQ','status':'new','client_order_id':'existing'}
 e=PaperExecutor(SymbolBroker(b,'SPY'),MemoryJournal(),polls=1,sleep=lambda _:None)
 r=e.execute(TradePlan.create('SPY',1,'100'))
 assert r.status=='flat' and b.orders['existing']['status']=='new'

def test_coordinator_runs_two_positions_and_finishes_flat(tmp_path,monkeypatch):
 import importlib.util,sys
 from pathlib import Path
 from datetime import timedelta
 from test_paper_breakout import NOW,bars
 root=Path(__file__).resolve().parents[1];monkeypatch.syspath_prepend(str(root/'scripts'))
 spec=importlib.util.spec_from_file_location('portfolio_runner',root/'scripts/paper_portfolio_until_close.py')
 runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
 out=tmp_path/'session';barrier=threading.Barrier(2);b=MultiBroker()
 b.__class__.__enter__=lambda self:self
 b.__class__.__exit__=lambda self,*a:None
 b.account=lambda:{'status':'ACTIVE','equity':'100000','cash':'100000','trading_blocked':False}
 b.clock=lambda:{'is_open':True,'timestamp':NOW.isoformat(),'next_close':(NOW+timedelta(minutes=30)).isoformat()}
 b.positions=lambda:[{'symbol':s,'qty':str(q)} for s,q in b.positions_map.items() if q]
 # Preserve the fake's position storage while exposing the real adapter method.
 b.positions_map={}
 def qty(s):return b.positions_map.get(s,Decimal(0))
 b.position_qty=qty
 original_submit=b.submit
 def submit(p):
  with b.lock:
   method=b.positions;b.positions=b.positions_map
   try:return original_submit(p)
   finally:b.positions=method
 b.submit=submit
 class Market:
  def __init__(self,*,feed='iex'):assert feed=='iex'
  def assets(self):return ['SPY','QQQ']
  def shortlist(self,*a):return ['SPY','QQQ'],2,2
  def bars(self,s):return {ticker:bars() for ticker in s}
  def quotes(self,s):return {ticker:{'bp':101,'ap':101.01,'t':NOW.isoformat()} for ticker in s}
 simultaneous=[]
 def holding(plan,entry,engine,**kwargs):
  engine._send(plan,plan.exit_ids[0],Decimal(entry['filled_qty']),stop_price=Decimal('99'))
  barrier.wait(timeout=5)
  with b.lock:simultaneous.append(sum(v>0 for v in b.positions_map.values()))
  barrier.wait(timeout=5)
  (out/'STOP').touch()
 monkeypatch.setattr(runner,'AlpacaPaperClient',lambda **k:b)
 monkeypatch.setattr(runner,'Market',Market);monkeypatch.setattr(runner,'utc',lambda:NOW)
 monkeypatch.setattr(runner,'hold_breakout',holding)
 monkeypatch.setenv('APCA_API_KEY_ID','test-portfolio-account')
 result=runner.run(out)
 assert result['status']=='stopped',result
 assert result['completed_round_trips']==2
 assert result['remaining_positions']==[] and result['remaining_open_orders']==[]
 assert simultaneous==[2,2]
