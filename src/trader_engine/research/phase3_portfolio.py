"""Offline modeled portfolio transitions; never broker state or execution authority."""
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from typing import Mapping

from trader_engine.execution.portfolio import Reservations
from .phase3_daily import (CRYPTO, EQUITIES, UNIVERSES, EntryPlan, FeeSchedule,
                          PortfolioSnapshot, PriceObservation, SignalDecision,
                          estimate_entry, estimate_exit, plan_entry, _fees)

D = Decimal


@dataclass(frozen=True)
class Mark:
    price: Decimal
    event_at: datetime
    received_at: datetime
    source: str


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('aware UTC evidence timestamp required')
    return value.astimezone(timezone.utc)


def _number(value, *, positive=False):
    result = D(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('finite nonnegative number required')
    return result


def _encode(value):
    if is_dataclass(value): return _encode(asdict(value))
    if isinstance(value, (datetime, date)): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_encode(v) for v in value]
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(_encode(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def initial_state(strategy_id, utc_day, prior_utc_close_equity=None):
    if strategy_id not in UNIVERSES or type(utc_day) is not date:
        raise ValueError('supported strategy and explicit UTC day required')
    if prior_utc_close_equity is not None:
        raise ValueError('supply a verified baseline with set_day_baseline, never infer it at initialization')
    return {'schema_version': 1, 'strategy_id': strategy_id, 'cash': '100000',
            'positions': {}, 'pending_exits': [], 'utc_day': utc_day.isoformat(),
            'prior_utc_close_equity': None, 'baseline_provenance': None,
            'entry_halted': False, 'batches': {}, 'modeled_fills': [],
            'attempted_entries': {}, 'closed_sessions': {}, 'last_applied_at': None,
            'execution_authorized': False, 'live_approved': False}


def validate_state(state):
    if state.get('schema_version') != 1 or state.get('strategy_id') not in UNIVERSES:
        raise ValueError('unsupported portfolio state')
    if any(state.get(k) is not False for k in ('execution_authorized', 'live_approved')):
        raise ValueError('research state cannot confer authority')
    if type(state.get('entry_halted')) is not bool:
        raise ValueError('explicit latched halt required')
    _number(state['cash']); date.fromisoformat(state['utc_day'])
    if state['prior_utc_close_equity'] is not None:
        _number(state['prior_utc_close_equity'], positive=True)
        if not state.get('baseline_provenance'): raise ValueError('baseline provenance missing')
    universe = UNIVERSES[state['strategy_id']]
    if not isinstance(state['positions'], dict) or any(s not in universe for s in state['positions']):
        raise ValueError('unknown owned instrument')
    for quantity in state['positions'].values():
        q = _number(quantity, positive=True)
        if state['strategy_id'] == EQUITIES and q != q.to_integral_value():
            raise ValueError('fractional equity ownership unsupported')
    pending = state['pending_exits']
    if not isinstance(pending, list) or len(set(pending)) != len(pending) or not set(pending) <= set(state['positions']):
        raise ValueError('pending exits require exact owned symbols')
    if not isinstance(state['batches'], dict) or not isinstance(state['modeled_fills'], list):
        raise ValueError('journal schema invalid')
    for name in ('attempted_entries', 'closed_sessions'):
        if not isinstance(state[name], dict) or any(s not in universe for s in state[name]):
            raise ValueError('session guard schema invalid')
        for value in state[name].values(): date.fromisoformat(value)
    if state['last_applied_at'] is not None: _utc(datetime.fromisoformat(state['last_applied_at']))
    json.dumps(state, allow_nan=False)


def set_day_baseline(state, utc_day, prior_close_equity, *, boundary_at, provenance):
    """Supply independently reconciled prior-close equity; reset latch only on new day.

    The provenance string is a caller assertion/reference, not authenticated evidence.
    Same-day replacement with a different value/reference is refused.
    """
    validate_state(state)
    if type(utc_day) is not date or utc_day < date.fromisoformat(state['utc_day']):
        raise ValueError('cannot rewind UTC day')
    if state['last_applied_at'] is not None and utc_day < _utc(datetime.fromisoformat(state['last_applied_at'])).date():
        raise ValueError('baseline day predates applied execution')
    if _utc(boundary_at) != datetime.combine(utc_day, time(), timezone.utc):
        raise ValueError('exact UTC boundary required')
    amount = _number(prior_close_equity, positive=True)
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValueError('verified baseline reference required')
    result = deepcopy(state)
    if utc_day.isoformat() == state['utc_day']:
        if state['prior_utc_close_equity'] is not None and (D(state['prior_utc_close_equity']) != amount or state['baseline_provenance'] != provenance):
            raise ValueError('same-day baseline cannot be replaced')
    else:
        result['utc_day'] = utc_day.isoformat(); result['entry_halted'] = False
    result['prior_utc_close_equity'] = str(amount); result['baseline_provenance'] = provenance
    return result


def _snapshot(state, marks, now):
    if _utc(now).date().isoformat() != state['utc_day']:
        raise ValueError('explicit new UTC-day baseline transition required')
    universe = UNIVERSES[state['strategy_id']]
    if not set(state['positions']) <= set(marks) or not set(marks) <= universe:
        raise ValueError('causal marks required for every owned instrument; unknown marks refused')
    source = 'sip' if state['strategy_id'] == EQUITIES else 'alpaca_crypto'
    for symbol, mark in marks.items():
        if not isinstance(mark, Mark) or mark.source != source:
            raise ValueError('mark provenance/schema mismatch')
        _number(mark.price, positive=True)
        if _utc(mark.event_at) != _utc(now) or not _utc(mark.event_at) <= _utc(mark.received_at) <= _utc(now):
            raise ValueError('portfolio mark not available at exact research valuation boundary')
    gross = sum((D(quantity)*marks[symbol].price for symbol, quantity in state['positions'].items()), D(0))
    cash = D(state['cash']); equity = cash+gross
    baseline = None if state['prior_utc_close_equity'] is None else D(state['prior_utc_close_equity'])
    limit = D('.01') if state['strategy_id'] == EQUITIES else D('.03')
    if baseline is not None and equity <= baseline*(1-limit): state['entry_halted'] = True
    return PortfolioSnapshot(equity, cash, gross, baseline, state['entry_halted'])


def _decision(value):
    return SignalDecision(value['strategy_id'], value['symbol'], value['action'], value['reason'],
                          date.fromisoformat(value['signal_day']), datetime.fromisoformat(value['execution_at']),
                          None if value['raw_signal_price'] is None else D(value['raw_signal_price']),
                          D(value['held_quantity']), value['pending_exit'], datetime.fromisoformat(value['decided_at']))


def _plan(value):
    f = value['fees']
    return EntryPlan(_decision(value['decision']), D(value['quantity']), D(value['budget']), D(value['step']),
                     FeeSchedule(D(f['rate']), D(f['fixed_cash']), f['currency']), value['reason'])


def _owned_matches(state, decisions):
    for symbol, decision in decisions.items():
        quantity = D(state['positions'].get(symbol, '0'))
        if decision.held_quantity != quantity:
            raise ValueError('decision ownership differs from current research ledger')
        if symbol in state['pending_exits'] and (decision.action != 'exit' or not decision.pending_exit):
            raise ValueError('pending exit cannot be dropped')
        if decision.action == 'exit' and (quantity <= 0 or not decision.pending_exit):
            raise ValueError('exit requires owned quantity and pending intent')
        if decision.action == 'enter' and (quantity or decision.pending_exit):
            raise ValueError('entry must have flat ownership')


def prepare_batch(state, batch_id, decisions, marks, now, fees, quantity_steps):
    """Freeze original adapter plans with conservative shared Reservations budgets."""
    with localcontext() as context:
        context.prec = 64
        return _prepare_batch(state, batch_id, decisions, marks, now, fees, quantity_steps)


def _prepare_batch(state, batch_id, decisions, marks, now, fees, quantity_steps):
    validate_state(state)
    if not isinstance(batch_id, str) or not batch_id.strip(): raise ValueError('batch ID required')
    universe = UNIVERSES[state['strategy_id']]
    if set(decisions) != universe or set(fees) != universe or set(quantity_steps) != universe:
        raise ValueError('every frozen-universe instrument must be explicit')
    for symbol, decision in decisions.items():
        if (not isinstance(decision, SignalDecision) or decision.strategy_id != state['strategy_id']
                or decision.symbol != symbol or decision.action not in {'enter', 'exit', 'hold', 'flat', 'skip'}
                or _utc(decision.decided_at) > _utc(now)):
            raise ValueError('invalid or future decision')
    digest = _hash(dict(decisions=decisions, marks=marks, now=now, fees=fees, steps=quantity_steps))
    if batch_id in state['batches']:
        if state['batches'][batch_id]['prepare_hash'] != digest: raise ValueError('batch ID preparation conflict')
        return deepcopy(state)
    if any(b['apply_hash'] is None for b in state['batches'].values()):
        raise ValueError('one unresolved prepared batch at a time')
    if state['last_applied_at'] is not None and _utc(now) < _utc(datetime.fromisoformat(state['last_applied_at'])):
        raise ValueError('preparation clock regression')
    _owned_matches(state, decisions)
    result = deepcopy(state)
    try:
        snapshot = _snapshot(result, marks, now)
    except ValueError:
        snapshot = None
    reservations = Reservations(snapshot.cash if snapshot is not None else D(0))
    plans = {}
    for symbol in sorted(universe):
        decision = decisions[symbol]
        normalized_fees = _fees(state['strategy_id'], fees[symbol])
        step = quantity_steps[symbol]
        if not isinstance(step, Decimal) or not step.is_finite() or step <= 0 or (state['strategy_id'] == EQUITIES and step != 1):
            raise ValueError('valid instrument quantity step required')
        if decision.action == 'enter' and _utc(now) > _utc(decision.execution_at):
            plans[symbol] = _encode(EntryPlan(decision, D(0), D(0), step, normalized_fees, 'entry_plan_prepared_after_execution'))
        elif decision.action == 'enter' and (snapshot is None or symbol not in marks):
            plans[symbol] = _encode(EntryPlan(decision, D(0), D(0), step, normalized_fees, 'portfolio_valuation_unavailable'))
        elif decision.action == 'enter':
            reserved_gross, available_cash = reservations.snapshot()
            limited = PortfolioSnapshot(snapshot.equity, available_cash, snapshot.gross+reserved_gross,
                                        snapshot.prior_utc_close_equity, snapshot.entry_halted)
            plan = plan_entry(decision, limited, fees[symbol], quantity_step=quantity_steps[symbol])
            plans[symbol] = _encode(plan)
            if plan.quantity > 0: reservations.reserve(symbol, plan.budget, plan.budget)
        if decision.action == 'exit' and symbol not in result['pending_exits']:
            result['pending_exits'].append(symbol)
    result['pending_exits'].sort()
    body = dict(prepare_hash=digest, prepared_at=_utc(now).isoformat(), decisions=_encode(decisions), plans=plans,
                fees=_encode(fees), steps=_encode(quantity_steps), apply_hash=None, report=None)
    body['plan_integrity'] = _hash({key: body[key] for key in ('prepared_at', 'decisions', 'plans', 'fees', 'steps')})
    result['batches'][batch_id] = body
    validate_state(result)
    return result


def apply_batch(state, batch_id, observations, marks, now, *, stress=False):
    """Atomic pure transition with explicit modeled fills; missing exits retain dust.

    Original plan quantities/budgets are never increased using later observations.
    Same input replay returns current state unchanged and marks report replayed.
    """
    with localcontext() as context:
        context.prec = 64
        return _apply_batch(state, batch_id, observations, marks, now, stress=stress)


def _apply_batch(state, batch_id, observations, marks, now, *, stress):
    validate_state(state)
    if batch_id not in state['batches']: raise ValueError('batch not prepared')
    if type(stress) is not bool: raise ValueError('explicit scenario required')
    universe = UNIVERSES[state['strategy_id']]
    if set(observations) != universe: raise ValueError('explicit observation or None for every instrument required')
    if any(o is not None and not isinstance(o, PriceObservation) for o in observations.values()):
        raise ValueError('observation schema invalid')
    batch = state['batches'][batch_id]
    if batch['plan_integrity'] != _hash({key: batch[key] for key in ('prepared_at', 'decisions', 'plans', 'fees', 'steps')}):
        raise ValueError('frozen plan content changed')
    if _utc(now) < _utc(datetime.fromisoformat(batch['prepared_at'])):
        raise ValueError('execution cannot precede batch preparation')
    digest = _hash(dict(observations=observations, marks=marks, now=now, stress=stress))
    if batch['apply_hash'] is not None:
        if batch['apply_hash'] != digest: raise ValueError('batch ID execution-content conflict')
        report = deepcopy(batch['report']); report['replayed'] = True
        return deepcopy(state), report
    decisions = {s: _decision(value) for s, value in batch['decisions'].items()}
    if state['last_applied_at'] is not None and _utc(now) < _utc(datetime.fromisoformat(state['last_applied_at'])):
        raise ValueError('execution clock regression')
    if any(_utc(d.execution_at).date() != _utc(now).date() for d in decisions.values()):
        raise ValueError('batch must execute on its scheduled UTC session day')
    _owned_matches(state, decisions)
    result = deepcopy(state); fills = []; blocked = {}
    try:
        _snapshot(result, marks, now)
    except ValueError:
        pass  # Valuation blocks entries, never valid risk-reducing exits.
    exiting = {s for s, d in decisions.items() if d.action == 'exit'}
    for symbol in sorted(exiting):
        d = decisions[symbol]
        if state['strategy_id'] == EQUITIES and _utc(datetime.fromisoformat(batch['prepared_at'])) > _utc(d.execution_at):
            blocked[symbol] = 'missed_preparation_open'
            continue
        raw = batch['fees'][symbol]
        fee = FeeSchedule(D(raw['rate']), D(raw['fixed_cash']), raw['currency'])
        estimate = estimate_exit(d, observations[symbol], fee, now,
                                 quantity_step=D(batch['steps'][symbol]), stress=stress)
        if estimate.quantity <= 0:
            blocked[symbol] = estimate.reason; continue
        remaining = D(result['positions'][symbol])-estimate.owned_units_debit
        if remaining < 0: raise ValueError('modeled sell exceeds ownership')
        result['cash'] = str(D(result['cash'])+estimate.cash_credit)
        if remaining:
            result['positions'][symbol] = str(remaining)
        else:
            del result['positions'][symbol]
            result['pending_exits'].remove(symbol)
            result['closed_sessions'][symbol] = _utc(now).date().isoformat()
        modeled = _encode(asdict(estimate))
        modeled['adapter_pending_exit'] = modeled.pop('pending_exit')
        fills.append(dict(symbol=symbol, side='sell', **modeled, pending_exit=bool(remaining),
                          modeled_fill=True, batch_id=batch_id, ownership_remaining=str(remaining)))
        try:
            _snapshot(result, marks, now)
        except ValueError:
            pass
    for symbol in sorted(universe):
        if symbol not in batch['plans'] or symbol in exiting or symbol in result['pending_exits']:
            continue
        if symbol in result['positions']: raise ValueError('no pyramiding/reentry')
        session = _utc(now).date().isoformat()
        if result['closed_sessions'].get(symbol) == session or result['attempted_entries'].get(symbol) == session:
            blocked[symbol] = 'session_entry_already_attempted_or_closed'
            continue
        result['attempted_entries'][symbol] = session
        plan = _plan(batch['plans'][symbol])
        if symbol not in marks:
            blocked[symbol] = 'portfolio_valuation_unavailable'
            continue
        try:
            snapshot = _snapshot(result, marks, now)
        except ValueError:
            blocked[symbol] = 'portfolio_valuation_unavailable'
            continue
        estimate = estimate_entry(plan, observations[symbol], snapshot, now, stress=stress)
        if estimate.quantity <= 0:
            blocked[symbol] = estimate.reason; continue
        if estimate.quantity > plan.quantity or estimate.cash_debit > plan.budget:
            raise ValueError('original plan maximum exceeded')
        result['cash'] = str(D(result['cash'])-estimate.cash_debit)
        result['positions'][symbol] = str(estimate.acquired_quantity)
        fills.append(dict(symbol=symbol, side='buy', **_encode(asdict(estimate)),
                          modeled_fill=True, batch_id=batch_id))
    try:
        snapshot = _snapshot(result, marks, now)
    except ValueError:
        snapshot = None
    report = dict(batch_id=batch_id, modeled_fills=fills, blocked=blocked,
                  cash=result['cash'], equity=str(snapshot.equity) if snapshot is not None else None,
                  gross=str(snapshot.gross) if snapshot is not None else None,
                  valuation_available=snapshot is not None,
                  valuation_basis='exact_research_boundary_marks_not_independent_provenance_verification',
                  mark_age_seconds={s: (_utc(now)-_utc(m.event_at)).total_seconds() for s,m in marks.items() if isinstance(m,Mark)},
                  entry_halted=result['entry_halted'], pending_exits=list(result['pending_exits']),
                  replayed=False, execution_authorized=False, live_approved=False,
                  evidence_scope='offline_modeled_portfolio_not_broker_fills')
    result['modeled_fills'].extend(fills)
    result['last_applied_at'] = _utc(now).isoformat()
    result['batches'][batch_id]['apply_hash'] = digest
    result['batches'][batch_id]['report'] = report
    validate_state(result)
    return result, report
