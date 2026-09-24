"""JSON-to-portfolio integration for restart-safe offline research transitions."""
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sys
from types import CodeType, FunctionType

from trader_engine.execution import portfolio as reservations
from trader_engine.operations import accounting_ledger, decision_ledger
from . import phase3_daily as daily, phase3_portfolio as portfolio, phase3_store as store
from . import protocol_registry

SUPPORTED_REGISTRY = 'b33d9064bb674f3948ac4b004a3ec88ad4553e08236ad42e5278741a6f4bedf0'


def _verified_sources():
    """Bind source bytes only when source-defined loaded functions still match.

    This guards accidental hot edits, not a hostile interpreter or runtime monkey
    patching. Python/library dependencies still need a separate release manifest.
    """
    modules = [daily, portfolio, store, reservations, accounting_ledger, decision_ledger,
               protocol_registry, sys.modules[__name__]]
    hashes = {}
    for module in modules:
        path = Path(module.__file__)
        raw = path.read_bytes()
        expected = {}
        def collect(code):
            expected[code.co_qualname] = code
            for item in code.co_consts:
                if isinstance(item, CodeType): collect(item)
        collect(compile(raw, str(path), 'exec', dont_inherit=True))
        functions = []
        for obj in vars(module).values():
            if getattr(obj, '__module__', None) != module.__name__: continue
            if isinstance(obj, FunctionType): functions.append(obj)
            elif isinstance(obj, type):
                for method in vars(obj).values():
                    if isinstance(method, (classmethod, staticmethod)): method = method.__func__
                    if isinstance(method, FunctionType): functions.append(method)
        for function in functions:
            code = function.__code__
            # Dataclass-generated methods have no counterpart in the source AST.
            if code.co_filename != str(path): continue
            if expected.get(code.co_qualname) != code:
                raise ValueError('Loaded implementation differs from source; restart reviewed run')
        hashes[str(path.relative_to(Path(__file__).parents[1]))] = sha256(raw).hexdigest()
    return hashes


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
    protocol = protocol_registry.registry_hash(registry)
    if protocol != SUPPORTED_REGISTRY:
        raise ValueError('Portfolio does not implement changed registry')
    hashes = _verified_sources()
    if hashes != _IMPORTED_SOURCES:
        raise ValueError('Implementation changed since import; restart reviewed run')
    code = decision_ledger.digest(hashes)
    initial = portfolio.initial_state(strategy_id, date.fromisoformat(start_utc_day))
    def checked_transition(state, inputs):
        if _verified_sources() != hashes:
            raise ValueError('Implementation changed during transition')
        output = _transition(state, inputs)
        if _verified_sources() != hashes:
            raise ValueError('Implementation changed during transition')
        return output
    result = store.apply_batch(database, run_id=run_id, registry_sha256=protocol,
                               implementation_sha256=code, initial_state=initial,
                               batch_id=operation_id, inputs=operation, transition=checked_transition)
    return dict(result, source_hashes=hashes, mode='offline_modeled_state')


# A clean interpreter is required. This additionally detects constant/default-only
# edits after import, whose top-level assignments do not live in function code.
_IMPORTED_SOURCES = _verified_sources()
