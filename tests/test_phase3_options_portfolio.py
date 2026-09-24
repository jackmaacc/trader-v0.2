from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D
import json
import pytest
from trader_engine.research.phase3_options import ArchivedCalendar,Session,DailyClose,Quote,Contract,NY,UNIVERSE,calendar_digest,plan_entries,plan_exit
from trader_engine.research.phase3_options_portfolio import initial_state,set_day_baseline,adapter_positions,prepare_batch,apply_batch,validate_state

DAY=date(2026,3,9)


@pytest.fixture
def evidence():
    sessions=[];day=date(2025,1,1)
    while day<=date(2026,6,30):
        if day.weekday()<5:sessions.append(Session(day,datetime.combine(day,time(16),NY)))
        day+=timedelta(days=1)
    cal=ArchivedCalendar(tuple(sessions),calendar_digest(sessions));at=cal.decision_at(DAY)
    bars={}
    for symbol in UNIVERSE:
        bars[symbol]=[DailyClose(symbol,s.day,D(101) if s==sessions[cal.index(DAY)-1] else D(100),s.close_at+timedelta(seconds=1),True) for s in sessions[cal.index(DAY)-200:cal.index(DAY)]]
    contracts={s+'_C':Contract(s+'_C',s,DAY+timedelta(days=52),D(100),at-timedelta(days=30),at-timedelta(days=1),'call',100,True,True,True) for s in UNIVERSE}
    marks={s+'_C':quote(s+'_C','opra',at) for s in UNIVERSE}
    sip={s:quote(s,'sip',at,'99.9','100.1') for s in UNIVERSE}
    return cal,at,bars,contracts,marks,sip


def quote(symbol,feed,at,bid='1.9',ask='2'):
    return Quote(symbol,feed,D(bid),D(ask),D(10),D(10),at,at)


def state():
    return set_day_baseline(initial_state(DAY),DAY,D(100000),boundary_at=datetime.combine(DAY,time(),timezone.utc),provenance='synthetic-reconciled-prior-close')


def entry_outputs(e,s=None):
    cal,at,bars,contracts,marks,sip=e;s=state() if s is None else s
    return plan_entries(cal,DAY,bars_by_underlying=bars,sip_quotes=sip,contracts=list(contracts.values()),opra_quotes=marks,positions=adapter_positions(s),equity=100000,cash=s['cash'],fees_per_contract=1,utc_day_loss_fraction=0,chain_complete=True,fee_schedule_verified=True,owned_underlyings_at_signal=[],utc_day_entry_halted=False)


def buy(e):
    cal,at,bars,contracts,marks,sip=e
    pending=prepare_batch(state(),'buy',cal,DAY,at,entry_outputs(e),{},contracts,marks)
    return apply_batch(pending,'buy',at,marks)


def test_actual_adapter_output_base_booking_and_roundtrip(evidence):
    result,report=buy(evidence)
    assert len(result['positions'])==3 and result['cash']=='99391.00'
    assert [f['underlying'] for f in report['modeled_fills']]==list(UNIVERSE)
    assert all(f['quantity']==1 and f['modeled_fill'] for f in report['modeled_fills'])
    assert not report['execution_authorized'] and not report['investment_qualified']
    validate_state(json.loads(json.dumps(result)))


