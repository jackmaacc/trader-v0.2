"""Append-only research decision evidence. No broker or strategy activation path."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .accounting_ledger import _private_database

TABLES = {'decisions', 'input_evidence', 'decision_inputs', 'execution_links'}
DIGEST = re.compile(r'[0-9a-f]{64}')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def stamp(value):
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('Timezone-aware timestamps required')
    return t.astimezone(timezone.utc)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Nonempty text required')
    return value


def _schema(connection):
    existing = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if existing and existing != TABLES:
        raise ValueError('Not a research decision ledger')
    connection.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS input_evidence (sha256 TEXT PRIMARY KEY, body TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS decision_inputs (decision_id TEXT NOT NULL, ordinal INTEGER NOT NULL, input_sha256 TEXT NOT NULL, PRIMARY KEY(decision_id,ordinal))')
    connection.execute('CREATE TABLE IF NOT EXISTS execution_links (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL)')


def record_decision(database, *, decision_id, strategy_id, decision_at,
                    registry_sha256, code_sha256, inputs, result, amends=None):
    """Record causal input snapshots and adapter output in one durable transaction.

    Inputs are {role,event_at,received_at,payload}. Serialization is canonical JSON,
    not exact HTTP bytes. Hashes identify caller-supplied code/protocol; they do not
    certify their approval. Amendments preserve, rather than rewrite, old decisions.
    """
    decision_id, strategy_id = _text(decision_id), _text(strategy_id)
    for value in [registry_sha256, code_sha256]:
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            raise ValueError('SHA256 identity required')
    when = stamp(decision_at)
    if not isinstance(result, dict) or not isinstance(inputs, list) or not inputs:
        raise ValueError('Result object and nonempty input evidence required')
    snapshots = []
    for source in inputs:
        if not isinstance(source, dict) or set(source) != {'role','event_at','received_at','payload'}:
            raise ValueError('Explicit source timing and payload required')
        _text(source['role'])
        if not stamp(source['event_at']) <= stamp(source['received_at']) <= when:
            raise ValueError('Input was unavailable at decision time')
        snapshots.append(dict(source, event_at=stamp(source['event_at']).isoformat(),
                              received_at=stamp(source['received_at']).isoformat()))
    source_hashes = [digest(s) for s in snapshots]
    if amends is not None:
        _text(amends)
        if amends == decision_id:
            raise ValueError('Cannot amend itself')
    body = dict(id=decision_id, strategy_id=strategy_id, decision_at=when.isoformat(),
                registry_sha256=registry_sha256, code_sha256=code_sha256,
                input_sha256=source_hashes, result=result, amends=amends,
                mode='research_evidence', broker_execution_verified=False)
    serialized, checksum = encoded(body), digest(body)
    path = Path(database)
    _private_database(path)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA synchronous=FULL')
        c.execute('BEGIN IMMEDIATE')
        _schema(c)
        if amends is not None:
            parent = c.execute('SELECT body FROM decisions WHERE id=?',(amends,)).fetchone()
            if parent is None or stamp(json.loads(parent[0])['decision_at']) > when:
                raise ValueError('Amendment requires an earlier recorded decision')
        old = c.execute('SELECT body,sha256 FROM decisions WHERE id=?',(decision_id,)).fetchone()
        if old and old != (serialized, checksum):
            raise ValueError('Conflicting decision ID; record an explicit amendment')
        for ordinal, source in enumerate(snapshots):
            h = source_hashes[ordinal]; raw = encoded(source)
            prior = c.execute('SELECT body FROM input_evidence WHERE sha256=?',(h,)).fetchone()
            if prior and prior[0] != raw:
                raise ValueError('Existing input evidence corrupted')
            c.execute('INSERT OR IGNORE INTO input_evidence VALUES (?,?)',(h,raw))
            c.execute('INSERT OR IGNORE INTO decision_inputs VALUES (?,?,?)',(decision_id,ordinal,h))
        c.execute('INSERT OR IGNORE INTO decisions VALUES (?,?,?)',(decision_id,serialized,checksum))
    return {'decision_id':decision_id,'sha256':checksum,'inserted':old is None,
            'execution_authorized':False}


def record_execution_link(database, *, link_id, decision_id, client_order_id,
                          activity_ids, observed_at, evidence_sha256):
    """Append an offline evidence association, never infer actual broker acceptance."""
    for s in [link_id,decision_id,client_order_id]: _text(s)
    if not isinstance(activity_ids,list) or any(not isinstance(v,str) or not v for v in activity_ids) or len(set(activity_ids))!=len(activity_ids):
        raise ValueError('Unique activity IDs required')
    if not isinstance(evidence_sha256,str) or not DIGEST.fullmatch(evidence_sha256):
        raise ValueError('Order/fill evidence SHA256 required')
    body=dict(id=link_id,decision_id=decision_id,client_order_id=client_order_id,
              activity_ids=activity_ids,observed_at=stamp(observed_at).isoformat(),
              evidence_sha256=evidence_sha256,broker_execution_verified=False)
    path=Path(database).resolve()
    if not path.is_file(): raise ValueError('Decision ledger required')
    path=Path(database)
    _private_database(path)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');_schema(c)
        parent=c.execute('SELECT body FROM decisions WHERE id=?',(decision_id,)).fetchone()
        if parent is None or stamp(json.loads(parent[0])['decision_at'])>stamp(observed_at):
            raise ValueError('Link requires an earlier decision')
        old=c.execute('SELECT body,sha256 FROM execution_links WHERE id=?',(link_id,)).fetchone()
        row=(encoded(body),digest(body))
        if old and old!=row: raise ValueError('Conflicting execution link ID')
        c.execute('INSERT OR IGNORE INTO execution_links VALUES (?,?,?,?)',(link_id,decision_id,*row))
    return {'inserted':old is None,'broker_execution_verified':False}


def verify_decisions(database):
    """Read-only integrity check; unsigned hashes cannot prevent coordinated edits."""
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables!=TABLES: raise ValueError('Not a research decision ledger')
        sources={h:json.loads(body) for h,body in c.execute('SELECT * FROM input_evidence')}
        for h,body in sources.items():
            if digest(body)!=h: raise ValueError('Input evidence hash mismatch')
        decisions={}
        for ident,raw,h in c.execute('SELECT * FROM decisions'):
            body=json.loads(raw)
            if digest(body)!=h or body['id']!=ident: raise ValueError('Decision hash mismatch')
            actual=[r[0] for r in c.execute('SELECT input_sha256 FROM decision_inputs WHERE decision_id=? ORDER BY ordinal',(ident,))]
            if actual!=body['input_sha256'] or not actual or any(s not in sources for s in actual):
                raise ValueError('Decision input linkage mismatch')
            for source in (sources[h] for h in actual):
                if not stamp(source['event_at'])<=stamp(source['received_at'])<=stamp(body['decision_at']):
                    raise ValueError('Noncausal evidence')
            decisions[ident]=body
        for body in decisions.values():
            parent=body['amends']
            if parent is not None and (parent not in decisions or stamp(decisions[parent]['decision_at'])>stamp(body['decision_at'])):
                raise ValueError('Missing or noncausal amendment parent')
            seen={body['id']}
            while parent is not None:
                if parent in seen: raise ValueError('Cyclic amendment chain')
                seen.add(parent)
                if parent not in decisions: raise ValueError('Missing amendment parent')
                parent=decisions[parent]['amends']
        links=0
        for ident,parent,raw,h in c.execute('SELECT * FROM execution_links'):
            body=json.loads(raw)
            if digest(body)!=h or body['id']!=ident or body['decision_id']!=parent or parent not in decisions:
                raise ValueError('Execution link hash/parent mismatch')
            if stamp(body['observed_at']) < stamp(decisions[parent]['decision_at']):
                raise ValueError('Execution link precedes decision')
            links+=1
        if c.execute('SELECT count(*) FROM decision_inputs WHERE decision_id NOT IN (SELECT id FROM decisions)').fetchone()[0]:
            raise ValueError('Orphan decision input links')
    return {'integrity_verified':True,'decisions':len(decisions),'inputs':len(sources),
            'execution_links':links,'broker_execution_verified':False,'execution_authorized':False}
