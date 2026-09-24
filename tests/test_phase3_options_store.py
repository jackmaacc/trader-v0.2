from copy import deepcopy
from dataclasses import asdict,is_dataclass
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal as D
import json
from pathlib import Path
import pytest
from trader_engine.research import phase3_options as adapter
from trader_engine.research.phase3_options_portfolio import adapter_positions
from trader_engine.research.phase3_options_store import record_options_operation
from trader_engine.research.phase3_store import read_state

DAY=date(2026,3,9)


def encode(value):
    if is_dataclass(value):return encode(asdict(value))
    if isinstance(value,(date,datetime)):return value.isoformat()
    if isinstance(value,D):return str(value)
    if isinstance(value,dict):return {k:encode(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [encode(v) for v in value]
    return value


def quote(symbol,feed,at,bid='1.9',ask='2'):
    return adapter.Quote(symbol,feed,D(bid),D(ask),D(10),D(10),at,at)


@pytest.fixture
def evidence():
    sessions=[];day=date(2025,1,1)
    while day<=date(2026,6,30):
        if day.weekday()<5:sessions.append(adapter.Session(day,datetime.combine(day,time(16),adapter.NY)))
        day+=timedelta(days=1)
    cal=adapter.ArchivedCalendar(tuple(sessions),adapter.calendar_digest(sessions));at=cal.decision_at(DAY)
    bars={s:[adapter.DailyClose(s,x.day,D(101 if x==sessions[cal.index(DAY)-1] else 100),x.close_at+timedelta(seconds=1),True) for x in sessions[cal.index(DAY)-200:cal.index(DAY)]] for s in adapter.UNIVERSE}
    contracts={s+'_C':adapter.Contract(s+'_C',s,DAY+timedelta(days=52),D(100),at-timedelta(days=30),at-timedelta(days=1),'call',100,True,True,True) for s in adapter.UNIVERSE}
    marks={s+'_C':quote(s+'_C','opra',at) for s in adapter.UNIVERSE}
    outputs=adapter.plan_entries(cal,DAY,bars_by_underlying=bars,sip_quotes={s:quote(s,'sip',at,'99.9','100.1') for s in adapter.UNIVERSE},contracts=list(contracts.values()),opra_quotes=marks,positions=[],equity=100000,cash=100000,fees_per_contract=1,utc_day_loss_fraction=0,chain_complete=True,fee_schedule_verified=True,owned_underlyings_at_signal=[],utc_day_entry_halted=False)
    return cal,at,contracts,marks,outputs


def record(db,ident,operation,**overrides):
    args=dict(run_id='synthetic-options',start_utc_day=DAY.isoformat(),operation_id=ident,operation=operation,
              registry=json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text()))
    args.update(overrides)
    return record_options_operation(db,**args)


def baseline():
    return dict(kind='baseline',utc_day=DAY.isoformat(),prior_close_equity='100000',boundary_at=datetime.combine(DAY,time(),timezone.utc).isoformat(),provenance='synthetic-verified-boundary')


def prepare(e,batch='buy'):
    cal,at,contracts,marks,outputs=e
    return dict(kind='prepare',portfolio_batch_id=batch,calendar=encode(cal),session_date=DAY.isoformat(),now=at.isoformat(),entry_outputs=outputs,exit_outputs={},contracts=encode(contracts),marks=encode(marks))


def execute(e,batch='buy'):
    return dict(kind='execute',portfolio_batch_id=batch,now=e[1].isoformat(),marks=encode(e[3]))


def buy(db,e):
    record(db,'baseline',baseline());record(db,'prepare',prepare(e));return record(db,'execute',execute(e))


def test_actual_outputs_restart_prepared_and_duplicate_booking(tmp_path,evidence):
    db=tmp_path/'options.sqlite';record(db,'baseline',baseline());record(db,'prepare',prepare(evidence))
    saved=read_state(db)
    assert not saved['state']['positions'] and saved['state']['batches']['buy']['apply_hash'] is None
    result=record(db,'execute',execute(evidence))
    assert len(read_state(db)['state']['positions'])==3
    assert len(result['result']['modeled_fills'])==3 and not result['prospective_credit']
    replay=record(db,'execute',execute(evidence))
    assert not replay['inserted'] and read_state(db)['batches']==3
    assert len(read_state(db)['state']['modeled_fills'])==3
    assert any('phase3_options_store.py' in key for key in result['source_hashes'])


def test_pending_exit_survives_restart_and_recovers_with_new_observation(tmp_path,evidence):
    db=tmp_path/'options.sqlite';bought=buy(db,evidence)['state'];cal=evidence[0]
    day=cal.sessions[cal.index(DAY)+10].day;at=cal.decision_at(day)
    exits={p.contract.underlying:adapter.plan_exit(cal,day,p,bars=[],option_quotes=[],underlying_quotes=[],fees_per_contract=1,fee_schedule_verified=True) for p in adapter_positions(bought)}
    op=dict(kind='prepare',portfolio_batch_id='exit',calendar=encode(cal),session_date=day.isoformat(),now=at.isoformat(),entry_outputs=[],exit_outputs=exits,contracts={},marks={})
    record(db,'exit-prep',op);record(db,'exit-exec',dict(kind='execute',portfolio_batch_id='exit',now=at.isoformat(),marks={}))
    pending=read_state(db)['state'];assert len(pending['positions'])==3
    assert all(p['pending_exit_reason'] for p in pending['positions'].values())
    day2=cal.sessions[cal.index(day)+1].day;at2=cal.decision_at(day2)
    exits={p.contract.underlying:adapter.plan_exit(cal,day2,p,bars=[],option_quotes=[quote(p.contract.symbol,'opra',at2)],underlying_quotes=[quote(p.contract.underlying,'sip',at2,'99.9','100.1')],fees_per_contract=1,fee_schedule_verified=True) for p in adapter_positions(pending)}
    op.update(portfolio_batch_id='recovered',session_date=day2.isoformat(),now=at2.isoformat(),exit_outputs=exits)
    record(db,'recovered-prep',op)
    result=record(db,'recovered-exec',dict(kind='execute',portfolio_batch_id='recovered',now=at2.isoformat(),marks={}))
    assert not result['state']['positions'] and len(result['result']['modeled_fills'])==3


@pytest.mark.parametrize('mutation',[
    lambda o:o['contracts']['SPY_C'].update(strike=100.0),
    lambda o:o['marks']['SPY_C'].update(ask=2),
    lambda o:o['entry_outputs'][0].update(base_fees=1.0),
    lambda o:o['entry_outputs'][0].update(quantity=True),
    lambda o:o['calendar'].update(archived_sha256='0'*64),
    lambda o:o.update(now='2026-03-09T09:35:00'),
    lambda o:o.update(initial_positions={'SPY':1}),
])
def test_malformed_evidence_rolls_back(tmp_path,evidence,mutation):
    db=tmp_path/'options.sqlite';record(db,'baseline',baseline());before=read_state(db)
    bad=prepare(evidence);mutation(bad)
    with pytest.raises(ValueError):record(db,'bad',bad)
    assert read_state(db)==before


def test_clock_failure_conflict_and_scenario_scope(tmp_path,evidence):
    db=tmp_path/'options.sqlite';record(db,'baseline',baseline());record(db,'prepare',prepare(evidence));before=read_state(db)
    bad=execute(evidence);bad['now']=(evidence[1]-timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError,match='before preparation'):record(db,'bad-clock',bad)
    assert read_state(db)==before
    with pytest.raises(ValueError,match='base same-observation'):record(db,'stress',execute(evidence),scenario='stress')
    with pytest.raises(ValueError):record(db,'changed-start',execute(evidence),start_utc_day='2026-03-10')
    assert read_state(db)==before
    record(db,'execute',execute(evidence))
    changed=execute(evidence);changed['marks']['SPY_C']['ask']='2.01'
    with pytest.raises(ValueError):record(db,'execute',changed)


def test_changed_registry_cannot_reuse_adapter(tmp_path,evidence):
    registry=json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text())
    registry['tracks'][1]['rules']['signal']='changed'
    with pytest.raises(ValueError,match='changed registry'):record(tmp_path/'new.sqlite','base',baseline(),registry=registry)
    assert not (tmp_path/'new.sqlite').exists()
