"""Frozen Phase 3 causal benchmark targets; no fills, ledger or qualification."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Mapping, Sequence

D = Decimal
EQUITIES = 'E_DONCHIAN20_V1'
CRYPTO = 'C_SMA200_CONFIRM_V1'
OPTIONS = 'O_ETF_TREND_CALL_V1'
UNIVERSES = {EQUITIES: ('IWM','QQQ','SPY'), CRYPTO: ('BTC/USD','ETH/USD'), OPTIONS: ('IWM','QQQ','SPY')}
CAPS = {EQUITIES: D('.24'), CRYPTO: D('.25'), OPTIONS: D('1')}


def _number(value, label, *, positive=False):
    if not isinstance(value, D) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f'{label} must be a finite {"positive" if positive else "nonnegative"} Decimal')
    return value


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware timestamps required')
    return value.astimezone(timezone.utc)


def _hash(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('Archived source/model SHA256 required')


@dataclass(frozen=True)
class SourceStamp:
    as_of: datetime
    received_at: datetime
    source_sha256: str


@dataclass(frozen=True)
class Timing:
    expected_as_of: datetime
    decision_at: datetime
    execution_at: datetime
    calendar_sha256: str
    startup: bool = False


def _timing(timing):
    if not isinstance(timing,Timing):
        raise ValueError('Explicit Timing required')
    _hash(timing.calendar_sha256)
    if type(timing.startup) is not bool:
        raise ValueError('Explicit boolean startup boundary required')
    source,decision,execution=map(_utc,(timing.expected_as_of,timing.decision_at,timing.execution_at))
    if source>decision or decision>execution or (not timing.startup and source>=decision):
        raise ValueError('Prior close must precede decision and execution')
    return source,decision,execution


def _stamp(stamp,timing):
    source,decision,_=_timing(timing)
    if not isinstance(stamp,SourceStamp):
        raise ValueError('Explicit source stamp required')
    _hash(stamp.source_sha256)
    if _utc(stamp.as_of)!=source or not source<=_utc(stamp.received_at)<=decision:
        raise ValueError('Evidence must match expected causal boundary and arrive before decision')


@dataclass(frozen=True)
class EquitySnapshot:
    equity: Decimal
    stamp: SourceStamp


@dataclass(frozen=True)
class ExposureSnapshot:
    candidate_equity: Decimal
    gross_dollars: Decimal
    stamp: SourceStamp


@dataclass(frozen=True)
class PlannedWeights:
    weights: Mapping[str,Decimal]
    stamp: SourceStamp


@dataclass(frozen=True)
class DeltaExposure:
    underlying: str
    contracts: int
    delta: Decimal
    underlying_price: Decimal
    stamp: SourceStamp
    delta_model_sha256: str
    multiplier: int = 100


@dataclass(frozen=True)
class DeltaSnapshot:
    candidate_equity: Decimal
    exposures: tuple[DeltaExposure,...]
    stamp: SourceStamp
    complete: bool
    basis: str = 'actual'


@dataclass(frozen=True)
class BenchmarkTarget:
    strategy_id: str
    weights: Mapping[str,Decimal]
    dollars: Mapping[str,Decimal]
    cash_weight: Decimal
    reference_gross: Decimal
    target_gross: Decimal
    clipped_gross: Decimal
    timing: Timing
    source_hashes: tuple[str,...]
    control: str = 'matched'
    execution_authorized: bool = False
    investment_qualified: bool = False


def _result(strategy,weights,reference,equity,timing,hashes,*,control='matched'):
    gross=sum(weights.values(),D(0))
    if gross>1:
        raise ValueError('Benchmark cannot borrow')
    return BenchmarkTarget(strategy,weights,{s:w*equity for s,w in weights.items()},1-gross,
                           reference,gross,max(D(0),reference-gross),timing,tuple(sorted(set(hashes))),control)


def passive_initial_target(strategy_id: str, benchmark: EquitySnapshot,
                           timing: Timing) -> BenchmarkTarget:
    """Frozen initial equal-weight sleeve only; never rebalance passive holdings."""
    if strategy_id not in UNIVERSES or not isinstance(benchmark,EquitySnapshot):
        raise ValueError('Known strategy and benchmark equity snapshot required')
    _timing(timing);_stamp(benchmark.stamp,timing)
    if not timing.startup:
        raise ValueError('Passive control initializes once; retain holdings after startup')
    equity=_number(benchmark.equity,'benchmark initial equity',positive=True)
    sleeve={EQUITIES:D('.24'),CRYPTO:D('.25'),OPTIONS:D('.0075')}[strategy_id]
    universe=UNIVERSES[strategy_id]
    weight=sleeve/D(len(universe))
    weights={symbol:weight for symbol in universe[:-1]}
    weights[universe[-1]]=sleeve-sum(weights.values(),D(0))
    return _result(strategy_id,weights,sleeve,equity,timing,
                   (benchmark.stamp.source_sha256,timing.calendar_sha256),control='passive_initial')


def exposure_target(strategy_id: str, evidence: ExposureSnapshot | PlannedWeights,
                    benchmark: EquitySnapshot, timing: Timing) -> BenchmarkTarget:
    """Equal weights use lagged candidate gross or startup planned weights.

    Candidate equity determines its exposure fraction. Benchmark own equity
    determines independent target dollars; cash/fees/whole-share fills are upstream.
    """
    if strategy_id not in (EQUITIES,CRYPTO):
        raise ValueError('Exposure control is equity/crypto only')
    if not isinstance(evidence,(ExposureSnapshot,PlannedWeights)) or not isinstance(benchmark,EquitySnapshot):
        raise ValueError('Explicit exposure/planned and benchmark snapshots required')
    _timing(timing);_stamp(benchmark.stamp,timing)
    _number(benchmark.equity,'benchmark equity',positive=True)
    _stamp(evidence.stamp,timing)
    universe=UNIVERSES[strategy_id]
    if timing.startup:
        if not isinstance(evidence,PlannedWeights) or set(evidence.weights)!=set(universe):
            raise ValueError('Startup requires complete explicit planned candidate weights')
        reference=sum((_number(w,'planned weight') for w in evidence.weights.values()),D(0))
    else:
        if not isinstance(evidence,ExposureSnapshot):
            raise ValueError('Post-startup requires actual prior-close exposure')
        reference=_number(evidence.gross_dollars,'candidate gross')/_number(evidence.candidate_equity,'candidate equity',positive=True)
    gross=min(CAPS[strategy_id],reference)
    # A deterministic final remainder avoids Decimal division drift in sum(weights).
    weight=gross/D(len(universe))
    weights={s:weight for s in universe[:-1]}
    weights[universe[-1]]=gross-sum(weights.values(),D(0))
    return _result(strategy_id,weights,reference,benchmark.equity,timing,
                   (evidence.stamp.source_sha256,benchmark.stamp.source_sha256,timing.calendar_sha256))


def delta_target(evidence: DeltaSnapshot, benchmark: EquitySnapshot,
                 timing: Timing, *, expected_delta_model_sha256: str) -> BenchmarkTarget:
    """Use archived actual contracts; startup caller supplies planned contracts.

    No model is fitted and no missing delta is substituted. A complete empty
    exposure snapshot explicitly denotes flat ownership. Delta-notional controls
    do not match gamma, vega, theta, volatility or tail risk.
    """
    if not isinstance(evidence,DeltaSnapshot) or not isinstance(benchmark,EquitySnapshot):
        raise ValueError('Explicit delta and benchmark snapshots required')
    _timing(timing);_stamp(evidence.stamp,timing);_stamp(benchmark.stamp,timing)
    if evidence.basis != ('planned' if timing.startup else 'actual'):
        raise ValueError('Delta ownership basis must match startup versus lagged boundary')
    _hash(expected_delta_model_sha256)
    if evidence.complete is not True:
        raise ValueError('Complete archived delta exposure evidence required')
    candidate_equity=_number(evidence.candidate_equity,'candidate equity',positive=True)
    _number(benchmark.equity,'benchmark equity',positive=True)
    weights=dict.fromkeys(UNIVERSES[OPTIONS],D(0));seen=set()
    hashes=[evidence.stamp.source_sha256,benchmark.stamp.source_sha256,expected_delta_model_sha256,timing.calendar_sha256]
    if not isinstance(evidence.exposures,tuple):
        raise ValueError('Explicit immutable exposure tuple required')
    for exposure in evidence.exposures:
        if not isinstance(exposure,DeltaExposure):
            raise ValueError('Explicit archived delta rows required')
        if exposure.underlying not in weights or exposure.underlying in seen:
            raise ValueError('Unique frozen underlying exposure required')
        seen.add(exposure.underlying)
        if type(exposure.contracts) is not int or exposure.contracts!=1 or type(exposure.multiplier) is not int or exposure.multiplier!=100:
            raise ValueError('Exactly one standard 100-multiplier contract per held underlying')
        if not isinstance(exposure.delta,D) or not exposure.delta.is_finite() or not -1<=exposure.delta<=1:
            raise ValueError('Explicit finite delta in [-1,1] required')
        _number(exposure.underlying_price,'underlying price',positive=True)
        _stamp(exposure.stamp,timing);_hash(exposure.delta_model_sha256)
        if exposure.delta_model_sha256!=expected_delta_model_sha256:
            raise ValueError('Delta method differs from frozen model')
        hashes.append(exposure.stamp.source_sha256)
        weights[exposure.underlying]=max(D(0),exposure.delta*D(100)*exposure.underlying_price/candidate_equity)
    reference=sum(weights.values(),D(0))
    if reference>1:
        weights={s:w/reference for s,w in weights.items()}
        # Put arithmetic remainder on last positive component, not a flat asset.
        last=next(s for s in reversed(UNIVERSES[OPTIONS]) if weights[s]>0)
        weights[last]=1-sum(w for s,w in weights.items() if s!=last)
    return _result(OPTIONS,weights,reference,benchmark.equity,timing,hashes)


@dataclass(frozen=True)
class TrackingObservation:
    day: date
    reference_gross: Decimal | None
    planned_benchmark_gross: Decimal | None
    reference_by_underlying: Mapping[str,Decimal] | None = None
    benchmark_by_underlying: Mapping[str,Decimal] | None = None


def tracking_gate(strategy_id: str, expected_dates: Sequence[date], observations: Sequence[TrackingObservation]) -> dict:
    """Evaluate gross exposure/delta-notional validity, not strategy profitability.

    Reference is uncapped candidate gross fraction (or positive delta-notional
    fraction). Planned target is measured on the same normalized exposure basis.
    Missing dates/values are retained as inconclusive, never dropped from coverage.
    """
    if strategy_id not in UNIVERSES:
        raise ValueError('Unknown frozen strategy')
    expected=list(expected_dates)
    if not expected or any(type(day) is not date for day in expected) or expected!=sorted(set(expected)):
        raise ValueError('Exact nonempty ordered evaluation dates required')
    by_day={}
    for item in observations:
        if not isinstance(item,TrackingObservation) or item.day not in expected or item.day in by_day:
            raise ValueError('Duplicate, outside-calendar or malformed tracking row')
        for value in (item.reference_gross,item.planned_benchmark_gross):
            if value is not None:
                _number(value,'tracking exposure')
        for values in (item.reference_by_underlying,item.benchmark_by_underlying):
            if values is not None:
                if set(values)!=set(UNIVERSES[strategy_id]):
                    raise ValueError('Complete underlying tracking map required')
                for value in values.values():
                    _number(value,'underlying tracking exposure')
        by_day[item.day]=item
    missing=[];positive=matched=zero=zero_with_exposure=0;details=[];underlying_details=[]
    for day in expected:
        row=by_day.get(day)
        if row is None or row.reference_gross is None or row.planned_benchmark_gross is None:
            missing.append(day.isoformat());continue
        reference,target=row.reference_gross,row.planned_benchmark_gross
        if row.reference_by_underlying is not None and row.benchmark_by_underlying is not None:
            if sum(row.reference_by_underlying.values(),D(0))!=reference or sum(row.benchmark_by_underlying.values(),D(0))!=target:
                raise ValueError('Underlying and aggregate tracking totals disagree')
            for symbol in UNIVERSES[strategy_id]:
                ref=row.reference_by_underlying[symbol];actual=row.benchmark_by_underlying[symbol]
                underlying_details.append({'day':day.isoformat(),'underlying':symbol,
                    'reference':str(ref),'planned':str(actual),'absolute_error':str(abs(actual-ref)),
                    'relative_error':str(abs(actual-ref)/ref) if ref else None})
        if reference==0:
            zero+=1
            if target>0: zero_with_exposure+=1
            continue
        positive+=1
        error=abs(target-reference)/reference
        within=error<=D('.10');matched+=int(within)
        details.append({'day':day.isoformat(),'relative_error':str(error),'within_tolerance':within})
    fraction=D(matched)/D(positive) if positive else None
    status='inconclusive' if missing or not positive else ('pass' if fraction>=D('.95') else 'inconclusive')
    reason='missing_observations' if missing else ('no_positive_reference_dates' if not positive else ('within_frozen_tolerance' if status=='pass' else 'matching_tolerance_failed'))
    if strategy_id==OPTIONS:
        status='inconclusive';reason='gate_aggregation_not_frozen'
    return {'status':status,'reason':reason,'tracking_valid':status=='pass','expected_dates':len(expected),
            'missing_dates':missing,'positive_reference_dates':positive,'matched_dates':matched,
            'zero_reference_dates':zero,'zero_reference_with_exposure_dates':zero_with_exposure,
            'matched_fraction':str(fraction) if fraction is not None else None,'details':details,
            'underlying_details':underlying_details,
            'investment_qualified':False,'execution_authorized':False}
