from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from dataclasses import replace
import pytest

from trader_engine.research.phase3_benchmarks import EQUITIES,CRYPTO,OPTIONS,Timing,SourceStamp,EquitySnapshot,ExposureSnapshot,exposure_target,passive_initial_target,delta_target,DeltaExposure,DeltaSnapshot
from trader_engine.research.phase3_daily import FeeSchedule
from trader_engine.research.phase3_benchmark_ledger import initial_state,SignalPrice,ExecutionPrice,prepare_target,execute_target

UTC=timezone.utc
CLOSE=datetime(2026,9,30,20,tzinfo=UTC)
AT=datetime(2026,10,1,13,30,tzinfo=UTC)
STAMP=SourceStamp(CLOSE,CLOSE+timedelta(seconds=1),'a'*64)
TIMING=Timing(CLOSE,AT-timedelta(minutes=10),AT,'b'*64)


def inputs(state,timing=TIMING):
 symbols=('BTC/USD','ETH/USD') if state['strategy_id']==CRYPTO else ('IWM','QQQ','SPY')
 stamp=SourceStamp(timing.expected_as_of,timing.expected_as_of+timedelta(seconds=1),'a'*64)
 prices={s:SignalPrice(D(100),stamp) for s in symbols}
 fees={s:FeeSchedule(D(0)) for s in symbols}
 steps={s:D('.0001') if state['strategy_id']==CRYPTO else D(1) for s in symbols}
 return stamp,prices,fees,steps


def target_for(state,fraction=D('.24'),timing=TIMING):
 stamp,prices,fees,steps=inputs(state,timing)
 equity=D(state['cash'])+sum((D(q)*100 for q in state['positions'].values()),D(0))
 bench=EquitySnapshot(equity,stamp)
 if state['control']=='passive':target=passive_initial_target(state['strategy_id'],bench,replace(timing,startup=True))
 else:target=exposure_target(state['strategy_id'],ExposureSnapshot(D(100000),fraction*100000,stamp),bench,timing)
 return target,prices,fees,steps


def observations(state,at=AT,price=D(100)):
 source='alpaca_crypto' if state['strategy_id']==CRYPTO else 'sip'
 return {s:ExecutionPrice(at,at,source,'c'*64,raw_open=price,bid=price,ask=price,bid_size=D(10),ask_size=D(10)) for s in inputs(state)[1]}


def test_equity_fee_paid_and_no_borrow_quantity_never_increases():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 state=prepare_target(state,'first',target,prices,fees,steps)
 new,report=execute_target(state,'first','attempt',observations(state),AT)
 assert len(report['modeled_fills'])==3
 assert all(D(q)==79 for q in new['positions'].values())
 assert D(new['cash'])+sum(D(q)*D('100.07') for q in new['positions'].values())==100000
 assert not report['execution_authorized'] and report['pending_sells']=={}
 repeated,replay=execute_target(new,'first','attempt',observations(state),AT)
 assert repeated==new and replay==report
 with pytest.raises(ValueError,match='Conflicting'):
  execute_target(new,'first','attempt',observations(state,price=D(99)),AT)


def test_own_equity_mismatch_rejected():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 bad=replace(target,dollars={s:D(1) for s in target.dollars})
 with pytest.raises(ValueError,match='own reconciled equity'):
  prepare_target(state,'bad',bad,prices,fees,steps)


def test_passive_no_rebalance_even_after_initial_missing_buy():
 state=initial_state(EQUITIES,'passive');target,prices,fees,steps=target_for(state)
 state=prepare_target(state,'first',target,prices,fees,steps)
 obs=observations(state);obs['SPY']=None
 new,report=execute_target(state,'first','attempt',obs,AT)
 assert 'SPY' not in new['positions'] and report['issues'][0]['reason']=='missing_execution_evidence'
 with pytest.raises(ValueError,match='Passive holdings'):
  prepare_target(new,'again',target,prices,fees,steps)


