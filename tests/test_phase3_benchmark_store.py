from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from pathlib import Path
import json
import pytest

from trader_engine.research.phase3_benchmark_store import record_benchmark_operation
from trader_engine.research.phase3_store import read_state

E='E_DONCHIAN20_V1';C='C_SMA200_CONFIRM_V1';O='O_ETF_TREND_CALL_V1'
UTC=timezone.utc
AT=datetime(2026,10,1,13,30,tzinfo=UTC)


def record(db,ident,operation,*,strategy=E,control='matched',scenario='base'):
 registry=json.loads((Path(__file__).resolve().parents[1]/'config/research/phase3_protocols.json').read_text())
 return record_benchmark_operation(db,run_id='synthetic-benchmark',strategy_id=strategy,control=control,
  cost_scenario=scenario,operation_id=ident,operation=operation,registry=registry)


def symbols(strategy):return ('BTC/USD','ETH/USD') if strategy==C else ('IWM','QQQ','SPY')


def prepare(batch='entry',*,strategy=E,at=AT,equity='100000',fraction='0.24',control='matched',fee_currency='quote'):
 close=at.replace(hour=0,minute=0) if strategy==C else (at-timedelta(days=1)).replace(hour=20,minute=0)
 decision=at if strategy in (C,O) else at-timedelta(minutes=10)
 stamp={'as_of':close.isoformat(),'received_at':(close+timedelta(seconds=1)).isoformat(),'source_sha256':'a'*64}
 timing={'expected_as_of':close.isoformat(),'decision_at':decision.isoformat(),'execution_at':at.isoformat(),'calendar_sha256':'b'*64,'startup':control=='passive'}
 target={'kind':'passive' if control=='passive' else 'exposure','timing':timing,'benchmark':{'equity':equity,'stamp':stamp}}
 if control!='passive':
  if strategy==O:
   target.update(kind='delta',expected_delta_model_sha256='d'*64)
   rows=[] if fraction=='0' else [{'underlying':'SPY','contracts':1,'delta':'0.5','underlying_price':'500','stamp':stamp,'delta_model_sha256':'d'*64,'multiplier':100}]
   target['candidate']={'candidate_equity':'100000','exposures':rows,'stamp':stamp,'complete':True,'basis':'actual'}
  else:target['candidate']={'candidate_equity':'100000','gross_dollars':str(D(fraction)*100000),'stamp':stamp}
 return {'kind':'prepare','portfolio_batch_id':batch,'target_input':target,
  'signal_prices':{s:{'price':'100','stamp':stamp} for s in symbols(strategy)},
  'fees':{s:{'rate':'0','fixed_cash':'0','currency':fee_currency} for s in symbols(strategy)},
  'quantity_steps':{s:'0.0001' if strategy==C else '1' for s in symbols(strategy)}}


def execute(batch='entry',*,attempt='attempt',strategy=E,at=AT,missing=False,retry=False):
 operation={'kind':'execute','portfolio_batch_id':batch,'attempt_id':attempt,'now':at.isoformat(),
  'observations':{s:None if missing else {'event_at':at.isoformat(),'received_at':at.isoformat(),
    'source':'alpaca_crypto' if strategy==C else 'sip','source_sha256':'c'*64,
    'raw_open':'100','bid':'100','ask':'100','bid_size':'10','ask_size':'10'} for s in symbols(strategy)}}
 if retry:operation['retry_execution_at']=at.isoformat()
 return operation


def equity(state):return str(D(state['cash'])+sum((D(q)*100 for q in state['positions'].values()),D(0)))


def test_target_derived_fills_are_durable_and_replay_does_not_duplicate(tmp_path):
 db=tmp_path/'control.db'
 prepared=record(db,'prepare',prepare())
 assert prepared['state']['positions']=={}
 result=record(db,'execute',execute())
 assert len(result['result']['modeled_fills'])==3
 assert sum(D(q) for q in result['state']['positions'].values())==237
 replay=record(db,'execute',execute())
 assert not replay['inserted'] and len(read_state(db)['state']['modeled_fills'])==3
 assert replay['state']==result['state'] and not replay['prospective_credit']


def test_restart_preserves_missing_sell_then_later_exit_without_rebuy(tmp_path):
 db=tmp_path/'control.db';record(db,'prepare',prepare());bought=record(db,'execute',execute())['state']
 later=AT+timedelta(days=1)
 record(db,'exit-plan',prepare('exit',at=later,equity=equity(bought),fraction='0'))
 pending=record(db,'missing',execute('exit',attempt='missing',at=later,missing=True))
 assert len(pending['result']['pending_sells'])==3
 assert read_state(db)['state']['positions']==bought['positions']
 retry=later+timedelta(days=1)
 sold=record(db,'retry',execute('exit',attempt='retry',at=retry,retry=True))
 assert sold['state']['positions']=={} and not sold['result']['pending_sells']
 assert all(f['side']=='sell' for f in sold['result']['modeled_fills'])


