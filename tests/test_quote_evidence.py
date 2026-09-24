"""Offline capture and synthetic incident categories; never broker connections."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
import pytest
from trader_engine.data.quote_validation import QuoteEnvelope, QuotePolicy, validate_quote
from trader_engine.execution.breakout import hold_breakout
from trader_engine.execution.journal import ReservedJournal
from trader_engine.execution.recovery import QuoteRecoveryPolicy

NOW = datetime(2026, 9, 23, 19, 15, tzinfo=timezone.utc)
LEGACY = QuotePolicy(expected_feed='iex', max_source_age_seconds=10,
                     max_cache_age_seconds=10, max_spread_bps=None, require_sizes=False)


def quote(**changes):
    return dict(t=NOW.isoformat(), bp=100, ap=100.01, bs=10, **{'as': 10}) | changes


def envelope(raw, symbol='XOM'):
    return QuoteEnvelope(symbol, 'iex', NOW.isoformat(), NOW.isoformat(), raw)


def test_direct_envelope_evidence_is_owned_and_roundtrips_policy():
    raw = quote(extra={'conditions': ['R']})
    result = validate_quote(envelope(raw), LEGACY)
    raw['bp'] = 999
    raw['extra']['conditions'].append('changed')
    record = json.loads(json.dumps(result.to_record(), allow_nan=False))
    assert record['envelope']['raw_payload']['bp'] == 100
    assert record['envelope']['raw_payload']['extra']['conditions'] == ['R']
    replay = validate_quote(QuoteEnvelope(**record['envelope']), QuotePolicy(**record['policy']))
    assert replay.valid and replay.reasons == result.reasons


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_raw_can_be_journaled_and_replayed(tmp_path, bad):
    result = validate_quote(envelope(quote(bp=bad)), LEGACY)
    with ReservedJournal(tmp_path/'quote.journal', capacity=2) as journal:
        journal.append(result.to_record())
        record = journal.records()[0]
    json.dumps(record, allow_nan=False)
    raw = json.loads(record['raw_payload_json'])
    assert not math.isfinite(raw['bp'])
    replay = validate_quote(QuoteEnvelope(**(record['envelope'] | {'raw_payload': raw})),
                            QuotePolicy(**record['policy']))
    assert replay.reasons == ('invalid_price',) == result.reasons


def test_expected_symbol_is_an_exact_rejection_reason():
    result = validate_quote(envelope(quote(), 'TJX'), LEGACY, expected_symbol='XOM')
    assert not result.valid and result.reasons == ('quote_symbol_mismatch',)


class Clock:
    seconds = 0.
    def now(self): return NOW + timedelta(seconds=self.seconds)
    def sleep(self, seconds): self.seconds += seconds


class Engine:
    stop_requested = entries_blocked = storage_failed = False
    def __init__(self, fail_capture=False, entry='100'):
        self.events = []
        self.fail_capture = fail_capture
        from decimal import Decimal, ROUND_FLOOR
        self.stop_price = str((Decimal(entry)*Decimal('.99')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
    def _send(self, *args, **kwargs): return self._lookup()
    def _lookup(self, *args):
        return dict(status='new', qty='10', filled_qty='0', stop_price=self.stop_price)
    def _event(self, event):
        self.events.append(event)
        if self.fail_capture: self.storage_failed = self.entries_blocked = True
    def stop(self): self.stop_requested = True


def holding(raw, *, recovery=False, fail_capture=False, entry='100'):
    timer = Clock(); engine = Engine(fail_capture, entry)
    reason = hold_breakout(SimpleNamespace(symbol='XOM', exit_ids=('stop',)),
        dict(filled_avg_price=entry, filled_qty='10'), engine, get_quote=lambda _: raw,
        close_at=NOW+timedelta(hours=1), now=timer.now, sleep=timer.sleep,
        monotonic=lambda:timer.seconds, max_hold_seconds=3,
        recovery_policy=QuoteRecoveryPolicy() if recovery else None)
    return reason, engine.events


@pytest.mark.parametrize('recovery', [False, True])
def test_every_successful_holding_quote_has_raw_decision_and_thresholds(recovery):
    raw = quote(bp=102.1, ap=102.11)
    reason, events = holding(raw, recovery=recovery)
    assert reason == 'profit_target'
    record = events[0]
    assert record['kind'] == 'holding_quote_accepted'
    assert record['decision'] == 'profit_target'
    assert record['envelope']['raw_payload'] == raw
    assert record['stop_price'] == '99.00' and record['reasons'] == ()


def test_legacy_holding_does_not_add_size_or_spread_requirements():
    reason, events = holding(dict(t=NOW.isoformat(), bp=100, ap=110))
    assert reason == 'time_exit'
    assert events[0]['valid'] and events[0]['policy']['max_spread_bps'] is None
    assert events[0]['policy']['require_sizes'] is False


def test_holding_raw_rejection_is_saved_before_raising():
    timer = Clock(); engine = Engine(); raw = quote(bp=101, ap=100)
    with pytest.raises(RuntimeError, match='crossed_quote'):
        hold_breakout(SimpleNamespace(symbol='XOM', exit_ids=('stop',)),
            dict(filled_avg_price='100', filled_qty='10'), engine, get_quote=lambda _:raw,
            close_at=NOW+timedelta(hours=1), now=timer.now, sleep=timer.sleep,
            monotonic=lambda:timer.seconds)
    assert engine.events[0]['envelope']['raw_payload'] == raw
    assert engine.events[0]['reasons'] == ('crossed_quote',)


def test_evidence_failure_prevents_using_target_quote():
    with pytest.raises(RuntimeError, match='evidence could not be persisted'):
        holding(quote(bp=103, ap=103.01), fail_capture=True)


def test_retrieval_errors_save_type_without_exception_message():
    timer = Clock(); engine = Engine()
    def unavailable(_): raise ConnectionError('secret-bearing upstream text')
    with pytest.raises(RuntimeError, match='quote_retrieval_failed'):
        hold_breakout(SimpleNamespace(symbol='XOM', exit_ids=('stop',)),
            dict(filled_avg_price='100', filled_qty='10'), engine, get_quote=unavailable,
            close_at=NOW+timedelta(hours=1), now=timer.now, sleep=timer.sleep,
            monotonic=lambda:timer.seconds)
    record = engine.events[0]
    assert record['envelope']['raw_payload'] is None
    assert record['retrieval_error'] == 'ConnectionError'
    assert 'secret-bearing' not in json.dumps(record)


def runner():
    path = Path(__file__).resolve().parents[1]/'scripts/paper_breakout_until_close.py'
    spec = importlib.util.spec_from_file_location('quote_evidence_runner', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_entry_and_submission_decisions_retain_same_payload_and_real_cache_age(tmp_path):
    module = runner(); raw = quote(); path = tmp_path/'quote_decisions.jsonl'
    save = lambda row:module.record_quote_decision(path, row)
    assert module.qualified_quote(raw, NOW, symbol='XOM', received_at=NOW.isoformat(), evidence=save)
    assert module.qualified_quote(raw, NOW+timedelta(seconds=6), symbol='XOM',
        received_at=NOW.isoformat(), evidence=save, phase='entry_submission') is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]['decision'] == 'accepted' and rows[1]['decision'] == 'rejected'
    assert rows[1]['source_age_seconds'] == rows[1]['cache_age_seconds'] == 6
    assert rows[1]['reasons'] == ['stale_source']
    assert rows[0]['envelope']['raw_payload'] == rows[1]['envelope']['raw_payload'] == raw


def test_entry_evidence_write_failure_raises_before_return(tmp_path):
    module = runner()
    def failure(row): raise OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        module.qualified_quote(quote(), NOW, symbol='TJX', evidence=failure)


def test_entry_signal_checks_have_specific_reason_and_preserve_legacy_clock_allowance():
    module = runner(); records=[]
    assert module.qualified_quote(quote(), NOW, evidence=records.append,
        signal_row={'breakout_level':'101', 'signal_close':'100'}) is None
    assert records[-1]['reasons'] == ['bid_not_above_breakout']
    # The old entry gate accepts once source time is no longer in the future.
    assert module.qualified_quote(quote(), NOW, received_at=(NOW-timedelta(seconds=.1)).isoformat(), evidence=records.append)
    assert records[-1]['valid'] and records[-1]['reasons'] == []
    assert 'event_after_receipt' in records[-1]['validation_reasons']


def test_named_incident_categories_are_explicitly_synthetic_and_reproducible():
    fixture = json.loads((Path(__file__).parent/'fixtures/quote_incidents.json').read_text())
    assert fixture['provenance'] == 'synthetic_category_reproduction'
    assert fixture['historical_rejected_payloads_available'] is False
    for case in fixture['cases']:
        assert case['provenance'] == 'synthetic_category_reproduction'
        e = QuoteEnvelope(case['symbol'], 'iex', case['received_at'], case['decision_at'], case['raw_payload'])
        result = validate_quote(e, LEGACY)
        assert list(result.reasons) == case['expected_reasons']
        if case['symbol'] == 'KLAC':
            # Only the known bid/entry/stop/exit facts are historical. A usable
            # synthetic timestamp and ask reproduce the local stop branch.
            raw = case['raw_payload'] | {'t':NOW.isoformat()}
            reason, events = holding(raw, entry=case['known_historical_fields']['entry_price'])
            assert reason == 'stop_level'
            assert events[0]['envelope']['raw_payload']['bp'] == 182.31
            assert events[0]['stop_price'] == '185.97'


def test_entry_retrieval_failure_preserves_only_class_and_status():
    module=runner(); rows=[]
    class Unauthorized(Exception): status=403
    def unavailable(): raise Unauthorized('credential-bearing upstream response')
    with pytest.raises(Unauthorized):
        module.fetch_entry_quotes(unavailable,('XOM','TJX'),evidence=rows.append,now=lambda:NOW)
    assert [r['symbol'] for r in rows] == ['XOM','TJX']
    assert all(r['retrieval_status']==403 and r['retrieval_error']=='Unauthorized' for r in rows)
    assert all('quote_retrieval_failed' in r['reasons'] for r in rows)
    assert 'credential-bearing' not in json.dumps(rows)


@pytest.mark.parametrize('capacity,extra', [(1,''),(10,'x'*5000)])
def test_real_journal_capacity_or_slot_failure_prevents_quote_use(tmp_path,capacity,extra):
    timer=Clock(); engine=Engine()
    with ReservedJournal(tmp_path/'bounded.journal',capacity=capacity) as journal:
        def persist(record):
            try:journal.append(record)
            except Exception:engine.storage_failed=engine.entries_blocked=True
        engine._event=persist
        raw=quote(extra=extra)
        def get_quote(_):return raw | {'t':timer.now().isoformat()}
        with pytest.raises(RuntimeError,match='evidence could not be persisted'):
            hold_breakout(SimpleNamespace(symbol='XOM',exit_ids=('stop',)),
                dict(filled_avg_price='100',filled_qty='10'),engine,get_quote=get_quote,
                close_at=NOW+timedelta(hours=1),now=timer.now,sleep=timer.sleep,
                monotonic=lambda:timer.seconds,max_hold_seconds=9)
        assert engine.storage_failed and engine.entries_blocked


def test_future_wait_decisions_are_captured_and_revalidated():
    raw=quote(t=(NOW+timedelta(seconds=.2)).isoformat(),bp=103,ap=103.01)
    reason,events=holding(raw)
    assert reason=='profit_target'
    assert events[0]['decision']=='wait_for_event'
    assert events[0]['source_age_seconds']<0
    assert events[1]['decision']=='profit_target' and events[1]['source_age_seconds']>=0
    assert events[0]['envelope']['raw_payload']==events[1]['envelope']['raw_payload']==raw
