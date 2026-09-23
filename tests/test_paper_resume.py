"""Same-process cleanup must not weaken deliberately conservative restart recovery."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import pytest
from trader_engine.execution.lifecycle import EntryBlocked, PaperExecutor, TradePlan

class Journal:
    def __init__(self): self.rows = []; self.fail = False
    def append(self, row):
        if self.fail: raise OSError(28, 'disk full')
        self.rows.append(deepcopy(row))
    def records(self): return deepcopy(self.rows)

class Broker:
    telemetry_failed = False
    def __init__(self):
        self.orders = {}; self.sent = []; self.position = Decimal(0)
        self.read_failures = 0; self.fail_after_buy = False
        self.hidden_sell = None; self.hide_sell = False
        self.partial_sell = None; self.cancel_pending = False
    def open_orders(self):
        return [deepcopy(o) for o in self.orders.values()
                if o['status'] not in {'filled', 'canceled', 'rejected', 'expired'}]
    def position_qty(self, symbol): return self.position
    def get_order(self, client_id):
        if self.read_failures:
            self.read_failures -= 1
            raise ConnectionError('transient read failure')
        return deepcopy(self.orders.get(client_id))
    def submit(self, payload):
        self.sent.append(deepcopy(payload)); quantity = Decimal(payload['qty'])
        filled = quantity; status = 'filled'
        if payload['side'] == 'sell' and self.partial_sell is not None:
            filled = self.partial_sell; self.partial_sell = None; status = 'partially_filled'
        self.position += filled if payload['side'] == 'buy' else -filled
        order = {**payload, 'id': f'order-{len(self.sent)}', 'status': status,
                 'filled_qty': str(filled), 'filled_avg_price': '100'}
        self.orders[payload['client_order_id']] = order
        if payload['side'] == 'buy' and self.fail_after_buy:
            self.fail_after_buy = False; self.read_failures = 3
        if payload['side'] == 'sell' and self.hide_sell:
            self.hidden_sell = self.orders.pop(payload['client_order_id'])
            raise TimeoutError('accepted sell response lost')
        return deepcopy(order)
    def cancel(self, order_id):
        for order in self.orders.values():
            if order['id'] == order_id:
                order['status'] = 'pending_cancel' if self.cancel_pending else 'canceled'

def setup():
    broker, journal = Broker(), Journal()
    executor = PaperExecutor(broker, journal, polls=1, read_retries=3,
                             sleep=lambda _: None, poll_seconds=0)
    return broker, journal, executor, TradePlan.create('SPY', 10, '100.00')

def test_same_process_resumes_after_exhausted_reads_without_duplicate_entry():
    broker, journal, executor, plan = setup(); broker.fail_after_buy = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    assert len(broker.sent) == 1
    result = executor.resume_drain()
    assert result.status == 'flat' and result.sold == '10'
    assert broker.position == 0 and len(broker.sent) == 2
    with pytest.raises(EntryBlocked): executor.execute(TradePlan.create('QQQ', 10, '100.00'))

def test_unknown_sell_remains_unresolved_across_repeated_resume_attempts():
    broker, journal, executor, plan = setup(); broker.hide_sell = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    for _ in range(3): assert executor.resume_drain().status == 'needs_reconciliation'
    assert len(broker.sent) == 2

def test_later_visible_ambiguous_sell_does_not_submit_again():
    broker, journal, executor, plan = setup(); broker.hide_sell = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    broker.orders[plan.exit_ids[0]] = broker.hidden_sell
    assert executor.resume_drain().status == 'flat'
    assert len(broker.sent) == 2 and broker.position == 0

def test_terminal_partial_sell_resumes_only_remaining_quantity():
    broker, journal, executor, plan = setup()
    broker.partial_sell = Decimal(3); broker.cancel_pending = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    assert len(broker.sent) == 2 and broker.position == 7
    broker.cancel_pending = False
    result = executor.resume_drain()
    assert result.status == 'flat' and result.sold == '10'
    assert [p['qty'] for p in broker.sent] == ['10', '10', '7']

def test_disk_failure_and_stop_do_not_prevent_retained_cleanup():
    broker, journal, executor, plan = setup(); broker.fail_after_buy = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    journal.fail = True; executor.stop()
    result = executor.resume_drain()
    assert result.status == 'flat' and result.storage_failed
    assert broker.position == 0 and len(broker.sent) == 2

def test_restart_recover_cannot_enable_absent_exit_submission():
    broker, journal, executor, plan = setup(); broker.fail_after_buy = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    fresh = PaperExecutor(broker, journal, polls=1, sleep=lambda _: None)
    assert fresh.recover(plan).status == 'needs_reconciliation'
    assert fresh.resume_drain().status == 'needs_reconciliation'
    assert len(broker.sent) == 1 and broker.position == 10

def test_changed_active_plan_cannot_reuse_original_entry_identity():
    broker, journal, executor, plan = setup(); broker.fail_after_buy = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    altered = replace(plan, exit_ids=('foreign-exit',))
    assert executor.recover(altered).status == 'needs_reconciliation'
    assert executor.resume_drain().status == 'needs_reconciliation'
    assert len(broker.sent) == 1

@pytest.mark.parametrize('damage', ['attempt', 'requested', 'side', 'quantity'])
def test_original_entry_submission_state_is_required(damage):
    broker, journal, executor, plan = setup(); broker.fail_after_buy = True
    assert executor.execute(plan).status == 'needs_reconciliation'
    if damage == 'attempt': executor.attempted.clear()
    elif damage == 'requested': executor.requested.clear()
    elif damage == 'side': executor.requested[plan.entry_id]['side'] = 'sell'
    else: executor.requested[plan.entry_id]['qty'] = '9'
    assert executor.resume_drain().status == 'needs_reconciliation'
    assert len(broker.sent) == 1

def test_no_active_plan_returns_explicit_unresolved_without_broker_actions():
    broker, journal, executor, plan = setup(); result = executor.resume_drain()
    assert result.status == 'needs_reconciliation' and result.entry_id == ''
    assert executor.entries_blocked and not broker.sent

def test_completed_plan_cannot_sell_again():
    broker, journal, executor, plan = setup()
    assert executor.execute(plan).status == 'flat'
    assert executor.resume_drain().status == 'needs_reconciliation'
    assert len(broker.sent) == 2 and broker.position == 0
