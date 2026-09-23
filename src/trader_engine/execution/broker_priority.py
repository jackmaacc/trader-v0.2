"""Priority-aware order reads without breaking legacy/fake broker protocols."""
import inspect


def lookup_order(broker, client_id, *, priority='emergency'):
    """One read attempt; caller retains retry and reconciliation ownership.

    Inspect before calling: catching TypeError and retrying could execute an
    adapter twice if its body raised TypeError. Legacy adapters retain their
    admission behavior and cannot gain priority from this compatibility helper.
    """
    if priority not in ('emergency', 'monitor', 'entry', 'discovery'):
        raise ValueError('Unknown request priority')
    method = broker.get_order
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return method(client_id)
    parameter = params.get('priority')
    supports = (parameter is not None and parameter.kind != inspect.Parameter.POSITIONAL_ONLY) or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    return method(client_id, priority=priority) if supports else method(client_id)
