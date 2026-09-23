from decimal import Decimal
import errno
from io import BytesIO
import json
import os
from urllib.error import HTTPError, URLError
import pytest
from trader_engine.execution.alpaca_paper import AlpacaPaperClient, BrokerError, RollingRateLimiter, _NoRedirect
from trader_engine.execution.journal import ReservedJournal, ExecutionLock, JournalCapacity, JournalCorrupt, JournalError, JournalLocked

class FakeTime:
    def __init__(self): self.now = 0.0; self.sleeps = []
    def clock(self): return self.now
    def sleep(self, duration): self.sleeps.append(duration); self.now += duration

class FakeLimiter:
    def __init__(self): self.calls = []; self.delays = []
    def acquire(self, *, entry=False): self.calls.append(entry)
    def throttle(self, seconds): self.delays.append(seconds)

class FakeResponse:
    def __init__(self, data, status=200):
        self.status = status
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, size): return self.data[:size]

class FakeOpener:
    def __init__(self, *responses): self.responses = list(responses); self.requests = []; self.closed = False
    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, BaseException): raise result
        return result
    def close(self): self.closed = True

def client(*responses, **kwargs):
    result = AlpacaPaperClient('test-key', 'test-secret', limiter=FakeLimiter(), **kwargs)
    result._opener = FakeOpener(*responses)
    return result

def http_error(status, headers=None):
    return HTTPError('https://paper-api.alpaca.markets/v2/orders', status,
                     'sensitive server detail', headers or {}, BytesIO(b'sensitive body'))

ORDER = {'symbol': 'SPY', 'side': 'buy', 'qty': '3', 'type': 'market',
         'time_in_force': 'day', 'client_order_id': 'run-entry-1'}

def test_mutations_disabled_by_default():
    c = client()
    with pytest.raises(BrokerError, match='disabled'): c.submit(ORDER)
    with pytest.raises(BrokerError, match='disabled'): c.cancel('order-1')
    assert not c._opener.requests

def test_successful_post_survives_disk_failed_telemetry():
    def failed(_): raise OSError(errno.ENOSPC, 'disk full')
    c = client(FakeResponse({'id': 'accepted'}), allow_orders=True, telemetry=failed)
    assert c.submit(ORDER) == {'id': 'accepted'}
    assert c.telemetry_failed
    assert len(c._opener.requests) == 1
    assert c._opener.requests[0].full_url == 'https://paper-api.alpaca.markets/v2/orders'
    assert c.limiter.calls == [True]

def test_post_timeout_is_never_retried_and_sanitized():
    c = client(URLError('test-secret leaked in underlying error'), allow_orders=True)
    with pytest.raises(BrokerError) as exc: c.submit(ORDER)
    assert 'test-secret' not in str(exc.value)
    assert len(c._opener.requests) == 1

def test_only_genuine_404_means_order_missing():
    c = client(http_error(404), FakeResponse(None), http_error(500))
    assert c.get_order('run-entry-1') is None
    with pytest.raises(BrokerError): c.get_order('run-entry-1')
    with pytest.raises(BrokerError): c.get_order('run-entry-1')

def test_position_zero_only_on_genuine_404_and_validate_identity():
    c = client(http_error(404), FakeResponse({'symbol': 'SPY', 'qty': '2.125'}),
               FakeResponse(None), FakeResponse({'symbol': 'QQQ', 'qty': '4'}),
               FakeResponse({'symbol': 'SPY', 'qty': 'NaN'}))
    assert c.position_qty('SPY') == 0
    assert c.position_qty('SPY') == Decimal('2.125')
    for _ in range(3):
        with pytest.raises(BrokerError): c.position_qty('SPY')

def test_http_429_throttles_all_requests_and_never_retries():
    c = client(http_error(429, {'Retry-After': '12'}), allow_orders=True)
    with pytest.raises(BrokerError) as exc: c.submit(ORDER)
    assert exc.value.status == 429
    assert c.limiter.delays == [12.0]
    assert len(c._opener.requests) == 1
    assert 'sensitive' not in str(exc.value)

