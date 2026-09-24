"""Local, read-only-source operational evidence. Never grants trading approval."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3

SOURCES = {'paper_crypto_service': 180, 'continuous_market_scan': 600,
           'plus_stream': 600, 'plus_options_stream': 600, 'plus_research': 600}
REQUIRED_SECONDS = 30 * 24 * 60 * 60
STATES = {'running', 'healthy', 'degraded', 'scanning', 'evaluating', 'streaming',
          'subscribed_idle', 'error', 'shutdown', 'disconnected', 'connecting',
          'authenticating', 'needs_attention', 'stopped'}
ACTIONS = {'hold', 'entry', 'exit', 'blocked', 'no_signal', 'data_blocked'}
SYMBOLS = {'BTC/USD', 'ETH/USD', 'BTCUSD', 'ETHUSD'}


def utc(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc)


def decimal(value):
    try:
        result = Decimal(str(value))
        return str(result) if result.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def read(path):
    try:
        if path.stat().st_size > 40_000_000:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sanitize(source, raw, options=None):
    """An allowlist, not redaction: arbitrary text and nested payloads never persist."""
    account = raw.get('account') if isinstance(raw.get('account'), dict) else {}
    result = {'status': raw.get('status') if isinstance(raw.get('status'),str) and raw.get('status') in STATES else 'unknown',
              'has_error': bool(raw.get('error')), 'reported_error_count': len(raw.get('errors', [])) if isinstance(raw.get('errors'), list) else 0,
              'cash': decimal(account.get('cash')), 'equity': decimal(account.get('equity')),
              'ownership': {}, 'pending': bool(raw.get('pending')), 'state_pending': bool(raw.get('_state_pending')), 'state_discrepancy': bool(raw.get('_state_discrepancy')), 'decisions': {}, 'coverage': {},
              'connected': raw.get('connected') is True, 'authenticated': raw.get('authenticated') is True,
              'subscribed': raw.get('subscribed') is True}
    owned = raw.get('ownership', {})
    if isinstance(owned, dict):
        result['ownership'] = {s: decimal(q) for s, q in owned.items() if s in SYMBOLS and decimal(q) is not None}
    for row in raw.get('decisions', []) if isinstance(raw.get('decisions'), list) else []:
        if isinstance(row, dict) and isinstance(row.get('action'),str) and row.get('action') in ACTIONS:
            action = row['action']; result['decisions'][action] = result['decisions'].get(action, 0) + 1
    coverage = raw.get('coverage', {})
    if isinstance(coverage, dict):
        result['coverage'] = {k: count(coverage.get(k)) for k in ('total', 'active_total')}
        by_state = coverage.get('by_state', {})
        if isinstance(by_state, dict):
            result['coverage']['by_state'] = {k: count(v) for k, v in by_state.items() if k in {'fresh','stale','missing','invalid','unavailable','closed_last_observation','indicative_unknown_latency','inactive','pending'}}
    sources = raw.get('sources', {})
    if isinstance(sources, dict):
        result['per_class'] = {k: {f: count(v.get(f)) for f in ('requested','received')} for k, v in sources.items() if k in {'equities','crypto','futures'} and isinstance(v, dict)}
    for field in ('evaluated', 'signals', 'reconnect_count'):
        result[field] = count(raw.get(field))
    for field in ('counts', 'fresh_counts'):
        value = raw.get(field, {})
        if isinstance(value, dict):
            result[field] = {k: count(v) for k, v in value.items() if k in {'bars','quotes','invalid','out_of_order','evictions','restored'}}
    if source == 'plus_research' and isinstance(options, dict):
        result['options'] = {k: {f: count(row.get(f)) for f in ('assessed_count','input_count')} for row in options.get('sources', []) if isinstance(row, dict) and isinstance((k := row.get('underlying')),str) and k in {'SPY','QQQ','IWM'}} if isinstance(options.get('sources'), list) else {}
    return result


def health(source, raw, now):
    try:
        stamp = utc(raw['checked_at'])
        age = (now-stamp).total_seconds()
        if age < 0:
            return None, None, 'future_heartbeat'
        if age > SOURCES[source]:
            return stamp.isoformat(), age, 'stale_heartbeat'
    except (KeyError, ValueError, TypeError, OverflowError):
        return None, None, 'missing_or_invalid_heartbeat'
    state = raw.get('status') if isinstance(raw.get('status'),str) else 'unknown'
    good = state in {'running','healthy','scanning','evaluating','streaming','subscribed_idle','degraded'} and not raw.get('error') and not raw.get('errors')
    if source in {'plus_stream', 'plus_options_stream'}:
        good = good and all(raw.get(k) is True for k in ('connected','authenticated','subscribed'))
    if source == 'paper_crypto_service':
        if raw.get('_pending_stale'): return stamp.isoformat(), age, 'pending_reconciliation_stale'
        if raw.get('_state_discrepancy'): return stamp.isoformat(), age, 'state_status_disagreement'
        good = good and state == 'running' and not raw.get('data_issues') and not raw.get('_state_discrepancy')
        account = raw.get('account', {})
        good = good and isinstance(account, dict) and decimal(account.get('cash')) is not None and decimal(account.get('equity')) is not None
    return stamp.isoformat(), age, 'observed_healthy' if good else 'source_not_operational'


class PaperTrial:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        path = self.directory/'observations.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(source TEXT NOT NULL,heartbeat TEXT NOT NULL,observed_at TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source,heartbeat));
          CREATE TABLE IF NOT EXISTS polls(observed_at TEXT PRIMARY KEY,summary TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY,source TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,reason TEXT NOT NULL);''')
        self.db.commit()
        for name in ('observations.sqlite3-wal', 'observations.sqlite3-shm'):
            target = self.directory/name
            if target.exists(): os.chmod(target, 0o600)

    def close(self):
        self.db.close()

    def poll(self, artifacts, now=None):
        now = utc(now or datetime.now(timezone.utc)); stamp = now.isoformat()
        previous_poll = self.db.execute('SELECT summary FROM polls WHERE observed_at=?', (stamp,)).fetchone()
        if previous_poll: return json.loads(previous_poll[0])
        meta = dict(self.db.execute('SELECT key,value FROM meta'))
        if meta.get('last_poll') and now < utc(meta['last_poll']):
            raise ValueError('observation clock moved backwards')
        raw = {s: read(Path(artifacts)/s/'status.json') for s in SOURCES}
        options = read(Path(artifacts)/'plus_research/options.json')
        broker_state = read(Path(artifacts)/'paper_crypto_service/state.json')
        crypto = raw['paper_crypto_service']
        crypto['_state_pending'] = broker_state.get('pending')
        def owned(value):
            if not isinstance(value, dict): return None
            try:
                result = {k: Decimal(str(v)) for k,v in value.items()}
                return result if all(v.is_finite() for v in result.values()) else None
            except (ValueError, TypeError, InvalidOperation): return None
        current_owned, state_owned = owned(crypto.get('ownership')), owned(broker_state.get('ownership'))
        crypto['_state_discrepancy'] = not broker_state or current_owned is None or state_owned is None or current_owned != state_owned or (crypto.get('pending') or None) != (broker_state.get('pending') or None)
        crypto['_pending_stale'] = False
        if broker_state.get('pending'):
            try:
                age = (now-utc(broker_state['pending']['created_at'])).total_seconds()
                crypto['_pending_stale'] = not 0 <= age <= 180
            except (KeyError, TypeError, ValueError): crypto['_pending_stale'] = True
        sources = []
        failures = {}
        if meta.get('last_poll') and (now-utc(meta['last_poll'])).total_seconds() > 180:
            failures['collector'] = 'observation_gap'
        for source in SOURCES:
            heartbeat, age, state = health(source, raw[source], now)
            sources.append({'source': source, 'heartbeat_at': heartbeat, 'age_seconds': age, 'health': state})
            previous = self.db.execute('SELECT heartbeat FROM observations WHERE source=? ORDER BY heartbeat DESC LIMIT 1',(source,)).fetchone()
            if heartbeat and previous:
                delta = (utc(heartbeat)-utc(previous[0])).total_seconds()
                if delta < 0: state = 'heartbeat_regressed'
                elif delta > SOURCES[source] and state == 'observed_healthy': state = 'source_heartbeat_gap'
                sources[-1]['health'] = state
            if state != 'observed_healthy': failures[source] = state
        started = meta.get('started_at', stamp)
        streak = meta.get('streak_started_at')
        if failures:
            streak = None
        elif not streak:
            streak = stamp
        continuous = (now-utc(streak)).total_seconds() if streak else 0
        summary = dict(checked_at=stamp,status='interrupted' if failures else 'observing',started_at=started,
                       streak_started_at=streak,continuous_seconds=continuous,required_seconds=REQUIRED_SECONDS,
                       operational_qualified=continuous >= REQUIRED_SECONDS,investment_qualified=False,live_approved=False,
                       operational_scope='observer_and_current_crypto_paper_experiment',
                       asset_operational_qualified={'crypto':continuous >= REQUIRED_SECONDS,'equities':False,'options':False},
                       cashflow_adjusted_pnl_available=False,source_quality_qualification=False,
                       asset_status={'crypto':'paper_experiment','equities':'research_only','options':'research_only'},sources=sources)
        with self.db:
            for source, reason in failures.items():
                if not self.db.execute('SELECT 1 FROM gaps WHERE source=? AND ended_at IS NULL',(source,)).fetchone():
                    gap_start = meta.get('last_poll', stamp) if source == 'collector' else stamp
                    self.db.execute('INSERT INTO gaps(source,started_at,reason) VALUES(?,?,?)',(source,gap_start,reason))
            for source in {'collector', *SOURCES}-failures.keys():
                self.db.execute('UPDATE gaps SET ended_at=? WHERE source=? AND ended_at IS NULL',(stamp,source))
            for row in sources:
                if row['heartbeat_at']:
                    self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',(row['source'],row['heartbeat_at'],stamp,json.dumps(sanitize(row['source'],raw[row['source']],options),allow_nan=False)))
            summary['gap_count'] = self.db.execute('SELECT COUNT(*) FROM gaps').fetchone()[0]
            self.db.execute('INSERT INTO polls VALUES(?,?)',(stamp,json.dumps(summary,allow_nan=False)))
            for key,value in {'started_at':started,'last_poll':stamp,'streak_started_at':streak or ''}.items():
                self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,value))
        return summary
