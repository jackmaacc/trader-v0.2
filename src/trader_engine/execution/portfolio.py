"""Account-wide reservations and symbol-scoped executors for a single paper owner."""
from decimal import Decimal
import threading

class Reservations:
    """Reservations stay charged until an exit is confirmed flat; never recycle uncertainty."""
    def __init__(self, cash):
        self.cash=Decimal(str(cash));self.active={};self.lock=threading.Lock()
        if not self.cash.is_finite() or self.cash<0:raise ValueError('Invalid cash')
    def snapshot(self):
        with self.lock:
            gross=sum((v['notional'] for v in self.active.values()),Decimal(0))
            reserved=sum((v['cash'] for v in self.active.values()),Decimal(0))
            return gross,max(Decimal(0),self.cash-reserved)
    def reserve(self,symbol,notional,cash):
        n,c=Decimal(str(notional)),Decimal(str(cash))
        if not n.is_finite() or not c.is_finite() or n<=0 or c<n:raise ValueError('Invalid reservation')
        with self.lock:
            if symbol in self.active:raise ValueError('Symbol already reserved')
            if sum((v['cash'] for v in self.active.values()),Decimal(0))+c>self.cash:raise ValueError('Insufficient unreserved cash')
            self.active[symbol]={'notional':n,'cash':c}
    def release_flat(self,symbol,pnl):
        p=Decimal(str(pnl))
        if not p.is_finite():raise ValueError('Unresolved P&L')
        with self.lock:
            if symbol not in self.active:raise ValueError('Unknown reservation')
            self.cash+=min(Decimal(0),p) # Gains are not assumed spendable before broker reconciliation.
            del self.active[symbol]

class SymbolBroker:
    """Scope only order-list reads; the coordinator owns the account and all symbols.

    Each worker has a distinct underlying client and guard. Foreign orders on its
    own symbol remain visible so lifecycle reconciliation still rejects overlap.
    """
    def __init__(self,broker,symbol):self.broker=broker;self.symbol=symbol
    def open_orders(self):return [o for o in self.broker.open_orders() if o.get('symbol')==self.symbol]
    def submit(self,payload):
        if payload.get('symbol')!=self.symbol:raise ValueError('Cross-symbol submission')
        return self.broker.submit(payload)
    def position_qty(self,symbol):
        if symbol!=self.symbol:raise ValueError('Cross-symbol position lookup')
        return self.broker.position_qty(symbol)
    def __getattr__(self,name):return getattr(self.broker,name)