def test_redirects_are_not_followed():
    assert _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://hostile.invalid') is None
    c = client(http_error(302))
    with pytest.raises(BrokerError) as exc: c.account()
    assert exc.value.status == 302
    assert len(c._opener.requests) == 1

def test_reads_and_sells_use_recovery_capacity_and_close_disables_client():
    c = client(FakeResponse({'id': 'sold'}), FakeResponse([]),
               FakeResponse({'status': 'ACTIVE'}), FakeResponse({'is_open': True}),
               FakeResponse(b'', 204), FakeResponse([]), allow_orders=True)
    assert c.submit({**ORDER, 'side': 'sell'})['id'] == 'sold'
    assert c.open_orders() == []
    assert c.account()['status'] == 'ACTIVE'
    assert c.clock()['is_open']
    c.cancel('order-1')
    assert c.positions() == []
    assert c.limiter.calls == [False] * 6
    c.close()
    with pytest.raises(BrokerError, match='closed'): c.account()

@pytest.mark.parametrize('payload', [
    {**ORDER, 'qty': 'NaN'}, {**ORDER, 'qty': '0'},
    {**ORDER, 'symbol': 'https://hostile'}, {**ORDER, 'client_order_id': '../bad'},
])
def test_invalid_payload_never_hits_network(payload):
    c = client(allow_orders=True)
    with pytest.raises((ValueError, BrokerError)): c.submit(payload)
    assert not c._opener.requests

def test_truncated_order_listing_fails_closed():
    c = client(FakeResponse([{'id': str(i)} for i in range(500)]))
    with pytest.raises(BrokerError, match='truncated'): c.open_orders()

def test_limiter_spaces_requests_and_reserves_recovery_capacity():
    t = FakeTime()
    limiter = RollingRateLimiter(clock=t.clock, sleep=t.sleep)
    for _ in range(150): limiter.acquire(entry=True)
    assert t.now == pytest.approx(149 * .36)
    limiter.acquire(entry=False)
    assert t.now == pytest.approx(150 * .36)
    limiter.acquire(entry=True)
    assert t.now == pytest.approx(60.36)

def test_limiter_429_wait_shared_by_entry_and_recovery():
    t = FakeTime()
    limiter = RollingRateLimiter(clock=t.clock, sleep=t.sleep)
    limiter.acquire(entry=True)
    limiter.throttle(12)
    limiter.acquire(entry=False)
    assert t.now == pytest.approx(12)
    limiter.acquire(entry=True)
    assert t.now == pytest.approx(12.36)

def test_journal_roundtrip_preallocation_and_no_external_mutation(tmp_path):
    path = tmp_path / 'records.bin'
    record = {'type': 'intent', 'payload': {'qty': '3'}}
    with ReservedJournal(path, capacity=3) as j:
        assert path.stat().st_size == 4 * 4096
        j.append(record)
        record['payload']['qty'] = '99'
        view = j.records(); view[0]['payload']['qty'] = '100'
        assert j.records()[0]['payload']['qty'] == '3'
    with ReservedJournal(path, capacity=3) as j:
        assert j.records() == [{'type': 'intent', 'payload': {'qty': '3'}}]
        j.append({'type': 'terminal'})
        assert len(j.records()) == 2

def test_journal_existing_evidence_never_truncated(tmp_path):
    path = tmp_path / 'records.bin'
    path.write_bytes(b'corrupted original evidence')
    with pytest.raises(JournalCorrupt): ReservedJournal(path)
    assert path.read_bytes() == b'corrupted original evidence'

def test_journal_lifetime_lock(tmp_path):
    path = tmp_path / 'records.bin'
    with ReservedJournal(path, capacity=2):
        with pytest.raises(JournalLocked): ReservedJournal(path, capacity=2)
    with ReservedJournal(path, capacity=2) as j:
        assert j.records() == []

