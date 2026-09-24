"""Independent offline reconstruction of durable research books, never broker P&L."""
from collections import defaultdict, deque
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3

from .phase3_store import _verify
from trader_engine.operations.decision_ledger import encoded

D = Decimal
ZERO = D(0)
# FIFO proportional allocations can repeat. Cash and holdings are compared exactly.
ATTRIBUTION_TOLERANCE = D('1e-24')


def _n(value, *, signed=False):
    if not isinstance(value, str):
        raise ValueError('Exact financial strings required')
    result = D(value)
    if not result.is_finite() or (not signed and result < 0):
        raise ValueError('Invalid financial amount')
    if len(result.as_tuple().digits) > 128 or abs(result.as_tuple().exponent) > 128:
        raise ValueError('Unsupported financial precision')
    return result


def _positions(state):
    result = {}
    for symbol, value in state['positions'].items():
        if isinstance(value, dict):
            metadata = value['contract']
            contract = metadata['symbol']
            if (type(metadata.get('multiplier')) is not int or metadata['multiplier'] != 100
                    or metadata.get('underlying') != symbol
                    or metadata.get('option_type') != 'call'
                    or metadata.get('standard_unadjusted') is not True
                    or metadata.get('physically_delivered') is not True):
                raise ValueError('Option ownership metadata must match standard multiplier and underlying')
            if type(value['quantity']) is not int or value['quantity'] != 1:
                raise ValueError('Only one standard option contract supported')
            if contract in result:
                raise ValueError('Duplicate option ownership')
            result[contract] = D(1)
        else:
            result[symbol] = _n(value)
    return {s: q for s, q in result.items() if q}


def _fill(row):
    if row.get('modeled_fill') is not True or row['side'] not in ('buy', 'sell'):
        raise ValueError('Explicit modeled buy/sell required')
    buy = row['side'] == 'buy'
    if 'contract' in row:
        if type(row['quantity']) is not int or row['quantity'] != 1:
            raise ValueError('One standard option contract required')
        symbol, units, multiplier = row['contract'], D(1), D(100)
        price, fee = _n(row['premium']) / 100, _n(row['fee'])
        gross = price * 100
    else:
        symbol, multiplier = row['symbol'], D(1)
        quantity, price = _n(row['quantity']), _n(row['price'])
        if quantity <= 0:
            raise ValueError('Positive traded quantity required')
        base, quote = _n(row['base_fee']), _n(row['quote_fee'])
        units = quantity - base if buy else quantity + base
        fee = quote + base * price
        gross = units * price
        asserted_units = row.get('acquired_quantity' if buy else 'owned_units_debit')
        if asserted_units is not None and _n(asserted_units) != units:
            raise ValueError('Fill unit assertion differs from independent arithmetic')
    if units <= 0 or price <= 0:
        raise ValueError('Positive modeled fill required')
    cash = -(gross + fee) if buy else gross - fee
    for field in ('cash_debit', 'cash_credit', 'cash_change', 'quantity_change'):
        if field not in row:
            continue
        expected = {'cash_debit': -cash, 'cash_credit': cash,
                    'cash_change': cash, 'quantity_change': units if buy else -units}[field]
        if _n(row[field], signed=True) != expected:
            raise ValueError('Fill cash/quantity assertion differs from independent arithmetic')
    return symbol, units, price, multiplier, fee, cash


def audit_database(database, *, terminal_marks=None):
    """Read one verified snapshot; recompute every cash/quantity boundary and FIFO.

    Marks are exact financial strings keyed by actual instrument (option contract,
    not underlying). They are valuation assertions, not authenticated observations.
    Missing marks leave accounting reconciliation available but P&L inconclusive.
    """
    path = Path(database).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink database paths rejected')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        run, final, batches, head = _verify(connection)
    with localcontext() as context:
        context.prec = 256
        return _audit(run, final, batches, head, terminal_marks)