def test_crypto_base_fee_dust_retained_across_restart(tmp_path):
 db=tmp_path/'control.db';at=datetime(2026,10,1,0,5,tzinfo=UTC)
 record(db,'prepare',prepare(strategy=C,at=at,fraction='.25',fee_currency='base'),strategy=C)
 bought=record(db,'execute',execute(strategy=C,at=at),strategy=C)['state']
 later=at+timedelta(days=1)
 record(db,'exit-plan',prepare('exit',strategy=C,at=later,equity=equity(bought),fraction='0',fee_currency='base'),strategy=C)
 sold=record(db,'exit',execute('exit',strategy=C,at=later,attempt='exit-attempt'),strategy=C)
 assert sold['result']['dust_owned'] and not sold['result']['pending_sells']
 saved=read_state(db)['state']
 assert saved['positions']==sold['state']['positions']
 assert all(D(q)>0 for q in saved['positions'].values())
 assert saved['batches']['exit']['apply_hash'] is not None


def test_frozen_cost_scenario_and_unknown_execution_override_rejected(tmp_path):
 db=tmp_path/'control.db';record(db,'prepare',prepare());before=read_state(db)
 with pytest.raises(ValueError,match='identity changed'):
  record(db,'execute',execute(),scenario='stress')
 assert read_state(db)==before
 bad=execute();bad['stress']=True
 with pytest.raises(ValueError,match='unknown/missing'):
  record(db,'execute',bad)
 assert read_state(db)==before


def test_passive_initialization_uses_frozen_sleeve_once(tmp_path):
 db=tmp_path/'control.db'
 record(db,'prepare',prepare(control='passive'),control='passive')
 bought=record(db,'execute',execute(),control='passive')['state']
 before=read_state(db)
 with pytest.raises(ValueError,match='Passive holdings'):
  record(db,'rebalance',prepare('rebalance',at=AT+timedelta(days=1),equity=equity(bought),control='passive'),control='passive')
 assert read_state(db)==before


def test_direct_weights_and_inexact_decimal_rejected_atomically(tmp_path):
 db=tmp_path/'control.db'
 bad=prepare();bad['target_input']['weights']={'SPY':'1'}
 with pytest.raises(ValueError,match='unknown/missing'):record(db,'prepare',bad)
 bad=prepare();bad['signal_prices']['SPY']['price']=100.0
 with pytest.raises(ValueError,match='decimal string'):record(db,'prepare',bad)
 good=record(db,'prepare',prepare())
 assert good['state']['cash']=='100000' and read_state(db)['batches']==1


def test_option_underlying_control_uses_archived_delta_and_own_cash(tmp_path):
 db=tmp_path/'control.db';at=AT+timedelta(minutes=5)
 record(db,'prepare',prepare(strategy=O,at=at),strategy=O)
 bought=record(db,'execute',execute(strategy=O,at=at),strategy=O)
 assert set(bought['state']['positions'])=={'SPY'}
 assert D(bought['state']['positions']['SPY'])==249
 assert D(bought['state']['cash'])>=0
 assert not bought['result']['investment_qualified']


def test_future_source_and_mixed_control_identity_reject(tmp_path):
 db=tmp_path/'control.db';bad=prepare()
 bad['target_input']['candidate']['stamp']=dict(bad['target_input']['candidate']['stamp'],received_at=(AT+timedelta(seconds=1)).isoformat())
 with pytest.raises(ValueError,match='causal boundary'):record(db,'bad',bad)
 record(db,'prepare',prepare())
 with pytest.raises(ValueError,match='identity changed'):
  record(db,'passive',prepare(control='passive'),control='passive')


def test_distribution_remains_unspendable_until_durable_payment(tmp_path):
 db=tmp_path/'control.db';record(db,'prepare',prepare());bought=record(db,'execute',execute())['state']
 boundary='2026-10-02T00:00:00Z'
 event={'id':'synthetic-dividend','kind':'cash_dividend','symbol':'SPY','currency':'USD',
  'effective_at':boundary,'received_at':'2026-10-01T00:00:00Z','source_sha256':'e'*64,
  'quantity_before':bought['positions']['SPY'],'amount_per_share':'1','payment_at':None}
 entitled=record(db,'entitlement',{'kind':'corporate_action','event':event,'now':boundary})['state']
 assert entitled['cash']==bought['cash']
 assert entitled['corporate_actions']['receivables']['synthetic-dividend']['amount']=='79'
 paid_at='2026-10-05T12:00:00Z'
 payment={'id':'synthetic-payment','kind':'dividend_payment','dividend_id':'synthetic-dividend','currency':'USD',
  'effective_at':paid_at,'received_at':paid_at,'source_sha256':'f'*64,'amount':'79'}
 paid=record(db,'payment',{'kind':'corporate_action','event':payment,'now':paid_at})['state']
 assert D(paid['cash'])==D(bought['cash'])+79
 assert read_state(db)['state']['corporate_actions']['receivables']['synthetic-dividend']['paid']


def test_run_cost_scenario_changes_economics_but_never_mixes(tmp_path):
 base=tmp_path/'base.db';stress=tmp_path/'stress.db'
 for db,scenario in ((base,'base'),(stress,'stress')):
  record(db,'prepare',prepare(),scenario=scenario)
  record(db,'execute',execute(),scenario=scenario)
 b=read_state(base)['state'];s=read_state(stress)['state']
 assert b['cost_scenario']=='base' and s['cost_scenario']=='stress'
 assert D(b['modeled_fills'][0]['price'])<D(s['modeled_fills'][0]['price'])
