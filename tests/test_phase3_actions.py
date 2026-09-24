from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from trader_engine.research.phase3_actions import apply_corporate_action, receivable_value
from trader_engine.research.phase3_daily import EQUITIES
from trader_engine.research.phase3_portfolio import initial_state, Mark, _snapshot

AT='2026-10-02T13:30:00+00:00'
PAID='2026-10-09T12:00:00+00:00'


def held():
    state=initial_state(EQUITIES,date(2026,10,2))
    state['cash']='90000';state['positions']={'SPY':'100'}
    return state


def dividend(**changes):
    event=dict(id='div1',kind='cash_dividend',symbol='SPY',quantity_before='100',
               amount_per_share='1',currency='USD',effective_at=AT,received_at=AT,
               source_sha256='a'*64,payment_at=None)
    return dict(event,**changes)


def payment(**changes):
    event=dict(id='paid1',kind='dividend_payment',dividend_id='div1',amount='100',
               currency='USD',effective_at=PAID,received_at=PAID,source_sha256='b'*64)
    return dict(event,**changes)


def test_ex_date_dividend_offsets_price_drop_without_creating_cash():
    state=held();before=deepcopy(state)
    result,receipt=apply_corporate_action(state,dividend(),AT)
    assert state==before and result['cash']=='90000'
    assert receivable_value(result)==D(100)
    at=datetime.fromisoformat(AT)
    snapshot=_snapshot(result,{'SPY':Mark(D(99),at,at,'sip')},at)
    assert snapshot.equity==D(100000) and snapshot.cash==D(90000)
    assert snapshot.gross==D(9900)
    assert receipt['cash_delta']=='0' and receipt['receivable_delta']=='100'


def test_entitlement_survives_sale_and_payment_does_not_double_count_equity():
    result,_=apply_corporate_action(held(),dividend(),AT)
    result['positions']={};result['cash']='99900' # explicit synthetic sale at ex-price99
    paid,receipt=apply_corporate_action(result,payment(),PAID)
    assert paid['cash']=='100000' and receivable_value(paid)==0
    assert D(receipt['cash_delta'])+D(receipt['receivable_delta'])==0
    repeat,again=apply_corporate_action(paid,payment(),PAID)
    assert repeat==paid and again['replayed']
    with pytest.raises(ValueError,match='already paid'):
        apply_corporate_action(paid,payment(id='duplicate-payment'),PAID)


def test_unknown_payment_stays_receivable_until_actual_payment_evidence():
    result,_=apply_corporate_action(held(),dividend(),AT)
    at='2026-10-03T12:00:00Z'
    update=dict(id='schedule',kind='dividend_schedule',dividend_id='div1',payment_at=PAID,
                currency='USD',effective_at=at,received_at=at,source_sha256='c'*64)
    scheduled,_=apply_corporate_action(result,update,at)
    assert scheduled['cash']=='90000' and receivable_value(scheduled)==100
    assert not scheduled['corporate_actions']['receivables']['div1']['paid']
    with pytest.raises(ValueError,match='precedes verified'):
        apply_corporate_action(scheduled,payment(effective_at=at,received_at=at),at)
    paid,_=apply_corporate_action(scheduled,payment(),PAID)
    assert paid['cash']=='90100'


def test_split_preserves_pending_ownership_and_receivable():
    result,_=apply_corporate_action(held(),dividend(),AT)
    result['pending_exits']=['SPY']
    at='2026-10-03T13:30:00Z'
    split=dict(id='split1',kind='split',symbol='SPY',ratio='2',quantity_before='100',
               effective_at=at,received_at=at,source_sha256='d'*64)
    adjusted,receipt=apply_corporate_action(result,split,at)
    assert adjusted['positions']=={'SPY':'200'} and adjusted['pending_exits']==['SPY']
    assert receivable_value(adjusted)==100 and receipt['cash_delta']=='0'
    assert receipt['receivable_delta']=='0'
    # 200*49.5 post-split = 100*99, and prior dividend entitlement is unchanged.
    assert D(adjusted['positions']['SPY'])*D('49.5')==D(9900)


@pytest.mark.parametrize('change',[
    {'quantity_before':'101'}, {'source_sha256':'unverified'}, {'currency':'EUR'},
    {'received_at':PAID}, {'payment_at':'2026-10-01T00:00:00Z'}, {'amount_per_share':1.0},
])
def test_invalid_evidence_leaves_input_state_unchanged(change):
    state=held();before=deepcopy(state)
    with pytest.raises(ValueError):apply_corporate_action(state,dividend(**change),AT)
    assert state==before


def test_conflicting_event_id_cannot_rewrite_entitlement():
    state,_=apply_corporate_action(held(),dividend(),AT)
    with pytest.raises(ValueError,match='Conflicting action'):
        apply_corporate_action(state,dividend(amount_per_share='2'),AT)


def test_fractional_split_and_unresolved_plan_require_explicit_lifecycle():
    state=held();before=deepcopy(state)
    event=dict(id='split',kind='split',symbol='SPY',ratio='.333',quantity_before='100',
               effective_at=AT,received_at=AT,source_sha256='a'*64)
    with pytest.raises(ValueError,match='cash-in-lieu'):apply_corporate_action(state,event,AT)
    assert state==before
    state['batches']['queued']={'apply_hash':None}
    with pytest.raises(ValueError,match='Resolve prepared'):apply_corporate_action(state,dividend(),AT)


def test_late_entitlement_and_wrong_payment_cannot_be_invented():
    state=held();state['last_applied_at']=PAID
    with pytest.raises(ValueError,match='earlier portfolio'):apply_corporate_action(state,dividend(),AT)
    with pytest.raises(ValueError,match='effective boundary'):apply_corporate_action(held(),dividend(),PAID)
    entitled,_=apply_corporate_action(held(),dividend(),AT)
    with pytest.raises(ValueError,match='does not reconcile'):
        apply_corporate_action(entitled,payment(amount='99'),PAID)


def test_precise_receivables_sum_is_independent_of_global_decimal_precision():
    from decimal import localcontext
    state=held();state['corporate_actions']={'receivables':{
        'a':{'amount':'1.00000000000000000000000000001','paid':False},
        'b':{'amount':'0.00000000000000000000000000001','paid':False}}}
    with localcontext() as context:
        context.prec=6
        assert receivable_value(state)==D('1.00000000000000000000000000002')


def test_ex_date_buys_cannot_receive_preexisting_entitlement_at_same_timestamp():
    state=held();state['last_applied_at']=AT;state['last_portfolio_at']=AT
    with pytest.raises(ValueError,match='precede portfolio'):
        apply_corporate_action(state,dividend(),AT)