def test_missing_sell_is_pending_and_does_not_fund_buys():
 state=initial_state(OPTIONS,'matched');state['cash']='0';state['positions']={'SPY':'1000'}
 timing=replace(TIMING,execution_at=AT+timedelta(minutes=5))
 stamp,prices,fees,steps=inputs(state,timing)
 data=DeltaSnapshot(D(100000),(DeltaExposure('QQQ',1,D(1),D(1000),stamp,'d'*64),),stamp,True)
 target=delta_target(data,EquitySnapshot(D(100000),stamp),timing,expected_delta_model_sha256='d'*64)
 state=prepare_target(state,'rebalance',target,prices,fees,steps)
 obs=observations(state,timing.execution_at);obs['SPY']=None
 new,report=execute_target(state,'rebalance','attempt',obs,timing.execution_at)
 assert new['positions']=={'SPY':'1000'} and D(new['cash'])==0
 assert report['pending_sells']=={'SPY':'1000'} and new['batches']['rebalance']['apply_hash'] is None
 assert any(x['reason']=='precision_or_cash_constraint' for x in report['issues'])
 retry=timing.execution_at+timedelta(days=1)
 new,report=execute_target(new,'rebalance','retry',observations(new,retry),retry,retry_execution_at=retry)
 assert not new['positions'] and D(new['cash'])>0
 assert report['pending_sells']=={} and all(f['side']=='sell' for f in report['modeled_fills'])


def test_sells_precede_buys_and_only_actual_cash_funds_them():
 state=initial_state(OPTIONS,'matched');state['cash']='0';state['positions']={'SPY':'1000'}
 timing=replace(TIMING,execution_at=AT+timedelta(minutes=5))
 stamp,prices,fees,steps=inputs(state,timing)
 data=DeltaSnapshot(D(100000),(DeltaExposure('QQQ',1,D(1),D(1000),stamp,'d'*64),),stamp,True)
 target=delta_target(data,EquitySnapshot(D(100000),stamp),timing,expected_delta_model_sha256='d'*64)
 state=prepare_target(state,'rebalance',target,prices,fees,steps)
 new,report=execute_target(state,'rebalance','attempt',observations(state,timing.execution_at),timing.execution_at)
 assert [(f['symbol'],f['side']) for f in report['modeled_fills']]==[('SPY','sell'),('QQQ','buy')]
 assert D(new['cash'])>=0 and D(new['positions']['QQQ'])<1000


def test_crypto_base_fee_reduces_owned_units_and_sell_never_oversells():
 timing=Timing(datetime(2026,10,1,tzinfo=UTC),datetime(2026,10,1,0,5,tzinfo=UTC),datetime(2026,10,1,0,5,tzinfo=UTC),'b'*64)
 state=initial_state(CRYPTO,'matched');target,prices,fees,steps=target_for(state,D('.25'),timing)
 fees={s:FeeSchedule(D('.001'),currency='base') for s in fees}
 state=prepare_target(state,'first',target,prices,fees,steps)
 new,report=execute_target(state,'first','attempt',observations(state,timing.execution_at),timing.execution_at)
 for f in report['modeled_fills']:
  assert D(f['base_fee'])==D(f['quantity'])*D('.0025')
  assert D(f['quantity_change'])==D(f['quantity'])-D(f['base_fee'])
 later=replace(timing,expected_as_of=timing.expected_as_of+timedelta(days=1),decision_at=timing.decision_at+timedelta(days=1),execution_at=timing.execution_at+timedelta(days=1))
 target,prices,_,steps=target_for(new,D(0),later)
 prepared=prepare_target(new,'exit',target,prices,fees,steps)
 closed,report=execute_target(prepared,'exit','exit-attempt',observations(new,later.execution_at),later.execution_at)
 assert all(D(q)>=0 for q in closed['positions'].values())
 assert not report['pending_sells'] and report['dust_owned']
 assert closed['batches']['exit']['apply_hash'] is not None
 assert all(abs(D(f['quantity_change']))<=D(new['positions'][f['symbol']]) for f in report['modeled_fills'])


def test_future_quote_and_duplicate_boundary_reject():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 state=prepare_target(state,'first',target,prices,fees,steps)
 obs={s:replace(o,received_at=AT+timedelta(seconds=1)) for s,o in observations(state).items()}
 new,report=execute_target(state,'first','attempt',obs,AT)
 assert not report['modeled_fills'] and len(report['issues'])==3
 with pytest.raises(ValueError,match='duplicate'):
  prepare_target(new,'again',target,prices,fees,steps)


