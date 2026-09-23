"""Paper order lifecycle. Storage faults block entries, never reconciled exits.

No trading starts on import. This component executes one explicitly supplied
long-only plan at a time. A broker outage/process death cannot guarantee exits;
ambiguous mutations remain unresolved until the broker identifies them.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import re
import time
import uuid
from typing import Protocol
from trader_engine.execution.alpaca_paper import EntryNotSubmitted

TERMINAL = frozenset({'filled', 'canceled', 'expired', 'rejected'})

class ReconciliationRequired(RuntimeError):
    pass

class EntryBlocked(RuntimeError):
    pass

class Broker(Protocol):
    def get_order(self, client_id: str) -> dict | None: ...
    def submit(self, payload: dict) -> dict: ...
    def cancel(self, order_id: str) -> None: ...
    def position_qty(self, symbol: str) -> Decimal: ...
    def open_orders(self) -> list[dict]: ...


def number(value, *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ReconciliationRequired('Invalid broker quantity') from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ReconciliationRequired('Invalid broker quantity')
    return result


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    quantity: str
    limit_price: str
    entry_id: str
    exit_ids: tuple[str, ...]
    entry_tif: str = 'ioc'
    strategy_id: str = 'legacy'
    horizon: str = 'intraday'
    protective_stop_price: str | None = None

    @classmethod
    def create(cls, symbol, quantity, limit_price, *, entry_tif='ioc', strategy_id='legacy', horizon='intraday', protective_stop_price=None):
        tag = uuid.uuid4().hex[:24]
        return cls(symbol, str(quantity), str(limit_price), f'guard-{tag}-b',
                   tuple(f'guard-{tag}-s{i}' for i in range(3)), entry_tif, strategy_id, horizon,
                   str(protective_stop_price) if protective_stop_price is not None else None)

    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', self.strategy_id) or self.horizon not in ('intraday', 'swing'):
            raise ValueError('Valid strategy identity and horizon required')
        if self.horizon == 'swing':
            stop = number(self.protective_stop_price, positive=True)
            if stop >= number(self.limit_price) or stop != stop.quantize(Decimal('.01')):
                raise ValueError('Swing protection requires cent-rounded stop below entry limit')
        if self.entry_tif not in ('ioc','day'):
            raise ValueError('Unsupported entry time in force')
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', self.symbol):
            raise ValueError('Invalid equity symbol')
        qty, price = number(self.quantity, positive=True), number(self.limit_price, positive=True)
        if qty != qty.to_integral_value() or price < 1 or price != price.quantize(Decimal('.01')):
            raise ValueError('Whole shares and dollar prices with at most two decimals required')
        ids = (self.entry_id, *self.exit_ids)
        if not self.exit_ids or len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', x) for x in ids):
            raise ValueError('Unique bounded client IDs required')

    @classmethod
    def from_record(cls, record):
        return cls(**{**record, 'exit_ids': tuple(record['exit_ids'])})


@dataclass
class ExecutionResult:
    status: str
    entry_id: str
    acquired: str = '0'
    sold: str = '0'
    residual: str | None = None
    pnl: str | None = None
    storage_failed: bool = False
    reason: str | None = None


class PaperExecutor:
    def __init__(self, broker: Broker, journal, *, polls=8, read_retries=3,
                 sleep=time.sleep, poll_seconds=.4, hold_callback=None):
        if polls < 1 or read_retries < 1 or poll_seconds < 0:
            raise ValueError('Invalid reconciliation bounds')
        self.broker, self.journal = broker, journal
        self.hold_callback = hold_callback
        self.polls, self.read_retries = polls, read_retries
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self.storage_failed = False
        self.entries_blocked = False
        self.cache: dict[str, dict] = {}
        self.requested: dict[str, dict] = {}
        self.attempted: set[str] = set()
        self.uncertain: set[str] = set()
        self.active: TradePlan | None = None
        # Only execute() establishes in-process ownership; restart recovery does not.
        self._live_owned_plan: TradePlan | None = None
        self.completed: set[str] = set()
        self.stop_requested = False

    def stop(self):
        # Signal handlers set this flag; they must not raise during a POST.
        self.stop_requested = self.entries_blocked = True

    def _event(self, record):
        try:
            self.journal.append(record)
        except Exception:
            self.storage_failed = self.entries_blocked = True
        if getattr(self.broker, 'telemetry_failed', False):
            self.storage_failed = self.entries_blocked = True

    def _read(self, operation):
        for attempt in range(self.read_retries):
            try:
                return operation()
            except Exception:
                if attempt + 1 == self.read_retries:
                    raise ReconciliationRequired('Broker read unavailable; no replacement order sent') from None
                self.sleep(self.poll_seconds * (attempt + 1))

    def _validate(self, plan, client_id, order):
        expected_side = 'buy' if client_id == plan.entry_id else 'sell'
        if (order.get('client_order_id') != client_id or order.get('symbol') != plan.symbol
                or order.get('side') != expected_side or not order.get('id')):
            raise ReconciliationRequired('Broker order identity mismatch')
        requested = number(order.get('qty'), positive=True)
        filled = number(order.get('filled_qty'))
        expected = self.requested.get(client_id)
        if expected and requested != number(expected['qty']):
            raise ReconciliationRequired('Broker requested quantity differs from submitted quantity')
        if filled > requested or requested > number(plan.quantity):
            raise ReconciliationRequired('Broker order quantity exceeds plan')
        if expected_side == 'buy' and requested != number(plan.quantity):
            raise ReconciliationRequired('Entry quantity differs from plan')
        if order.get('status') == 'filled' and filled != requested:
            raise ReconciliationRequired('Filled status disagrees with quantity')
        if order.get('legs') or order.get('order_class') not in (None, '', 'simple'):
            raise ReconciliationRequired('Unexpected child orders require separate reconciliation')
        if plan.horizon == 'swing' and expected:
            for field in ('type', 'time_in_force', 'stop_price', 'limit_price'):
                if field in expected and (str(order.get(field)) != str(expected[field])):
                    # Numeric formatting differences are harmless; missing values are not.
                    if field not in ('stop_price', 'limit_price') or number(order.get(field)) != number(expected[field]):
                        raise ReconciliationRequired('Broker order semantics differ from durable intent')
        old = self.cache.get(client_id)
        if old and (old['id'] != order['id'] or number(old['filled_qty']) > filled):
            raise ReconciliationRequired('Broker order identity changed or fill quantity regressed')
        self.cache[client_id] = order  # Retain acknowledgment BEFORE any persistence.
        self.uncertain.discard(client_id)
        self._event({'kind': 'order', 'order': order})
        return order

    def _lookup(self, plan, client_id, *, priority="monitor"):
        from trader_engine.execution.broker_priority import lookup_order
        order = self._read(lambda: lookup_order(self.broker, client_id, priority=priority))
        if order is None and (client_id in self.attempted or client_id in self.cache):
            for _ in range(self.read_retries - 1):
                self.sleep(self.poll_seconds)
                order = self._read(lambda: lookup_order(self.broker, client_id, priority=priority))
                if order is not None:
                    break
        return None if order is None else self._validate(plan, client_id, order)

    def _send(self, plan, client_id, quantity, *, stop_price=None):
        if client_id in self.attempted:
            found = self._lookup(plan, client_id, priority="emergency")
            if found is None:
                raise ReconciliationRequired('Previously attempted order absent; refusing another POST')
            return found
        side = 'buy' if client_id == plan.entry_id else 'sell'
        if side == 'buy' and (self.entries_blocked or self.stop_requested):
            raise EntryBlocked('New entries disabled')
        payload = {'symbol': plan.symbol, 'side': side, 'qty': str(quantity),
                   'type': 'limit' if side == 'buy' else 'market',
                   'time_in_force': plan.entry_tif if side == 'buy' else ('gtc' if stop_price is not None and plan.horizon == 'swing' else 'day'),
                   'client_order_id': client_id}
        if stop_price is not None:
            if side != 'sell' or number(stop_price, positive=True) != number(stop_price).quantize(Decimal('.01')):
                raise ValueError('Protective stop requires a positive cent-rounded sell price')
            payload['type']='stop'; payload['stop_price']=str(stop_price)
        if side == 'buy':
            payload['limit_price'] = plan.limit_price
        if side == 'buy' or plan.horizon == 'swing':
            # Swing restart adoption requires every mutation intent durable.
            # Mandatory intent durability; failure means no exposure is accepted.
            try:
                self.journal.append({'kind': 'attempt', 'client_id': client_id, 'payload': payload})
            except Exception:
                self.storage_failed = self.entries_blocked = True
                if side == 'sell':
                    raise ReconciliationRequired('Swing exit intent could not be persisted') from None
                raise EntryBlocked('Entry intent could not be persisted') from None
        else:
            # Exit IDs were reserved in the durable plan. A failed write must
            # not prevent draining the confirmed owned quantity in this process.
            self._event({'kind': 'attempt', 'client_id': client_id, 'payload': payload})
        if side == 'buy' and (self.entries_blocked or self.stop_requested):
            raise EntryBlocked('Stop requested while recording entry intent')
        self.requested[client_id] = payload.copy()
        self.attempted.add(client_id)
        self.uncertain.add(client_id)
        try:
            acknowledgment = self.broker.submit(payload)
        except EntryNotSubmitted:
            self.attempted.discard(client_id); self.uncertain.discard(client_id)
            self.requested.pop(client_id,None)
            self._event({'kind':'not_submitted','client_id':client_id})
            raise
        except Exception:
            self.entries_blocked = True
            for _ in range(self.read_retries):
                found = self._lookup(plan, client_id, priority="emergency")
                if found is not None:
                    return found
                self.sleep(self.poll_seconds)
            raise ReconciliationRequired('Ambiguous submission; lookup pending, no POST retry') from None
        return self._validate(plan, client_id, acknowledgment)

    def _terminal(self, plan, order):
        cid = order['client_order_id']
        partial_entry = cid == plan.entry_id and number(order.get('filled_qty')) > 0
        immediate_cancel = (order.get('type') == 'stop' or partial_entry) and order.get('status') not in TERMINAL
        if immediate_cancel:
            try:
                self.broker.cancel(order['id'])
            except Exception:
                pass  # Reconcile cancel/fill race; never assume cancellation.
        for phase in range(1 if immediate_cancel else 2):
            for _ in range(self.polls):
                if order.get('status') in TERMINAL:
                    return order
                found = self._lookup(plan, cid, priority="emergency")
                if found is None:
                    raise ReconciliationRequired('Previously accepted order disappeared')
                order = found
                if (not immediate_cancel and cid == plan.entry_id
                        and order.get('status') not in TERMINAL
                        and number(order.get('filled_qty')) > 0):
                    # Stop accumulating unprotected exposure immediately. The
                    # recursive call cancels once, then reconciles final fills;
                    # it cannot take this branch again (immediate_cancel=True).
                    return self._terminal(plan, order)
                self.sleep(self.poll_seconds)
            if phase == 0 and not immediate_cancel:
                try:
                    self.broker.cancel(order['id'])
                except Exception:
                    pass  # Cancellation result must be reconciled either way.
        if order.get('status') in TERMINAL:
            return order
        raise ReconciliationRequired('Order still working after cancel; no overlapping exit')

    def _drain(self, plan, *, recovering=False, durable_attempts=None, allow_hold=False):
        entry = self._lookup(plan, plan.entry_id, priority="emergency")
        if entry is None:
            raise ReconciliationRequired('Prepared entry absent; submission state not proven')
        entry = self._terminal(plan, entry)
        acquired = number(entry['filled_qty'])
        if (allow_hold and plan.horizon == 'swing' and acquired > 0
                and not self.storage_failed and not self.entries_blocked and not self.stop_requested):
            self._send(plan, plan.exit_ids[0], acquired, stop_price=number(plan.protective_stop_price))
            snapshot = self.verify_protection(plan, priority="emergency")
            if self.storage_failed:
                raise ReconciliationRequired('Storage failed while establishing swing protection')
            return ExecutionResult('holding_protected', plan.entry_id, str(acquired),
                                   str(snapshot.sold), str(snapshot.residual))
        if (allow_hold and self.hold_callback and acquired > 0 and not self.storage_failed
                and not self.entries_blocked and not self.stop_requested):
            self.hold_callback(plan, entry, self)
        if plan.horizon == 'swing':
            # Verify durability before removing persistent protection. Later disk
            # failures can still require reconciliation; never claim atomicity.
            self.journal.append({'kind': 'drain_requested', 'entry_id': plan.entry_id})
        exits = []
        absent = []
        # Reconcile ALL reserved IDs before another sell, including later slots.
        for cid in plan.exit_ids:
            found = self._lookup(plan, cid, priority="emergency")
            if found is None:
                if cid in self.attempted or cid in self.uncertain or (recovering and cid in (durable_attempts or set())):
                    raise ReconciliationRequired('An earlier exit may exist; no replacement sell')
                absent.append(cid)
            else:
                self.attempted.add(cid)
                exits.append(self._terminal(plan, found))
        sold = sum((number(x['filled_qty']) for x in exits), Decimal(0))
        if sold > acquired:
            raise ReconciliationRequired('Owned sells exceed owned buys')
        for cid in absent:
            residual = acquired - sold
            if residual == 0:
                break
            if recovering:
                raise ReconciliationRequired('Restart cannot prove an absent exit was never sent; manual reconciliation required')
            working = self._read(self.broker.open_orders)
            ids = {plan.entry_id, *plan.exit_ids}
            if any(x.get('symbol') == plan.symbol and x.get('client_order_id') not in ids for x in working):
                raise ReconciliationRequired('Foreign working order on owned symbol; no overlapping exit')
            held = number(self._read(lambda: self.broker.position_qty(plan.symbol)))
            if held != residual:
                raise ReconciliationRequired('Account position differs from owned residual; external activity or lag')
            # Every previous entry/exit is terminal; no potential overlap remains.
            sell = self._terminal(plan, self._send(plan, cid, residual))
            exits.append(sell)
            sold += number(sell['filled_qty'])
        residual = acquired - sold
        if residual != 0:
            raise ReconciliationRequired('Exit slots exhausted with residual exposure')
        ids = {plan.entry_id, *plan.exit_ids}
        for attempt in range(self.read_retries):
            held = number(self._read(lambda: self.broker.position_qty(plan.symbol)))
            working = self._read(self.broker.open_orders)
            if any(x.get('symbol') == plan.symbol and x.get('client_order_id') not in ids for x in working):
                raise ReconciliationRequired('Foreign working order remains on owned symbol')
            owned_working = any(x.get('client_order_id') in ids for x in working)
            if held == 0 and not owned_working:
                break
            if attempt + 1 == self.read_retries:
                reason = 'Broker position not yet flat' if held else 'Owned working orders remain'
                raise ReconciliationRequired(reason)
            # Filled order details can become visible before account/list caches.
            # Confirm again without submitting or canceling anything.
            self.sleep(self.poll_seconds * (attempt + 1))
        pnl = Decimal(0)
        try:
            for order in [entry, *exits]:
                qty = number(order['filled_qty'])
                if qty:
                    price = number(order.get('filled_avg_price'), positive=True)
                    pnl += qty * price * (1 if order['side'] == 'sell' else -1)
        except ReconciliationRequired:
            pnl = None  # Accounting gaps must not block risk reduction.
        self.completed.add(plan.entry_id)
        self.active = None
        return ExecutionResult('flat' if pnl is not None else 'flat_accounting_pending', plan.entry_id,
                               str(acquired), str(sold), '0', str(pnl) if pnl is not None else None,
                               self.storage_failed)

    def execute(self, plan):
        if self.active or plan.entry_id in self.completed or self.entries_blocked or self.stop_requested:
            raise EntryBlocked('Executor already owns a plan or new entries are disabled')
        # The runner must also hold the shared account lock and check account,
        # session clock and sizing before calling this method.
        if self._read(self.broker.open_orders) or number(self._read(lambda: self.broker.position_qty(plan.symbol))) != 0:
            raise EntryBlocked('Foreign orders or existing symbol exposure')
        if getattr(self.broker, 'telemetry_failed', False):
            self.storage_failed = self.entries_blocked = True
            raise EntryBlocked('Broker telemetry failed before entry')
        try:
            self.journal.append({'kind': 'plan', 'plan': asdict(plan)})
        except Exception:
            self.storage_failed = self.entries_blocked = True
            raise EntryBlocked('Recovery plan could not be persisted') from None
        self.active = plan  # Establish ownership before the entry POST.
        self._live_owned_plan = plan
        try:
            acknowledgment = self._send(plan, plan.entry_id, number(plan.quantity))
            if acknowledgment.get('status') not in TERMINAL and number(acknowledgment.get('filled_qty')) > 0:
                # The POST acknowledgment itself may reveal partial exposure;
                # do not wait for another GET before requesting cancellation.
                self._terminal(plan, acknowledgment)
            result = self._drain(plan, allow_hold=True)
        except EntryNotSubmitted:
            self.active=None; self.completed.add(plan.entry_id)
            result=ExecutionResult('not_submitted',plan.entry_id,residual='0',pnl='0',reason='Entry guard blocked before HTTP submission')
        except EntryBlocked:
            self.active = None
            raise
        except Exception as error:
            self.entries_blocked = True
            result = ExecutionResult('needs_reconciliation', plan.entry_id,
                                     storage_failed=self.storage_failed,
                                     reason=str(error))
        self._event({'kind': 'result', 'result': asdict(result)})
        result.storage_failed = self.storage_failed
        return result

    def resume_drain(self):
        """Continue cleanup using only this process's retained submission state.

        No new entry is ever submitted. A known active plan can use an unused
        reserved exit only after every existing/uncertain order is reconciled.
        Calling recover() on a fresh executor does not grant this capability.
        Even successful cleanup permanently disables further entries here.
        """
        self.entries_blocked = True
        plan = self.active
        entry_id = plan.entry_id if plan is not None else ''
        try:
            if plan is None or plan != self._live_owned_plan:
                raise ReconciliationRequired('No original in-process active plan; restart recovery required')
            requested = self.requested.get(plan.entry_id)
            if (plan.entry_id not in self.attempted or not requested
                    or requested.get('side') != 'buy'
                    or requested.get('symbol') != plan.symbol
                    or requested.get('client_order_id') != plan.entry_id
                    or number(requested.get('qty'), positive=True) != number(plan.quantity)):
                raise ReconciliationRequired('No retained original entry submission state')
            result = self._drain(plan)
        except Exception as error:
            result = ExecutionResult('needs_reconciliation', entry_id,
                                     storage_failed=self.storage_failed,
                                     reason=str(error))
        self._event({'kind': 'result', 'result': asdict(result)})
        result.storage_failed = self.storage_failed
        return result

    def verify_protection(self, plan, *, priority="monitor"):
        """Fresh broker reconciliation; does not cancel or submit orders."""
        from trader_engine.execution.ownership import protected_snapshot
        orders = {cid: self._lookup(plan, cid, priority=priority) for cid in (plan.entry_id, *plan.exit_ids)}
        return protected_snapshot(plan, orders, self._read(self.broker.open_orders),
                                  self._read(lambda: self.broker.position_qty(plan.symbol)))

    def adopt_protected(self, plan):
        """Adopt only durable swing ownership with current exact GTC coverage.

        Account-level lock remains caller-owned. No broker mutation occurs.
        All swing mutation intents are durable before POST, permitting later
        resume_drain to use only provably unused reserved exits.
        """
        from trader_engine.execution.ownership import restore_intents
        self.entries_blocked = True
        if self.active is not None:
            return ExecutionResult('needs_reconciliation', plan.entry_id, reason='Executor already owns a plan')
        try:
            requested = restore_intents(plan, self.journal.records())
            self.requested.update(requested)
            self.attempted.update(requested)
            self.uncertain.update(requested)
            # Every durable attempted ID must now resolve, including terminal exits.
            for cid in requested:
                if self._lookup(plan, cid, priority="emergency") is None:
                    raise ReconciliationRequired('Durable mutation absent; adoption refused')
            snapshot = self.verify_protection(plan, priority="emergency")
            if any(cid not in requested for cid in self.cache):
                raise ReconciliationRequired('Broker order has no durable mutation intent')
            if self.storage_failed:
                raise ReconciliationRequired('Ownership journal unavailable during adoption')
            self.journal.append({'kind': 'adopted', 'plan': asdict(plan)})
            self.active = self._live_owned_plan = plan
            return ExecutionResult('holding_protected', plan.entry_id, str(snapshot.acquired),
                                   str(snapshot.sold), str(snapshot.residual))
        except Exception as error:
            return ExecutionResult('needs_reconciliation', plan.entry_id,
                                   storage_failed=self.storage_failed, reason=str(error))

    def recover(self, plan):
        """Reconcile a prepared plan; never replay its entry or uncertain exit.

        The entire journal must validate. Corrupt/missing records require manual
        reconciliation; a hard process death during a storage outage is not
        advertised as automatically recoverable.
        """
        self.entries_blocked = True
        self.active = plan
        try:
            records = self.journal.records()
            if not any(r.get('kind') == 'plan' and TradePlan.from_record(r['plan']) == plan for r in records):
                raise ReconciliationRequired('No durable ownership plan')
            attempted = {r['client_id'] for r in records if r.get('kind') == 'attempt'}
            result = self._drain(plan, recovering=True, durable_attempts=attempted)
        except Exception as error:
            result = ExecutionResult('needs_reconciliation', plan.entry_id,
                                     storage_failed=self.storage_failed, reason=str(error))
        self._event({'kind': 'result', 'result': asdict(result)})
        result.storage_failed = self.storage_failed
        return result
