from copy import deepcopy
import json
from pathlib import Path
from decimal import Decimal

import pytest

from trader_engine.research.phase3_portfolio_store import record_portfolio_operation
from trader_engine.research.phase3_store import read_state
from trader_engine.research import phase3_portfolio_store as integration
from trader_engine.operations.accounting_ledger import normalize_event
from trader_engine.operations.pnl_attribution import attribute

STRATEGY='E_DONCHIAN20_V1'
SYMBOLS=('IWM','QQQ','SPY')
AT='2026-10-01T13:30:00+00:00'


def record(db, ident, operation, registry=None):
    if registry is None:
        registry=json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text())
    return record_portfolio_operation(db,run_id='synthetic-integration',strategy_id=STRATEGY,
        start_utc_day='2026-10-01',operation_id=ident,operation=operation,registry=registry)


def baseline():
    return dict(kind='baseline',utc_day='2026-10-01',prior_close_equity='100000',
                boundary_at='2026-10-01T00:00:00Z',provenance='synthetic-fixture-boundary')


def prepare(batch='entry',holdings=None,price='100',now=AT):
    holdings=holdings or {}
    return dict(kind='prepare',portfolio_batch_id=batch,now=now,
        decisions={s:dict(strategy_id=STRATEGY,symbol=s,action='hold' if s in holdings else 'enter',
            reason='synthetic-fixture',signal_day='2026-09-30',execution_at=AT,
            raw_signal_price='100',held_quantity=holdings.get(s,'0'),pending_exit=False,decided_at=now) for s in SYMBOLS},
        marks={s:dict(price=price,event_at=now,received_at=now,source='sip') for s in SYMBOLS},
        fees={s:dict(rate='0',fixed_cash='0',currency='quote') for s in SYMBOLS},
        quantity_steps={s:'1' for s in SYMBOLS})


def execute(batch='entry',price='100',now=AT):
    return dict(kind='execute',portfolio_batch_id=batch,now=now,stress=False,
        observations={s:dict(event_at=AT,received_at=AT,raw_open=price) for s in SYMBOLS},
        marks={s:dict(price=price,event_at=now,received_at=now,source='sip') for s in SYMBOLS})


def test_actual_portfolio_persists_plans_fills_and_restart_without_duplicates(tmp_path):
    db=tmp_path/'state.db'
    record(db,'baseline',baseline())
    planned=record(db,'prepare',prepare())
    assert planned['state']['positions']=={}
    result=record(db,'execute',execute())
    assert len(result['result']['modeled_fills'])==3
    assert all(f['modeled_fill'] for f in result['result']['modeled_fills'])
    saved=read_state(db)
    assert saved['state']==result['state'] and saved['batches']==3
    again=record(db,'execute',execute())
    assert not again['inserted'] and again['state']==result['state']
    assert len(read_state(db)['state']['modeled_fills'])==3
    assert len(result['source_hashes'])==9
    assert not result['execution_authorized'] and not result['prospective_credit']


def test_restart_preserves_halt_after_recovery(tmp_path):
    db=tmp_path/'state.db';record(db,'baseline',baseline());record(db,'prepare',prepare())
    bought=record(db,'execute',execute())['state']
    holdings=bought['positions']
    loss_at='2026-10-01T13:31:00+00:00'
    loss=record(db,'loss-prepare',prepare('loss',holdings,'90',loss_at))
    assert loss['state']['entry_halted']
    record(db,'loss-execute',execute('loss','90',loss_at))
    recovered_at='2026-10-01T13:32:00+00:00'
    recovered=record(db,'recovered',prepare('recovered',holdings,'100',recovered_at))
    assert recovered['state']['entry_halted']
    assert read_state(db)['state']['entry_halted']


def test_bad_operation_or_numeric_type_rolls_back(tmp_path):
    db=tmp_path/'state.db';record(db,'baseline',baseline());before=read_state(db)
    bad=prepare();bad['fees']['SPY']['rate']=0.0
    with pytest.raises(ValueError,match='decimal string'):record(db,'prepare',bad)
    assert read_state(db)==before
    with pytest.raises(ValueError,match='Unknown'):record(db,'bad',{'kind':'submit_order'})
    assert read_state(db)==before


def test_changed_protocol_never_claims_adapter_compatibility(tmp_path):
    registry=json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text())
    registry['tracks'][0]['rules']['signal']='changed'
    with pytest.raises(ValueError,match='changed registry'):
        record(tmp_path/'state.db','baseline',baseline(),registry)
    assert not (tmp_path/'state.db').exists()


def test_changed_source_cannot_describe_already_loaded_functions(tmp_path,monkeypatch):
    original=Path.read_bytes
    def changed(path):
        raw=original(path)
        if path.name=='phase3_portfolio.py':
            raw=raw.replace(b"'cash': '100000'",b"'cash': '100001'")
        return raw
    monkeypatch.setattr(Path,'read_bytes',changed)
    with pytest.raises(ValueError,match='Loaded implementation differs'):
        record(tmp_path/'state.db','baseline',baseline())
    assert not (tmp_path/'state.db').exists()


