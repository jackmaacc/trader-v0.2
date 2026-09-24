from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D
import pytest
from trader_engine.research.phase3_options import (
    ArchivedCalendar, Session, DailyClose, Quote, Contract, Position, NY, EvidenceError,
    calendar_digest, weekly_trend, plan_entries, plan_exit, modeled_prices,
)


@pytest.fixture
def calendar():
    # Synthetic archived test sessions, explicitly not a production exchange calendar.
    days = []; day = date(2025, 1, 1)
    while day <= date(2026, 12, 31):
        if day.weekday() < 5 and day not in {date(2026, 4, 3), date(2026, 5, 25)}:
            days.append(Session(day, datetime.combine(day, time(16), NY)))
        day += timedelta(days=1)
    return ArchivedCalendar(tuple(days), calendar_digest(days))


def bars(calendar, day, symbol='SPY', positive=True):
    i = calendar.index(day)
    result = [DailyClose(symbol, s.day, D(100), s.close_at+timedelta(seconds=1), True) for s in calendar.sessions[i-200:i]]
    result[-1] = replace(result[-1], total_return_close=D(101 if positive else 99))
    return result


def quote(symbol, feed, at, bid='1.9', ask='2.0'):
    return Quote(symbol, feed, D(bid), D(ask), D(10), D(10), at-timedelta(seconds=1), at-timedelta(seconds=.5))


def contract(day, at, symbol='SPY_CALL', underlying='SPY', days=52, strike='100'):
    return Contract(symbol, underlying, day+timedelta(days=days), D(strike), at-timedelta(days=30), at-timedelta(days=1), 'call', 100, True, True, True)


def entries(calendar, day, **overrides):
    at = calendar.decision_at(day); c = contract(day, at)
    args = dict(bars_by_underlying={'SPY': bars(calendar, day)}, sip_quotes={'SPY': quote('SPY', 'sip', at, '99.9', '100.1')}, contracts=[c], opra_quotes={c.symbol: quote(c.symbol, 'opra', at)}, positions=[], equity=100000, cash=100000, fees_per_contract=1, utc_day_loss_fraction=0, chain_complete=True, fee_schedule_verified=True, owned_underlyings_at_signal=[], utc_day_entry_halted=False)
    args.update(overrides)
    return plan_entries(calendar, day, **args)[-1]


def test_weekly_positive_entry_is_not_fill(calendar):
    result = entries(calendar, date(2026, 3, 9))
    assert result['action'] == 'entry_candidate'
    assert result['quantity'] == 1 and D(result['planned_cash_debit']) == D('203')
    assert not result['execution_authorized'] and not result['fill_created']


def test_holiday_end_week_and_monday_holiday(calendar):
    assert weekly_trend(calendar, date(2026, 4, 6), 'SPY', bars(calendar, date(2026, 4, 6))) is True
    assert calendar.sessions[calendar.index(date(2026, 4, 6))-1].day == date(2026, 4, 2)
    assert weekly_trend(calendar, date(2026, 5, 26), 'SPY', bars(calendar, date(2026, 5, 26))) is True
    assert entries(calendar, date(2026, 3, 10))['reason'] == 'not_weekly_entry_session'


def test_dst_and_bad_calendar_hash(calendar):
    assert calendar.decision_at(date(2026, 3, 6)).hour == 14
    assert calendar.decision_at(date(2026, 3, 9)).hour == 13
    with pytest.raises(EvidenceError, match='hash'):
        ArchivedCalendar(calendar.sessions, 'bad')


