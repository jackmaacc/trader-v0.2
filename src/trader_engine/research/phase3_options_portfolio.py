"""Base, same-observation options booking of asserted pure-adapter outputs only."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, localcontext

from .phase3_options import (ArchivedCalendar, Contract, Position, Quote, UNIVERSE,
                             _valid_contract, _valid_quote)
from .phase3_portfolio import _encode, _hash

D = Decimal
STRATEGY = 'O_ETF_TREND_CALL_V1'


def _utc(value):
    if isinstance(value, str): value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None: raise ValueError('aware timestamp required')
    return value.astimezone(timezone.utc)


def _n(value, *, positive=False):
    result = D(str(value))
    if not result.is_finite() or result < 0 or (positive and not result): raise ValueError('nonnegative finite amount required')
    return result


def _contract(value):
    return Contract(value['symbol'], value['underlying'], date.fromisoformat(value['expiry']), D(value['strike']),
                    _utc(value['listed_at']), _utc(value['metadata_received_at']), value['option_type'],
                    value['multiplier'], value['standard_unadjusted'], value['physically_delivered'], value['active'])


def initial_state(utc_day):
    if type(utc_day) is not date: raise ValueError('explicit UTC day required')
    return dict(schema_version=1, strategy_id=STRATEGY, cash='100000', positions={},
                utc_day=utc_day.isoformat(), prior_close_equity=None, baseline_reference=None,
                entry_halted=False, batches={}, modeled_fills=[], attempted_entries={},
                closed_sessions={}, last_applied_at=None, execution_authorized=False,
                live_approved=False, investment_qualified=False)


def validate_state(state):
    if state.get('schema_version') != 1 or state.get('strategy_id') != STRATEGY: raise ValueError('wrong options state')
    if any(state.get(k) is not False for k in ('execution_authorized','live_approved','investment_qualified')): raise ValueError('no options execution authority')
    _n(state['cash']); date.fromisoformat(state['utc_day'])
    if type(state['entry_halted']) is not bool: raise ValueError('explicit halt latch required')
    if state['prior_close_equity'] is not None:
        _n(state['prior_close_equity'],positive=True)
        if not state['baseline_reference']: raise ValueError('baseline reference missing')
    if not isinstance(state['positions'],dict) or not set(state['positions']) <= set(UNIVERSE): raise ValueError('unknown underlying')
    for underlying, row in state['positions'].items():
        c=_contract(row['contract'])
        if c.underlying!=underlying or c.option_type!='call' or type(c.multiplier) is not int or c.multiplier!=100 or c.standard_unadjusted is not True or c.physically_delivered is not True:
            raise ValueError('owned standard 100-share call required')
        if type(row['quantity']) is not int or row['quantity']!=1: raise ValueError('one owned contract required')
        _n(row['entry_premium'],positive=True);_n(row['entry_fee'])
        date.fromisoformat(row['entry_session'])
        if row['pending_exit_reason'] not in {None,'holding_session_limit','weekly_trend_failed','expiry_proximity'}: raise ValueError('invalid pending reason')
    for key in ('batches','attempted_entries','closed_sessions'):
        if not isinstance(state[key],dict): raise ValueError('invalid journal mapping')
    if not isinstance(state['modeled_fills'],list): raise ValueError('invalid fill journal')
    if state['last_applied_at'] is not None:_utc(state['last_applied_at'])
    _hash(state)


def set_day_baseline(state, utc_day, prior_close_equity, *, boundary_at, provenance):
    validate_state(state)
    if type(utc_day) is not date or utc_day<date.fromisoformat(state['utc_day']): raise ValueError('day regression')
    if state['last_applied_at'] and utc_day<_utc(state['last_applied_at']).date(): raise ValueError('day predates execution')
    if _utc(boundary_at)!=datetime.combine(utc_day,time(),timezone.utc): raise ValueError('exact UTC boundary required')
    amount=_n(prior_close_equity,positive=True)
    if not isinstance(provenance,str) or not provenance.strip(): raise ValueError('baseline provenance required')
    result=deepcopy(state)
    if utc_day.isoformat()==state['utc_day'] and state['prior_close_equity'] is not None:
        if D(state['prior_close_equity'])!=amount or state['baseline_reference']!=provenance: raise ValueError('same-day baseline replacement refused')
    elif utc_day.isoformat()!=state['utc_day']:
        result['entry_halted']=False
    result.update(utc_day=utc_day.isoformat(),prior_close_equity=str(amount),baseline_reference=provenance)
    return result


def adapter_positions(state):
    validate_state(state)
    return tuple(Position(_contract(row['contract']),date.fromisoformat(row['entry_session']),D(row['entry_premium']),1,row['pending_exit_reason']) for row in state['positions'].values())


def _valuation(state, marks, now):
    if _utc(now).date().isoformat()!=state['utc_day']: return None
    total=D(state['cash'])
    for row in state['positions'].values():
        symbol=row['contract']['symbol'];q=marks.get(symbol)
        if not _valid_quote(q,symbol,'opra',_utc(now)): return None
        total+=_n(q.bid,positive=True)*100
    baseline=state['prior_close_equity']
    if baseline is not None and total<=D(baseline)*D('.99'):state['entry_halted']=True
    return total


def _output(output, actions):
    if not isinstance(output,dict) or output.get('strategy_id')!=STRATEGY or output.get('action') not in actions:raise ValueError('adapter output schema mismatch')
    if any(output.get(k) is not False for k in ('execution_authorized','fill_created','investment_qualified')):raise ValueError('only pure adapter outputs accepted')


def _pricing(output, side):
    if type(output.get('quantity')) is not int or output['quantity']!=1:raise ValueError('one-contract candidate required')
    p=output.get('modeled_prices',{})
    price=_n(p.get('base_buy' if side=='buy' else 'base_sell'),positive=True)
    fee=_n(output.get('base_fees'))
    if side=='buy':
        premium=price*100; debit=premium+fee
        if _n(output.get('base_premium'))!=premium or _n(output.get('planned_cash_debit'))!=debit:raise ValueError('entry arithmetic mismatch')
        return premium,fee,debit
    credit=price*100-fee
    if credit<0 or D(str(output.get('modeled_net_proceeds')))!=credit:raise ValueError('exit arithmetic mismatch')
    return price*100,fee,credit


def prepare_batch(state,batch_id,calendar,session_date,now,entry_outputs,exit_outputs,contracts,marks):
    with localcontext() as context:
        context.prec=64
        return _prepare(state,batch_id,calendar,session_date,now,entry_outputs,exit_outputs,contracts,marks)


def _prepare(state,batch_id,calendar,session_date,now,entry_outputs,exit_outputs,contracts,marks):
    validate_state(state);now=_utc(now)
    if not isinstance(calendar,ArchivedCalendar) or not isinstance(batch_id,str) or not batch_id:raise ValueError('calendar and batch ID required')
    scheduled=calendar.decision_at(session_date)
    close=_utc(calendar.sessions[calendar.index(session_date)].close_at)
    if now.date()!=session_date or not scheduled<=now<=close:raise ValueError('batch outside research session')
    if set(exit_outputs)!=set(state['positions']):raise ValueError('every owned position requires explicit exit/hold output')
    if entry_outputs and ([r.get('underlying') for r in entry_outputs]!=list(UNIVERSE)):raise ValueError('full ordered entry adapter output required')
    contents=dict(calendar_sha256=calendar.archived_sha256,session=session_date.isoformat(),prepared_at=now.isoformat(),entries=_encode(entry_outputs),exits=_encode(exit_outputs),contracts=_encode(contracts),marks=_encode(marks))
    digest=_hash(contents)
    if batch_id in state['batches']:
        if state['batches'][batch_id]['prepare_hash']!=digest:raise ValueError('preparation replay conflict')
        return deepcopy(state)
    if state['last_applied_at'] and now<_utc(state['last_applied_at']):raise ValueError('preparation clock regression')
    if any(b['apply_hash'] is None for b in state['batches'].values()):raise ValueError('one unresolved options batch required')
    result=deepcopy(state);equity=_valuation(result,marks,now)
    ceilings={};cash=D(state['cash']);outstanding=sum((D(p['entry_premium']) for p in state['positions'].values()),D(0))
    for underlying,output in exit_outputs.items():
        _output(output,{'hold','pending_exit','exit_candidate'})
        row=result['positions'][underlying]
        if row['pending_exit_reason'] is not None and output['action']=='hold':raise ValueError('pending exit cannot be dropped')
        if output['action'] in {'pending_exit','exit_candidate'}:
            reason=output.get('pending_exit_reason')
            if reason not in {'holding_session_limit','weekly_trend_failed','expiry_proximity'}:raise ValueError('explicit pending reason required')
            row['pending_exit_reason']=reason
        if output['action']=='exit_candidate':
            if output.get('contract')!=row['contract']['symbol'] or _utc(output['observed_at'])!=now:raise ValueError('exit must use its own current observation and owned contract')
            if date.fromisoformat(row['contract']['expiry'])<session_date:raise ValueError('expired lifecycle cannot be liquidated synthetically')
            _pricing(output,'sell')
    for output in entry_outputs:
        _output(output,{'skip','entry_candidate'})
        if _utc(output['decision_at'])!=scheduled:raise ValueError('output is not frozen 09:35 session decision')
        if output['action']=='skip':continue
        underlying=output['underlying'];symbol=output.get('contract');c=contracts.get(symbol)
        if _utc(output['decision_at'])!=scheduled:raise ValueError('entry is not frozen 09:35 decision')
        if not isinstance(c,Contract) or c.symbol!=symbol or not _valid_contract(c,underlying,scheduled,session_date):raise ValueError('point-in-time selected contract metadata missing')
        premium,fee,debit=_pricing(output,'buy')
        ceiling=D(0)
        if now==scheduled and equity is not None and state['prior_close_equity'] is not None and not result['entry_halted']:
            ceiling=max(D(0),min(equity*D('.0025'),cash,equity*D('.0075')-outstanding+fee))
        ceilings[underlying]=str(ceiling)
        if debit<=ceiling:cash-=debit;outstanding+=premium
    contents['budget_ceilings']=ceilings
    result['batches'][batch_id]=dict(prepare_hash=digest,body=contents,integrity=_hash(contents),apply_hash=None,report=None)
    validate_state(result)
    return result


def apply_batch(state,batch_id,now,marks):
    with localcontext() as context:
        context.prec=64
        return _apply(state,batch_id,now,marks)


def _apply(state,batch_id,now,marks):
    validate_state(state);now=_utc(now)
    if batch_id not in state['batches']:raise ValueError('unprepared options batch')
    batch=state['batches'][batch_id];body=batch['body']
    if batch['integrity']!=_hash(body):raise ValueError('frozen options evidence changed')
    if now<_utc(body['prepared_at']):raise ValueError('application before preparation')
    digest=_hash(dict(now=now,marks=marks))
    if batch['apply_hash'] is not None:
        if batch['apply_hash']!=digest:raise ValueError('application replay conflict')
        report=deepcopy(batch['report']);report['replayed']=True
        return deepcopy(state),report
    if state['last_applied_at'] and now<_utc(state['last_applied_at']):raise ValueError('execution clock regression')
    if now.date().isoformat()!=body['session']:raise ValueError('candidate session changed')
    if set(body['exits'])!=set(state['positions']):raise ValueError('ownership changed after preparation')
    result=deepcopy(state);fills=[];blocked={};session=body['session'];_valuation(result,marks,now)
    for underlying in sorted(body['exits']):
        output=body['exits'][underlying]
        if output['action']!='exit_candidate':continue
        row=result['positions'][underlying]
        if output['contract']!=row['contract']['symbol']:raise ValueError('owned contract changed')
        if now!=_utc(output['observed_at']):blocked[underlying]='missed_exit_observation';continue
        premium,fee,credit=_pricing(output,'sell')
        result['cash']=str(D(result['cash'])+credit)
        del result['positions'][underlying];result['closed_sessions'][underlying]=session
        fills.append(dict(underlying=underlying,contract=output['contract'],side='sell',quantity=1,premium=str(premium),fee=str(fee),cash_credit=str(credit),modeled_fill=True,execution_authorized=False))
    for output in body['entries']:
        if output['action']!='entry_candidate':
            result['attempted_entries'][output['underlying']]=session
            continue
        underlying=output['underlying']
        if result['attempted_entries'].get(underlying)==session or result['closed_sessions'].get(underlying)==session:
            blocked[underlying]='session_entry_already_attempted_or_closed';continue
        result['attempted_entries'][underlying]=session
        if underlying in result['positions']:blocked[underlying]='already_owned';continue
        if now!=_utc(output['decision_at']):blocked[underlying]='missed_entry_observation';continue
        equity=_valuation(result,marks,now)
        if equity is None or result['prior_close_equity'] is None:blocked[underlying]='valuation_or_baseline_missing';continue
        if result['entry_halted']:blocked[underlying]='latched_utc_entry_halt';continue
        premium,fee,debit=_pricing(output,'buy')
        if debit>D(body['budget_ceilings'][underlying]):blocked[underlying]='original_budget_exceeded';continue
        outstanding=sum((D(p['entry_premium']) for p in result['positions'].values()),D(0))
        if debit>equity*D('.0025') or outstanding+premium>equity*D('.0075') or len(result['positions'])>=3:blocked[underlying]='premium_cap';continue
        if debit>D(result['cash']):blocked[underlying]='insufficient_cash';continue
        symbol=output['contract'];q=marks.get(symbol)
        if not _valid_quote(q,symbol,'opra',now):blocked[underlying]='entry_mark_missing';continue
        result['cash']=str(D(result['cash'])-debit)
        result['positions'][underlying]=dict(contract=body['contracts'][symbol],entry_session=session,entry_premium=str(premium),entry_fee=str(fee),quantity=1,pending_exit_reason=None)
        fills.append(dict(underlying=underlying,contract=symbol,side='buy',quantity=1,premium=str(premium),fee=str(fee),cash_debit=str(debit),modeled_fill=True,execution_authorized=False))
    equity=_valuation(result,marks,now)
    report=dict(batch_id=batch_id,modeled_fills=fills,blocked=blocked,cash=result['cash'],equity=str(equity) if equity is not None else None,
                pending_exits=[u for u,p in result['positions'].items() if p['pending_exit_reason']],entry_halted=result['entry_halted'],replayed=False,
                execution_authorized=False,live_approved=False,investment_qualified=False,scope='base_same_observation_offline_only')
    result['modeled_fills'].extend(fills);result['last_applied_at']=now.isoformat()
    result['batches'][batch_id]['apply_hash']=digest;result['batches'][batch_id]['report']=report
    validate_state(result)
    return result,report
