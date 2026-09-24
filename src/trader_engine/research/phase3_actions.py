"""Causal modeled equity actions; distributions stay receivable until evidenced paid."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from trader_engine.operations.decision_ledger import digest

ZERO = Decimal(0)
SYMBOLS = {'SPY', 'QQQ', 'IWM'}


def _amount(value, *, positive=False):
    if not isinstance(value, str):
        raise ValueError('Exact decimal string required')
    result = Decimal(value)
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('Finite nonnegative amount required')
    if len(result.as_tuple().digits) > 60 or abs(result.as_tuple().exponent) > 30:
        raise ValueError('Amount exceeds supported research precision')
    return result


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Aware evidence timestamp required')
    return result.astimezone(timezone.utc)


def receivable_value(state):
    """Receivables contribute equity, never spendable cash or equity-market gross."""
    with localcontext() as context:
        context.prec = 128
        total = ZERO
        for row in state.get('corporate_actions', {}).get('receivables', {}).values():
            if type(row['paid']) is not bool:
                raise ValueError('Explicit receivable payment status required')
            amount = _amount(row['amount'])
            if not row['paid']: total += amount
        return total


def apply_corporate_action(state, event, now):
    """Return new offline cash/quantity/receivable state with an auditable receipt.

    Events must be applied at their supplied effective time in chronological
    state order. This does not recover missing historical entitlement or verify
    provider evidence. Caller quantity-before assertions must match the ledger.
    """
    with localcontext() as context:
        context.prec = 128
        return _apply(state, event, now)


def _apply(state, event, now):
    if state.get('execution_authorized') is not False or state.get('strategy_id') not in {
            'E_DONCHIAN20_V1', 'O_ETF_TREND_CALL_V1'}:
        raise ValueError('Offline equity/underlying-control ledger required')
    if any(s not in SYMBOLS for s in state['positions']):
        raise ValueError('Only fixed equity instruments supported')
    for value in state['positions'].values(): _amount(value)
    _amount(state['cash'])
    ident = event['id']
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError('Stable corporate action identity required')
    if not isinstance(event['source_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', event['source_sha256']):
        raise ValueError('Retained source SHA256 required')
    at = _time(now)
    effective, received = _time(event['effective_at']), _time(event['received_at'])
    if effective != at or received > at:
        raise ValueError('Action must be available and applied at its effective boundary')
    checksum = digest({'event': event, 'applied_at': at.isoformat()})
    result = deepcopy(state)
    book = result.setdefault('corporate_actions', {'events': {}, 'receivables': {}, 'last_event_at': None})
    if ident in book['events']:
        if book['events'][ident]['sha256'] != checksum:
            raise ValueError('Conflicting action identity')
        return result, dict(book['events'][ident], replayed=True)
    clocks = [book.get('last_event_at'), state.get('last_applied_at')]
    if any(value is not None and _time(value) > at for value in clocks):
        raise ValueError('Action cannot alter an earlier portfolio boundary')
    if any(batch.get('apply_hash') is None for batch in state.get('batches', {}).values()):
        raise ValueError('Resolve prepared plans before applying an action')
    kind = event['kind']
    portfolio_at = state.get('last_portfolio_at', state.get('last_applied_at'))
    if kind in {'cash_dividend', 'split'} and portfolio_at is not None and at <= _time(portfolio_at):
        raise ValueError('Entitlements and splits must precede portfolio processing at the boundary')
    if kind != 'split' and event.get('currency') != 'USD':
        raise ValueError('Explicit USD distribution evidence required')
    cash_before = _amount(result['cash'])
    receivable_before = receivable_value(result)
    if kind in {'cash_dividend', 'split'}:
        symbol = event['symbol']
        if symbol not in SYMBOLS:
            raise ValueError('Unknown equity action symbol')
        quantity = _amount(event['quantity_before'])
        if quantity != _amount(result['positions'].get(symbol, '0')):
            raise ValueError('Entitlement/ownership differs from effective-boundary ledger')
        if kind == 'cash_dividend':
            amount = quantity * _amount(event['amount_per_share'], positive=True)
            payment_at = event.get('payment_at')
            if payment_at is not None and _time(payment_at) < at:
                raise ValueError('Payment precedes entitlement boundary')
            book['receivables'][ident] = dict(symbol=symbol, amount=str(amount), paid=False,
                payment_at=payment_at, source_sha256=event['source_sha256'], effective_at=at.isoformat())
        else:
            ratio = _amount(event['ratio'], positive=True)
            adjusted = quantity * ratio
            if adjusted != adjusted.to_integral_value():
                raise ValueError('Fractional split entitlement requires reviewed cash-in-lieu lifecycle')
            if adjusted: result['positions'][symbol] = str(adjusted)
    elif kind in {'dividend_schedule', 'dividend_payment'}:
        dividend_id = event['dividend_id']
        if dividend_id not in book['receivables']:
            raise ValueError('Original retained dividend entitlement required')
        row = book['receivables'][dividend_id]
        if row['paid']:
            raise ValueError('Dividend already paid')
        if kind == 'dividend_schedule':
            pay = event['payment_at']; pay_time = _time(pay)
            if pay_time < _time(row['effective_at']):
                raise ValueError('Payment precedes entitlement')
            if row['payment_at'] is not None and _time(row['payment_at']) != pay_time:
                raise ValueError('Conflicting known payment date; correction evidence required')
            row['payment_at'] = pay
        else:
            if row['payment_at'] is not None and at < _time(row['payment_at']):
                raise ValueError('Payment precedes verified scheduled date')
            paid = _amount(event['amount'])
            if paid != _amount(row['amount']):
                raise ValueError('Payment does not reconcile to retained entitlement')
            result['cash'] = str(cash_before + paid)
            row['paid'] = True; row['paid_at'] = at.isoformat()
            row['payment_source_sha256'] = event['source_sha256']
    else:
        raise ValueError('Unsupported corporate action lifecycle')
    receipt = dict(sha256=checksum, event=deepcopy(event), applied_at=at.isoformat(),
                   cash_delta=str(_amount(result['cash']) - cash_before),
                   receivable_delta=str(receivable_value(result) - receivable_before),
                   modeled_only=True, execution_authorized=False, replayed=False)
    book['events'][ident] = receipt
    book['last_event_at'] = at.isoformat()
    result['last_applied_at'] = at.isoformat()
    return result, receipt
