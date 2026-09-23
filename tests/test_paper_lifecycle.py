from copy import deepcopy
from decimal import Decimal
import pytest
from trader_engine.execution.lifecycle import TradePlan, PaperExecutor, EntryBlocked

class MemoryJournal:
    def __init__(self, fail_kind=None):
        self.rows=[];self.fail_kind=fail_kind
    def append(self,row):
        if self.fail_kind in ('all',row['kind']):raise OSError(28,'No space left on device')
        self.rows.append(deepcopy(row))
    def records(self):return deepcopy(self.rows)

class FakeBroker:
    telemetry_failed=False
    def __init__(self):
        self.orders={};self.sent=[];self.position=Decimal(0);self.counter=0
        self.partial_buy=None;self.partial_sell=None;self.timeout_side=None
        self.lookup_delay=0;self.cancel_pending=False;self.missing_price=False
        self.cancel_fill_extra=Decimal(0);self.foreign_orders=[];self.read_failures=0
    def open_orders(self):
        return self.foreign_orders+[deepcopy(o) for o in self.orders.values() if o['status'] not in {'filled','canceled','rejected','expired'}]
    def position_qty(self,s):return self.position
    def get_order(self,cid):
        if self.read_failures:
            self.read_failures-=1;raise ConnectionError('temporary')
        if self.lookup_delay and cid in self.orders:
            self.lookup_delay-=1;return None
        return deepcopy(self.orders.get(cid))
    def submit(self,payload):
        self.sent.append(deepcopy(payload));self.counter+=1
        qty=Decimal(payload['qty']);status='filled';filled=qty
        partial=self.partial_buy if payload['side']=='buy' else self.partial_sell
        if partial is not None:
            filled=partial;status='partially_filled'
            if payload['side']=='sell':self.partial_sell=None
        if payload['side']=='buy':self.position+=filled
        else:self.position-=filled
        o={**payload,'id':f'order-{self.counter}','status':status,'filled_qty':str(filled),'filled_avg_price':None if self.missing_price else ('100' if payload['side']=='buy' else '101')}
        self.orders[payload['client_order_id']]=o
        if self.timeout_side==payload['side']:
            self.timeout_side=None;raise TimeoutError('lost response after acceptance')
        return deepcopy(o)
    def cancel(self,oid):
        for o in self.orders.values():
            if o['id']==oid:
                if self.cancel_pending:o['status']='pending_cancel';return
                if o['side']=='buy' and self.cancel_fill_extra:
                    o['filled_qty']=str(Decimal(o['filled_qty'])+self.cancel_fill_extra);self.position+=self.cancel_fill_extra
                o['status']='canceled'

def engine(b=None,j=None):
    b=b or FakeBroker();j=j or MemoryJournal()
    return b,j,PaperExecutor(b,j,polls=1,read_retries=3,sleep=lambda _:None,poll_seconds=0)

def plan():return TradePlan.create('SPY',10,'100.00')

def test_normal_round_trip_reconciles_fills_and_account():
    b,j,e=engine();r=e.execute(plan())
    assert r.status=='flat' and r.pnl=='10' and b.position==0
    assert [o['side'] for o in b.sent]==['buy','sell']

@pytest.mark.parametrize('kind',['plan','attempt','all'])
def test_disk_full_before_entry_never_posts(kind):
    b,j,e=engine(j=MemoryJournal(kind))
    with pytest.raises(EntryBlocked):e.execute(plan())
    assert not b.sent and e.entries_blocked

@pytest.mark.parametrize('kind',['order','result'])
def test_disk_full_after_acknowledgment_still_exits(kind):
    b,j,e=engine(j=MemoryJournal(kind));r=e.execute(plan())
    assert r.status=='flat' and r.storage_failed and b.position==0
    assert len(b.sent)==2
    with pytest.raises(EntryBlocked):e.execute(plan())

def test_disk_full_during_exit_intent_does_not_gate_exit():
    b,j,e=engine();original=j.append
    def write(row):
        if row['kind']=='attempt' and row['payload']['side']=='sell':raise OSError(28,'disk full')
        original(row)
    j.append=write;r=e.execute(plan())
    assert r.status=='flat' and r.storage_failed and b.position==0