def test_prepared_target_rejects_corporate_action_change():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 state=prepare_target(state,'first',target,prices,fees,steps)
 state['corporate_actions']={'events':{},'receivables':{},'last_event_at':None}
 with pytest.raises(ValueError,match='Corporate action'):
  execute_target(state,'first','attempt',observations(state),AT)


def test_out_of_band_ownership_change_rejected_and_clock_advances():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 prepared=prepare_target(state,'first',target,prices,fees,steps)
 tampered=deepcopy(prepared);tampered['cash']='100001'
 with pytest.raises(ValueError,match='outside modeled'):
  execute_target(tampered,'first','attempt',observations(state),AT)
 result,_=execute_target(prepared,'first','attempt',observations(state),AT)
 assert result['last_portfolio_at']==AT.isoformat()


def test_falling_execution_price_does_not_increase_frozen_buy_quantity():
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 prepared=prepare_target(state,'first',target,prices,fees,steps)
 new,report=execute_target(prepared,'first','attempt',observations(state,price=D(50)),AT)
 assert all(D(f['quantity'])==80 for f in report['modeled_fills'])
 assert all(D(q)<=D(prepared['batches']['first']['buys'][s]) for s,q in new['positions'].items())


def test_retry_cannot_switch_execution_cost_scenario():
 state=initial_state(EQUITIES,'matched');state['positions']={'SPY':'10'}
 target,prices,fees,steps=target_for(state,D(0))
 prepared=prepare_target(state,'exit',target,prices,fees,steps)
 new,_=execute_target(prepared,'exit','first',{s:None for s in prices},AT)
 later=AT+timedelta(days=1)
 with pytest.raises(ValueError,match='Frozen scenario'):
  execute_target(new,'exit','retry',observations(new,later),later,retry_execution_at=later,stress=True)


@pytest.mark.parametrize('field,value', [('buys','900'),('budgets','90000'),('original_sells','900'),('steps','0.1')])
def test_mutated_frozen_plan_rejected_before_execution(field,value):
 state=initial_state(EQUITIES,'matched');target,prices,fees,steps=target_for(state)
 prepared=prepare_target(state,'first',target,prices,fees,steps)
 tampered=deepcopy(prepared);tampered['batches']['first'][field]['IWM']=value
 with pytest.raises(ValueError,match='plan integrity'):
  execute_target(tampered,'first','attempt',observations(state),AT)
 assert prepared['positions']=={} and prepared['cash']=='100000'


def test_mutated_fee_rejected_before_pending_retry():
 state=initial_state(EQUITIES,'matched');state['positions']={'SPY':'10'}
 target,prices,fees,steps=target_for(state,D(0))
 prepared=prepare_target(state,'exit',target,prices,fees,steps)
 pending,_=execute_target(prepared,'exit','attempt',{s:None for s in prices},AT)
 pending['batches']['exit']['fees']['SPY']['rate']='.99'
 later=AT+timedelta(days=1)
 with pytest.raises(ValueError,match='plan integrity'):
  execute_target(pending,'exit','retry',observations(pending,later),later,retry_execution_at=later)


@pytest.mark.parametrize('change', ['excess','negative','missing','extra'])
def test_pending_sell_progress_cannot_change_frozen_maximum_or_symbols(change):
 state=initial_state(EQUITIES,'matched');state['positions']={'SPY':'10'}
 target,prices,fees,steps=target_for(state,D(0))
 prepared=prepare_target(state,'exit',target,prices,fees,steps)
 pending,_=execute_target(prepared,'exit','first',{s:None for s in prices},AT)
 progress=pending['batches']['exit']['remaining_sells']
 if change=='excess':progress['SPY']='11'
 elif change=='negative':progress['SPY']='-1'
 elif change=='missing':progress.pop('SPY')
 else:progress['AAPL']='1'
 later=AT+timedelta(days=1)
 with pytest.raises(ValueError):
  execute_target(pending,'exit','retry',observations(pending,later),later,retry_execution_at=later)
