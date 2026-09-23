from copy import deepcopy
from decimal import Decimal
import pytest
from trader_engine.execution.lifecycle import TradePlan, PaperExecutor
from test_paper_lifecycle import MemoryJournal, FakeBroker

class SwingBroker(FakeBroker):
    def __init__(self):
        super().__init__(); self.cancel_reads=[]; self.reads=0; self.stop_lost_ack=False; self.race_qty=Decimal(0)
    def submit(self, payload):
        if payload['type'] != 'stop': return super().submit(payload)
        self.sent.append(deepcopy(payload)); self.counter+=1
        o={**payload,'id':f'order-{self.counter}','status':'new','filled_qty':'0','filled_avg_price':None}
        self.orders[payload['client_order_id']]=o
        if self.stop_lost_ack: raise TimeoutError('lost stop acknowledgement')
        return deepcopy(o)
    def get_order(self,cid):
        self.reads+=1
        return super().get_order(cid)
    def cancel(self,oid):
        self.cancel_reads.append(self.reads)
        for o in self.orders.values():
            if o['id']==oid and o['type']=='stop' and self.race_qty:
                o['filled_qty']=str(Decimal(o['filled_qty'])+self.race_qty)
                o['filled_avg_price']='99';self.position-=self.race_qty
        super().cancel(oid)

def setup(journal=None):
    b=SwingBroker();j=journal or MemoryJournal()
    e=PaperExecutor(b,j,polls=2,sleep=lambda _:None,poll_seconds=0)
    p=TradePlan.create('SPY',10,'100.00',strategy_id='trend_v1',horizon='swing',protective_stop_price='99')
    return b,j,e,p

def fresh(b,j):return PaperExecutor(b,j,polls=2,sleep=lambda _:None,poll_seconds=0)

def test_swing_retains_gtc_protection_overnight_and_metadata():
    b,j,e,p=setup();r=e.execute(p)
    assert r.status=='holding_protected' and b.position==10
    assert b.sent[-1]['time_in_force']=='gtc' and b.sent[-1]['qty']=='10'
    assert j.rows[0]['plan']['strategy_id']=='trend_v1'
    assert j.rows[0]['plan']['horizon']=='swing'
    adopted=fresh(b,j);n=len(b.sent)
    assert adopted.adopt_protected(p).status=='holding_protected'
    assert len(b.sent)==n and not b.cancel_reads
    assert adopted.resume_drain().status=='flat'
    assert b.position==0

def test_partial_entry_coverage_uses_final_fills():
    b,j,e,p=setup();b.partial_buy=Decimal(3);b.cancel_fill_extra=Decimal(2)
    assert e.execute(p).status=='holding_protected'
    assert b.sent[-1]['qty']=='5'

def test_partial_stop_fill_restart_matches_residual():
    b,j,e,p=setup();e.execute(p)
    stop=b.orders[p.exit_ids[0]];stop.update(status='partially_filled',filled_qty='3',filled_avg_price='99');b.position-=3
    r=fresh(b,j).adopt_protected(p)
    assert r.status=='holding_protected' and r.residual=='7'

def test_cancel_fill_race_only_sells_residual():
    b,j,e,p=setup();e.execute(p);b.race_qty=Decimal(4)
    assert e.resume_drain().status=='flat'
    assert b.sent[-1]['qty']=='6' and b.position==0

def test_stop_cancel_has_no_initial_poll_wait():
    b,j,e,p=setup();e.execute(p);b.reads=0
    e.resume_drain()
    assert b.cancel_reads==[2]  # entry lookup then stop lookup, cancel immediately

def test_lost_stop_ack_does_not_repeat_post():
    b,j,e,p=setup();b.stop_lost_ack=True
    assert e.execute(p).status=='holding_protected'
    assert len(b.sent)==2

def test_duplicate_identical_intent_idempotent_on_restart():
    b,j,e,p=setup();e.execute(p)
    j.rows.append(deepcopy(next(r for r in j.rows if r['kind']=='attempt')))
    assert fresh(b,j).adopt_protected(p).status=='holding_protected'
    assert len(b.sent)==2

@pytest.mark.parametrize('damage',['position','foreign','day','missing','intent','conflict','coverage'])
def test_adoption_fails_closed_without_mutations(damage):
    b,j,e,p=setup();e.execute(p);stop=b.orders[p.exit_ids[0]]
    if damage=='position':b.position+=1
    elif damage=='foreign':b.foreign_orders=[{'symbol':'SPY','client_order_id':'foreign','id':'x'}]
    elif damage=='day':stop['time_in_force']='day'
    elif damage=='missing':b.orders.pop(p.exit_ids[0])
    elif damage=='intent':j.rows=[r for r in j.rows if not(r['kind']=='attempt' and r['client_id']==p.exit_ids[0])]
    elif damage=='conflict':j.rows[0]['plan']['strategy_id']='unknown'
    else:stop['qty']='9'
    n=len(b.sent);a=fresh(b,j)
    assert a.adopt_protected(p).status=='needs_reconciliation'
    assert a.resume_drain().status=='needs_reconciliation'
    assert len(b.sent)==n and not b.cancel_reads

def test_failed_swing_exit_intent_never_posts_unrecorded_order():
    b,j,e,p=setup();e.execute(p);original=j.append
    def append(row):
        if row['kind']=='attempt':raise OSError('disk full')
        original(row)
    j.append=append
    assert e.resume_drain().status=='needs_reconciliation'
    assert len(b.sent)==2


def test_known_journal_failure_preserves_working_protection():
    b,j,e,p=setup();e.execute(p);j.fail_kind='all'
    assert e.resume_drain().status=='needs_reconciliation'
    assert not b.cancel_reads and b.position==10
    assert b.orders[p.exit_ids[0]]['status']=='new'


def test_partial_entry_cancels_before_any_deliberate_poll_sleep():
    b,j,e,p=setup();b.partial_buy=Decimal(3);b.cancel_fill_extra=Decimal(2)
    events=[]
    original=b.cancel
    def cancel(oid):events.append('cancel');return original(oid)
    b.cancel=cancel;e.sleep=lambda _:events.append('sleep')
    assert e.execute(p).status=='holding_protected'
    assert events[0]=='cancel' and b.cancel_reads==[0]
    assert b.sent[-1]['qty']=='5'


def test_partial_first_observed_on_poll_cancels_before_next_sleep():
    b,j,e,p=setup();b.partial_buy=Decimal(0)
    original_get=b.get_order;original_cancel=b.cancel;events=[]
    def get(cid):
        if cid==p.entry_id and b.reads==1:
            b.orders[cid].update(status='partially_filled',filled_qty='3');b.position=Decimal(3)
        return original_get(cid)
    def cancel(oid):events.append('cancel');return original_cancel(oid)
    b.get_order=get;b.cancel=cancel;e.sleep=lambda _:events.append('sleep')
    assert e.execute(p).status=='holding_protected'
    assert events[0]=='cancel' and b.cancel_reads==[2]
    assert b.sent[-1]['qty']=='3'


def test_uncertain_partial_entry_cancel_does_not_guess_protection_quantity():
    b,j,e,p=setup();b.partial_buy=Decimal(3);b.cancel_pending=True
    result=e.execute(p)
    assert result.status=='needs_reconciliation' and e.entries_blocked
    assert len(b.sent)==1 and b.sent[0]['side']=='buy'
    assert len(b.cancel_reads)==1
    # Still-held quantity is explicitly uncertain/unprotected; no false claim.
    assert b.position==3 and result.status!='holding_protected'
