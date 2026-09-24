from dataclasses import replace
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from trader_engine.research.phase3_daily import (
    EQUITIES,CRYPTO,Session,DailyBar,PortfolioSnapshot,FeeSchedule,PriceObservation,
    decide_signal,plan_entry,estimate_entry,estimate_exit)

UTC=timezone.utc
NY=ZoneInfo('America/New_York')


def equity_data():
    days=[]
    day=date(2026,8,1)
    while len(days)<22:
        if day.weekday()<5:
            days.append(day)
        day+=timedelta(days=1)
    sessions=[Session(d,datetime.combine(d,time(9,30),NY),datetime.combine(d,time(16),NY)) for d in days]
    bars=[DailyBar('SPY',s.day,D(100),D(100 if i<20 else 110),s.close_at+timedelta(minutes=1)) for i,s in enumerate(sessions[:-1])]
    now=datetime.combine(days[-1],time(9,20),NY)
    return sessions,bars,now


def equity_decision(held=D(0),pending=False):
    sessions,bars,now=equity_data()
    return decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now,held_quantity=held,pending_exit=pending)


def crypto_data():
    first=date(2026,1,1)
    sessions=[]
    for i in range(201):
        day=first+timedelta(days=i)
        start=datetime.combine(day,time(),UTC)
        sessions.append(Session(day,start,start+timedelta(days=1)))
    bars=[DailyBar('BTC/USD',s.day,D(100 if i<199 else 110),None,s.close_at+timedelta(seconds=1)) for i,s in enumerate(sessions[:-1])]
    now=sessions[-1].open_at+timedelta(minutes=5)
    return sessions,bars,now


def crypto_decision(held=D(0),pending=False):
    sessions,bars,now=crypto_data()
    return decide_signal(CRYPTO,'BTC/USD',bars,sessions,sessions[-2].day,now,held_quantity=held,pending_exit=pending)


def snap(**kwargs):
    return PortfolioSnapshot(**dict(equity=D(100000),cash=D(100000),gross=D(0),prior_utc_close_equity=D(100000),entry_halted=False,**kwargs))


def test_equity_uses_total_return_signal_and_raw_sizing():
    decision=equity_decision()
    assert decision.action=='enter' and decision.raw_signal_price==100
    plan=plan_entry(decision,snap(),FeeSchedule(D(0)),quantity_step=D(1))
    assert plan.quantity==80 and plan.budget==8000


def test_equity_ties_neither_enter_nor_exit():
    sessions,bars,now=equity_data()
    bars[-1]=replace(bars[-1],total_return_close=D(100))
    for held,action in [(D(0),'flat'),(D(2),'hold')]:
        assert decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now,held_quantity=held).action==action


def test_equity_exit_strict_prior_ten_and_no_reentry():
    sessions,bars,now=equity_data()
    bars[-1]=replace(bars[-1],total_return_close=D(99))
    result=decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now,held_quantity=D(3))
    assert result.action=='exit' and result.pending_exit


def test_future_bars_and_later_revisions_do_not_change_decision():
    sessions,bars,now=equity_data()
    before=decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now)
    future=replace(bars[-1],raw_close=D(9999),total_return_close=D(9999),received_at=now+timedelta(seconds=1))
    assert decide_signal(EQUITIES,'SPY',bars+[future],sessions,sessions[-2].day,now)==before


@pytest.mark.parametrize('late', [False,True])
def test_missing_and_unavailable_bars_skip(late):
    sessions,bars,now=equity_data()
    if late:
        bars[5]=replace(bars[5],received_at=now+timedelta(seconds=1))
    else:
        bars.pop(5)
    assert decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now).action=='skip'


def test_invalid_adjustment_or_duplicate_rejected():
    sessions,bars,now=equity_data()
    with pytest.raises(ValueError):
        decide_signal(EQUITIES,'SPY',bars+[bars[0]],sessions,sessions[-2].day,now)
    bars[0]=replace(bars[0],total_return_close=None)
    with pytest.raises(ValueError):
        decide_signal(EQUITIES,'SPY',bars,sessions,sessions[-2].day,now)


def test_pending_exit_survives_missing_data_and_late_decision():
    sessions,_,now=equity_data()
    result=decide_signal(EQUITIES,'SPY',[],sessions,sessions[-2].day,now+timedelta(hours=1),held_quantity=D(2),pending_exit=True)
    assert result.action=='exit' and result.pending_exit
    assert estimate_exit(result,None,FeeSchedule(D(0)),now,quantity_step=D(1)).pending_exit


def test_crypto_mean_includes_current_and_tie_exits():
    sessions,bars,now=crypto_data()
    assert crypto_decision().action=='enter'
    bars[-1]=replace(bars[-1],raw_close=D(100))
    assert decide_signal(CRYPTO,'BTC/USD',bars,sessions,sessions[-2].day,now,held_quantity=D(1)).action=='exit'
    assert decide_signal(CRYPTO,'BTC/USD',bars,sessions,sessions[-2].day,now).action=='flat'


def test_crypto_calendar_gap_and_wrong_decision_time():
    sessions,bars,now=crypto_data()
    with pytest.raises(ValueError,match='gap'):
        decide_signal(CRYPTO,'BTC/USD',bars,sessions[:10]+sessions[11:],sessions[-2].day,now)
    assert decide_signal(CRYPTO,'BTC/USD',bars,sessions,sessions[-2].day,now+timedelta(seconds=1)).action=='skip'


@pytest.mark.parametrize('baseline,equity,reason', [(None,D(100000),'missing_utc_loss_baseline'),(D(100000),D(99000),'daily_entry_halt')])
def test_entry_halts(baseline,equity,reason):
    state=PortfolioSnapshot(equity,D(100000),D(0),baseline,False)
    plan=plan_entry(equity_decision(),state,FeeSchedule(D(0)),quantity_step=D(1))
    assert plan.quantity==0 and plan.reason==reason


