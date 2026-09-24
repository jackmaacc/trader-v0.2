"""Offline FIFO attribution with explicit basis and boundary marks; never trading authority."""
from collections import defaultdict, deque
from decimal import Decimal, ROUND_DOWN, localcontext
import json
from pathlib import Path
import sqlite3

from .accounting_ledger import EvidenceError, amount, input_amount, timestamp

ZERO = Decimal(0)
QUANTUM = Decimal('0.000000000001')


def _allocated(total, closed, available):
    # The final portion receives every residual; no fee disappears through rounding.
    return total if closed == available else (total * closed / available).quantize(QUANTUM, rounding=ROUND_DOWN)


def attribute_database(database, evidence):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('BEGIN')
        context = json.loads(connection.execute("SELECT value FROM metadata WHERE key='context'").fetchone()[0])
        events = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM events')]
        sources = [dict(source_sha256=row[0], context_sha256=row[1]) for row in connection.execute('SELECT source_hash,context_hash FROM imports')]
    result = attribute(context, events, evidence)
    result['sources'] = sources
    if not sources:
        result['missing_evidence'].append('source_evidence')
        if result['status'] == 'attributed': result['status'] = 'inconclusive'
    return result


def attribute(context, events, evidence):
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        return _attribute(context, events, evidence)


