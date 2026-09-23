"""Fail-closed research review checks; never authorize or execute orders."""
from dataclasses import dataclass
from datetime import date
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ReviewRequirements:
    minimum_sessions: int = 60
    minimum_closed_trades: int = 100
    maximum_drawdown: float = .05

    def __post_init__(self):
        if self.minimum_sessions<1 or self.minimum_closed_trades<1 or not 0<self.maximum_drawdown<1:
            raise ValueError('Invalid review requirements')


def assess_prospective_record(journal: pd.DataFrame, *, frozen_on: date, as_of: date,
                             independent_data_verified: bool, execution_checks_passed: bool,
                             universe_scope_documented: bool, requirements=ReviewRequirements()) -> dict:
    """Paths start at $100,000; closed_trades is a per-session, not cumulative, count.

    This validates submitted evidence, not its authenticity. Calendar and source
    verification must be performed independently. Acceptance permits review only.
    """
    reasons=[]
    for flag,name in [(independent_data_verified,'independent_data_not_verified'),
                      (execution_checks_passed,'execution_checks_not_passed'),
                      (universe_scope_documented,'universe_scope_not_documented')]:
        if flag is not True:reasons.append(name)
    required={'session','equity','stressed_equity','benchmark_equity','closed_trades','reconciled'}
    if journal.empty:return dict(eligible_for_review=False,reasons=reasons+['no_prospective_sessions'],sessions=0)
    if not required.issubset(journal):raise ValueError('Missing journal columns')
    dates=pd.to_datetime(journal.session,errors='raise',utc=True)
    if dates.isna().any() or dates.dt.normalize().duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError('Sessions must be unique and chronological')
    if (dates.dt.date<=frozen_on).any():reasons.append('contains_pre_freeze_or_reused_sessions')
    if (dates.dt.date>as_of).any():reasons.append('contains_future_sessions')
    if (dates.dt.dayofweek>=5).any():reasons.append('contains_non_weekday_equity_sessions')
    columns=['equity','stressed_equity','benchmark_equity','closed_trades']
    values=journal[columns].apply(pd.to_numeric,errors='raise')
    if not np.isfinite(values.to_numpy()).all() or (values[columns[:3]]<=0).any().any():raise ValueError('Invalid equity paths')
    if (values.closed_trades<0).any() or (values.closed_trades%1!=0).any():raise ValueError('Trade counts must be nonnegative integers')
    if not journal.reconciled.map(lambda x:isinstance(x,(bool,np.bool_)) and bool(x)).all():reasons.append('unreconciled_sessions')
    n=len(journal);trades=int(values.closed_trades.sum())
    if n<requirements.minimum_sessions:reasons.append('insufficient_sessions')
    if trades<requirements.minimum_closed_trades:reasons.append('insufficient_closed_trades')
    paths=np.vstack([np.full((1,3),100000.),values[columns[:3]].to_numpy()])
    net=paths[-1]/100000-1
    dd=-(paths/np.maximum.accumulate(paths,axis=0)-1).min(axis=0)
    if net[0]<=0:reasons.append('nonpositive_net_return')
    if net[1]<=0:reasons.append('nonpositive_stressed_return')
    if net[0]<=net[2]:reasons.append('no_benchmark_excess')
    if max(dd[:2])>requirements.maximum_drawdown:reasons.append('drawdown_limit_exceeded')
    daily=paths[1:,0]/paths[:-1,0]-1
    return dict(eligible_for_review=not reasons,reasons=reasons,sessions=n,closed_trades=trades,
        net_return=float(net[0]),stressed_return=float(net[1]),benchmark_return=float(net[2]),
        max_drawdown=float(dd[0]),stressed_max_drawdown=float(dd[1]),one_percent_days=int((daily>=.01).sum()),
        losing_days=int((daily<0).sum()),worst_day=float(daily.min()),
        note='Review eligibility is not an order authorization or return guarantee.')

# Versioned net-edge contract; legacy assessment remains unchanged.
from trader_engine.research.net_edge import (EvidenceManifest, ConfirmationProtocol, ReviewDecision,
    assess_net_edge_confirmation, stationary_bootstrap_bounds, select_champion)
