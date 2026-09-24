"""Frozen slower ETF hypotheses. Pure completed-session rules, no execution ledger."""
from dataclasses import dataclass
from math import isfinite
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import ETF_UNIVERSE

DAILY_CANDIDATES = ('H1', 'H2', 'H3', 'H4')

@dataclass(frozen=True)
class HoldingState:
    completed_sessions: int
    weight: float

@dataclass(frozen=True)
class DailyFeature:
    valid: bool
    reason: str
    close: float = 0.
    atr: float = 0.
    trend: bool = False
    score: float = 0.
    breakout: bool = False
    breakdown: bool = False
    volatility: float = 0.

@dataclass(frozen=True)
class DailyIntent:
    target_weight: float
    reason: str


def decision_due(candidate_id, session, next_session):
    if candidate_id not in DAILY_CANDIDATES:
        raise ValueError('Unknown frozen daily hypothesis')
    d, n = pd.Timestamp(session), pd.Timestamp(next_session)
    if n <= d:
        raise ValueError('Next actual session must follow decision session')
    if candidate_id == 'H3':
        return True
    if candidate_id == 'H2':
        return d.isocalendar()[:2] != n.isocalendar()[:2]
    return (d.year, d.month) != (n.year, n.month)


def daily_feature(candidate_id, history, sessions, decision_session):
    """Calendar labels are exchange dates. History must have causal split-adjusted OHLC.

    ``signal_scale`` converts that OHLC to the raw decision-session price scale.
    Future observations are discarded before validation and feature calculation.
    """
    required = {'H1': 200, 'H2': 127, 'H3': 64, 'H4': 61}[candidate_id]
    labels = pd.DatetimeIndex(pd.to_datetime(sessions)).normalize()
    decision = pd.Timestamp(decision_session).normalize()
    labels = labels[labels <= decision]
    if len(labels) < required or labels[-1] != decision:
        return DailyFeature(False, 'insufficient_calendar_history')
    if history.index.has_duplicates or not history.index.is_monotonic_increasing:
        raise ValueError('Daily observations must be ordered and unique')
    f = history.copy()
    f.index = pd.DatetimeIndex(pd.to_datetime(f.index)).normalize()
    if f.index.has_duplicates:
        raise ValueError('Duplicate exchange-session labels')
    f = f.reindex(labels[-required:])
    columns = ['high', 'low', 'close', 'total_return_close', 'signal_scale']
    if not set(columns).issubset(f):
        return DailyFeature(False, 'missing_signal_columns')
    values = f[columns].to_numpy(float)
    if not np.isfinite(values).all() or (values <= 0).any():
        return DailyFeature(False, 'missing_or_invalid_history')
    if (f.high < f.low).any() or (f.high < f.close).any() or (f.low > f.close).any():
        return DailyFeature(False, 'invalid_signal_ohlc')
    prev = f.close.shift()
    tr = pd.concat([f.high-f.low, (f.high-prev).abs(), (f.low-prev).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(20).mean() * f.signal_scale.iloc[-1])
    c = f.total_return_close
    vol = float(c.pct_change(fill_method=None).tail(60).std(ddof=1)) if candidate_id == 'H4' else 0.
    if not isfinite(atr) or atr <= 0 or (candidate_id == 'H4' and (not isfinite(vol) or vol <= 0)):
        return DailyFeature(False, 'invalid_atr_or_volatility')
    return DailyFeature(True, 'valid', float(f.close.iloc[-1] * f.signal_scale.iloc[-1]), atr,
        bool(c.iloc[-1] > c.tail(200).mean()) if candidate_id == 'H1' else False,
        float(c.iloc[-22] / c.iloc[-127] - 1) if candidate_id == 'H2' else 0.,
        bool(c.iloc[-1] > c.iloc[-64:-1].max()) if candidate_id == 'H3' else False,
        bool(c.iloc[-1] < c.iloc[-22:-1].min()) if candidate_id == 'H3' else False, vol)


def target_intents(candidate_id, features, holdings, *, blocked_symbols=()):
    """Only called on a scheduled decision close; protective/risk exits are external.

    Omitted symbols keep their existing quantities. A zero target requests an exit.
    Cooldown symbols cannot receive new or added exposure.
    """
    if candidate_id not in DAILY_CANDIDATES or set(features) != set(ETF_UNIVERSE):
        raise ValueError('Frozen candidate and complete five-ETF features required')
    blocked = set(blocked_symbols)
    out = {}
    if candidate_id in ('H2', 'H4') and not all(f.valid for f in features.values()):
        return out
    if candidate_id == 'H1':
        for s, f in features.items():
            if not f.valid: continue
            if s in holdings and not f.trend: out[s] = DailyIntent(0., 'trend_exit')
            elif s not in holdings and s not in blocked and f.trend: out[s] = DailyIntent(.05, 'trend_entry')
    elif candidate_id == 'H2':
        ranked = sorted(features, key=lambda s: (-features[s].score, s))
        remaining = set(holdings)
        for s, h in holdings.items():
            if (ranked.index(s) >= 3 or features[s].score <= 0) and h.completed_sessions >= 10:
                out[s] = DailyIntent(0., 'rotation_exit'); remaining.remove(s)
        vacant = max(0, 2-len(remaining))
        for s in ranked[:2]:
            if vacant and s not in holdings and s not in blocked and features[s].score > 0:
                out[s] = DailyIntent(.10, 'rotation_entry'); vacant -= 1
    elif candidate_id == 'H3':
        for s, f in features.items():
            if s in holdings:
                age = holdings[s].completed_sessions
                if age >= 63: out[s] = DailyIntent(0., 'maximum_hold_exit')
                elif f.valid and age >= 10 and f.breakdown: out[s] = DailyIntent(0., 'channel_exit')
            elif f.valid and s not in blocked and f.breakout:
                out[s] = DailyIntent(.05, 'breakout_entry')
    else:
        inverse = {s: 1/f.volatility for s, f in features.items()}
        total = sum(inverse.values())
        weights = {s: min(.10, .25*v/total) for s, v in inverse.items()}
        cluster = sum(weights[s] for s in ('SPY', 'QQQ', 'IWM'))
        if cluster > .20:
            for s in ('SPY', 'QQQ', 'IWM'): weights[s] *= .20/cluster
        for s, weight in weights.items():
            current = holdings[s].weight if s in holdings else 0.
            if s in blocked and weight > current: continue
            if s not in holdings or abs(weight-current) >= .02-1e-12:
                out[s] = DailyIntent(weight, 'volatility_rebalance')
    return out