def test_telemetry_failure_keeps_broker_acknowledgment():
    b,j,e=engine();original=b.submit
    def submit(p):
        r=original(p);b.telemetry_failed=True;return r
    b.submit=submit;r=e.execute(plan())
    assert r.status=='flat' and e.entries_blocked

@pytest.mark.parametrize('side',['buy','sell'])
def test_lost_post_response_reconciles_without_duplicate(side):
    b,j,e=engine();b.timeout_side=side;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2 and b.position==0

def test_delayed_order_visibility_does_not_cause_resubmit():
    b,j,e=engine();b.timeout_side='buy';b.lookup_delay=2;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2

def test_unknown_exit_remains_pending_never_sends_second_sell():
    b,j,e=engine();original=b.submit
    def submit(payload):
        if payload['side']=='sell':
            original(payload);b.orders.pop(payload['client_order_id']);raise TimeoutError('unknown')
        return original(payload)
    b.submit=submit;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and r.residual is None
    assert [p['side'] for p in b.sent]==['buy','sell']

def test_cancel_race_uses_final_partial_entry_quantity():
    b,j,e=engine();b.partial_buy=Decimal('3');b.cancel_fill_extra=Decimal('2');r=e.execute(plan())
    assert r.status=='flat' and b.sent[1]['qty']=='5' and b.position==0

def test_partial_exit_sends_only_confirmed_residual():
    b,j,e=engine();b.partial_sell=Decimal('4');r=e.execute(plan())
    assert r.status=='flat' and [p['qty'] for p in b.sent]==['10','10','6']

def test_pending_cancel_never_overlaps_sell():
    b,j,e=engine();b.partial_buy=Decimal('3');b.cancel_pending=True;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==1

def test_pending_exit_cancel_never_sends_replacement():
    b,j,e=engine();b.partial_sell=Decimal('3');b.cancel_pending=True;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==2

def test_missing_fill_price_does_not_block_closure():
    b,j,e=engine();b.missing_price=True;r=e.execute(plan())
    assert r.status=='flat_accounting_pending' and r.pnl is None and b.position==0

def test_foreign_orders_block_entries():
    b,j,e=engine();b.foreign_orders=[{'client_order_id':'other'}]
    with pytest.raises(EntryBlocked):e.execute(plan())
    assert not b.sent

def test_foreign_position_not_liquidated():
    b,j,e=engine();b.position=Decimal(2)
    with pytest.raises(EntryBlocked):e.execute(plan())
    assert b.position==2 and not b.sent

def test_same_symbol_external_activity_stops_exit():
    b,j,e=engine();original=b.submit
    def submit(p):
        r=original(p)
        if p['side']=='buy':b.position+=1
        return r
    b.submit=submit;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==1 and b.position==11

def test_identity_mismatch_never_generates_sell():
    b,j,e=engine();original=b.get_order
    def get(cid):
        r=original(cid)
        if r:r['symbol']='QQQ'
        return r
    b.get_order=get;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==1

def test_transient_read_failures_recover_without_new_post():
    b,j,e=engine();b.read_failures=2;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2

def test_stop_after_entry_does_not_prevent_exit():
    b,j,e=engine();original=b.submit
    def submit(p):
        r=original(p)
        if p['side']=='buy':e.stop()
        return r
    b.submit=submit;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2

def test_restart_after_exit_fill_does_not_sell_twice():
    b,j,e=engine();p=plan();assert e.execute(p).status=='flat'
    recovered=PaperExecutor(b,j,polls=1,sleep=lambda _:None).recover(p)
    assert recovered.status=='flat' and len(b.sent)==2

def test_restart_ambiguous_exit_absent_stays_unresolved():
    b,j,e=engine();p=plan();assert e.execute(p).status=='flat'
    b.orders.pop(p.exit_ids[0]);r=PaperExecutor(b,j,polls=1,sleep=lambda _:None).recover(p)
    assert r.status=='needs_reconciliation' and len(b.sent)==2