def test_fee_inclusive_cash_and_quantity_never_increases():
    decision=equity_decision()
    plan=plan_entry(decision,snap(),FeeSchedule(D('.001'),D(3)),quantity_step=D(1))
    for open_price in (D(50),D(100),D(200)):
        quote=PriceObservation(decision.execution_at,decision.execution_at,raw_open=open_price)
        result=estimate_entry(plan,quote,snap(),decision.execution_at)
        assert result.quantity<=plan.quantity and result.cash_debit<=plan.budget
        assert not result.execution_authorized


def test_execution_cash_cap_reduces_plan():
    decision=equity_decision()
    plan=plan_entry(decision,snap(),FeeSchedule(D(0)),quantity_step=D(1))
    state=PortfolioSnapshot(D(100000),D(500),D(23900),D(100000),False)
    quote=PriceObservation(decision.execution_at,decision.execution_at,raw_open=D(50))
    result=estimate_entry(plan,quote,state,decision.execution_at)
    assert result.cash_debit<=100 and result.quantity==1


def test_crypto_base_fee_and_frozen_floor():
    decision=crypto_decision()
    plan=plan_entry(decision,snap(),FeeSchedule(D('.001'),currency='base'),quantity_step=D('.0001'))
    assert plan.fees.rate==D('.0025')
    quote=PriceObservation(decision.execution_at,decision.execution_at,bid=D(99),ask=D(100))
    result=estimate_entry(plan,quote,snap(),decision.execution_at)
    assert result.acquired_quantity==result.quantity*(1-D('.0025'))
    assert result.cash_debit<=plan.budget and result.base_fee>0


@pytest.mark.parametrize('offset,received_offset', [(3,0),(0,1),(61,0)])
def test_crypto_stale_future_or_out_of_window(offset,received_offset):
    decision=crypto_decision()
    plan=plan_entry(decision,snap(),FeeSchedule(D('.0025')),quantity_step=D('.0001'))
    quote=PriceObservation(decision.execution_at,decision.execution_at+timedelta(seconds=received_offset),bid=D(99),ask=D(100))
    assert estimate_entry(plan,quote,snap(),decision.execution_at+timedelta(seconds=offset)).quantity==0


def test_exit_base_fee_never_oversells_and_remains_pending():
    decision=crypto_decision(D(1),True)
    quote=PriceObservation(decision.execution_at,decision.execution_at,bid=D(99),ask=D(100))
    result=estimate_exit(decision,quote,FeeSchedule(D('.0025'),currency='base'),decision.execution_at,quantity_step=D('.0001'))
    assert result.owned_units_debit<=1 and result.quantity<1
    assert result.pending_exit and not result.execution_authorized


def test_equity_sell_uses_adverse_price_and_fees_without_baseline():
    decision=equity_decision(D(3),True)
    quote=PriceObservation(decision.execution_at,decision.execution_at,raw_open=D(100))
    result=estimate_exit(decision,quote,FeeSchedule(D('.001'),D(1)),decision.execution_at,quantity_step=D(1))
    assert result.price==D('99.9300') and result.cash_credit<D(300)
    assert result.owned_units_debit==3


def test_late_pending_decision_cannot_retroactively_sell_prior_open():
    sessions,_,cutoff=equity_data()
    now=sessions[-1].open_at+timedelta(minutes=10)
    decision=decide_signal(EQUITIES,'SPY',[],sessions,sessions[-2].day,now,held_quantity=D(2),pending_exit=True)
    quote=PriceObservation(sessions[-1].open_at,sessions[-1].open_at,raw_open=D(100))
    result=estimate_exit(decision,quote,FeeSchedule(D(0)),now,quantity_step=D(1))
    assert result.quantity==0 and result.pending_exit


def test_crypto_valid_fresh_quote_preceding_scheduled_time():
    decision=crypto_decision()
    plan=plan_entry(decision,snap(),FeeSchedule(D('.0025')),quantity_step=D('.0001'))
    event=decision.execution_at-timedelta(seconds=1)
    quote=PriceObservation(event,event,bid=D(99),ask=D(100))
    assert estimate_entry(plan,quote,snap(),decision.execution_at).quantity>0


def test_late_crypto_pending_exit_cannot_be_priced_before_decision():
    sessions,_,scheduled=crypto_data()
    late=scheduled+timedelta(minutes=10)
    decision=decide_signal(CRYPTO,'BTC/USD',[],sessions,sessions[-2].day,late,held_quantity=D(1),pending_exit=True)
    quote=PriceObservation(scheduled,scheduled,bid=D(99),ask=D(100))
    result=estimate_exit(decision,quote,FeeSchedule(D('.0025')),scheduled,quantity_step=D('.0001'))
    assert result.quantity==0 and result.pending_exit


def test_latched_daily_halt_blocks_recovered_equity_entry_plan_and_execution():
    decision=equity_decision()
    recovered=replace(snap(),entry_halted=True)
    assert plan_entry(decision,recovered,FeeSchedule(D(0)),quantity_step=D(1)).quantity==0
    plan=plan_entry(decision,snap(),FeeSchedule(D(0)),quantity_step=D(1))
    quote=PriceObservation(decision.execution_at,decision.execution_at,raw_open=D(100))
    assert estimate_entry(plan,quote,recovered,decision.execution_at).quantity==0


def test_halt_latch_requires_explicit_boolean():
    with pytest.raises(ValueError,match='latched'):
        plan_entry(equity_decision(),replace(snap(),entry_halted=None),FeeSchedule(D(0)),quantity_step=D(1))
