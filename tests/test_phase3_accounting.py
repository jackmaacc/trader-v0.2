from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
import pytest

from trader_engine.research.phase3_accounting import audit_database
from trader_engine.research.phase3_store import apply_batch
import test_phase3_benchmark_store as bench
import test_phase3_portfolio_store as daily
import test_phase3_options_store as options
from test_phase3_options_store import evidence


def test_equity_reconciles_each_durable_boundary_and_missing_marks(tmp_path):
    db=tmp_path/'daily.db'
    daily.record(db,'baseline',daily.baseline());daily.record(db,'prepare',daily.prepare())
    result=daily.record(db,'execute',daily.execute())
    incomplete=audit_database(db)
    assert incomplete['status']=='inconclusive' and incomplete['metrics'] is None
    report=audit_database(db,terminal_marks={s:'100' for s in result['state']['positions']})
    assert report['status']=='reconciled' and len(report['boundaries'])==3
    assert D(report['metrics']['net_pnl_before_overhead'])==D(result['result']['equity'])-100000
    assert all(D(b['cash_difference'])==0 for b in report['boundaries'])
    assert not report['prospective_credit']


@pytest.mark.parametrize('currency',['base','quote'])
def test_crypto_benchmark_buy_sell_fee_units_and_dust(tmp_path,currency):
    db=tmp_path/'crypto.db';at=bench.AT.replace(hour=0,minute=5)
    bench.record(db,'prepare',bench.prepare(strategy=bench.C,at=at,fee_currency=currency),strategy=bench.C)
    bought=bench.record(db,'buy',bench.execute(strategy=bench.C,at=at),strategy=bench.C)['state']
    later=at+timedelta(days=1)
    bench.record(db,'exit-plan',bench.prepare('exit',strategy=bench.C,at=later,equity=bench.equity(bought),fraction='0',fee_currency=currency),strategy=bench.C)
    sold=bench.record(db,'sell',bench.execute('exit',attempt='sell',strategy=bench.C,at=later),strategy=bench.C)['state']
    report=audit_database(db,terminal_marks={s:'100' for s in sold['positions']})
    assert report['status']=='reconciled' and report['fill_count']==4
    assert D(report['metrics']['modeled_fees'])>0
    assert abs(D(report['metrics']['identity_difference']))<=D('1e-24')
    assert len(report['fifo_matches'])==2


def test_options_multiplier_and_fee_basis(tmp_path,evidence):
    db=tmp_path/'options.db';result=options.buy(db,evidence)
    marks={row['contract']['symbol']:'1.9' for row in result['state']['positions'].values()}
    report=audit_database(db,terminal_marks=marks)
    assert report['status']=='reconciled' and report['fill_count']==3
    assert D(report['metrics']['marked_equity'])==D(result['state']['cash'])+D('570')
    assert D(report['metrics']['modeled_fees'])==3


def test_dividend_payment_and_split_basis_are_independently_rebuilt(tmp_path):
    db=tmp_path/'actions.db'
    bench.record(db,'prepare',bench.prepare());bought=bench.record(db,'buy',bench.execute())['state']
    qty=bought['positions']['SPY'];at='2026-10-02T00:00:00Z'
    event=dict(id='div',kind='cash_dividend',symbol='SPY',quantity_before=qty,amount_per_share='1',currency='USD',effective_at=at,received_at=at,payment_at=None,source_sha256='a'*64)
    op=dict(kind='corporate_action',event=event,now=at)
    bench.record(db,'div',op);bench.record(db,'same-div-new-operation',op)
    split=dict(id='split',kind='split',symbol='SPY',quantity_before=qty,ratio='2',effective_at=at,received_at=at,source_sha256='b'*64)
    bench.record(db,'split',dict(kind='corporate_action',event=split,now=at))
    report=audit_database(db,terminal_marks={'SPY':'50','QQQ':'100','IWM':'100'})
    assert report['status']=='reconciled' and report['metrics']['receivables']==qty
    before=D(report['metrics']['net_pnl_before_overhead'])
    pay_at='2026-10-09T12:00:00Z'
    pay=dict(id='payment',kind='dividend_payment',dividend_id='div',amount=qty,currency='USD',effective_at=pay_at,received_at=pay_at,source_sha256='c'*64)
    bench.record(db,'paid',dict(kind='corporate_action',event=pay,now=pay_at))
    paid=audit_database(db,terminal_marks={'SPY':'50','QQQ':'100','IWM':'100'})
    assert paid['status']=='reconciled' and D(paid['metrics']['receivables'])==0
    assert D(paid['metrics']['net_pnl_before_overhead'])==before