def test_corrupt_journal_recovery_never_submits():
    b,j,e=engine();p=plan()
    def fail():raise OSError('corrupt')
    j.records=fail;r=e.recover(p)
    assert r.status=='needs_reconciliation' and not b.sent

@pytest.mark.parametrize('qty',['0','-1','NaN','Infinity','1.5'])
def test_invalid_plan_quantities_rejected(qty):
    with pytest.raises((ValueError,RuntimeError)):TradePlan.create('SPY',qty,'100')


def test_restart_after_lost_exit_intent_does_not_guess_absent_slot():
    from dataclasses import asdict
    b,j,e=engine();p=plan();j.rows=[{'kind':'plan','plan':asdict(p)}]
    b.submit({'symbol':p.symbol,'side':'buy','qty':'10','client_order_id':p.entry_id})
    r=e.recover(p)
    assert r.status=='needs_reconciliation' and len(b.sent)==1

def test_foreign_sell_appearing_after_entry_blocks_our_exit():
    b,j,e=engine();original=b.submit
    def submit(p):
        r=original(p)
        if p['side']=='buy':b.foreign_orders=[{'symbol':'SPY','side':'sell','client_order_id':'foreign'}]
        return r
    b.submit=submit;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==1

def test_preflight_telemetry_failure_blocks_entry():
    b,j,e=engine();b.telemetry_failed=True
    with pytest.raises(EntryBlocked):e.execute(plan())
    assert not b.sent


def test_stop_during_intent_write_prevents_post():
    b,j,e=engine();original=j.append
    def write(row):
        original(row)
        if row['kind']=='attempt':e.stop()
    j.append=write
    with pytest.raises(EntryBlocked):e.execute(plan())
    assert not b.sent


def test_accepted_entry_temporarily_missing_is_queried_again():
    b,j,e=engine();b.lookup_delay=1;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2

def test_mismatched_exit_requested_quantity_is_not_trusted():
    b,j,e=engine();original=b.submit
    def submit(p):
        r=original(p)
        if p['side']=='sell':r['qty']='9';r['filled_qty']='9'
        return r
    b.submit=submit;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==2


def test_recovery_rejects_changed_exit_ids_before_broker_action():
    from dataclasses import replace,asdict
    b,j,e=engine();p=plan();j.rows=[{'kind':'plan','plan':asdict(p)}]
    changed=replace(p,exit_ids=('unowned-other-sell',))
    def forbidden(*args):raise AssertionError('Recovery touched broker before ownership validation')
    b.get_order=forbidden
    r=e.recover(changed)
    assert r.status=='needs_reconciliation' and r.reason=='No durable ownership plan'
    assert not b.sent


def test_delayed_open_order_list_after_filled_exit_is_confirmed_without_posts():
    b,j,e=engine();original=b.open_orders;lag=[2]
    def orders():
        sells=[o for o in b.orders.values() if o['side']=='sell']
        if sells and lag[0]:
            lag[0]-=1
            return [{**sells[0],'status':'new','filled_qty':'0'}]
        return original()
    b.open_orders=orders;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2 and not e.entries_blocked

def test_persistently_stale_open_order_list_never_claims_flat_or_resells():
    b,j,e=engine();original=b.open_orders
    def orders():
        sells=[o for o in b.orders.values() if o['side']=='sell']
        return [{**sells[0],'status':'new'}] if sells else original()
    b.open_orders=orders;r=e.execute(plan())
    assert r.status=='needs_reconciliation' and len(b.sent)==2

def test_delayed_position_cache_after_exit_is_confirmed_without_resell():
    b,j,e=engine();original=b.position_qty;lag=[2]
    def position(symbol):
        if any(o['side']=='sell' for o in b.orders.values()) and lag[0]:
            lag[0]-=1;return Decimal(10)
        return original(symbol)
    b.position_qty=position;r=e.execute(plan())
    assert r.status=='flat' and len(b.sent)==2