@pytest.mark.parametrize('mutation', [
    lambda b, at: replace(b, received_at=at+timedelta(seconds=1)),
    lambda b, at: replace(b, received_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
    lambda b, at: replace(b, point_in_time=False),
])
def test_causal_daily_inputs(calendar, mutation):
    day = date(2026, 3, 9); bs = bars(calendar, day); bs[-1] = mutation(bs[-1], calendar.decision_at(day))
    assert entries(calendar, day, bars_by_underlying={'SPY': bs})['reason'] == 'daily_signal_evidence_missing'


def test_missing_warmup_and_flat_trend(calendar):
    day = date(2026, 3, 9)
    assert entries(calendar, day, bars_by_underlying={'SPY': bars(calendar, day)[1:]})['reason'] == 'daily_signal_evidence_missing'
    assert entries(calendar, day, bars_by_underlying={'SPY': bars(calendar, day, positive=False)})['reason'] == 'weekly_trend_not_positive'


def test_expiry_then_strike_deterministic_ties(calendar):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    cs = [contract(day, at, symbol='late', days=53, strike='100'), contract(day, at, symbol='high', days=51, strike='101'), contract(day, at, symbol='low', days=51, strike='99')]
    qs = {c.symbol: quote(c.symbol, 'opra', at) for c in cs}
    result = entries(calendar, day, contracts=list(reversed(cs)), opra_quotes=qs)
    assert result['contract'] == 'low'


@pytest.mark.parametrize('change', [
    {'listed_at': datetime(2030, 1, 1, tzinfo=timezone.utc)},
    {'metadata_received_at': datetime(2030, 1, 1, tzinfo=timezone.utc)},
    {'standard_unadjusted': False}, {'physically_delivered': False}, {'multiplier': 10}, {'option_type': 'put'},
])
def test_point_in_time_standard_contract_only(calendar, change):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    c = replace(contract(day, at), **change)
    assert entries(calendar, day, contracts=[c])['reason'] == 'no_qualifying_contract'


@pytest.mark.parametrize('change', [
    {'event_at': datetime(2020, 1, 1, tzinfo=timezone.utc)},
    {'received_at': datetime(2030, 1, 1, tzinfo=timezone.utc)},
    {'bid_size': D(0)}, {'ask_size': D(0)}, {'bid': D('2.1')}, {'ask': D(4)}, {'feed': 'indicative'},
])
def test_quote_evidence_rejections(calendar, change):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    q = replace(quote('SPY_CALL', 'opra', at), **change)
    assert entries(calendar, day, opra_quotes={'SPY_CALL': q})['reason'] == 'no_qualifying_contract'


def test_expensive_selected_contract_not_replaced_by_cheaper_one(calendar):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    cs = [contract(day, at), contract(day, at, symbol='cheap', strike='101')]
    qs = {'SPY_CALL': quote('SPY_CALL', 'opra', at, '3.0', '3.1'), 'cheap': quote('cheap', 'opra', at)}
    assert entries(calendar, day, contracts=cs, opra_quotes=qs)['reason'] == 'selected_contract_unaffordable'


def test_unknown_chain_fee_and_daily_halt(calendar):
    day = date(2026, 3, 9)
    assert entries(calendar, day, chain_complete=False)['reason'] == 'chain_or_fee_evidence_unverified'
    assert entries(calendar, day, fee_schedule_verified=False)['reason'] == 'chain_or_fee_evidence_unverified'
    assert entries(calendar, day, utc_day_loss_fraction='.01')['reason'] == 'utc_day_entry_halt'


def test_ordered_cash_reservations_without_mutation(calendar):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    cs = [contract(day, at, symbol=s+'_CALL', underlying=s) for s in ('SPY', 'IWM', 'QQQ')]
    args = dict(bars_by_underlying={s: bars(calendar, day, s) for s in ('SPY', 'IWM', 'QQQ')}, sip_quotes={s: quote(s, 'sip', at, '99.9', '100.1') for s in ('SPY', 'IWM', 'QQQ')}, contracts=cs, opra_quotes={c.symbol: quote(c.symbol, 'opra', at) for c in cs}, positions=[], equity=100000, cash=250, fees_per_contract=1, utc_day_loss_fraction=0, chain_complete=True, fee_schedule_verified=True, owned_underlyings_at_signal=[], utc_day_entry_halted=False)
    result = plan_entries(calendar, day, **args)
    assert [row['underlying'] for row in result] == ['IWM', 'QQQ', 'SPY']
    assert [row['action'] for row in result] == ['entry_candidate', 'skip', 'skip']
    assert args['cash'] == 250 and args['positions'] == []


def exit_case(calendar, day, *, holding=10, pending=None, positive=True, quotes=True):
    at = calendar.decision_at(day)
    c = contract(day, at)
    p = Position(c, calendar.sessions[calendar.index(day)-holding].day, D(202), pending_exit_reason=pending)
    kwargs = dict(bars=bars(calendar, day, positive=positive), option_quotes=[quote(c.symbol, 'opra', at)] if quotes else [], underlying_quotes=[quote('SPY', 'sip', at, '99.9', '100.1')], fees_per_contract=1, fee_schedule_verified=True)
    return p, kwargs


def test_ten_completed_holding_sessions_exit(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day)
    result = plan_exit(calendar, day, p, **kwargs)
    assert result['action'] == 'exit_candidate' and result['reason'] == 'holding_session_limit'
    assert result['ownership_retained'] and not result['fill_created']
    p = replace(p, entry_session=calendar.sessions[calendar.index(day)-9].day)
    assert plan_exit(calendar, day, p, **kwargs)['action'] == 'hold'


def test_weekly_trend_failure_and_expiry(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day, holding=3, positive=False)
    assert plan_exit(calendar, day, p, **kwargs)['reason'] == 'weekly_trend_failed'
    p = replace(p, contract=replace(p.contract, expiry=day+timedelta(days=21)))
    assert plan_exit(calendar, day, p, **kwargs)['reason'] == 'expiry_proximity'
    p = replace(p, contract=replace(p.contract, expiry=day-timedelta(days=1)))
    assert plan_exit(calendar, day, p, **kwargs)['action'] == 'pending_exit'


def test_missing_exit_preserves_pending_and_later_first_valid_quote(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day, quotes=False)
    result = plan_exit(calendar, day, p, **kwargs)
    assert result['action'] == 'pending_exit' and result['coverage_failure'] and result['ownership_retained']
    at = calendar.decision_at(day)+timedelta(hours=1)
    kwargs['option_quotes'] = [quote(p.contract.symbol, 'opra', at)]
    # Underlying's morning quote is stale at the later option observation.
    assert plan_exit(calendar, day, p, **kwargs)['action'] == 'pending_exit'
    kwargs['underlying_quotes'].append(quote('SPY', 'sip', at, '99.9', '100.1'))
    result = plan_exit(calendar, day, p, **kwargs)
    assert result['action'] == 'exit_candidate'
    assert datetime.fromisoformat(result['observed_at']) >= at-timedelta(seconds=.5)


def test_pending_exit_survives_recovered_trend(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day, holding=3, pending='weekly_trend_failed')
    assert plan_exit(calendar, day, p, **kwargs)['reason'] == 'weekly_trend_failed'


def test_price_arithmetic_only():
    at = datetime(2026, 3, 9, 13, 35, tzinfo=timezone.utc)
    result = modeled_prices(quote('C', 'opra', at))
    assert result == {'base_buy': D('2.020'), 'base_sell': D('1.881'), 'stress_buy': D('2.0910'), 'stress_sell': D('1.8130')}


def test_aggregate_premium_and_existing_ownership(calendar):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    previous = calendar.sessions[calendar.index(day)-1].day
    owned = [Position(contract(day, at, symbol=s+'_CALL', underlying=s), previous, D(300)) for s in ('IWM', 'QQQ')]
    assert entries(calendar, day, positions=owned)['reason'] == 'aggregate_premium_cap'
    owned.append(Position(contract(day, at), previous, D(200)))
    assert entries(calendar, day, positions=owned)['reason'] == 'already_owned'


def test_expiry_bounds_are_inclusive(calendar):
    day = date(2026, 3, 9); at = calendar.decision_at(day)
    for days in (45, 60):
        assert entries(calendar, day, contracts=[contract(day, at, days=days)])['action'] == 'entry_candidate'
    for days in (44, 61):
        assert entries(calendar, day, contracts=[contract(day, at, days=days)])['reason'] == 'no_qualifying_contract'


def test_exit_does_not_use_future_or_after_close_quotes(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day)
    close = calendar.sessions[calendar.index(day)].close_at
    kwargs['option_quotes'] = [quote(p.contract.symbol, 'opra', close+timedelta(seconds=10))]
    kwargs['underlying_quotes'] = [quote('SPY', 'sip', close+timedelta(seconds=10), '99.9', '100.1')]
    assert plan_exit(calendar, day, p, **kwargs)['action'] == 'pending_exit'


def test_signal_close_ownership_prevents_same_session_reentry(calendar):
    day = date(2026, 3, 9)
    assert entries(calendar, day, owned_underlyings_at_signal=['SPY'])['reason'] == 'owned_at_weekly_signal_close'


def test_bad_owned_contract_cannot_assume_multiplier(calendar):
    day = date(2026, 3, 9); p, kwargs = exit_case(calendar, day)
    p = replace(p, contract=replace(p.contract, multiplier=10))
    with pytest.raises(EvidenceError, match='standard-call'):
        plan_exit(calendar, day, p, **kwargs)


def test_latched_utc_day_halt_survives_recovered_equity(calendar):
    day = date(2026, 3, 9)
    assert entries(calendar, day, utc_day_loss_fraction='.011')['reason'] == 'utc_day_entry_halt'
    assert entries(calendar, day, utc_day_loss_fraction='-.002', utc_day_entry_halted=True)['reason'] == 'utc_day_entry_halt'
    assert entries(calendar, day, utc_day_loss_fraction='-.002', utc_day_entry_halted=False)['action'] == 'entry_candidate'
    with pytest.raises(EvidenceError, match='latched'):
        entries(calendar, day, utc_day_entry_halted=None)


def test_future_correction_preserves_causal_signal_prefix(calendar):
    day = date(2026, 3, 9); original = bars(calendar, day)
    corrected = replace(original[-1], total_return_close=D(1), received_at=calendar.decision_at(day)+timedelta(seconds=1))
    baseline = entries(calendar, day, bars_by_underlying={'SPY': original})
    assert entries(calendar, day, bars_by_underlying={'SPY': [corrected, *original]}) == baseline
    assert entries(calendar, day, bars_by_underlying={'SPY': [*original, corrected]}) == baseline
    available_correction = replace(corrected, received_at=calendar.decision_at(day))
    assert entries(calendar, day, bars_by_underlying={'SPY': [*original, available_correction]})['reason'] == 'daily_signal_evidence_missing'


def test_outside_warmup_duplicate_is_irrelevant(calendar):
    day = date(2026, 3, 9); original = bars(calendar, day)
    old_session = calendar.sessions[calendar.index(day)-201]
    old = DailyClose('SPY', old_session.day, D(10), old_session.close_at+timedelta(seconds=1), True)
    assert entries(calendar, day, bars_by_underlying={'SPY': [old, old, *original]}) == entries(calendar, day)
