"""Pure research adapters. Estimates are not fills, orders, or broker authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Sequence
from zoneinfo import ZoneInfo

D = Decimal
EQUITIES = 'E_DONCHIAN20_V1'
CRYPTO = 'C_SMA200_CONFIRM_V1'
UNIVERSES = {EQUITIES: {'SPY', 'QQQ', 'IWM'}, CRYPTO: {'BTC/USD', 'ETH/USD'}}


def _number(value: Decimal, name: str, *, zero: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or (value < 0 if zero else value <= 0):
        raise ValueError(f'{name} requires finite {"nonnegative" if zero else "positive"} Decimal')
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware timestamps required')
    return value.astimezone(timezone.utc)


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


@dataclass(frozen=True)
class Session:
    day: date
    open_at: datetime
    close_at: datetime


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    day: date
    raw_close: Decimal
    total_return_close: Decimal | None
    received_at: datetime


@dataclass(frozen=True)
class SignalDecision:
    strategy_id: str
    symbol: str
    action: str
    reason: str
    signal_day: date
    execution_at: datetime
    raw_signal_price: Decimal | None
    held_quantity: Decimal
    pending_exit: bool
    decided_at: datetime


def decide_signal(strategy_id: str, symbol: str, bars: Sequence[DailyBar],
                  calendar: Sequence[Session], signal_day: date, decision_at: datetime,
                  *, held_quantity: Decimal = D('0'), pending_exit: bool = False) -> SignalDecision:
    """Use only a contiguous completed, available prefix. Calendar is caller evidence.

    Equity decisions may be computed after close through next09:20Eastern. Crypto
    decisions are exactly00:05UTC after the signal day. Late decisions skip entries;
    pending exits remain explicit even when market evidence is unavailable.
    """
    if strategy_id not in UNIVERSES or symbol not in UNIVERSES[strategy_id]:
        raise ValueError('Unsupported frozen strategy/universe')
    _number(held_quantity, 'held_quantity', zero=True)
    if type(pending_exit) is not bool:
        raise ValueError('pending_exit must be bool')
    now = _utc(decision_at)
    days = [s.day for s in calendar]
    if not days or days != sorted(set(days)) or signal_day not in days:
        raise ValueError('Calendar must have ordered unique dates including signal day')
    for i, session in enumerate(calendar):
        if _utc(session.open_at) >= _utc(session.close_at):
            raise ValueError('Invalid session times')
        if i and _utc(calendar[i-1].close_at) > _utc(session.open_at):
            raise ValueError('Overlapping calendar sessions')
        if strategy_id == CRYPTO:
            start = datetime.combine(session.day, time(), timezone.utc)
            if _utc(session.open_at) != start or _utc(session.close_at) != start + timedelta(days=1):
                raise ValueError('Crypto requires complete UTC days')
            if i and session.day != calendar[i-1].day + timedelta(days=1):
                raise ValueError('Crypto daily calendar gap')
    idx = days.index(signal_day)
    if idx + 1 >= len(calendar):
        raise ValueError('Next actual session required')
    next_session = calendar[idx+1]
    if strategy_id == EQUITIES:
        cutoff = datetime.combine(next_session.day, time(9,20), ZoneInfo('America/New_York')).astimezone(timezone.utc)
        execution = _utc(next_session.open_at)
        if execution <= cutoff:
            raise ValueError('Next regular open must follow data cutoff')
    else:
        cutoff = _utc(next_session.open_at) + timedelta(minutes=5)
        execution = cutoff
    def result(action: str, reason: str, price: Decimal | None = None) -> SignalDecision:
        return SignalDecision(strategy_id,symbol,action,reason,signal_day,execution,price,held_quantity,
                              held_quantity > 0 and (pending_exit or action == 'exit'),now)
    if pending_exit:
        if held_quantity == 0:
            raise ValueError('Pending exit requires owned quantity')
        return result('exit', 'pending_exit_preserved')
    if now < _utc(calendar[idx].close_at) or now > cutoff or (strategy_id == CRYPTO and now != cutoff):
        return result('skip', 'decision_outside_window')
    lookback = 21 if strategy_id == EQUITIES else 200
    if idx + 1 < lookback:
        return result('skip', 'insufficient_warmup')
    expected = days[idx+1-lookback:idx+1]
    eligible = {}
    for bar in bars:
        # Later bars/revisions do not affect this decision or invalidate its prefix.
        if bar.symbol != symbol or bar.day not in expected or _utc(bar.received_at) > now:
            continue
        if bar.day in eligible:
            raise ValueError('Ambiguous available duplicate/revision')
        session = calendar[days.index(bar.day)]
        if _utc(bar.received_at) < _utc(session.close_at):
            raise ValueError('Bar received before session completion')
        _number(bar.raw_close, 'raw_close')
        if strategy_id == EQUITIES:
            _number(bar.total_return_close, 'total_return_close')
        eligible[bar.day] = bar
    if any(day not in eligible for day in expected):
        return result('skip', 'missing_or_unavailable_completed_bar')
    ordered = [eligible[day] for day in expected]
    prices = [b.total_return_close if strategy_id == EQUITIES else b.raw_close for b in ordered]
    last = prices[-1]
    if strategy_id == EQUITIES:
        enter = last > max(prices[:-1])
        exit_signal = last < min(prices[-11:-1])
    else:
        enter = last > sum(prices,D('0'))/D(200)
        exit_signal = not enter
    action = ('exit' if exit_signal else 'hold') if held_quantity else ('enter' if enter else 'flat')
    return result(action, 'completed_signal', ordered[-1].raw_close)


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    cash: Decimal
    gross: Decimal
    prior_utc_close_equity: Decimal | None
    entry_halted: bool


@dataclass(frozen=True)
class FeeSchedule:
    """Verified buy fees: notional rate, cash fixed fee, and fee currency.

    For base-asset fees the percentage is deducted from acquired units. Fixed fees
    always use quote currency. Rates are fractions (0.0025 means25bps).
    """
    rate: Decimal
    fixed_cash: Decimal = D('0')
    currency: str = 'quote'


@dataclass(frozen=True)
class EntryPlan:
    decision: SignalDecision
    quantity: Decimal
    budget: Decimal
    step: Decimal
    fees: FeeSchedule
    reason: str


def _snapshot(snapshot: PortfolioSnapshot) -> None:
    if type(snapshot.entry_halted) is not bool:
        raise ValueError('Explicit latched UTC-day entry halt required')
    _number(snapshot.equity, 'equity')
    _number(snapshot.cash, 'cash', zero=True)
    _number(snapshot.gross, 'gross', zero=True)
    if snapshot.prior_utc_close_equity is not None:
        _number(snapshot.prior_utc_close_equity, 'prior_utc_close_equity')


def _fees(strategy: str, fees: FeeSchedule) -> FeeSchedule:
    _number(fees.rate, 'fee rate', zero=True)
    _number(fees.fixed_cash, 'fixed fee', zero=True)
    if fees.rate >= 1 or fees.currency not in ('quote','base'):
        raise ValueError('Unsupported fee model')
    if strategy == EQUITIES and fees.currency != 'quote':
        raise ValueError('Equity fees must use quote currency')
    return FeeSchedule(max(fees.rate,D('.0025')) if strategy == CRYPTO else fees.rate,
                       fees.fixed_cash,fees.currency)


def plan_entry(decision: SignalDecision, snapshot: PortfolioSnapshot,
               fees: FeeSchedule, *, quantity_step: Decimal) -> EntryPlan:
    """Freeze maximum quantity and cash budget at decision; no order is submitted."""
    if decision.strategy_id not in UNIVERSES or decision.symbol not in UNIVERSES[decision.strategy_id]:
        raise ValueError('Unsupported frozen strategy/universe')
    _snapshot(snapshot)
    _number(quantity_step,'quantity_step')
    if decision.strategy_id == EQUITIES and quantity_step != 1:
        raise ValueError('Whole shares required')
    fees = _fees(decision.strategy_id,fees)
    def blocked(reason: str) -> EntryPlan:
        return EntryPlan(decision,D(0),D(0),quantity_step,fees,reason)
    if decision.action != 'enter' or decision.held_quantity != 0 or decision.pending_exit:
        return blocked('not_a_flat_entry')
    if snapshot.entry_halted:
        return blocked('latched_daily_entry_halt')
    if snapshot.prior_utc_close_equity is None:
        return blocked('missing_utc_loss_baseline')
    equity_track = decision.strategy_id == EQUITIES
    loss_limit = D('.01') if equity_track else D('.03')
    if snapshot.equity <= snapshot.prior_utc_close_equity * (1-loss_limit):
        return blocked('daily_entry_halt')
    target, cap = (D('.08'),D('.24')) if equity_track else (D('.125'),D('.25'))
    price = _number(decision.raw_signal_price,'raw signal price')
    budget = max(D(0),min(target*snapshot.equity,cap*snapshot.equity-snapshot.gross,snapshot.cash))
    quantity = _floor(budget/price,quantity_step)
    return EntryPlan(decision,quantity,budget,quantity_step,fees,'planned' if quantity else 'insufficient_budget')


@dataclass(frozen=True)
class PriceObservation:
    event_at: datetime
    received_at: datetime
    raw_open: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass(frozen=True)
class EntryEstimate:
    quantity: Decimal
    acquired_quantity: Decimal
    price: Decimal | None
    cash_debit: Decimal
    base_fee: Decimal
    quote_fee: Decimal
    reason: str
    execution_authorized: bool = False


def estimate_entry(plan: EntryPlan, observation: PriceObservation | None,
                   snapshot: PortfolioSnapshot, now: datetime, *, stress: bool = False,
                   minimum_quantity: Decimal = D('0'), minimum_notional: Decimal = D('0')) -> EntryEstimate:
    """Only reduce planned quantity; return hypothetical buy economics, not a fill.

    Caller must retain original plan for delayed scenarios; use an explicitly dated
    replacement execution schedule outside this primitive after independent review.
    """
    _snapshot(snapshot)
    if type(stress) is not bool:
        raise ValueError('stress must be bool')
    if plan.decision.strategy_id not in UNIVERSES or plan.decision.symbol not in UNIVERSES[plan.decision.strategy_id]:
        raise ValueError('Unsupported frozen strategy/universe')
    _number(plan.quantity,'planned quantity',zero=True)
    _number(plan.budget,'planned budget',zero=True)
    _number(plan.step,'quantity step')
    if plan.decision.strategy_id==EQUITIES and plan.step!=1:
        raise ValueError('Whole shares required')
    if plan.fees != _fees(plan.decision.strategy_id,plan.fees):
        raise ValueError('Unnormalized frozen fee schedule')
    if plan.quantity>0 and (plan.decision.action!='enter' or plan.decision.held_quantity!=0 or plan.decision.pending_exit):
        raise ValueError('Nonzero entry plan requires flat entry intent')
    now = _utc(now)
    _number(minimum_quantity,'minimum_quantity',zero=True)
    _number(minimum_notional,'minimum_notional',zero=True)
    def skip(reason: str) -> EntryEstimate:
        return EntryEstimate(D(0),D(0),None,D(0),D(0),D(0),reason)
    if plan.quantity <= 0:
        return skip(plan.reason)
    if observation is None:
        return skip('missing_execution_observation')
    event,received = _utc(observation.event_at),_utc(observation.received_at)
    scheduled = _utc(plan.decision.execution_at)
    if event > received or received > now or now < scheduled or now < _utc(plan.decision.decided_at):
        return skip('unavailable_or_future_execution_observation')
    equities = plan.decision.strategy_id == EQUITIES
    if equities:
        if event != scheduled or event < _utc(plan.decision.decided_at):
            return skip('not_scheduled_raw_open')
        price = _number(observation.raw_open,'raw_open') * (1 + (D('.0014') if stress else D('.0007')))
        cap = D('.24')
    else:
        if not scheduled <= now <= scheduled+timedelta(seconds=60) or (now-event).total_seconds() > 2:
            return skip('outside_fresh_crypto_window')
        bid,ask = _number(observation.bid,'bid'),_number(observation.ask,'ask')
        if bid > ask:
            return skip('crossed_quote')
        price = ask*(1+(D('.0015') if stress else D('.0005')))
        cap = D('.25')
    limit = D('.01') if equities else D('.03')
    if snapshot.entry_halted:
        return skip('latched_daily_entry_halt')
    if snapshot.prior_utc_close_equity is None:
        return skip('missing_utc_loss_baseline')
    if snapshot.equity <= snapshot.prior_utc_close_equity*(1-limit):
        return skip('daily_entry_halt')
    budget=max(D(0),min(plan.budget,snapshot.cash,cap*snapshot.equity-snapshot.gross))
    fee=plan.fees
    available=max(D(0),budget-fee.fixed_cash)
    cash_unit=price*(1+fee.rate if fee.currency=='quote' else D(1))
    qty=min(plan.quantity,_floor(available/cash_unit,plan.step))
    if qty<=0 or qty<minimum_quantity or qty*price<minimum_notional:
        return skip('below_minimum_or_budget')
    base_fee=qty*fee.rate if fee.currency=='base' else D(0)
    quote_fee=fee.fixed_cash+(qty*price*fee.rate if fee.currency=='quote' else D(0))
    return EntryEstimate(qty,qty-base_fee,price,qty*price+quote_fee,base_fee,quote_fee,'hypothetical_only')


@dataclass(frozen=True)
class ExitEstimate:
    quantity: Decimal
    price: Decimal | None
    cash_credit: Decimal
    owned_units_debit: Decimal
    base_fee: Decimal
    quote_fee: Decimal
    pending_exit: bool
    reason: str
    execution_authorized: bool = False


def estimate_exit(decision: SignalDecision, observation: PriceObservation | None,
                  fees: FeeSchedule, now: datetime, *, quantity_step: Decimal,
                  stress: bool = False) -> ExitEstimate:
    """Estimate an owned-quantity sell; missing evidence preserves pending intent.

    Even a priced estimate keeps pending_exit true: only an external reconciled
    ledger can establish actual execution and clear ownership/pending state.
    """
    if decision.strategy_id not in UNIVERSES or decision.symbol not in UNIVERSES[decision.strategy_id]:
        raise ValueError('Unsupported frozen strategy/universe')
    _number(decision.held_quantity,'owned quantity')
    _number(quantity_step,'quantity_step')
    equities=decision.strategy_id==EQUITIES
    if equities and quantity_step != 1:
        raise ValueError('Whole shares required')
    if decision.action != 'exit' or not decision.pending_exit:
        raise ValueError('Exit intent required')
    if type(stress) is not bool:
        raise ValueError('stress must be bool')
    fee=_fees(decision.strategy_id,fees)
    now=_utc(now)
    def blocked(reason: str) -> ExitEstimate:
        return ExitEstimate(D(0),None,D(0),D(0),D(0),D(0),True,reason)
    if observation is None:
        return blocked('missing_execution_observation')
    event,received=_utc(observation.event_at),_utc(observation.received_at)
    scheduled=_utc(decision.execution_at)
    if event>received or received>now or now<scheduled or now<_utc(decision.decided_at):
        return blocked('unavailable_or_future_execution_observation')
    if equities:
        if event != scheduled or event < _utc(decision.decided_at):
            return blocked('not_scheduled_raw_open')
        price=_number(observation.raw_open,'raw_open')*(1-(D('.0014') if stress else D('.0007')))
    else:
        if not scheduled<=now<=scheduled+timedelta(seconds=60) or (now-event).total_seconds()>2:
            return blocked('outside_fresh_crypto_window')
        bid,ask=_number(observation.bid,'bid'),_number(observation.ask,'ask')
        if bid>ask:
            return blocked('crossed_quote')
        price=bid*(1-(D('.0015') if stress else D('.0005')))
    qty=_floor(decision.held_quantity/(1+fee.rate if fee.currency=='base' else D(1)),quantity_step)
    if qty<=0:
        return blocked('below_quantity_step')
    base_fee=qty*fee.rate if fee.currency=='base' else D(0)
    quote_fee=fee.fixed_cash+(qty*price*fee.rate if fee.currency=='quote' else D(0))
    if qty*price < quote_fee:
        return blocked('fees_exceed_proceeds')
    return ExitEstimate(qty,price,qty*price-quote_fee,qty+base_fee,base_fee,quote_fee,True,'hypothetical_only')