def _attribute(context, events, evidence):
    missing = [key for key in ('activities_complete', 'cashflows_complete', 'fees_complete', 'corporate_actions_complete') if context.get(key) is not True]
    result = dict(status='inconclusive', missing_evidence=missing, execution_authorized=False,
                  return_calculated=False, metrics=None, matches=[])
    start, end = timestamp(context['start']), timestamp(context['end'])
    if start >= end or context.get('currency') != 'USD': raise EvidenceError('Invalid USD interval')
    opening, ending = context.get('opening'), context.get('ending')
    for label, snapshot, boundary in [('opening', opening, start), ('ending', ending, end)]:
        if snapshot is None: missing.append(label + '_snapshot')
        elif timestamp(snapshot['timestamp']) != boundary or not isinstance(snapshot.get('positions'), dict):
            raise EvidenceError('Exact boundary snapshots and signed positions required')
    ids = [event['id'] for event in events]
    if len(ids) != len(set(ids)): raise EvidenceError('Duplicate event IDs')
    for event in events:
        if not start < timestamp(event['timestamp']) <= end: raise EvidenceError('Event outside window')
        if event['kind'] in ('POSITION_ADJUSTMENT', 'CASH_ADJUSTMENT'):
            missing.append('unsupported_adjustment:' + event['id'])
        elif event['kind'] not in ('FILL', 'CSD', 'CSW', 'DIV', 'INT', 'FEE'):
            missing.append('unsupported_event:' + event['id'])
    order = evidence.get('event_order')
    if order is not None:
        if len(order) != len(ids) or set(order) != set(ids): raise EvidenceError('event_order must include each event exactly once')
        ranks = {id: i for i, id in enumerate(order)}
        events = sorted(events, key=lambda event: (timestamp(event['timestamp']), ranks[event['id']]))
    else:
        seen = set()
        for event in events:
            if event['kind'] == 'FILL':
                key = (timestamp(event['timestamp']), event['symbol'])
                if key in seen: missing.append('same_timestamp_fill_order')
                seen.add(key)
        events = sorted(events, key=lambda event: timestamp(event['timestamp']))
    if opening is None or ending is None or any(x.startswith('unsupported') or x == 'same_timestamp_fill_order' for x in missing): return result
    open_positions = {s: input_amount(q) for s,q in opening['positions'].items() if input_amount(q)}
    end_positions = {s: input_amount(q) for s,q in ending['positions'].items() if input_amount(q)}
    multipliers = {}
    symbols = set(open_positions) | set(end_positions) | {e['symbol'] for e in events if e['kind']=='FILL'}
    for symbol in symbols:
        if symbol not in context.get('instruments', {}): missing.append('multiplier:' + symbol)
        else:
            multipliers[symbol] = input_amount(context['instruments'][symbol]['multiplier'])
            if multipliers[symbol] <= 0: raise EvidenceError('Positive multiplier required')
    if len(multipliers) != len(symbols): return result
    lots = defaultdict(deque)
    supplied_lots = evidence.get('opening_lots')
    if supplied_lots is None:
        missing.append('explicit_opening_lots'); return result
    for entry in supplied_lots:
        s = entry['symbol']; q = input_amount(entry['quantity']); p = input_amount(entry['price']); f = input_amount(entry['entry_fee_remaining'])
        if not q or p < 0 or f < 0 or s not in open_positions or timestamp(entry['acquired_at']) > start:
            raise EvidenceError('Invalid opening lot evidence')
        lots[s].append(dict(quantity=q, price=p, fee=f, acquired_at=timestamp(entry['acquired_at'])))
    for s in set(lots) | set(open_positions):
        if sum((lot['quantity'] for lot in lots[s]), ZERO) != open_positions.get(s,ZERO): missing.append('opening_basis_quantity:' + s)
        if any(lot['quantity'] * open_positions.get(s,ZERO) <= 0 for lot in lots[s]): raise EvidenceError('Opening lots must match net position direction')
        lots[s] = deque(sorted(lots[s], key=lambda lot: lot['acquired_at']))
    if any(x.startswith('opening_basis_quantity') for x in missing): return result
    marks = {}
    for label, positions, boundary in [('opening',open_positions,start),('terminal',end_positions,end)]:
        marks[label] = {}
        for s in positions:
            mark = evidence.get(label + '_marks',{}).get(s)
            if mark is None: missing.append(label + '_mark:' + s); continue
            price = input_amount(mark['price'])
            if price < 0 or timestamp(mark['timestamp']) != boundary: raise EvidenceError('Marks must be nonnegative at exact boundary')
            marks[label][s] = price
    # Missing terminal symbols after event processing are also checked below.
    if any('_mark:' in x for x in missing): return result
    opening_gross = sum((lot['quantity'] * (marks['opening'][s]-lot['price']) * multipliers[s] for s in lots for lot in lots[s]), ZERO)
    opening_fees = sum((lot['fee'] for rows in lots.values() for lot in rows), ZERO)
    realized, realized_fees, cash_change, external, income, account_fees, fill_fees = (ZERO,)*7
    for event in events:
        cash, fee, flow = amount(event['cash']), amount(event['fee']), amount(event['external_cashflow'])
        if fee < 0: raise EvidenceError('Negative normalized fee')
        cash_change += cash; external += flow
        if event['kind'] != 'FILL':
            if amount(event['quantity']) != 0: raise EvidenceError('Cash activity changes quantity')
            kind = event['kind']
            if kind == 'CSD' and (cash <= 0 or flow != cash or fee): raise EvidenceError('Invalid deposit')
            if kind == 'CSW' and (cash >= 0 or flow != cash or fee): raise EvidenceError('Invalid withdrawal')
            if kind in ('DIV','INT') and (flow or fee): raise EvidenceError('Invalid income activity')
            if kind == 'FEE' and (cash != -fee or flow): raise EvidenceError('Invalid standalone fee')
            if event['kind'] in ('DIV','INT'): income += cash
            elif event['kind'] == 'FEE': account_fees += fee
            continue
        s=event['symbol']; q=input_amount(event['quantity']); m=multipliers[s]
        if q == 0 or fee < 0 or flow: raise EvidenceError('Invalid normalized fill')
        price = -(cash + fee) / (q*m)
        if price < 0 or price*q*m != -(cash+fee): raise EvidenceError('Non-exact normalized execution price')
        input_amount(price)
        remaining, remaining_fee = q, fee; fill_fees += fee
        while remaining and lots[s] and remaining * lots[s][0]['quantity'] < 0:
            lot=lots[s][0]; closed=min(abs(remaining),abs(lot['quantity']))
            old_fee=_allocated(lot['fee'],closed,abs(lot['quantity']))
            exit_fee=_allocated(remaining_fee,closed,abs(remaining))
            sign=Decimal(1) if lot['quantity'] > 0 else Decimal(-1)
            gain=sign*closed*(price-lot['price'])*m
            realized += gain; realized_fees += old_fee+exit_fee
            result['matches'].append(dict(event_id=event['id'],symbol=s,quantity=str(closed),gross=str(gain),fees=str(old_fee+exit_fee),net=str(gain-old_fee-exit_fee)))
            lot['quantity']-=sign*closed;lot['fee']-=old_fee
            remaining+=sign*closed;remaining_fee-=exit_fee
            if not lot['quantity']: lots[s].popleft()
        if remaining: lots[s].append(dict(quantity=remaining,price=price,fee=remaining_fee))
    actual_positions = {s:sum((lot['quantity'] for lot in rows),ZERO) for s,rows in lots.items()}
    actual_positions = {s:q for s,q in actual_positions.items() if q}
    mismatch = actual_positions != end_positions
    expected_cash = input_amount(opening['cash'])+cash_change
    cash_difference=expected_cash-input_amount(ending['cash']);mismatch |= cash_difference != 0
    for s in actual_positions:
        if s not in marks['terminal']: missing.append('terminal_mark:' + s)
    if any('_mark:' in x for x in missing):
        result['status']='mismatch' if mismatch else 'inconclusive'; return result
    ending_gross=sum((lot['quantity']*(marks['terminal'][s]-lot['price'])*multipliers[s] for s,rows in lots.items() for lot in rows),ZERO)
    ending_fees=sum((lot['fee'] for rows in lots.values() for lot in rows),ZERO)
    opening_equity=input_amount(opening['cash'])+sum((q*marks['opening'][s]*multipliers[s] for s,q in open_positions.items()),ZERO)
    ending_equity=input_amount(ending['cash'])+sum((q*marks['terminal'][s]*multipliers[s] for s,q in end_positions.items()),ZERO)
    attributed=realized-realized_fees+ending_gross-ending_fees-(opening_gross-opening_fees)+income-account_fees
    equity_change=ending_equity-opening_equity-external
    identity_difference=equity_change-attributed;mismatch |= identity_difference != 0
    result['metrics']={k:str(v) for k,v in dict(realized_gross=realized,realized_net=realized-realized_fees,
        ending_unrealized_gross=ending_gross,ending_unrealized_net=ending_gross-ending_fees,
        opening_unrealized_net=opening_gross-opening_fees,income=income,account_fees=account_fees,
        period_fill_fees=fill_fees,external_cashflow=external,period_net_pnl=attributed,
        cashflow_adjusted_equity_change=equity_change,identity_difference=identity_difference,
        cash_difference=cash_difference).items()}
    result['position_match']=actual_positions == end_positions
    result['status']='mismatch' if mismatch else ('inconclusive' if missing else 'attributed')
    return result
