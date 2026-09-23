"""Bounded recovery decisions only: this module never places or cancels orders."""
from dataclasses import dataclass
import math
import threading


@dataclass(frozen=True)
class RecoveryDecision:
    freeze_entries: bool
    preserve_protection: bool
    retry_quote: bool
    reconcile_orders: bool
    escalate: bool
    drain_scope: str | None
    reason: str
    deadline: float | None = None


class QuoteRecoveryPolicy:
    """Process-local coordinator. Startup must reconcile before enabling entries.

    A caller supplies fresh independent protection verification; cached belief
    is not verification. All incident state is shared across the account.
    Restart does not imply recovery: persistent execution/account owners must
    restore or reconcile before using this policy. A healthy quote cannot clear
    a broker/account incident; explicit reconciliation is required.
    """
    def __init__(self, max_recovery_seconds=15):
        if not math.isfinite(max_recovery_seconds) or not 0 < max_recovery_seconds <= 15:
            raise ValueError('Recovery budget must be positive and no more than 15 seconds')
        self.budget = float(max_recovery_seconds)
        self.incidents = {}
        self.last_now = None
        self._lock = threading.RLock()

    @property
    def freeze_entries(self):
        with self._lock:
            return bool(self.incidents)

    def _time(self, now):
        if not math.isfinite(now) or (self.last_now is not None and now < self.last_now):
            raise ValueError('Recovery requires a nondecreasing monotonic clock')
        self.last_now = now

    def update(self, symbol, *, category, protection_verified, now):
        if not symbol or category not in ('data', 'order', 'account', 'healthy'):
            raise ValueError('Invalid incident identity or category')
        if type(protection_verified) is not bool:
            raise ValueError('Protection verification must be explicit')
        with self._lock:
            self._time(now)
            key = '__account__' if category == 'account' else symbol
            prior = self.incidents.get(key)
            if category == 'account':
                self.incidents[key] = {'category':'account','started':now,'deadline':None}
                return RecoveryDecision(True,True,False,True,True,None,'account_reconciliation_required')
            if category == 'order' or not protection_verified:
                self.incidents[key] = {'category':'order','started':prior['started'] if prior else now,'deadline':None}
                return RecoveryDecision(True,True,False,True,True,None,'protection_or_order_unknown')
            if prior and prior['category'] in ('order','expired'):
                return RecoveryDecision(True,True,False,True,True,None,'explicit_reconciliation_required',prior['deadline'])
            if category == 'healthy':
                if prior and now >= prior['deadline']:
                    prior['category']='expired'
                    return RecoveryDecision(True,True,False,False,True,'symbol','quote_recovery_expired',prior['deadline'])
                self.incidents.pop(key,None)
                return RecoveryDecision(bool(self.incidents),True,False,False,False,None,'quote_recovered')
            if prior is None:
                prior = {'category':'data','started':now,'deadline':now+self.budget}
                self.incidents[key] = prior
            if now >= prior['deadline']:
                prior['category']='expired'
                return RecoveryDecision(True,True,False,False,True,'symbol','quote_recovery_expired',prior['deadline'])
            return RecoveryDecision(True,True,True,False,False,None,'protected_quote_retry',prior['deadline'])

    def reconcile(self, symbol, *, ownership_verified, protection_verified, flat=False, now):
        """Clear only after explicit broker reconciliation, never on quote alone.

        For account incidents symbol must be '__account__'; ownership_verified
        means reconciliation of the entire account's positions and orders.
        """
        if any(type(v) is not bool for v in (ownership_verified,protection_verified,flat)):
            raise ValueError('Reconciliation evidence must be explicit')
        with self._lock:
            self._time(now)
            if not ownership_verified or not (flat or protection_verified):
                self.incidents[symbol] = {'category':'order','started':now,'deadline':None}
                return RecoveryDecision(True,True,False,True,True,None,'reconciliation_incomplete')
            self.incidents.pop(symbol,None)
            return RecoveryDecision(bool(self.incidents),not flat,False,False,False,None,'reconciled')
