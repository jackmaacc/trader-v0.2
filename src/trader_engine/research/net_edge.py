"""Frozen evidence and conservative net-edge review. Never authorizes orders.

All historical selection is development. Confirmation is one pre-registered
calendar window, not an extend-until-significant sequential experiment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone, timedelta
from calendar import monthrange
from pathlib import Path
from typing import Mapping
import hashlib
import json
import math

import numpy as np
import pandas as pd


def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


ETF_UNIVERSE = ("SPY", "QQQ", "IWM", "TLT", "GLD")


def canonical_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class EvidenceManifest:
    strategy_hash: str
    code_hash: str
    calendar_hash: str
    mode: str
    files: tuple[tuple[str, str], ...]
    created_at: str

    def __post_init__(self):
        object.__setattr__(self, "files", tuple(tuple(pair) for pair in self.files))
        if self.mode not in {"historical_development", "prospective_shadow", "paper_broker"}:
            raise ValueError("Unsupported evidence mode")
        if not all(_valid_hash(v) for v in (self.strategy_hash, self.code_hash, self.calendar_hash)):
            raise ValueError("Identity hashes are required")
        names = [name for name, _ in self.files]
        if not names or len(set(names)) != len(names):
            raise ValueError("Evidence must contain unique files")
        for name, digest in self.files:
            if not name or Path(name).is_absolute() or ".." in Path(name).parts or not _valid_hash(digest):
                raise ValueError("Unsafe evidence path or hash")
        if datetime.fromisoformat(self.created_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Evidence timestamp must include timezone")

    @property
    def digest(self):
        return canonical_hash(asdict(self))

    def verify(self, root: Path) -> tuple[str, ...]:
        root = Path(root).resolve()
        failures = []
        for name, digest in self.files:
            path = root / name
            if not path.resolve().is_relative_to(root):
                failures.append(f"unsafe_path:{name}")
            elif not path.is_file() or file_hash(path) != digest:
                failures.append(f"missing_or_changed:{name}")
        return tuple(failures)

    @classmethod
    def from_dict(cls, data):
        return cls(**(data | {"files": tuple(tuple(v) for v in data["files"])}))


@dataclass(frozen=True)
class ConfirmationProtocol:
    strategy_hash: str
    code_hash: str
    calendar_hash: str
    frozen_at: str
    sessions: tuple[str, ...]
    operating_cost_monthly: float | None
    initial_capital: float = 100000.0
    block_lengths: tuple[int, ...] = (5, 10)
    bootstrap_resamples: int = 100000
    alpha: float = 0.05 / 6
    minimum_episodes: int = 30
    maximum_drawdown: float = 0.03
    daily_halt: float = 0.005
    seed: int = 20260923

    def __post_init__(self):
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "block_lengths", tuple(self.block_lengths))
        if len(self.sessions) != 126 or tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("Exactly 126 fixed, unique chronological sessions required")
        frozen = datetime.fromisoformat(self.frozen_at.replace("Z", "+00:00"))
        if frozen.tzinfo is None:
            raise ValueError("Freeze timestamp requires timezone")
        if date.fromisoformat(self.sessions[0]) <= frozen.date():
            raise ValueError("Confirmation must begin after registration")
        for value in self.sessions:
            date.fromisoformat(value)
        if any(not _valid_hash(h) for h in (self.strategy_hash, self.code_hash, self.calendar_hash)):
            raise ValueError("Strategy, code and authoritative calendar hashes required")
        if self.operating_cost_monthly is not None and (not math.isfinite(self.operating_cost_monthly) or self.operating_cost_monthly < 0):
            raise ValueError("Operating overhead must be explicitly known")
        if self.initial_capital != 100000.0:
            raise ValueError("Invalid capital")
        if self.bootstrap_resamples < 100000 or not 0 < self.alpha <= .05 / 6:
            raise ValueError("Cannot weaken frozen confidence requirements")
        if self.block_lengths != (5, 10):
            raise ValueError("Invalid dependence sensitivity")
        if len(self.sessions) < 20 * min(self.block_lengths):
            raise ValueError("Insufficient sessions for chosen dependence assumption")
        if self.minimum_episodes < 30 or not 0 < self.maximum_drawdown <= .03 or not 0 < self.daily_halt <= .005:
            raise ValueError("Cannot weaken evidence or risk mandate")

    @property
    def digest(self):
        return canonical_hash(asdict(self))

    @classmethod
    def from_dict(cls, data):
        return cls(**(data | {"sessions": tuple(data["sessions"]),
                             "block_lengths": tuple(data.get("block_lengths", (5, 10)))}))


@dataclass(frozen=True)
class ReviewDecision:
    status: str
    reasons: tuple[str, ...]
    metrics: Mapping
    protocol_hash: str
    evidence_hash: str
    eligible_for_review: bool = False
    approved_for_trading: bool = False

    def __post_init__(self):
        if self.status not in {"pass", "fail", "inconclusive"}:
            raise ValueError("Invalid review result")
        if self.approved_for_trading or self.eligible_for_review != (self.status == "pass"):
            raise ValueError("Research cannot authorize trading")

    def to_dict(self):
        return asdict(self)


def stationary_bootstrap_bounds(daily, *, block_lengths=(5, 10), resamples=100000,
                                alpha=.05 / 6, seed=20260923, batch_size=2048):
    """Joint percentile lower bounds on mean daily dollars; preserves vector dependence.

    Stationary bootstrap uses geometrically distributed block lengths with circular
    continuation. Its validity requires suitable stationarity/dependence assumptions.
    Counts are simulations of existing observations, never new independent evidence.
    """
    values = np.asarray(daily, dtype=float)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or len(values) < 2 or not np.isfinite(values).all():
        raise ValueError("Need finite session-by-series observations")
    if not 0 < alpha < .5 or resamples < 100 or batch_size < 1:
        raise ValueError("Invalid bootstrap settings")
    n, width = values.shape
    results = {}
    for length in block_lengths:
        if length < 1 or length >= n:
            raise ValueError("Block length must be positive and below sample size")
        rng = np.random.default_rng(np.random.SeedSequence([seed, length]))
        means = np.empty((resamples, width))
        for first in range(0, resamples, batch_size):
            size = min(batch_size, resamples - first)
            indices = rng.integers(0, n, size=size)
            sums = values[indices].copy()
            for _ in range(1, n):
                restart = rng.random(size) < 1.0 / length
                fresh = rng.integers(0, n, size=size)
                indices = np.where(restart, fresh, (indices + 1) % n)
                sums += values[indices]
            means[first:first + size] = sums / n
        results[int(length)] = np.quantile(means, alpha, axis=0).tolist()
    return results


def _strict_true(series):
    return series.map(lambda v: isinstance(v, (bool, np.bool_)) and bool(v)).all()


def allocate_calendar_overhead(sessions, monthly):
    """Accrue actual calendar days first session through last, inclusive.

    Non-session days accrue to the next session. The closed registered window
    has no trailing days beyond its endpoint; idle sessions are never omitted.
    """
    if monthly is None or not math.isfinite(monthly) or monthly < 0:
        raise ValueError("Known nonnegative monthly overhead required")
    result=[];previous=None
    for value in sessions:
        current=date.fromisoformat(value)
        day=current if previous is None else previous+timedelta(days=1)
        total=0.0
        while day<=current:
            total+=monthly/monthrange(day.year,day.month)[1]
            day+=timedelta(days=1)
        result.append(total);previous=current
    return np.asarray(result)


def load_confirmation_inputs(root):
    """Convenience loader; assessment separately verifies every archived hash."""
    root=Path(root)
    read=lambda name:json.loads((root/name).read_text())
    return dict(daily=pd.DataFrame(read("daily.json")), marks=pd.DataFrame(read("marks.json")),
                leave_one_out={k:pd.Series(v) for k,v in read("leave_one_out.json").items()},
                calendar_sessions=tuple(read("calendar.json")),
                protocol=ConfirmationProtocol.from_dict(read("protocol.json")))


def _records(frame):
    # to_json canonicalizes numpy scalars and timestamps, preserving numeric precision.
    return json.loads(frame.to_json(orient="records",date_format="iso",double_precision=15))


def assess_net_edge_confirmation(daily: pd.DataFrame, *, protocol: ConfirmationProtocol,
                                evidence: EvidenceManifest, evidence_root: Path,
                                calendar_sessions: tuple[str, ...], marks: pd.DataFrame,
                                leave_one_out: Mapping[str, pd.Series], as_of: date) -> ReviewDecision:
    """Review exact preregistered sample; file integrity is not truth authentication.

    Required JSON artifacts: daily/marks records, leave_one_out vectors, protocol,
    calendar and engineering/data_quality/cost_calibration reports. Reports have
    passed, strategy_hash, checks [{name,passed}] and artifacts [hashed filenames].
    Only verified archived tables enter calculations; supplied tables must agree.
    """
    root=Path(evidence_root); reasons=list(evidence.verify(root)); insufficient=[]; metrics={}
    def finish():
        status="fail" if reasons else "inconclusive" if insufficient else "pass"
        return ReviewDecision(status,tuple(dict.fromkeys(reasons+insufficient)),metrics,
                              protocol.digest,evidence.digest,eligible_for_review=status=="pass")
    if reasons:return finish()  # Never read or compute using failed/unsafe evidence.
    listed={name for name,_ in evidence.files}
    needed={"daily.json","marks.json","leave_one_out.json","protocol.json","calendar.json",
            "engineering.json","data_quality.json","cost_calibration.json"}
    if not needed<=listed:
        insufficient.extend("missing_evidence:"+name for name in sorted(needed-listed));return finish()
    try:
        archived=load_confirmation_inputs(root)
        if canonical_hash(_records(daily))!=canonical_hash(_records(archived['daily'])):
            reasons.append("unbound_daily_table")
        if canonical_hash(_records(marks))!=canonical_hash(_records(archived['marks'])):
            reasons.append("unbound_marks_table")
        supplied={k:list(np.asarray(v,dtype=float)) for k,v in leave_one_out.items()}
        saved={k:list(np.asarray(v,dtype=float)) for k,v in archived['leave_one_out'].items()}
        if canonical_hash(supplied)!=canonical_hash(saved):reasons.append("unbound_leave_one_out")
        if protocol.digest!=archived['protocol'].digest:reasons.append("unbound_protocol")
        if tuple(calendar_sessions)!=archived['calendar_sessions']:reasons.append("unbound_calendar")
    except (ValueError,TypeError,KeyError,OSError,OverflowError):
        reasons.append("invalid_archived_inputs");return finish()
    if reasons:return finish()
    daily=archived['daily'];marks=archived['marks'];leave_one_out=archived['leave_one_out']
    if evidence.mode=="historical_development":reasons.append("historical_evidence_is_development_only")
    for key in ("strategy_hash","code_hash","calendar_hash"):
        if getattr(protocol,key)!=getattr(evidence,key):reasons.append(key+"_mismatch")
    if canonical_hash(list(calendar_sessions))!=protocol.calendar_hash:reasons.append("calendar_hash_mismatch")
    if tuple(sorted(set(calendar_sessions)))!=tuple(calendar_sessions):reasons.append("invalid_calendar")
    if tuple(s for s in calendar_sessions if protocol.sessions[0]<=s<=protocol.sessions[-1])!=protocol.sessions:
        reasons.append("registered_sessions_do_not_match_exchange_calendar")
    report_checks={
        "engineering.json":{"signal_parity","halt_and_drawdown","partial_canceled_fills_included","no_fee_double_count",
            "replay_shadow_parity","future_price_causality","future_missingness_causality",
            "continuous_nonoverlap_portfolio","order_transition_races","duplicate_lost_ack_restart",
            "overnight_split_dividend_calendar_dst","cash_position_fee_reconciliation",
            "stale_data_unknown_ownership_blocking","emergency_seven_position_load",
            "partial_fill_protection","overnight_restart_adoption"},
        "data_quality.json":{"exchange_calendar","corporate_actions","open_position_valuation","complete_sessions"},
        "cost_calibration.json":{"quotes_and_latency","double_cost_replay","adverse_delay_replay","leave_one_out_replays"}}
    for name,required_checks in report_checks.items():
        try:
            report=json.loads((root/name).read_text())
            checks=report.get("checks",[])
            names=[c['name'] for c in checks]
            references=report.get('artifacts',[])
            valid=(report.get('passed') is True and report.get('strategy_hash')==protocol.strategy_hash
                   and len(names)==len(set(names)) and required_checks<=set(names)
                   and all(c.get('passed') is True for c in checks)
                   and bool(references) and all(ref in listed and ref!=name for ref in references))
            if name=="engineering.json":
                for check_name in sorted(required_checks):
                    check=next((c for c in checks if c.get('name')==check_name),None)
                    links=check.get('artifacts',[]) if check is not None else []
                    if check is None or check.get('passed') is not True or not isinstance(links,list) or not links or any(ref not in listed or ref==name for ref in links):
                        reasons.append('engineering_check_missing_or_unverified:'+check_name);valid=False
                load=next((c for c in checks if c.get('name')=='emergency_seven_position_load'),{})
                elapsed=load.get('measured_seconds');positions=load.get('positions')
                if (type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<=elapsed<=1
                    or type(positions) is not int or positions<7 or load.get('mode')!='normal_simulation'):
                    reasons.append('emergency_load_requirement_unverified');valid=False
            if not valid:reasons.append("unverified_evidence:"+name)
        except (ValueError,KeyError,TypeError,OSError):reasons.append("invalid_evidence:"+name)
    if protocol.operating_cost_monthly is None:
        insufficient.append("unknown_operating_overhead")
    required={'session','gross_pnl','execution_cost','net_pnl','operating_cost','business_pnl','equity',
              'business_equity','stressed_business_pnl','adverse_business_pnl','closed_episodes',
              'reconciled','halt_compliant','valuation_valid'}
    if not required<=set(daily):insufficient.append('missing_session_ledger');return finish()
    try:
        dates=pd.to_datetime(daily.session,utc=True,errors='raise')
        sessions=tuple(dates.dt.strftime('%Y-%m-%d'))
        if sessions!=protocol.sessions:insufficient.append('confirmation_window_incomplete_or_changed')
        if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
            reasons.append('invalid_session_dates')
        if any(s>as_of.isoformat() for s in sessions):reasons.append('future_evidence')
        if as_of.isoformat()<protocol.sessions[-1]:insufficient.append('registered_endpoint_not_reached')
        numeric=list(required-{'session','reconciled','halt_compliant','valuation_valid'})
        if not np.isfinite(daily[numeric].to_numpy(dtype=float)).all():
            reasons.append('nonfinite_ledger');return finish()
    except (ValueError,TypeError):reasons.append('invalid_session_ledger');return finish()
    daily[numeric]=daily[numeric].apply(pd.to_numeric,errors='raise')
    if (daily.execution_cost<0).any() or (daily.operating_cost<0).any():reasons.append('negative_cost')
    if (daily[['equity','business_equity']]<=0).any().any():reasons.append('invalid_equity')
    identities=[(daily.gross_pnl-daily.execution_cost,daily.net_pnl,'net_pnl'),
                (daily.net_pnl-daily.operating_cost,daily.business_pnl,'business_pnl'),
                (protocol.initial_capital+daily.net_pnl.cumsum(),daily.equity,'equity'),
                (protocol.initial_capital+daily.business_pnl.cumsum(),daily.business_equity,'business_equity')]
    for left,right,name in identities:
        if not np.allclose(left,right,rtol=0,atol=1e-6):reasons.append(name+'_does_not_reconcile')
    if protocol.operating_cost_monthly is not None and sessions:
        expected=allocate_calendar_overhead(sessions,protocol.operating_cost_monthly)
        if not np.allclose(expected,daily.operating_cost,rtol=0,atol=1e-7):reasons.append('calendar_overhead_mismatch')
    episodes=daily.closed_episodes.to_numpy(dtype=float)
    if (episodes<0).any() or (episodes!=np.floor(episodes)).any():reasons.append('invalid_episode_count')
    if episodes.sum()<protocol.minimum_episodes:insufficient.append('insufficient_resolved_episodes')
    for column in ['reconciled','halt_compliant','valuation_valid']:
        if not _strict_true(daily[column]):reasons.append(column+'_failed')
    business_path=np.r_[protocol.initial_capital,daily.business_equity.to_numpy(dtype=float)]
    business_dd=float(np.max(1-business_path/np.maximum.accumulate(business_path)))
    metrics['business_close_drawdown']=business_dd
    if business_dd>protocol.maximum_drawdown+1e-12:reasons.append('business_drawdown_limit_exceeded')
    profits=daily.business_pnl.to_numpy(dtype=float)
    metrics.update(sessions=len(daily),closed_episodes=int(episodes.sum()),business_pnl=float(profits.sum()),
                   stressed_business_pnl=float(daily.stressed_business_pnl.sum()),adverse_business_pnl=float(daily.adverse_business_pnl.sum()))
    if profits.sum()<=0:reasons.append('nonpositive_business_profit')
    if daily.stressed_business_pnl.sum()<=0 or daily.adverse_business_pnl.sum()<=0:reasons.append('cost_or_delay_stress_failed')
    if len(profits)>=5 and profits.sum()-np.sort(profits)[-5:].sum()<=0:reasons.append('best_five_days_concentration')
    mark_columns={'timestamp','session','equity','day_start_equity','halt_compliant','entries_blocked'}
    if marks.empty or not mark_columns<=set(marks):insufficient.append('missing_intraday_risk_evidence')
    else:
        try:
            times=pd.to_datetime(marks.timestamp,utc=True,errors='raise')
            numbers=marks[['equity','day_start_equity']].to_numpy(dtype=float)
            mark_sessions=tuple(dict.fromkeys(marks.session))
            if not np.isfinite(numbers).all() or (numbers<=0).any() or times.isna().any() or not times.is_monotonic_increasing or times.duplicated().any():
                reasons.append('invalid_risk_marks')
            elif mark_sessions!=sessions:insufficient.append('incomplete_risk_sessions')
            else:
                if tuple(times.dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d'))!=tuple(marks.session):reasons.append('risk_mark_session_mismatch')
                if (times.dt.date>as_of).any():reasons.append('future_risk_marks')
                path=np.r_[protocol.initial_capital,marks.equity.to_numpy()]
                drawdown=float(np.max(1-path/np.maximum.accumulate(path)));metrics['observed_max_drawdown']=drawdown
                if drawdown>protocol.maximum_drawdown+1e-12:reasons.append('drawdown_limit_exceeded')
                if not _strict_true(marks.halt_compliant):reasons.append('intraday_halt_noncompliance')
                prior=protocol.initial_capital
                for (session,group),(_,row) in zip(marks.groupby('session',sort=False),daily.iterrows()):
                    if not np.allclose(group.day_start_equity,prior,rtol=0,atol=1e-6):reasons.append('day_start_equity_mismatch')
                    breached=np.maximum.accumulate(group.equity.to_numpy()<=prior*(1-protocol.daily_halt)+1e-9)
                    blocked=group.entries_blocked.map(lambda v:isinstance(v,(bool,np.bool_)) and bool(v)).to_numpy()
                    if (breached & ~blocked).any():reasons.append('daily_halt_not_latched')
                    if not math.isclose(float(group.equity.iloc[-1]),float(row.equity),abs_tol=1e-6,rel_tol=0):reasons.append('closing_mark_mismatch')
                    prior=float(row.equity)
        except (ValueError,TypeError,KeyError):reasons.append('invalid_risk_marks')
    if set(leave_one_out)!=set(ETF_UNIVERSE):insufficient.append('missing_leave_one_etf_out_replays')
    else:
        for symbol,series in leave_one_out.items():
            if len(series)!=len(protocol.sessions) or not np.isfinite(series.to_numpy(dtype=float)).all():insufficient.append('incomplete_leave_one_out:'+symbol)
            elif series.sum()<=0:reasons.append('leave_one_out_failed:'+symbol)
    if not reasons and not insufficient:
        # Joint three-series resampling keeps cost/delay cross-correlation intact.
        vectors=daily[['business_pnl','stressed_business_pnl','adverse_business_pnl']].to_numpy()
        bounds=stationary_bootstrap_bounds(vectors,block_lengths=protocol.block_lengths,
                    resamples=protocol.bootstrap_resamples,alpha=protocol.alpha,seed=protocol.seed)
        metrics['lower_bounds_mean_daily_business_pnl']=bounds
        if any(min(v)<=0 for v in bounds.values()):insufficient.append('net_edge_uncertainty_not_resolved')
    return finish()


def select_champion(candidates: list[dict]) -> dict | None:
    """Only frozen development survivors enter deterministic conservative ranking."""
    survivors = [c for c in candidates if c.get("development_passed") is True
                 and c.get("candidate_id") in {"MR30", "MR60", "MOM20", "MOM60"}
                 and math.isfinite(c.get("worst_lower_bound", float("nan")))
                 and c["worst_lower_bound"] > 0
                 and all(isinstance(c.get(k),(int,float)) and math.isfinite(c[k]) and c[k]>=0 for k in ("max_drawdown","turnover"))
                 and c["max_drawdown"]<=.03]
    if not survivors:
        return None
    return min(survivors, key=lambda c: (-c["worst_lower_bound"], c["max_drawdown"],
                                         c["turnover"], c["candidate_id"]))