def test_mid_transition_source_edit_rolls_back(tmp_path,monkeypatch):
    db=tmp_path/'state.db';record(db,'baseline',baseline());before=read_state(db)
    original_read=Path.read_bytes;original_transition=integration._transition
    def changing_transition(state,operation):
        output=original_transition(state,operation)
        def edited(path):
            raw=original_read(path)
            return raw+b'\n# injected edit\n' if path.name=='phase3_portfolio.py' else raw
        monkeypatch.setattr(Path,'read_bytes',edited)
        return output
    monkeypatch.setattr(integration,'_transition',changing_transition)
    with pytest.raises(ValueError,match='Implementation changed during'):
        record(db,'prepare',prepare())
    assert read_state(db)==before


def test_constant_only_source_edit_requires_new_process(tmp_path,monkeypatch):
    original=Path.read_bytes
    def changed(path):
        raw=original(path)
        if path.name=='phase3_daily.py':
            raw=raw.replace(b"EQUITIES = 'E_DONCHIAN20_V1'",b"EQUITIES = 'E_DONCHIAN20_V9'")
        return raw
    monkeypatch.setattr(Path,'read_bytes',changed)
    with pytest.raises(ValueError,match='changed since import'):
        record(tmp_path/'state.db','baseline',baseline())
    assert not (tmp_path/'state.db').exists()


def test_equity_modeled_roundtrip_reconciles_through_independent_fifo_accounting(tmp_path):
    db=tmp_path/'state.db';record(db,'baseline',baseline());record(db,'prepare',prepare())
    bought=record(db,'execute',execute())
    day2=baseline();day2.update(utc_day='2026-10-02',boundary_at='2026-10-02T00:00:00Z',
                               prior_close_equity=bought['result']['equity'],provenance='synthetic-day2-boundary')
    record(db,'day2',day2)
    now='2026-10-02T13:30:00+00:00'
    sell=prepare('exit',bought['state']['positions'],'105',now)
    for value in sell['decisions'].values():
        value.update(action='exit',pending_exit=True,signal_day='2026-10-01',execution_at=now)
    record(db,'exit-prepare',sell)
    execution=execute('exit','105',now)
    for value in execution['observations'].values():value.update(event_at=now,received_at=now)
    closed=record(db,'exit',execution)
    assert closed['state']['positions']=={}
    instruments={s:{'multiplier':'1'} for s in SYMBOLS}
    events=[]
    for prefix,report,at in [('entry',bought['result'],AT),('exit',closed['result'],now)]:
        for fill in report['modeled_fills']:
            events.append(normalize_event(dict(id=prefix+fill['symbol'],activity_type='FILL',
                transaction_time=at,symbol=fill['symbol'],side=fill['side'],qty=fill['quantity'],
                price=fill['price'],fee=fill['quote_fee']),instruments))
    start='2026-10-01T00:00:00Z';end='2026-10-03T00:00:00Z'
    context=dict(start=start,end=end,currency='USD',instruments=instruments,
        opening={'timestamp':start,'cash':'100000','positions':{}},
        ending={'timestamp':end,'cash':closed['state']['cash'],'positions':{}},
        activities_complete=True,cashflows_complete=True,fees_complete=True,corporate_actions_complete=True)
    report=attribute(context,events,{'opening_lots':[]})
    assert report['status']=='attributed'
    assert Decimal(report['metrics']['cash_difference'])==0
    assert Decimal(report['metrics']['identity_difference'])==0
    assert Decimal(report['metrics']['realized_net'])==Decimal(closed['state']['cash'])-Decimal('100000')
    assert len(report['matches'])==3


def test_dividend_receivable_and_payment_survive_durable_restart(tmp_path):
    db=tmp_path/'state.db';record(db,'baseline',baseline());record(db,'prepare',prepare())
    bought=record(db,'execute',execute())
    cash=bought['state']['cash'];qty=bought['state']['positions']['SPY']
    at='2026-10-02T13:30:00Z'
    event=dict(id='dividend1',kind='cash_dividend',symbol='SPY',quantity_before=qty,
               amount_per_share='1',currency='USD',effective_at=at,received_at=at,
               payment_at=None,source_sha256='a'*64)
    record(db,'dividend',{'kind':'corporate_action','event':event,'now':at})
    restarted=read_state(db)['state']
    assert restarted['cash']==cash
    assert restarted['corporate_actions']['receivables']['dividend1']['amount']==qty
    assert not restarted['corporate_actions']['receivables']['dividend1']['paid']
    paid='2026-10-09T12:00:00Z'
    payment=dict(id='payment1',kind='dividend_payment',dividend_id='dividend1',amount=qty,
                 currency='USD',effective_at=paid,received_at=paid,source_sha256='b'*64)
    operation={'kind':'corporate_action','event':payment,'now':paid}
    result=record(db,'paid',operation)
    assert Decimal(result['state']['cash'])==Decimal(cash)+Decimal(qty)
    assert result['state']['corporate_actions']['receivables']['dividend1']['paid']
    again=record(db,'paid',operation)
    assert not again['inserted'] and again['state']==result['state']
