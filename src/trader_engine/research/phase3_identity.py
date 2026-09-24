"""Common source-bound transaction gate for the frozen offline research books."""
from hashlib import sha256
from pathlib import Path
import sys
from types import CodeType, FunctionType

from trader_engine.operations import accounting_ledger, decision_ledger
from . import phase3_store, protocol_registry

SUPPORTED_REGISTRY = 'b33d9064bb674f3948ac4b004a3ec88ad4553e08236ad42e5278741a6f4bedf0'


def _sources(modules):
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
            # Dataclass-generated methods have no source-defined counterpart.
            if code.co_filename != str(path): continue
            if expected.get(code.co_qualname) != code:
                raise ValueError('Loaded implementation differs from source; restart reviewed run')
        hashes[str(path.relative_to(Path(__file__).parents[1]))] = sha256(raw).hexdigest()
    return hashes


class ImplementationGuard:
    """Capture at wrapper import; requires a clean interpreter and pinned runtime.

    Wrappers supply their full implementation dependency list and their own module.
    Core ledger/validator/gate dependencies are included automatically. Hashes are
    provenance identities, not signatures or protection against monkey patching.
    """
    def __init__(self, modules):
        combined = [*modules, accounting_ledger, decision_ledger, phase3_store,
                    protocol_registry, sys.modules[__name__]]
        self.modules = tuple({module.__name__: module for module in combined}.values())
        self.imported_sources = _sources(self.modules)

    def check(self):
        current = _sources(self.modules)
        if current != self.imported_sources:
            raise ValueError('Implementation changed since import; restart reviewed run')
        return current


def record_operation(database, *, run_id, registry, initial_state, operation_id,
                     operation, transition, guard):
    """Verify frozen source/protocol identity and commit one pure JSON transition."""
    protocol = protocol_registry.registry_hash(registry)
    if protocol != SUPPORTED_REGISTRY:
        raise ValueError('Portfolio does not implement changed registry')
    if not isinstance(guard, ImplementationGuard):
        raise ValueError('Import-time implementation guard required')
    hashes = guard.check()
    implementation = decision_ledger.digest(hashes)
    def checked_transition(state, inputs):
        try:
            guard.check()
            output = transition(state, inputs)
            guard.check()
        except ValueError as exc:
            if 'changed since import' in str(exc):
                raise ValueError('Implementation changed during transition') from exc
            raise
        return output
    result = phase3_store.apply_batch(database, run_id=run_id, registry_sha256=protocol,
        implementation_sha256=implementation, initial_state=initial_state,
        batch_id=operation_id, inputs=operation, transition=checked_transition)
    return dict(result, source_hashes=hashes, mode='offline_modeled_state')
