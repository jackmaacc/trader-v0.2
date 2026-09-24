"""JSON-to-portfolio integration for restart-safe offline research transitions."""
from datetime import date, datetime
from decimal import Decimal
import sys

from trader_engine.execution import portfolio as reservations
from trader_engine.operations import accounting_ledger, decision_ledger
from . import phase3_daily as daily, phase3_portfolio as portfolio, phase3_store as store
from . import phase3_actions as actions
from . import protocol_registry, phase3_identity as identity

SUPPORTED_REGISTRY = 'b33d9064bb674f3948ac4b004a3ec88ad4553e08236ad42e5278741a6f4bedf0'


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone-aware evidence required')
    return result


def _decimal(value):
    if not isinstance(value, str):
        raise ValueError('Exact decimal string required')
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('Finite decimal required')
    return result


def _marks(values):
    return {symbol: portfolio.Mark(_decimal(v['price']), _time(v['event_at']),
                                   _time(v['received_at']), v['source'])
            for symbol, v in values.items()}


def _transition(state, operation):
    kind = operation['kind']
    if kind == 'corporate_action':
        new, receipt = actions.apply_corporate_action(state, operation['event'], operation['now'])
        return {'state': new, 'result': receipt}
    if kind == 'baseline':
        new = portfolio.set_day_baseline(state, date.fromisoformat(operation['utc_day']),
                                        _decimal(operation['prior_close_equity']),
                                        boundary_at=_time(operation['boundary_at']),
                                        provenance=operation['provenance'])
        return {'state': new, 'result': {'operation': kind, 'execution_authorized': False}}
    if kind == 'prepare':
        decisions = {}
        for symbol, v in operation['decisions'].items():
            decisions[symbol] = daily.SignalDecision(
                v['strategy_id'], v['symbol'], v['action'], v['reason'],
                date.fromisoformat(v['signal_day']), _time(v['execution_at']),
                None if v['raw_signal_price'] is None else _decimal(v['raw_signal_price']),
                _decimal(v['held_quantity']), v['pending_exit'], _time(v['decided_at']))
        fees = {s: daily.FeeSchedule(_decimal(v['rate']), _decimal(v['fixed_cash']), v['currency'])
                for s, v in operation['fees'].items()}
        new = portfolio.prepare_batch(state, operation['portfolio_batch_id'], decisions,
                                      _marks(operation['marks']), _time(operation['now']), fees,
                                      {s: _decimal(v) for s, v in operation['quantity_steps'].items()})
        return {'state': new, 'result': {'operation': kind, 'portfolio_batch_id': operation['portfolio_batch_id'],
                                        'execution_authorized': False}}
    if kind == 'execute':
        observations = {s: None if v is None else daily.PriceObservation(
            _time(v['event_at']), _time(v['received_at']),
            *[None if v.get(k) is None else _decimal(v[k]) for k in ('raw_open', 'bid', 'ask')])
            for s, v in operation['observations'].items()}
        new, report = portfolio.apply_batch(state, operation['portfolio_batch_id'], observations,
                                            _marks(operation['marks']), _time(operation['now']),
                                            stress=operation['stress'])
        return {'state': new, 'result': report}
    raise ValueError('Unknown offline portfolio operation')


def record_portfolio_operation(database, *, run_id, strategy_id, start_utc_day,
                               operation_id, operation, registry):
    """Persist one actual adapter transition, with inputs and implementation identity.

    Caller evidence is not authenticated. Even real historical inputs remain
    offline modeled state; this interface cannot create paper/prospective credit.
    """
    initial = portfolio.initial_state(strategy_id, date.fromisoformat(start_utc_day))
    return identity.record_operation(database, run_id=run_id, registry=registry,
        initial_state=initial, operation_id=operation_id, operation=operation,
        transition=_transition, guard=_GUARD)


_GUARD = identity.ImplementationGuard([
    daily, portfolio, store, reservations, accounting_ledger, decision_ledger, actions,
    protocol_registry, sys.modules[__name__]])
