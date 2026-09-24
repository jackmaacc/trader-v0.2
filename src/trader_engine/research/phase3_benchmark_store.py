"""Durable JSON integration for frozen offline benchmark controls only."""
from datetime import datetime
from decimal import Decimal
import sys

from trader_engine.operations import accounting_ledger, decision_ledger
from . import phase3_actions as actions, phase3_benchmarks as targets
from . import phase3_benchmark_ledger as ledger, phase3_daily as daily
from . import phase3_store as store, protocol_registry
from .phase3_identity import ImplementationGuard, record_operation


def _fields(value, required, optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
        raise ValueError('Explicit JSON fields required; unknown/missing fields rejected')
    return value


def _decimal(value):
    if not isinstance(value,str):raise ValueError('Exact decimal string required')
    result=Decimal(value)
    if not result.is_finite():raise ValueError('Finite decimal required')
    return result


def _time(value):
    if not isinstance(value,str):raise ValueError('ISO timestamp string required')
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Aware timestamp required')
    return stamp


def _boolean(value):
    if type(value) is not bool:raise ValueError('Explicit boolean required')
    return value


def _stamp(value):
    _fields(value,('as_of','received_at','source_sha256'))
    return targets.SourceStamp(_time(value['as_of']),_time(value['received_at']),value['source_sha256'])


def _timing(value):
    _fields(value,('expected_as_of','decision_at','execution_at','calendar_sha256','startup'))
    return targets.Timing(_time(value['expected_as_of']),_time(value['decision_at']),
                          _time(value['execution_at']),value['calendar_sha256'],_boolean(value['startup']))


def _target(state,value):
    _fields(value,('kind','timing','benchmark'),('candidate','expected_delta_model_sha256'))
    timing=_timing(value['timing'])
    bench=value['benchmark'];_fields(bench,('equity','stamp'))
    benchmark=targets.EquitySnapshot(_decimal(bench['equity']),_stamp(bench['stamp']))
    strategy=state['strategy_id'];kind=value['kind']
    if state['control']=='passive':
        if kind!='passive' or 'candidate' in value or 'expected_delta_model_sha256' in value:
            raise ValueError('Passive target accepts only its own initial equity')
        return targets.passive_initial_target(strategy,benchmark,timing)
    candidate=value.get('candidate')
    if strategy==targets.OPTIONS:
        if kind!='delta':raise ValueError('Options control requires archived delta evidence')
        _fields(candidate,('candidate_equity','exposures','stamp','complete','basis'))
        if not isinstance(candidate['exposures'],list):raise ValueError('Explicit delta rows required')
        rows=[]
        for v in candidate['exposures']:
            _fields(v,('underlying','contracts','delta','underlying_price','stamp','delta_model_sha256','multiplier'))
            if type(v['contracts']) is not int or type(v['multiplier']) is not int:
                raise ValueError('Exact integer contract count/multiplier required')
            rows.append(targets.DeltaExposure(v['underlying'],v['contracts'],_decimal(v['delta']),
                _decimal(v['underlying_price']),_stamp(v['stamp']),v['delta_model_sha256'],v['multiplier']))
        evidence=targets.DeltaSnapshot(_decimal(candidate['candidate_equity']),tuple(rows),_stamp(candidate['stamp']),
            _boolean(candidate['complete']),candidate['basis'])
        return targets.delta_target(evidence,benchmark,timing,expected_delta_model_sha256=value.get('expected_delta_model_sha256'))
    if 'expected_delta_model_sha256' in value:raise ValueError('Unexpected delta model for exposure control')
    if timing.startup:
        if kind!='planned':raise ValueError('Startup requires planned candidate weights')
        _fields(candidate,('weights','stamp'))
        if not isinstance(candidate['weights'],dict):raise ValueError('Explicit startup weight map required')
        evidence=targets.PlannedWeights({s:_decimal(w) for s,w in candidate['weights'].items()},_stamp(candidate['stamp']))
    else:
        if kind!='exposure':raise ValueError('Lagged control requires actual gross exposure')
        _fields(candidate,('candidate_equity','gross_dollars','stamp'))
        evidence=targets.ExposureSnapshot(_decimal(candidate['candidate_equity']),_decimal(candidate['gross_dollars']),_stamp(candidate['stamp']))
    return targets.exposure_target(strategy,evidence,benchmark,timing)


def _map(value):
    if not isinstance(value,dict):raise ValueError('Explicit symbol map required')
    return value


def _transition(state,operation):
    if state.get('cost_scenario') not in ('base','stress'):raise ValueError('Frozen cost scenario required')
    if not isinstance(operation,dict):raise ValueError('JSON operation required')
    kind=operation.get('kind')
    if kind=='corporate_action':
        _fields(operation,('kind','event','now'))
        new,receipt=actions.apply_corporate_action(state,operation['event'],operation['now'])
        return {'state':new,'result':receipt}
    if kind=='prepare':
        _fields(operation,('kind','portfolio_batch_id','target_input','signal_prices','fees','quantity_steps'))
        target=_target(state,operation['target_input'])
        prices={};fees={}
        for s,v in _map(operation['signal_prices']).items():
            _fields(v,('price','stamp'));prices[s]=ledger.SignalPrice(_decimal(v['price']),_stamp(v['stamp']))
        for s,v in _map(operation['fees']).items():
            _fields(v,('rate','fixed_cash','currency'));fees[s]=daily.FeeSchedule(_decimal(v['rate']),_decimal(v['fixed_cash']),v['currency'])
        steps={s:_decimal(v) for s,v in _map(operation['quantity_steps']).items()}
        new=ledger.prepare_target(state,operation['portfolio_batch_id'],target,prices,fees,steps)
        return {'state':new,'result':{'operation':kind,'portfolio_batch_id':operation['portfolio_batch_id'],
            'cost_scenario':state['cost_scenario'],'execution_authorized':False}}
    if kind=='execute':
        _fields(operation,('kind','portfolio_batch_id','attempt_id','observations','now'),('retry_execution_at',))
        observations={}
        for s,v in _map(operation['observations']).items():
            if v is None:observations[s]=None;continue
            _fields(v,('event_at','received_at','source','source_sha256'),('raw_open','bid','ask','bid_size','ask_size'))
            observations[s]=ledger.ExecutionPrice(_time(v['event_at']),_time(v['received_at']),v['source'],v['source_sha256'],
                *[None if v.get(k) is None else _decimal(v[k]) for k in ('raw_open','bid','ask','bid_size','ask_size')])
        retry=operation.get('retry_execution_at')
        new,report=ledger.execute_target(state,operation['portfolio_batch_id'],operation['attempt_id'],observations,
            _time(operation['now']),stress=state['cost_scenario']=='stress',retry_execution_at=None if retry is None else _time(retry))
        return {'state':new,'result':report}
    raise ValueError('Unknown offline benchmark operation')


def record_benchmark_operation(database,*,run_id,strategy_id,control,cost_scenario,
                               operation_id,operation,registry):
    """Persist target-derived modeled control, never paper/prospective credit."""
    if cost_scenario not in ('base','stress'):raise ValueError('Fixed base or stress scenario required')
    initial=ledger.initial_state(strategy_id,control)
    initial['cost_scenario']=cost_scenario
    return record_operation(database,run_id=run_id,registry=registry,initial_state=initial,
                            operation_id=operation_id,operation=operation,transition=_transition,guard=_GUARD)


_GUARD=ImplementationGuard([ledger,targets,daily,actions,store,accounting_ledger,decision_ledger,
                            protocol_registry,sys.modules[__name__]])
