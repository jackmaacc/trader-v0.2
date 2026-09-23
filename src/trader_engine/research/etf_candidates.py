"""Causal, pure candidate decisions; input history includes completed observations only."""
from dataclasses import dataclass
from math import isfinite
import numpy as np
import pandas as pd
from trader_engine.research.strategy_spec import StrategySpec

@dataclass(frozen=True)
class ETFDecision:
    eligible: bool
    reason: str
    score: float = 0.
    atr: float = 0.
    target: float | None = None
    signal_time: object = None


def mean_reversion_decision(history: pd.DataFrame, spec: StrategySpec) -> ETFDecision:
    if history.empty:
        return ETFDecision(False, 'insufficient_history')
    now = pd.Timestamp(history.index[-1]) + pd.Timedelta(minutes=1)
    if now.tzinfo is None:
        raise ValueError('Timezone-aware completed bar times required')
    local = now.tz_convert('America/New_York')
    minute = local.hour * 60 + local.minute
    if not 630 <= minute <= 870:
        return ETFDecision(False, 'outside_signal_window', signal_time=now)
    session = history.loc[history.index.tz_convert('America/New_York').date == local.date()]
    if len(session) < 31:
        return ETFDecision(False, 'insufficient_history', signal_time=now)
    tail = session.tail(31)
    if (session.index.to_series().diff().dropna() != pd.Timedelta(minutes=1)).any():
        return ETFDecision(False, 'missing_minute', signal_time=now)
    values = session[['high', 'low', 'close', 'volume']].to_numpy(float)
    if not np.isfinite(values).all() or (values[:, :3] <= 0).any() or (values[:, 3] < 0).any():
        return ETFDecision(False, 'invalid_observation', signal_time=now)
    close = session.close
    volume = float(session.volume.sum())
    sigma = float(np.log(close.tail(30)).std(ddof=1))
    path = float(close.tail(31).diff().abs().sum())
    if volume <= 0 or sigma <= 0 or path <= 0:
        return ETFDecision(False, 'zero_denominator', signal_time=now)
    vwap = float((close * session.volume).sum() / volume)
    z = float((np.log(close.iloc[-1]) - np.log(vwap)) / sigma)
    efficiency = abs(float(close.iloc[-1] - close.iloc[-31])) / path
    prev = close.shift()
    atr = float(pd.concat([session.high-session.low, (session.high-prev).abs(),
                           (session.low-prev).abs()], axis=1).max(axis=1).tail(14).mean())
    eligible = z <= -2 and close.iloc[-1] > close.iloc[-2] and efficiency <= .25 and atr > 0
    return ETFDecision(bool(eligible), 'qualified' if eligible else 'signal_filter', -z, atr, vwap, now)


def momentum_decision(completed_daily: pd.DataFrame, spec: StrategySpec) -> ETFDecision:
    required = {'high', 'low', 'close', 'total_return_close'}
    if not required.issubset(completed_daily.columns):
        return ETFDecision(False, 'total_return_or_adjusted_history_missing')
    if len(completed_daily) < max(spec.horizon + 1, 21):
        return ETFDecision(False, 'insufficient_history')
    f = completed_daily.tail(max(spec.horizon + 1, 21))
    if not np.isfinite(f[list(required)].to_numpy(float)).all() or (f[list(required)] <= 0).any().any():
        return ETFDecision(False, 'invalid_observation')
    tr = f.total_return_close
    ret = float(tr.iloc[-1] / tr.iloc[-1-spec.horizon] - 1)
    vol = float(tr.pct_change(fill_method=None).tail(20).std(ddof=1))
    previous = f.close.shift()
    atr = float(pd.concat([f.high-f.low, (f.high-previous).abs(), (f.low-previous).abs()], axis=1).max(axis=1).tail(14).mean())
    scale = float(f.iloc[-1].get('signal_scale', 1.))
    if ret <= 0:
        return ETFDecision(False, 'nonpositive_momentum', signal_time=f.index[-1])
    if not isfinite(scale) or scale <= 0 or not isfinite(vol) or vol <= 0 or atr <= 0:
        return ETFDecision(False, 'zero_denominator', signal_time=f.index[-1])
    return ETFDecision(ret > 0, 'qualified' if ret > 0 else 'nonpositive_momentum', ret/vol, atr*scale, None, f.index[-1])


def mean_reversion_session_decisions(session: pd.DataFrame, spec: StrategySpec) -> dict:
    """Vectorized causal features with the same shared scalar decision semantics.

    Each mapping key is a minute bar's opening timestamp; the decision is available
    at key+one minute. All rolling/expanding operations look backward only.
    """
    if session.empty:return {}
    if session.index.tz is None:raise ValueError('Timezone-aware bars required')
    c=session.close;v=session.volume
    vwap=(c*v).cumsum()/v.cumsum()
    sigma=np.log(c).rolling(30).std(ddof=1)
    path=c.diff().abs().rolling(30).sum()
    efficiency=(c-c.shift(30)).abs()/path
    z=(np.log(c)-np.log(vwap))/sigma
    previous=c.shift()
    atr=pd.concat([session.high-session.low,(session.high-previous).abs(),(session.low-previous).abs()],axis=1).max(axis=1).rolling(14).mean()
    gaps=(session.index.to_series().diff().fillna(pd.Timedelta(minutes=1)) != pd.Timedelta(minutes=1)).cumsum()
    valid=np.isfinite(session[['high','low','close','volume']]).all(axis=1)&(session[['high','low','close']]>0).all(axis=1)&(v>=0)
    invalid=(~valid).cumsum()
    result={}
    known=session.index+pd.Timedelta(minutes=1)
    local=known.tz_convert('America/New_York')
    minutes=local.hour*60+local.minute
    # Convert once: avoid millions of Series.iloc/Timezone conversions in replay grids.
    records=zip(session.index,known,minutes,gaps.to_numpy(),invalid.to_numpy(),
                v.cumsum().to_numpy(),sigma.to_numpy(),path.to_numpy(),z.to_numpy(),
                c.to_numpy(),c.shift().to_numpy(),efficiency.to_numpy(),atr.to_numpy(),vwap.to_numpy())
    for n,(t,now,minute,gap,bad,volume,sd,distance,zvalue,price,prev,er,ar,vw) in enumerate(records):
        reason='qualified'
        if not 630<=minute<=870:reason='outside_signal_window'
        elif n<30:reason='insufficient_history'
        elif gap>0:reason='missing_minute'
        elif bad>0:reason='invalid_observation'
        elif volume<=0 or sd<=0 or distance<=0:reason='zero_denominator'
        elif not (zvalue<=-2 and price>prev and er<=.25 and ar>0):reason='signal_filter'
        result[t]=ETFDecision(reason=='qualified',reason,float(-zvalue) if np.isfinite(zvalue) else 0.,
                              float(ar) if np.isfinite(ar) else 0.,float(vw) if np.isfinite(vw) else None,now)
    return result