def test_journal_capacity_failure_is_sticky(tmp_path):
    with ReservedJournal(tmp_path / 'records.bin', capacity=1) as j:
        j.append({'one': 1})
        with pytest.raises(JournalCapacity): j.append({'two': 2})
        assert not j.usable
        with pytest.raises(JournalError): j.append({'three': 3})
        assert j.records() == [{'one': 1}]

def test_torn_append_fails_closed_after_restart(tmp_path, monkeypatch):
    path = tmp_path / 'records.bin'
    with ReservedJournal(path, capacity=3) as j:
        j.append({'good': True})
        real = os.pwrite
        count = [0]
        def fail_after_prefix(fd, content, offset):
            count[0] += 1
            if count[0] == 1: return real(fd, content[:17], offset)
            raise OSError(errno.ENOSPC, 'injected full disk')
        monkeypatch.setattr(os, 'pwrite', fail_after_prefix)
        with pytest.raises(OSError): j.append({'torn': True})
        assert not j.usable
        with pytest.raises(JournalError): j.append({'retry': True})
    monkeypatch.undo()
    with pytest.raises(JournalCorrupt): ReservedJournal(path, capacity=3)

def test_fsync_failure_marks_journal_unusable(tmp_path, monkeypatch):
    with ReservedJournal(tmp_path / 'records.bin', capacity=3) as j:
        def fail(_): raise OSError(errno.EIO, 'injected failed persistence')
        monkeypatch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError): j.append({'maybe_durable': True})
        assert not j.usable
        with pytest.raises(JournalError): j.append({'retry': True})

def test_checksum_corruption_and_gap_are_rejected(tmp_path):
    path = tmp_path / 'records.bin'
    with ReservedJournal(path, capacity=3) as j:
        j.append({'first': 1}); j.append({'second': 2})
    with path.open('r+b') as f:
        f.seek(4096 + 80); value = f.read(1)
        f.seek(4096 + 80); f.write(bytes([value[0] ^ 1]))
    with pytest.raises(JournalCorrupt): ReservedJournal(path, capacity=3)
    with path.open('r+b') as f:
        f.seek(4096); f.write(bytes(4096))
    with pytest.raises(JournalCorrupt, match='empty slot'): ReservedJournal(path, capacity=3)

def test_oversized_record_disables_journal_without_overwriting(tmp_path):
    with ReservedJournal(tmp_path / 'records.bin', capacity=2, slot_size=256) as j:
        j.append({'good': 1})
        with pytest.raises(JournalCapacity): j.append({'huge': 'x' * 300})
        assert not j.usable
        assert j.records() == [{'good': 1}]

def test_account_lock_no_truncation_and_exclusion(tmp_path):
    path = tmp_path / 'account.lock'; path.write_bytes(b'evidence')
    with ExecutionLock(path):
        with pytest.raises(JournalLocked): ExecutionLock(path)
        assert path.read_bytes() == b'evidence'
    with ExecutionLock(path): pass

def test_entry_guard_rechecked_after_limiter_wait():
    permitted = [True]
    c = client(allow_orders=True, entry_guard=lambda: permitted[0])
    def wait_then_stop(*, entry=False): permitted[0] = False
    c.limiter.acquire = wait_then_stop
    with pytest.raises(BrokerError, match='Entry stopped'): c.submit(ORDER)
    assert not c._opener.requests

def test_false_entry_guard_does_not_block_recovery_sell():
    c = client(FakeResponse({'id': 'exit'}), allow_orders=True, entry_guard=lambda: False)
    assert c.submit({**ORDER, 'side': 'sell'})['id'] == 'exit'

def test_bad_credential_format_does_not_echo_secret():
    with pytest.raises(ValueError) as exc:
        AlpacaPaperClient('test-key', 'test-secret\n')
    assert 'test-secret' not in str(exc.value)
