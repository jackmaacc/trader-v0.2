"""Offline exact cash/quantity ledger. No broker or credential dependencies."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


class EvidenceError(ValueError):
    """Invalid or conflicting accounting evidence."""


def amount(value):
    if isinstance(value, (float, bool)):
        raise EvidenceError('Amounts must be decimal strings or integers, not floats/bools')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EvidenceError('Invalid decimal amount') from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 120 or abs(result.as_tuple().exponent) > 100:
        raise EvidenceError('Nonfinite or excessive precision amount')
    return result


def input_amount(value):
    result = amount(value)
    if abs(result) > Decimal('1e12') or result.as_tuple().exponent < -12:
        raise EvidenceError('Input exceeds supported bound: magnitude 1e12 and 12 fractional places')
    return result


def _private_database(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise EvidenceError('Symlink database paths rejected')
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise EvidenceError('Existing ledger must be an owner-only regular file')
    else:
        os.close(fd)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError('Timestamp requires an explicit timezone') from exc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw, parse_float=Decimal), hashlib.sha256(raw).hexdigest()


def normalize_event(row, instruments):
    """Alpaca activities or canonical cash/position adjustments; never order snapshots."""
    event_id = row.get('id')
    if not isinstance(event_id, str) or not event_id.strip():
        raise EvidenceError('Every activity needs a stable nonempty ID')
    event_time = timestamp(row.get('transaction_time') or row.get('timestamp')).isoformat()
    kind = row.get('activity_type')
    symbol, quantity, cash, external = None, Decimal(0), Decimal(0), Decimal(0)
    fee = Decimal(0)
    if kind == 'FILL':
        symbol = row.get('symbol')
        if symbol not in instruments:
            raise EvidenceError(f'Explicit contract multiplier missing for {symbol}')
        multiplier = input_amount(instruments[symbol]['multiplier'])
        qty, price = input_amount(row['qty']), input_amount(row['price'])
        fee = input_amount(row.get('fee', '0'))
        if multiplier <= 0 or qty <= 0 or price < 0 or fee < 0 or row.get('side') not in ('buy', 'sell'):
            raise EvidenceError('Invalid fill quantity, price, side, fee or multiplier')
        quantity = qty if row['side'] == 'buy' else -qty
        cash = -quantity * price * multiplier - fee
    elif kind in ('CSD', 'CSW', 'DIV', 'INT', 'FEE', 'CASH_ADJUSTMENT'):
        cash = input_amount(row['net_amount'])
        if kind == 'CSD' and cash <= 0 or kind == 'CSW' and cash >= 0 or kind == 'FEE' and cash > 0:
            raise EvidenceError('Cash activity sign contradicts type')
        if kind in ('CSD', 'CSW'):
            external = cash
        if kind == 'FEE':
            fee = -cash
    elif kind == 'POSITION_ADJUSTMENT':
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or not symbol:
            raise EvidenceError('Position adjustment needs symbol')
        quantity = input_amount(row['quantity_delta'])
    else:
        raise EvidenceError(f'Unsupported activity type: {kind}; explicit reviewed mapping required')
    return dict(id=event_id, timestamp=event_time, kind=kind, symbol=symbol,
                quantity=str(quantity), cash=str(cash), external_cashflow=str(external), fee=str(fee))


def _snapshot(value):
    if value is None:
        return None
    if not isinstance(value.get('positions'), dict):
        raise EvidenceError('Snapshot must explicitly supply positions (empty object means flat)')
    return dict(timestamp=timestamp(value['timestamp']).isoformat(), cash=str(input_amount(value['cash'])),
                positions={s: str(input_amount(q)) for s, q in value['positions'].items()})


def import_bundle(database, activities_file, context_file):
    """Atomically import one fixed account/window; repeat IDs require identical evidence."""
    database = Path(database)
    if database.resolve() in (Path(activities_file).resolve(), Path(context_file).resolve()):
        raise EvidenceError('Database cannot overwrite source evidence')
    rows, source_hash = _load(activities_file)
    context, context_hash = _load(context_file)
    if not isinstance(rows, list):
        raise EvidenceError('Activities must be a JSON list')
    if not context.get('account_reference') or context.get('currency') != 'USD':
        raise EvidenceError('Explicit account reference and USD currency required')
    start, end = timestamp(context['start']), timestamp(context['end'])
    if start >= end:
        raise EvidenceError('Window must increase; event interval is (start, end]')
    opening, closing = _snapshot(context.get('opening')), _snapshot(context.get('ending'))
    if opening and timestamp(opening['timestamp']) != start or closing and timestamp(closing['timestamp']) != end:
        raise EvidenceError('Snapshots must match exact window boundaries')
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        events = [normalize_event(row, context.get('instruments', {})) for row in rows]
    for event in events:
        if not start < timestamp(event['timestamp']) <= end:
            raise EvidenceError('Activity outside declared window')
    _private_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL, raw_payload TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS imports (source_hash TEXT, context_hash TEXT, event_count INTEGER, PRIMARY KEY(source_hash,context_hash))')
        connection.execute('CREATE TABLE IF NOT EXISTS provenance (event_id TEXT, source_hash TEXT, PRIMARY KEY(event_id,source_hash))')
        existing = connection.execute("SELECT value FROM metadata WHERE key='context'").fetchone()
        encoded_context = canonical(context)
        if existing and existing[0] != encoded_context:
            raise EvidenceError('Context differs from fixed ledger account/window; use a separate ledger')
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('context', ?)", (encoded_context,))
        inserted = 0
        for row, event in zip(rows, events):
            encoded, raw = canonical(event), canonical(row)
            existing = connection.execute('SELECT payload,raw_payload FROM events WHERE id=?', (event['id'],)).fetchone()
            if existing and existing != (encoded, raw):
                raise EvidenceError(f'Conflicting duplicate activity ID: {event["id"]}')
            if not existing:
                connection.execute('INSERT INTO events VALUES (?,?,?)', (event['id'], encoded, raw))
                inserted += 1
            connection.execute('INSERT OR IGNORE INTO provenance VALUES (?,?)', (event['id'], source_hash))
        connection.execute('INSERT OR IGNORE INTO imports VALUES (?,?,?)', (source_hash, context_hash, len(rows)))
        connection.commit()
    report = reconcile(database)
    report['new_events'] = inserted
    return report


def reconcile(database):
    """Read-only comparison. Completeness is declared evidence, never inferred from sums."""
    path = Path(database).resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        context = json.loads(connection.execute("SELECT value FROM metadata WHERE key='context'").fetchone()[0])
        events = [json.loads(x[0]) for x in connection.execute('SELECT payload FROM events ORDER BY id')]
        sources = [dict(source_sha256=x[0], context_sha256=x[1], rows=x[2]) for x in connection.execute('SELECT * FROM imports ORDER BY source_hash')]
    missing = []
    for key in ('activities_complete', 'cashflows_complete', 'fees_complete', 'corporate_actions_complete'):
        if context.get(key) is not True:
            missing.append(key)
    opening, ending = _snapshot(context.get('opening')), _snapshot(context.get('ending'))
    if opening is None:
        missing.append('opening_snapshot')
    if ending is None:
        missing.append('ending_snapshot')
    if not sources:
        missing.append('source_evidence')
    report = dict(status='inconclusive', missing_evidence=missing, event_count=len(events), sources=sources,
                  return_calculated=False, execution_authorized=False)
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        report['external_cashflow'] = str(sum((amount(e['external_cashflow']) for e in events), Decimal(0)))
        report['fees'] = str(sum((amount(e['fee']) for e in events), Decimal(0)))
        if opening is not None and ending is not None:
            cash = amount(opening['cash']) + sum((amount(e['cash']) for e in events), Decimal(0))
            quantities = {s: amount(q) for s, q in opening['positions'].items()}
            for event in events:
                if event['symbol']:
                    s = event['symbol']
                    quantities[s] = quantities.get(s, Decimal(0)) + amount(event['quantity'])
            differences = {s: str(quantities.get(s, Decimal(0)) - amount(ending['positions'].get(s, '0')))
                           for s in sorted(set(quantities) | set(ending['positions']))}
            cash_diff = cash - amount(ending['cash'])
            report.update(expected_cash=str(cash), cash_difference=str(cash_diff), position_differences=differences)
            mismatch = cash_diff != 0 or any(amount(q) != 0 for q in differences.values())
            report['status'] = 'mismatch' if mismatch else ('inconclusive' if missing else 'reconciled')
    return report