def _audit(run, final, batches, head, terminal_marks):
    initial = run['initial_state']
    if _n(initial['cash']) != D(100000) or _positions(initial) or initial.get('modeled_fills'):
        raise ValueError('Flat 100000 research initializer required')
    if initial.get('corporate_actions'):
        raise ValueError('No initial corporate-action balances supported')
    cash, income, realized, realized_fees, total_fees = D(100000), ZERO, ZERO, ZERO, ZERO
    lots = defaultdict(deque)
    receivables = {}
    action_ids = {}
    journal = []
    mismatches = []
    boundaries = []
    matches = []
    for body, checksum in batches.values():
        state = body['state']
        current = state['modeled_fills']
        if len(current) < len(journal) or encoded(current[:len(journal)]) != encoded(journal):
            raise ValueError('Modeled fill journal must be append-only')
        operation = body['inputs']
        if operation.get('kind') == 'corporate_action':
            if len(current) != len(journal):
                raise ValueError('Corporate-action transition cannot also book fills')
            event = operation['event']; kind = event['kind']
            if event['id'] in action_ids:
                if encoded(action_ids[event['id']]) != encoded(event):
                    raise ValueError('Conflicting corporate-action identity')
                kind = 'replayed_action'
            else:
                action_ids[event['id']] = event
            if kind in ('cash_dividend', 'split'):
                symbol = event['symbol']
                held = sum((lot['units'] for lot in lots[symbol]), ZERO)
                if _n(event['quantity_before']) != held:
                    raise ValueError('Action entitlement differs from reconstructed ownership')
                if kind == 'split':
                    ratio = _n(event['ratio'])
                    if ratio <= 0 or held * ratio != (held * ratio).to_integral_value():
                        raise ValueError('Unsupported fractional split')
                    for lot in lots[symbol]:
                        lot['units'] *= ratio; lot['price'] /= ratio
                else:
                    if event['id'] in receivables:
                        raise ValueError('Duplicate dividend entitlement')
                    amount = held * _n(event['amount_per_share'])
                    receivables[event['id']] = {'amount': amount, 'paid': False}
                    income += amount
            elif kind == 'dividend_payment':
                row = receivables[event['dividend_id']]
                if row['paid'] or _n(event['amount']) != row['amount']:
                    raise ValueError('Dividend payment does not reconcile')
                cash += row['amount']; row['paid'] = True
            elif kind not in ('dividend_schedule', 'replayed_action'):
                raise ValueError('Unsupported corporate-action accounting')
        for index, row in enumerate(current[len(journal):], len(journal)):
            symbol, units, price, multiplier, fee, movement = _fill(row)
            cash += movement; total_fees += fee
            if row['side'] == 'buy':
                lots[symbol].append(dict(units=units, price=price, multiplier=multiplier, fee=fee))
            else:
                remaining, exit_fee = units, fee
                if sum((lot['units'] for lot in lots[symbol]), ZERO) < remaining:
                    raise ValueError('Sale exceeds independently reconstructed ownership')
                while remaining:
                    lot = lots[symbol][0]
                    if lot['multiplier'] != multiplier:
                        raise ValueError('Instrument multiplier changed without lifecycle evidence')
                    closed = min(remaining, lot['units'])
                    entry_part = lot['fee'] if closed == lot['units'] else lot['fee'] * closed / lot['units']
                    exit_part = exit_fee if closed == remaining else exit_fee * closed / remaining
                    gain = closed * (price - lot['price']) * multiplier
                    realized += gain; realized_fees += entry_part + exit_part
                    matches.append(dict(fill_index=index, symbol=symbol, units=str(closed),
                                        gross=str(gain), net=str(gain-entry_part-exit_part)))
                    lot['units'] -= closed; lot['fee'] -= entry_part
                    remaining -= closed; exit_fee -= exit_part
                    if not lot['units']: lots[symbol].popleft()
        journal = current
        reconstructed = {s: sum((lot['units'] for lot in rows), ZERO) for s, rows in lots.items() if rows}
        cash_difference = cash - _n(state['cash'])
        positions_match = reconstructed == _positions(state)
        actual_receivables = state.get('corporate_actions', {}).get('receivables', {})
        receivables_match = (set(actual_receivables) == set(receivables) and all(
            _n(actual_receivables[k]['amount']) == v['amount'] and actual_receivables[k]['paid'] is v['paid']
            for k, v in receivables.items()))
        boundary = dict(operation_id=body['batch_id'], cash_difference=str(cash_difference),
                        positions_match=positions_match, receivables_match=receivables_match)
        boundaries.append(boundary)
        if cash_difference or not positions_match or not receivables_match:
            mismatches.append(body['batch_id'])
    owned = {s: sum((lot['units'] for lot in rows), ZERO) for s, rows in lots.items() if rows}
    marks = {} if terminal_marks is None else {s: _n(v) for s, v in terminal_marks.items()}
    if set(marks) - set(owned):
        raise ValueError('Terminal marks contain unowned instrument')
    missing = sorted(set(owned) - set(marks))
    metrics = None
    if not missing:
        gross = sum((lot['units'] * (marks[s] - lot['price']) * lot['multiplier'] for s, rows in lots.items() for lot in rows), ZERO)
        unallocated = sum((lot['fee'] for rows in lots.values() for lot in rows), ZERO)
        receivable = sum((r['amount'] for r in receivables.values() if not r['paid']), ZERO)
        value = sum((lot['units'] * marks[s] * lot['multiplier'] for s, rows in lots.items() for lot in rows), ZERO)
        pnl = realized - realized_fees + gross - unallocated + income
        difference = cash + value + receivable - D(100000) - pnl
        if abs(difference) > ATTRIBUTION_TOLERANCE: mismatches.append('terminal_attribution')
        metrics = {k: str(v) for k, v in dict(cash=cash, receivables=receivable,
            marked_equity=cash+value+receivable, realized_gross_at_execution_prices=realized,
            realized_net=realized-realized_fees, unrealized_net=gross-unallocated,
            accrued_income=income, modeled_fees=total_fees, net_pnl_before_overhead=pnl,
            identity_difference=difference, attribution_tolerance=ATTRIBUTION_TOLERANCE).items()}
    return dict(status='mismatch' if mismatches else ('inconclusive' if missing else 'reconciled'),
        head_sha256=head, boundaries=boundaries, mismatched_operations=mismatches,
        missing_terminal_marks=missing, metrics=metrics, fifo_matches=matches,
        fill_count=len(journal), scope='offline_modeled_accounting',
        source_authenticity_verified=False, overhead_included=False,
        execution_authorized=False, prospective_credit=False, investment_qualified=False)