def fake_book(db, mutate):
    initial=dict(cash='100000',positions={},modeled_fills=[])
    def transition(state,inputs):
        mutate(state)
        return dict(state=state,result={})
    apply_batch(db,run_id='broken-modeled-fixture',registry_sha256='a'*64,implementation_sha256='b'*64,
        initial_state=initial,batch_id='bad',inputs={},transition=transition)


def test_valid_hash_chain_cannot_hide_wrong_cash(tmp_path):
    db=tmp_path/'wrong.db';fake_book(db,lambda s:s.update(cash='100001'))
    report=audit_database(db)
    assert report['status']=='mismatch' and report['mismatched_operations']==['bad']


def test_valid_hash_chain_cannot_hide_wrong_units(tmp_path):
    db=tmp_path/'wrong.db';fake_book(db,lambda s:s.update(positions={'SPY':'1'}))
    assert audit_database(db)['status']=='mismatch'


def test_fill_claimed_cash_is_checked_independently(tmp_path):
    db=tmp_path/'wrong.db'
    def mutate(s):
        s['modeled_fills']=[dict(symbol='SPY',side='buy',quantity='1',price='100',base_fee='0',quote_fee='1',cash_change='-100',quantity_change='1',modeled_fill=True)]
    fake_book(db,mutate)
    with pytest.raises(ValueError,match='assertion differs'):audit_database(db)


def test_options_roundtrip_fifo_matches_cash_and_fees(tmp_path,evidence):
    db=tmp_path/'roundtrip.db';bought=options.buy(db,evidence)['state'];cal=evidence[0]
    day=cal.sessions[cal.index(options.DAY)+10].day;at=cal.decision_at(day)
    exits={p.contract.underlying:options.adapter.plan_exit(cal,day,p,bars=[],
        option_quotes=[options.quote(p.contract.symbol,'opra',at)],
        underlying_quotes=[options.quote(p.contract.underlying,'sip',at,'99.9','100.1')],
        fees_per_contract=1,fee_schedule_verified=True) for p in options.adapter_positions(bought)}
    operation=dict(kind='prepare',portfolio_batch_id='exit',calendar=options.encode(cal),
        session_date=day.isoformat(),now=at.isoformat(),entry_outputs=[],exit_outputs=exits,contracts={},marks={})
    options.record(db,'exit-plan',operation)
    sold=options.record(db,'exit',dict(kind='execute',portfolio_batch_id='exit',now=at.isoformat(),marks={}))['state']
    report=audit_database(db)
    assert report['status']=='reconciled' and len(report['fifo_matches'])==3
    assert D(report['metrics']['realized_net'])==D(sold['cash'])-100000
    assert D(report['metrics']['modeled_fees'])==6


def test_valid_hash_state_with_wrong_option_multiplier_is_rejected(tmp_path):
    db=tmp_path/'wrong-multiplier.db'
    def mutate(s):
        s['positions']={'SPY':dict(quantity=1,contract=dict(symbol='SPY_C',underlying='SPY',multiplier=50,
            option_type='call',standard_unadjusted=True,physically_delivered=True))}
    fake_book(db,mutate)
    with pytest.raises(ValueError,match='metadata'):audit_database(db)
