"""Transactional offline research state. Never a broker ownership/locking service."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from trader_engine.operations.accounting_ledger import _private_database
from trader_engine.operations.decision_ledger import digest, encoded

TABLES = {'research_run', 'research_batches'}


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Nonempty identity required')
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    # Clone to keep a transition from mutating caller evidence or the input record.
    return json.loads(encoded(value))


def _schema(c, *, create=False):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables != TABLES:
        if tables or not create:
            raise ValueError('Not an offline research state database')
        c.execute('CREATE TABLE research_run (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL, sha256 TEXT NOT NULL)')
        c.execute('CREATE TABLE research_batches (sequence INTEGER PRIMARY KEY, batch_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL)')


def _verify(c):
    """Verify a complete chain within the caller's SQLite snapshot/transaction."""
    _schema(c)
    rows = c.execute('SELECT body,sha256 FROM research_run').fetchall()
    if len(rows) != 1:
        raise ValueError('Exactly one research run required')
    raw, last_hash = rows[0]
    run = json.loads(raw)
    if (not isinstance(run, dict) or set(run) != {'run_id','registry_sha256','implementation_sha256','initial_state','mode','execution_authorized','prospective_credit'}
            or digest(run) != last_hash or not isinstance(run['initial_state'], dict)
            or run['mode'] != 'offline_modeled_state' or run['execution_authorized'] is not False
            or run['prospective_credit'] is not False):
        raise ValueError('Run evidence hash mismatch')
    state = run['initial_state']
    batches = {}
    for number, (sequence, batch_id, raw, checksum) in enumerate(c.execute('SELECT sequence,batch_id,body,sha256 FROM research_batches ORDER BY sequence'), 1):
        body = json.loads(raw)
        if (not isinstance(body, dict) or set(body) != {'sequence','batch_id','inputs','input_state_sha256','previous_sha256','state','result'}
                or sequence != number or body['sequence'] != sequence or body['batch_id'] != batch_id
                or digest(body) != checksum or body['previous_sha256'] != last_hash
                or body['input_state_sha256'] != digest(state)):
            raise ValueError('Batch state chain mismatch')
        state = body['state']
        if not isinstance(state, dict) or not isinstance(body['result'], dict):
            raise ValueError('Invalid transition output')
        batches[batch_id] = (body, checksum)
        last_hash = checksum
    return run, state, batches, last_hash


def read_state(database):
    """Read and verify saved modeled state without running a transition."""
    path = Path(database).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink database paths rejected')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        run, state, batches, head = _verify(c)
    return dict(run=run, state=state, batches=len(batches), head_sha256=head,
                mode='offline_modeled_state', execution_authorized=False,
                prospective_credit=False)


def apply_batch(database, *, run_id, registry_sha256, implementation_sha256,
                initial_state, batch_id, inputs, transition):
    """Atomically persist input, output, and modeled state or nothing on failure.

    The pure callback receives isolated JSON copies of (state, inputs) and returns
    {state: object, result: object}. It must do no I/O. A successful replay never
    calls it again. This local store cannot provide exactly-once external effects.
    Run identities and caller-supplied code hashes bind replay; no approval or
    source authentication is inferred. Changing code requires a separate run.
    """
    _text(run_id); _text(batch_id)
    for value in (registry_sha256, implementation_sha256):
        if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise ValueError('Explicit SHA256 identity required')
    initial, inputs = _object(initial_state), _object(inputs)
    run = dict(run_id=run_id, registry_sha256=registry_sha256,
               implementation_sha256=implementation_sha256, initial_state=initial,
               mode='offline_modeled_state', execution_authorized=False,
               prospective_credit=False)
    path = Path(database).absolute()
    _private_database(path)
    with closing(sqlite3.connect(path, timeout=30)) as c:
        c.execute('PRAGMA synchronous=FULL')
        with c:
            c.execute('BEGIN IMMEDIATE')
            _schema(c, create=True)
            existing = c.execute('SELECT body FROM research_run').fetchone()
            if existing is None:
                c.execute('INSERT INTO research_run VALUES (1,?,?)', (encoded(run), digest(run)))
            elif existing[0] != encoded(run):
                raise ValueError('Run/code/initial-state identity changed; separate run required')
            saved_run, state, batches, head = _verify(c)
            if batch_id in batches:
                original, checksum = batches[batch_id]
                if encoded(original['inputs']) != encoded(inputs):
                    raise ValueError('Conflicting batch ID')
                return dict(inserted=False, batch_sha256=checksum, result=original['result'],
                            state=state, result_is_historical=True, execution_authorized=False,
                            prospective_credit=False)
            output = transition(_object(state), _object(inputs))
            if not isinstance(output, dict) or set(output) != {'state', 'result'}:
                raise ValueError('Transition must return state and result objects')
            next_state, result = _object(output['state']), _object(output['result'])
            number = len(batches)+1
            body = dict(sequence=number, batch_id=batch_id, inputs=inputs,
                        input_state_sha256=digest(state), previous_sha256=head,
                        state=next_state, result=result)
            checksum = digest(body)
            c.execute('INSERT INTO research_batches VALUES (?,?,?,?)',
                      (number, batch_id, encoded(body), checksum))
    return dict(inserted=True, batch_sha256=checksum, result=result, state=next_state,
                result_is_historical=False, execution_authorized=False, prospective_credit=False)
