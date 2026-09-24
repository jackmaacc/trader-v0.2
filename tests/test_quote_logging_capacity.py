"""Offline guard counts and conditional journal budget; no live transport."""
from collections import Counter
from datetime import timedelta
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse

import pytest

from trader_engine.execution.alpaca_paper import AlpacaPaperClient
from trader_engine.execution.breakout import hold_breakout
from trader_engine.execution.journal import JournalCapacity
from trader_engine.execution.lifecycle import PaperExecutor, TradePlan
from trader_engine.execution.portfolio import SymbolBroker
from test_paper_lifecycle import FakeBroker, MemoryJournal
from test_paper_support import FakeLimiter, FakeResponse, http_error
from test_quote_evidence import Clock, NOW, quote, runner


class LocalTransport:
    """Exercise real client._request against only an in-memory broker fixture."""
    def __init__(self, backend, *, reject_buy=False):
        self.backend = backend
        self.reject_buy = reject_buy
        self.posts = []

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        if request.method == 'POST':
            payload = json.loads(request.data)
            self.posts.append(payload)
            if self.reject_buy and payload['side'] == 'buy':
                raise http_error(429, {'Retry-After': '1'})
            return FakeResponse(self.backend.submit(payload))
        if request.method == 'DELETE':
            self.backend.cancel(parsed.path.rsplit('/', 1)[-1])
            return FakeResponse(None)
        if parsed.path == '/v2/orders:by_client_order_id':
            order = self.backend.get_order(parse_qs(parsed.query)['client_order_id'][0])
            if order is None:
                raise http_error(404)
            return FakeResponse(order)
        if parsed.path == '/v2/orders':
            return FakeResponse(self.backend.open_orders())
        if parsed.path.startswith('/v2/positions/'):
            symbol = parsed.path.rsplit('/', 1)[-1]
            return FakeResponse({'symbol': symbol, 'qty': str(self.backend.position_qty(symbol))})
        raise AssertionError('Unexpected request to offline transport')

    def close(self):
        pass


@pytest.mark.parametrize('scenario', ['accepted', 'post_timeout', 'delayed_lookup',
                                      'http_429', 'guard_rejects', 'guard_raises'])
def test_real_client_guard_runs_once_despite_lookup_and_drain_retries(monkeypatch, scenario):
    backend = FakeBroker()
    transport = LocalTransport(backend, reject_buy=scenario == 'http_429')
    monkeypatch.setattr('trader_engine.execution.alpaca_paper.build_opener', lambda *_: transport)
    if scenario in ('post_timeout', 'delayed_lookup'):
        backend.timeout_side = 'buy'
    if scenario == 'delayed_lookup':
        backend.lookup_delay = 2
        backend.read_failures = 2
    journal = MemoryJournal()
    module = runner()
    calls = []

    def guard():
        calls.append('guard')
        accepted = module.qualified_quote(quote(), NOW, symbol='SPY',
            phase='entry_submission', evidence=journal.append)
        if scenario == 'guard_raises':
            raise OSError('synthetic evidence failure')
        return accepted is not None and scenario != 'guard_rejects'

    with AlpacaPaperClient('offline-key', 'offline-secret', allow_orders=True,
                          limiter=FakeLimiter(), entry_guard=guard) as client:
        executor = PaperExecutor(SymbolBroker(client, 'SPY'), journal,
            polls=12, read_retries=3, sleep=lambda _: None, poll_seconds=0)
        result = executor.execute(TradePlan.create('SPY', 10, '100.00', entry_tif='day'))
        if result.status == 'needs_reconciliation':
            for _ in range(3):
                executor.resume_drain()
        assert calls == ['guard']
        assert sum(r['kind'] == 'entry_quote_decision' for r in journal.rows) == 1
        assert sum(p['side'] == 'buy' for p in transport.posts) == (
            0 if scenario in ('guard_rejects', 'guard_raises') else 1)
        assert result.status == ('not_submitted' if scenario == 'guard_rejects' else
                                 'needs_reconciliation' if scenario in ('guard_raises', 'http_429') else 'flat')


class ProtectiveBroker(FakeBroker):
    def submit(self, payload):
        if payload.get('type') == 'stop':
            self.partial_sell = Decimal(0)
        return super().submit(payload)


def run_holding(journal, timer, *, frozen_wall_clock=False):
    backend = ProtectiveBroker()
    clock_now = (lambda: NOW) if frozen_wall_clock else timer.now

    def holding(plan, entry, owner):
        hold_breakout(plan, entry, owner,
            get_quote=lambda _: quote(t=(clock_now() + timedelta(
                milliseconds=1 if frozen_wall_clock else 0)).isoformat()),
            close_at=NOW + timedelta(hours=1), now=clock_now,
            monotonic=lambda: timer.seconds, sleep=timer.sleep)

    executor = PaperExecutor(backend, journal, polls=12, read_retries=3,
                            sleep=timer.sleep, hold_callback=holding)
    # The runner adds these two records around execute; guard frequency is
    # exercised through the real client above, independently of this loop test.
    journal.append({'kind': 'sizing_quote'})
    journal.append({'kind': 'entry_quote_decision'})
    return executor.execute(TradePlan.create('SPY', 10, '100.00', entry_tif='day'))


def test_default_900_second_hold_record_count():
    journal = MemoryJournal()
    timer = Clock()
    result = run_holding(journal, timer)
    counts = Counter(row['kind'] for row in journal.rows)
    assert result.status == 'flat'
    assert counts['holding_quote_accepted'] == 300
    assert counts['entry_quote_decision'] == 1
    assert counts['order'] == 307
    assert len(journal.rows) == 615 < 2048


def test_future_quote_revalidation_preserves_polling_cadence_and_2048_record_budget():
    class BoundedMemoryJournal(MemoryJournal):
        def append(self, row):
            if len(self.rows) >= 2048:
                raise JournalCapacity('Synthetic journal record budget exhausted')
            super().append(row)

    journal = BoundedMemoryJournal()
    timer = Clock()
    result = run_holding(journal, timer, frozen_wall_clock=True)
    assert result.status == 'flat' and not result.storage_failed
    assert len(journal.rows) < 1000 < 2048
    assert timer.seconds >= 900
    assert sum(r['kind'] == 'entry_quote_decision' for r in journal.rows) == 1
