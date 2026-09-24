"""Strict JSON integration for durable, base-only offline options research state."""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import sys

from trader_engine.execution import portfolio as reservations
from trader_engine.operations import accounting_ledger, decision_ledger
from . import phase3_daily, phase3_portfolio, phase3_options as adapter
from . import phase3_options_portfolio as portfolio
from . import phase3_store, protocol_registry, phase3_identity


def _object(value, keys=None):
    if not isinstance(value, dict) or any(not isinstance(k,str) for k in value):
        raise ValueError('JSON object required')
    if keys is not None and set(value)!=set(keys):raise ValueError('unexpected or missing JSON fields')
    return value


def _text(value):
    if not isinstance(value,str) or not value.strip():raise ValueError('nonempty text required')
    return value


def _date(value):
    return date.fromisoformat(_text(value))


def _time(value):
    result=datetime.fromisoformat(_text(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('timezone-aware timestamp required')
    return result


def _decimal(value):
    if not isinstance(value,str):raise ValueError('exact decimal string required')
    try:result=Decimal(value)
    except InvalidOperation:raise ValueError('invalid decimal string') from None
    if not result.is_finite():raise ValueError('finite decimal required')
    return result


def _bool(value):
    if type(value) is not bool:raise ValueError('explicit JSON boolean required')
    return value


def _one(value):
    if type(value) is not int or value!=1:raise ValueError('one contract required')
    return value


def _calendar(value):
    _object(value,('sessions','archived_sha256'))
    if not isinstance(value['sessions'],list):raise ValueError('ordered calendar list required')
    sessions=[]
    for row in value['sessions']:
        _object(row,('day','close_at'))
        sessions.append(adapter.Session(_date(row['day']),_time(row['close_at'])))
    return adapter.ArchivedCalendar(tuple(sessions),_text(value['archived_sha256']))


def _contracts(values):
    result={}
    for symbol,row in _object(values).items():
        _object(row,('symbol','underlying','expiry','strike','listed_at','metadata_received_at','option_type','multiplier','standard_unadjusted','physically_delivered','active'))
        if _text(row['symbol'])!=symbol:raise ValueError('contract mapping symbol mismatch')
        if type(row['multiplier']) is not int or row['multiplier']!=100:raise ValueError('standard 100-share multiplier required')
        result[symbol]=adapter.Contract(symbol,_text(row['underlying']),_date(row['expiry']),_decimal(row['strike']),
            _time(row['listed_at']),_time(row['metadata_received_at']),_text(row['option_type']),row['multiplier'],
            _bool(row['standard_unadjusted']),_bool(row['physically_delivered']),_bool(row['active']))
    return result


def _marks(values):
    result={}
    for symbol,row in _object(values).items():
        _object(row,('symbol','feed','bid','ask','bid_size','ask_size','event_at','received_at'))
        if _text(row['symbol'])!=symbol or row['feed']!='opra':raise ValueError('OPRA mark symbol/source mismatch')
        result[symbol]=adapter.Quote(symbol,'opra',*[_decimal(row[k]) for k in ('bid','ask','bid_size','ask_size')],
                                    _time(row['event_at']),_time(row['received_at']))
    return result


def _outputs(entry_values,exit_values):
    if not isinstance(entry_values,list):raise ValueError('entry output list required')
    _object(exit_values)
    common={'action','reason','strategy_id','execution_authorized','fill_created','investment_qualified'}
    def check(row,entry):
        _object(row)
        action=row.get('action');_text(row.get('reason'));_text(row.get('strategy_id'))
        for key in ('execution_authorized','fill_created','investment_qualified'):
            if _bool(row.get(key)) is not False:raise ValueError('adapter outputs have no execution or qualification authority')
        if row['strategy_id']!=portfolio.STRATEGY:raise ValueError('wrong frozen options strategy')
        if entry:
            extra={'underlying','decision_at'};_text(row.get('underlying'));_time(row.get('decision_at'))
            if action=='entry_candidate':extra|={'contract','quantity','modeled_prices','base_premium','base_fees','planned_cash_debit'}
            elif action!='skip':raise ValueError('unknown entry action')
        else:
            extra={'ownership_retained','coverage_failure'}
            if _bool(row.get('ownership_retained')) is not True:raise ValueError('adapter must retain ownership until modeled booking')
            _bool(row.get('coverage_failure'))
            if action in {'pending_exit','exit_candidate'}:extra.add('pending_exit_reason');_text(row.get('pending_exit_reason'))
            if action=='exit_candidate':extra|={'contract','quantity','observed_at','modeled_prices','base_fees','modeled_net_proceeds'};_time(row.get('observed_at'))
            elif action not in {'hold','pending_exit'}:raise ValueError('unknown exit action')
        _object(row,common|extra)
        if action in {'entry_candidate','exit_candidate'}:
            _text(row['contract']);_one(row['quantity'])
            _object(row['modeled_prices'],('base_buy','base_sell','stress_buy','stress_sell'))
            for value in row['modeled_prices'].values():_decimal(value)
            for key in ('base_premium','base_fees','planned_cash_debit','modeled_net_proceeds'):
                if key in row:_decimal(row[key])
    for row in entry_values:check(row,True)
    for row in exit_values.values():check(row,False)
    return deepcopy(entry_values),deepcopy(exit_values)


def _transition(state,operation):
    _object(operation)
    kind=operation.get('kind')
    if kind=='baseline':
        _object(operation,('kind','utc_day','prior_close_equity','boundary_at','provenance'))
        new=portfolio.set_day_baseline(state,_date(operation['utc_day']),_decimal(operation['prior_close_equity']),
            boundary_at=_time(operation['boundary_at']),provenance=_text(operation['provenance']))
        return {'state':new,'result':{'operation':'baseline','execution_authorized':False,'investment_qualified':False}}
    if kind=='prepare':
        _object(operation,('kind','portfolio_batch_id','calendar','session_date','now','entry_outputs','exit_outputs','contracts','marks'))
        entries,exits=_outputs(operation['entry_outputs'],operation['exit_outputs'])
        new=portfolio.prepare_batch(state,_text(operation['portfolio_batch_id']),_calendar(operation['calendar']),
            _date(operation['session_date']),_time(operation['now']),entries,exits,_contracts(operation['contracts']),_marks(operation['marks']))
        return {'state':new,'result':{'operation':'prepare','portfolio_batch_id':operation['portfolio_batch_id'],'execution_authorized':False,'investment_qualified':False}}
    if kind=='execute':
        _object(operation,('kind','portfolio_batch_id','now','marks'))
        new,report=portfolio.apply_batch(state,_text(operation['portfolio_batch_id']),_time(operation['now']),_marks(operation['marks']))
        return {'state':new,'result':report}
    raise ValueError('unknown base-only options operation')


def record_options_operation(database,*,run_id,start_utc_day,operation_id,operation,registry,scenario='base'):
    """Record an actual portfolio transition; never accepts arbitrary initial holdings."""
    if scenario!='base':raise ValueError('only base same-observation options scope implemented')
    initial=portfolio.initial_state(_date(start_utc_day))
    return phase3_identity.record_operation(database,run_id=run_id,registry=registry,initial_state=initial,
        operation_id=operation_id,operation=operation,transition=_transition,guard=_GUARD)


_GUARD=phase3_identity.ImplementationGuard([
    adapter,portfolio,phase3_portfolio,phase3_daily,reservations,phase3_store,
    accounting_ledger,decision_ledger,protocol_registry,phase3_identity,sys.modules[__name__]])