def test_hash_replay_conflict_and_tamper(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    pending=prepare_batch(state(),'buy',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    result,report=apply_batch(pending,'buy',at,marks)
    replay,again=apply_batch(result,'buy',at,marks)
    assert replay==result and again['replayed']
    changed=deepcopy(pending);changed['batches']['buy']['body']['budget_ceilings']['SPY']='999999'
    with pytest.raises(ValueError,match='evidence changed'):apply_batch(changed,'buy',at,marks)
    with pytest.raises(ValueError,match='replay conflict'):apply_batch(result,'buy',at+timedelta(seconds=1),marks)


def test_no_backdated_or_late_entry_booking(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    pending=prepare_batch(state(),'buy',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    with pytest.raises(ValueError,match='before preparation'):apply_batch(pending,'buy',at-timedelta(seconds=1),marks)
    later,report=apply_batch(pending,'buy',at+timedelta(seconds=1),marks)
    assert not later['positions'] and all(v=='missed_entry_observation' for v in report['blocked'].values())


def test_original_budget_not_increased_and_fee_identity(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    s=state();s['cash']='50000';s['prior_close_equity']='50000'
    pending=prepare_batch(s,'small',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    result,report=apply_batch(pending,'small',at,marks)
    assert not result['positions'] and all(v=='original_budget_exceeded' for v in report['blocked'].values())
    malformed=entry_outputs(evidence);malformed[0]['base_fees']='2'
    with pytest.raises(ValueError,match='arithmetic'):prepare_batch(state(),'bad',cal,DAY,at,malformed,{},contracts,marks)


def test_pending_exit_with_missing_quotes_and_expired_contract(evidence):
    cal,at,bars,contracts,marks,sip=evidence;s,_=buy(evidence)
    later=DAY+timedelta(days=70);later_at=cal.decision_at(later)
    s=set_day_baseline(s,later,D(100000),boundary_at=datetime.combine(later,time(),timezone.utc),provenance='later-boundary')
    exits={p.contract.underlying:plan_exit(cal,later,p,bars=[],option_quotes=[],underlying_quotes=[],fees_per_contract=1,fee_schedule_verified=True) for p in adapter_positions(s)}
    pending=prepare_batch(s,'expired',cal,later,later_at,[],exits,{}, {})
    result,report=apply_batch(pending,'expired',later_at,{})
    assert len(result['positions'])==3 and len(report['pending_exits'])==3 and report['equity'] is None
    assert not report['modeled_fills']


def test_later_exit_cannot_supply_earlier_entry_cash(evidence):
    cal,at,bars,contracts,marks,sip=evidence;s,_=buy(evidence)
    later_day=cal.sessions[cal.index(DAY)+10].day;morning=cal.decision_at(later_day);noon=morning+timedelta(hours=2)
    s=set_day_baseline(s,later_day,D(100000),boundary_at=datetime.combine(later_day,time(),timezone.utc),provenance='exit-day')
    exits={p.contract.underlying:plan_exit(cal,later_day,p,bars=[],option_quotes=[quote(p.contract.symbol,'opra',noon)],underlying_quotes=[quote(p.contract.underlying,'sip',noon,'99.9','100.1')],fees_per_contract=1,fee_schedule_verified=True) for p in adapter_positions(s)}
    with pytest.raises(ValueError,match='current observation'):prepare_batch(s,'future-exits',cal,later_day,morning,[],exits,{}, {})
    pending=prepare_batch(s,'noon-exits',cal,later_day,noon,[],exits,{}, {})
    result,report=apply_batch(pending,'noon-exits',noon,{})
    assert not result['positions'] and len(report['modeled_fills'])==3


def test_pending_cannot_be_dropped_and_overlapping_batches(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    pending=prepare_batch(state(),'one',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    with pytest.raises(ValueError,match='one unresolved'):prepare_batch(pending,'two',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    s,_=buy(evidence);s['positions']['SPY']['pending_exit_reason']='weekly_trend_failed'
    holds={p.contract.underlying:dict(strategy_id='O_ETF_TREND_CALL_V1',action='hold',execution_authorized=False,fill_created=False,investment_qualified=False) for p in adapter_positions(s)}
    with pytest.raises(ValueError,match='cannot be dropped'):prepare_batch(s,'drop',cal,DAY,at,[],holds,{},marks)


def test_latched_halt_missing_baseline_and_missing_mark(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    for s in (initial_state(DAY),dict(state(),entry_halted=True)):
        pending=prepare_batch(s,'blocked',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
        result,report=apply_batch(pending,'blocked',at,marks)
        assert not result['positions']
    pending=prepare_batch(state(),'missing-mark',cal,DAY,at,entry_outputs(evidence),{},contracts,marks)
    result,report=apply_batch(pending,'missing-mark',at,{})
    assert not result['positions'] and all(v=='entry_mark_missing' for v in report['blocked'].values())


def test_skip_cannot_be_retried_with_different_batch_id(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    missing=list(entry_outputs(evidence))
    missing[0]=dict(action='skip',reason='no_qualifying_contract',underlying='IWM',decision_at=at.isoformat(),strategy_id='O_ETF_TREND_CALL_V1',execution_authorized=False,fill_created=False,investment_qualified=False)
    old_state=state();old_state['attempted_entries']['IWM']=(DAY-timedelta(days=7)).isoformat()
    pending=prepare_batch(old_state,'skip',cal,DAY,at,missing,{},contracts,marks)
    s,_=apply_batch(pending,'skip',at,marks)
    assert s['attempted_entries']['IWM']==DAY.isoformat()
    holds={p.contract.underlying:plan_exit(cal,DAY,p,bars=bars[p.contract.underlying],option_quotes=[],underlying_quotes=[],fees_per_contract=1,fee_schedule_verified=True) for p in adapter_positions(s)}
    entries=entry_outputs(evidence,s)
    pending=prepare_batch(s,'retry',cal,DAY,at,entries,holds,contracts,marks)
    result,report=apply_batch(pending,'retry',at,marks)
    assert 'IWM' not in result['positions'] and report['blocked']['IWM']=='session_entry_already_attempted_or_closed'


def test_expired_candidate_cannot_override_lifecycle_pending(evidence):
    cal,at,bars,contracts,marks,sip=evidence;s,_=buy(evidence)
    later=DAY+timedelta(days=70);at=cal.decision_at(later)
    s=set_day_baseline(s,later,D(100000),boundary_at=datetime.combine(later,time(),timezone.utc),provenance='late-boundary')
    exits={p.contract.underlying:dict(action='exit_candidate',reason='expiry_proximity',pending_exit_reason='expiry_proximity',contract=p.contract.symbol,observed_at=at.isoformat(),strategy_id='O_ETF_TREND_CALL_V1',execution_authorized=False,fill_created=False,investment_qualified=False) for p in adapter_positions(s)}
    with pytest.raises(ValueError,match='expired lifecycle'):prepare_batch(s,'bad-expiry',cal,later,at,[],exits,{}, {})


def test_contract_mapping_key_must_match_selected_contract_symbol(evidence):
    from dataclasses import replace
    cal,at,bars,contracts,marks,sip=evidence
    wrong=dict(contracts);wrong['SPY_C']=replace(contracts['SPY_C'],symbol='SPY_OTHER')
    with pytest.raises(ValueError,match='selected contract metadata'):
        prepare_batch(state(),'wrong-symbol',cal,DAY,at,entry_outputs(evidence),{},wrong,marks)


def test_skip_output_must_belong_to_current_session(evidence):
    cal,at,bars,contracts,marks,sip=evidence
    outputs=entry_outputs(evidence)
    outputs[0]=dict(action='skip',reason='no_qualifying_contract',underlying='IWM',decision_at=(at-timedelta(days=7)).isoformat(),strategy_id='O_ETF_TREND_CALL_V1',execution_authorized=False,fill_created=False,investment_qualified=False)
    with pytest.raises(ValueError,match='session decision'):
        prepare_batch(state(),'old-skip',cal,DAY,at,outputs,{},contracts,marks)
