"""Durable swing ownership proof; broker reads must be performed under account lock."""
from dataclasses import dataclass
from decimal import Decimal
from .lifecycle import ReconciliationRequired, TradePlan, TERMINAL, number

WORKING_PROTECTION = frozenset({'new', 'accepted', 'partially_filled', 'held'})

@dataclass(frozen=True)
class ProtectedSnapshot:
    acquired: Decimal
    sold: Decimal
    residual: Decimal

def restore_intents(plan, records):
    if plan.horizon != 'swing':
        raise ReconciliationRequired('Only durable swing plans support protected adoption')
    plans = [TradePlan.from_record(r['plan']) for r in records if r.get('kind') == 'plan']
    if not plans or any(p != plan for p in plans):
        raise ReconciliationRequired('Missing or conflicting durable ownership plan')
    allowed = {plan.entry_id, *plan.exit_ids}
    intents = {}
    for row in records:
        if row.get('kind') != 'attempt':
            continue
        cid, payload = row['client_id'], row['payload']
        side = 'buy' if cid == plan.entry_id else 'sell'
        if (cid not in allowed or payload.get('client_order_id') != cid
                or payload.get('symbol') != plan.symbol or payload.get('side') != side):
            raise ReconciliationRequired('Durable intent conflicts with ownership')
        if cid in intents and intents[cid] != payload:
            raise ReconciliationRequired('Conflicting duplicate mutation intent')
        intents[cid] = payload
    if plan.entry_id not in intents:
        raise ReconciliationRequired('Missing durable entry attempt')
    return intents

def protected_snapshot(plan, orders, working, held):
    entry = orders.get(plan.entry_id)
    if not entry or entry['status'] not in TERMINAL:
        raise ReconciliationRequired('Entry must be terminal before protected adoption')
    acquired = number(entry['filled_qty'])
    exits = [o for cid, o in orders.items() if cid != plan.entry_id and o]
    sold = sum((number(o['filled_qty']) for o in exits), Decimal(0))
    residual = acquired - sold
    if residual <= 0 or number(held) != residual:
        raise ReconciliationRequired('Position differs from positive owned residual')
    live = [o for o in exits if o['status'] not in TERMINAL]
    if len(live) != 1:
        raise ReconciliationRequired('Exactly one working protective stop required')
    stop = live[0]
    if (stop.get('status') not in WORKING_PROTECTION or stop.get('type') != 'stop'
            or stop.get('time_in_force') != 'gtc'
            or number(stop.get('stop_price'), positive=True) != number(plan.protective_stop_price, positive=True)
            or number(stop['qty']) - number(stop['filled_qty']) != residual):
        raise ReconciliationRequired('Stop semantics or residual coverage mismatch')
    ids = {plan.entry_id, *plan.exit_ids}
    relevant = [o for o in working if o.get('symbol') == plan.symbol]
    if any(o.get('client_order_id') not in ids for o in relevant):
        raise ReconciliationRequired('Foreign order on owned symbol')
    if len(relevant) != 1 or relevant[0].get('id') != stop['id']:
        raise ReconciliationRequired('Open-order snapshot disagrees with protection')
    return ProtectedSnapshot(acquired, sold, residual)
