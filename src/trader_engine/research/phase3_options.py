"""Pure O_ETF_TREND_CALL_V1 decisions, not fills, accounting or execution.

Inputs must come from an archived point-in-time dataset. Calendar hashes and
source declarations bind supplied evidence; they do not authenticate its origin.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
UNIVERSE = ('IWM', 'QQQ', 'SPY')
D = Decimal


class EvidenceError(ValueError):
    pass


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise EvidenceError('finite numeric evidence required')
    return result


def utc(stamp):
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise EvidenceError('aware timestamp required')
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class Session:
    day: date
    close_at: datetime


def calendar_digest(sessions):
    rows = [{'day': x.day.isoformat(), 'close_at': utc(x.close_at).isoformat()} for x in sessions]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class ArchivedCalendar:
    sessions: tuple[Session, ...]
    archived_sha256: str

    def __post_init__(self):
        days = [x.day for x in self.sessions]
        if not days or days != sorted(set(days)):
            raise EvidenceError('exact ordered session calendar required')
        if any(utc(x.close_at).astimezone(NY).date() != x.day for x in self.sessions):
            raise EvidenceError('session close date mismatch')
        if self.archived_sha256 != calendar_digest(self.sessions):
            raise EvidenceError('archived calendar hash mismatch')

    def index(self, day):
        for i, item in enumerate(self.sessions):
            if item.day == day:
                return i
        raise EvidenceError('session absent from archived calendar')

    def decision_at(self, day):
        session = self.sessions[self.index(day)]
        decision = datetime.combine(day, time(9, 35), NY).astimezone(timezone.utc)
        if decision >= utc(session.close_at):
            raise EvidenceError('invalid session decision boundary')
        return decision


@dataclass(frozen=True)
class DailyClose:
    symbol: str
    session: date
    total_return_close: Decimal
    received_at: datetime
    point_in_time: bool


@dataclass(frozen=True)
class Quote:
    symbol: str
    feed: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    event_at: datetime
    received_at: datetime


@dataclass(frozen=True)
class Contract:
    symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    listed_at: datetime
    metadata_received_at: datetime
    option_type: str
    multiplier: int
    standard_unadjusted: bool
    physically_delivered: bool
    active: bool


@dataclass(frozen=True)
class Position:
    contract: Contract
    entry_session: date
    entry_premium: Decimal
    quantity: int = 1
    pending_exit_reason: str | None = None


def _result(action, reason, **fields):
    return {'action': action, 'reason': reason, 'strategy_id': 'O_ETF_TREND_CALL_V1',
            'execution_authorized': False, 'fill_created': False,
            'investment_qualified': False, **fields}


def _valid_quote(quote, symbol, feed, at):
    if not isinstance(quote, Quote) or quote.symbol != symbol or quote.feed != feed:
        return False
    try:
        event, received, at = utc(quote.event_at), utc(quote.received_at), utc(at)
        bid, ask = number(quote.bid), number(quote.ask)
        return (event <= received <= at and 0 <= (at-event).total_seconds() <= 2
                and 0 < bid <= ask and number(quote.bid_size) >= 1 and number(quote.ask_size) >= 1
                and (ask-bid) / ((ask+bid)/2) <= D('.10'))
    except (ValueError, TypeError, ArithmeticError):
        return False


def modeled_prices(quote):
    """Per-share cost arithmetic, no claim any trade occurred or can be filled.

    Stress doubles the observed half-spread then adds 2% adverse premium impact
    to that stressed side. Base adds 1% to observed ask / subtracts 1% from bid.
    Fees are separately supplied currency per contract, never guessed.
    """
    bid, ask = number(quote.bid), number(quote.ask)
    if not 0 < bid <= ask:
        raise EvidenceError('positive uncrossed premium required')
    mid, half = (bid+ask)/2, (ask-bid)/2
    result = {'base_buy': ask * D('1.01'), 'base_sell': bid * D('.99'),
              'stress_buy': (mid+2*half)*D('1.02'), 'stress_sell': (mid-2*half)*D('.98')}
    if any(value <= 0 for value in result.values()):
        raise EvidenceError('stress requires positive prices')
    return result


def weekly_trend(calendar, execution_day, symbol, bars, *, as_of=None):
    """None outside first session after ISO-week end, else strict close > SMA200.

    The 200 bars must exactly cover preceding archived sessions, with receipt no
    earlier than each actual close and no later than the next 09:35 decision.
    """
    i = calendar.index(execution_day)
    at = calendar.decision_at(execution_day) if as_of is None else utc(as_of)
    if at != calendar.decision_at(execution_day):
        raise EvidenceError('weekly trend evaluated only at next 09:35')
    if i < 1:
        raise EvidenceError('previous session missing')
    previous = calendar.sessions[i-1]
    if previous.day.isocalendar()[:2] == execution_day.isocalendar()[:2]:
        return None
    if i < 200:
        raise EvidenceError('200-session warmup missing')
    required = calendar.sessions[i-200:i]
    required_days = {session.day for session in required}
    eligible = {}
    for bar in bars:
        if not isinstance(bar, DailyClose) or bar.symbol != symbol:
            raise EvidenceError('bar symbol or schema mismatch')
        # A later correction is not part of the information set at this
        # decision; it must not invalidate the signal that was available then.
        if bar.session not in required_days or utc(bar.received_at) > at:
            continue
        if bar.session in eligible:
            raise EvidenceError('duplicate daily bar')
        eligible[bar.session] = bar
    values = []
    for session in required:
        bar = eligible.get(session.day)
        if bar is None or bar.point_in_time is not True:
            raise EvidenceError('point-in-time warmup incomplete')
        received = utc(bar.received_at)
        if not utc(session.close_at) <= received <= at:
            raise EvidenceError('daily bar not causally available')
        value = number(bar.total_return_close)
        if value <= 0:
            raise EvidenceError('positive total return close required')
        values.append(value)
    return values[-1] > sum(values)/D(200)


def _valid_contract(contract, underlying, at, day):
    try:
        return (isinstance(contract, Contract) and bool(contract.symbol)
                and contract.underlying == underlying and contract.option_type == 'call'
                and type(contract.multiplier) is int and contract.multiplier == 100
                and contract.standard_unadjusted is True and contract.physically_delivered is True
                and contract.active is True and number(contract.strike) > 0
                and utc(contract.listed_at) <= utc(contract.metadata_received_at) <= at
                and 45 <= (contract.expiry-day).days <= 60)
    except (TypeError, ValueError, ArithmeticError):
        return False


def _positions(positions, calendar, day):
    seen = set(); total = D(0)
    for position in positions:
        if not isinstance(position, Position) or position.quantity != 1 or type(position.quantity) is not int:
            raise EvidenceError('exact one-contract owned positions required')
        contract = position.contract
        if (not isinstance(contract, Contract) or contract.option_type != 'call'
                or type(contract.multiplier) is not int or contract.multiplier != 100
                or contract.standard_unadjusted is not True or contract.physically_delivered is not True):
            raise EvidenceError('owned contract outside frozen standard-call scope')
        underlying = contract.underlying
        if underlying not in UNIVERSE or underlying in seen or position.entry_session > day:
            raise EvidenceError('invalid or duplicate underlying ownership')
        calendar.index(position.entry_session)
        premium = number(position.entry_premium)
        if premium <= 0:
            raise EvidenceError('positive original entry premium required')
        total += premium; seen.add(underlying)
    return seen, total


def plan_entries(calendar: ArchivedCalendar, session_date: date, *,
                 bars_by_underlying: Mapping[str, Sequence[DailyClose]],
                 sip_quotes: Mapping[str, Quote], contracts: Sequence[Contract],
                 opra_quotes: Mapping[str, Quote], positions: Sequence[Position],
                 equity, cash, fees_per_contract, utc_day_loss_fraction,
                 chain_complete: bool, fee_schedule_verified: bool,
                 owned_underlyings_at_signal: Sequence[str], utc_day_entry_halted: bool):
    """One ordered IWM/QQQ/SPY decision per call; reservations are returned only.

    Budget uses modeled base premium (including adverse impact) plus known fees.
    No cheaper-contract substitution if deterministic selected contract is costly.
    Portfolio accounting, actual fill lifecycle and delayed replay belong upstream.
    """
    at = calendar.decision_at(session_date)
    equity, cash, fee, loss = map(number, (equity, cash, fees_per_contract, utc_day_loss_fraction))
    if equity <= 0 or cash < 0 or fee < 0:
        raise EvidenceError('positive equity/nonnegative cash and verified fee required')
    if type(utc_day_entry_halted) is not bool:
        raise EvidenceError('explicit latched UTC-day entry halt required')
    owned, outstanding = _positions(positions, calendar, session_date)
    if (not isinstance(owned_underlyings_at_signal, (list, tuple))
            or any(not isinstance(s, str) or s not in UNIVERSE for s in owned_underlyings_at_signal)
            or len(set(owned_underlyings_at_signal)) != len(owned_underlyings_at_signal)):
        raise EvidenceError('explicit weekly-close ownership evidence required')
    if any(not isinstance(c, Contract) for c in contracts):
        raise EvidenceError('contract schema mismatch')
    names = [c.symbol for c in contracts]
    if len(names) != len(set(names)):
        raise EvidenceError('duplicate contract metadata')
    decisions = []
    for underlying in UNIVERSE:
        def skip(reason):
            decisions.append(_result('skip', reason, underlying=underlying, decision_at=at.isoformat()))
        if underlying in owned:
            skip('already_owned'); continue
        if underlying in owned_underlyings_at_signal:
            skip('owned_at_weekly_signal_close'); continue
        if utc_day_entry_halted or loss >= D('.01'):
            skip('utc_day_entry_halt'); continue
        if chain_complete is not True or fee_schedule_verified is not True:
            skip('chain_or_fee_evidence_unverified'); continue
        try:
            signal = weekly_trend(calendar, session_date, underlying, bars_by_underlying.get(underlying, ()))
        except (ValueError, ArithmeticError, TypeError):
            skip('daily_signal_evidence_missing'); continue
        if signal is None:
            skip('not_weekly_entry_session'); continue
        if not signal:
            skip('weekly_trend_not_positive'); continue
        sip = sip_quotes.get(underlying)
        if not _valid_quote(sip, underlying, 'sip', at):
            skip('underlying_quote_invalid'); continue
        midpoint = (number(sip.bid)+number(sip.ask))/2
        eligible = [c for c in contracts if _valid_contract(c, underlying, at, session_date)
                    and _valid_quote(opra_quotes.get(c.symbol), c.symbol, 'opra', at)]
        if not eligible:
            skip('no_qualifying_contract'); continue
        selected = min(eligible, key=lambda c: (abs((c.expiry-session_date).days-52), c.expiry,
                                               abs(number(c.strike)-midpoint), number(c.strike), c.symbol))
        prices = modeled_prices(opra_quotes[selected.symbol])
        premium = prices['base_buy']*100
        debit = premium + fee
        if debit > equity * D('.0025'):
            skip('selected_contract_unaffordable'); continue
        if outstanding + premium > equity * D('.0075') or len(owned) >= 3:
            skip('aggregate_premium_cap'); continue
        if debit > cash:
            skip('insufficient_cash'); continue
        decisions.append(_result('entry_candidate', 'weekly_trend', underlying=underlying,
                                 contract=selected.symbol, quantity=1, decision_at=at.isoformat(),
                                 modeled_prices={key: str(value) for key, value in prices.items()},
                                 base_premium=str(premium), base_fees=str(fee), planned_cash_debit=str(debit)))
        outstanding += premium; cash -= debit; owned.add(underlying)
    return decisions


def _latest(quotes, symbol, feed, at):
    candidates = [q for q in quotes if isinstance(q, Quote) and q.symbol == symbol and q.feed == feed
                  and utc(q.received_at) <= at]
    if not candidates:
        return None
    # Most recent market event, then receipt; an old out-of-order event cannot
    # replace a newer quote simply because it was received later.
    return max(candidates, key=lambda q: (utc(q.event_at), utc(q.received_at)))


def plan_exit(calendar: ArchivedCalendar, session_date: date, position: Position, *,
              bars: Sequence[DailyClose], option_quotes: Sequence[Quote],
              underlying_quotes: Sequence[Quote], fees_per_contract, fee_schedule_verified: bool):
    """Seek first valid causal quote from 09:35 through actual session close.

    Caller retains ownership and pending reason even for an exit_candidate: only
    the shared executor/accounting can record an actual closed episode.
    """
    decision = calendar.decision_at(session_date)
    _positions([position], calendar, session_date)
    close_at = utc(calendar.sessions[calendar.index(session_date)].close_at)
    fee = number(fees_per_contract)
    if fee < 0: raise EvidenceError('negative exit fee')
    holding = calendar.index(session_date)-calendar.index(position.entry_session)
    reason = position.pending_exit_reason
    if reason not in {None, 'holding_session_limit', 'weekly_trend_failed', 'expiry_proximity'}:
        raise EvidenceError('invalid pending exit reason')
    if reason is None and holding >= 10:
        reason = 'holding_session_limit'
    if reason is None and (position.contract.expiry-session_date).days <= 21:
        reason = 'expiry_proximity'
    if reason is None:
        try:
            trend = weekly_trend(calendar, session_date, position.contract.underlying, bars)
        except (ValueError, TypeError, ArithmeticError):
            return _result('hold', 'weekly_signal_evidence_missing', ownership_retained=True, coverage_failure=True)
        if trend is False:
            reason = 'weekly_trend_failed'
    if reason is None:
        return _result('hold', 'no_exit_signal', ownership_retained=True, coverage_failure=False)
    pending = dict(pending_exit_reason=reason, ownership_retained=True)
    if position.contract.expiry < session_date:
        return _result('pending_exit', 'expired_contract_requires_lifecycle_reconciliation', coverage_failure=True, **pending)
    if fee_schedule_verified is not True:
        return _result('pending_exit', 'fee_evidence_unverified', coverage_failure=True, **pending)
    observations = {decision}
    for quote in (*option_quotes, *underlying_quotes):
        if not isinstance(quote, Quote): raise EvidenceError('quote schema mismatch')
        received = utc(quote.received_at)
        if decision <= received <= close_at:
            observations.add(received)
    for at in sorted(observations):
        option = _latest(option_quotes, position.contract.symbol, 'opra', at)
        underlying = _latest(underlying_quotes, position.contract.underlying, 'sip', at)
        if _valid_quote(option, position.contract.symbol, 'opra', at) and _valid_quote(underlying, position.contract.underlying, 'sip', at):
            prices = modeled_prices(option)
            return _result('exit_candidate', reason, observed_at=at.isoformat(), quantity=1,
                           contract=position.contract.symbol, coverage_failure=False,
                           modeled_prices={key: str(value) for key, value in prices.items()},
                           base_fees=str(fee), modeled_net_proceeds=str(prices['base_sell']*100-fee), **pending)
    return _result('pending_exit', 'no_valid_quote_in_session', coverage_failure=True, **pending)
