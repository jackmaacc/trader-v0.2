from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal as D

import pytest
from trader_engine.research.phase3_benchmarks import (
 EQUITIES,CRYPTO,OPTIONS,SourceStamp,Timing,EquitySnapshot,ExposureSnapshot,PlannedWeights,
 DeltaExposure,DeltaSnapshot,TrackingObservation,exposure_target,delta_target,tracking_gate,passive_initial_target)

CLOSE=datetime(2026,9,30,20,tzinfo=timezone.utc)
DECISION=datetime(2026,10,1,13,20,tzinfo=timezone.utc)
EXECUTION=datetime(2026,10,1,13,30,tzinfo=timezone.utc)
STAMP=SourceStamp(CLOSE,CLOSE+timedelta(seconds=1),'a'*64)
TIMING=Timing(CLOSE,DECISION,EXECUTION,'d'*64)
BENCH=EquitySnapshot(D(80000),STAMP)


def test_candidate_fraction_and_benchmark_own_equity_are_distinct():
 target=exposure_target(EQUITIES,ExposureSnapshot(D(100000),D(12000),STAMP),BENCH,TIMING)
 assert target.reference_gross==D('.12')
 assert target.weights=={'IWM':D('.04'),'QQQ':D('.04'),'SPY':D('.04')}
 assert sum(target.dollars.values())==D(9600)
 assert target.cash_weight==D('.88') and not target.execution_authorized


@pytest.mark.parametrize('strategy,cap',[(EQUITIES,D('.24')),(CRYPTO,D('.25'))])
def test_cap_preserves_uncapped_reference(strategy,cap):
 target=exposure_target(strategy,ExposureSnapshot(D(100000),D(90000),STAMP),BENCH,TIMING)
 assert target.target_gross==cap and target.reference_gross==D('.9')
 assert target.clipped_gross==D('.9')-cap


def test_startup_requires_complete_planned_weights():
 timing=replace(TIMING,startup=True)
 weights=PlannedWeights({'IWM':D(0),'QQQ':D('.08'),'SPY':D('.08')},STAMP)
 result=exposure_target(EQUITIES,weights,BENCH,timing)
 assert result.target_gross==D('.16')
 with pytest.raises(ValueError,match='Startup'):
  exposure_target(EQUITIES,ExposureSnapshot(D(100000),D(0),STAMP),BENCH,timing)
 with pytest.raises(ValueError,match='Post-startup'):
  exposure_target(EQUITIES,weights,BENCH,TIMING)
 with pytest.raises(ValueError):
  exposure_target(EQUITIES,PlannedWeights({'SPY':D('.08')},STAMP),BENCH,timing)


@pytest.mark.parametrize('stamp',[
 replace(STAMP,as_of=DECISION),replace(STAMP,received_at=DECISION+timedelta(seconds=1)),
 replace(STAMP,received_at=CLOSE-timedelta(seconds=1)),replace(STAMP,source_sha256='unknown')])
def test_future_wrong_boundary_or_unverified_source_rejected(stamp):
 with pytest.raises(ValueError):
  exposure_target(EQUITIES,ExposureSnapshot(D(100000),D(12000),stamp),BENCH,TIMING)


def test_bad_equity_or_denominator_rejected():
 for equity in (D(0),D(-1),D('NaN')):
  with pytest.raises(ValueError):
   exposure_target(EQUITIES,ExposureSnapshot(equity,D(1),STAMP),BENCH,TIMING)


def delta_row(symbol='SPY',delta=D('.5'),price=D(500)):
 return DeltaExposure(symbol,1,delta,price,STAMP,'b'*64)


def test_options_delta_uses_candidate_equity_then_benchmark_equity():
 data=DeltaSnapshot(D(100000),(delta_row(),),STAMP,True)
 result=delta_target(data,BENCH,TIMING,expected_delta_model_sha256='b'*64)
 assert result.weights['SPY']==D('.25') and result.dollars['SPY']==D(20000)
 assert result.weights['IWM']==0 and result.cash_weight==D('.75')


def test_options_negative_clip_then_proportional_cash_cap():
 data=DeltaSnapshot(D(10000),(delta_row('SPY'),delta_row('QQQ',D('.5'),D(300)),delta_row('IWM',D('-.1'))),STAMP,True)
 result=delta_target(data,BENCH,TIMING,expected_delta_model_sha256='b'*64)
 assert result.reference_gross==4 and result.target_gross==1 and result.cash_weight==0
 assert result.weights=={'IWM':D(0),'QQQ':D('.375'),'SPY':D('.625')}


def test_missing_delta_model_and_incomplete_ownership_fail():
 for data in (DeltaSnapshot(D(100000),(),STAMP,False),DeltaSnapshot(D(100000),(replace(delta_row(),delta=None),),STAMP,True)):
  with pytest.raises(ValueError):
   delta_target(data,BENCH,TIMING,expected_delta_model_sha256='b'*64)
 with pytest.raises(ValueError,match='method'):
  delta_target(DeltaSnapshot(D(100000),(delta_row(),),STAMP,True),BENCH,TIMING,expected_delta_model_sha256='c'*64)


def dates(n):
 return [date(2026,10,1)+timedelta(days=i) for i in range(n)]


def test_tracking_exact95percent_and10percent_boundary():
 ds=dates(20)
 rows=[TrackingObservation(day,D('.2'),D('.18') if i<19 else D('.1')) for i,day in enumerate(ds)]
 result=tracking_gate(EQUITIES,ds,rows)
 assert result['status']=='pass' and result['matched_fraction']=='0.95'
 assert result['matched_dates']==19 and not result['investment_qualified']
 rows[0]=replace(rows[0],planned_benchmark_gross=D('.17999'))
 assert tracking_gate(EQUITIES,ds,rows)['status']=='inconclusive'


def test_missing_dates_not_silently_dropped_and_zeros_not_denominator():
 ds=dates(3)
 rows=[TrackingObservation(ds[0],D(0),D(0)),TrackingObservation(ds[1],D('.2'),D('.2'))]
 result=tracking_gate(CRYPTO,ds,rows)
 assert result['status']=='inconclusive' and result['positive_reference_dates']==1
 assert result['zero_reference_dates']==1 and result['missing_dates']==[ds[2].isoformat()]
 rows.append(TrackingObservation(ds[2],D('.2'),None))
 assert tracking_gate(CRYPTO,ds,rows)['missing_dates']==[ds[2].isoformat()]


def test_all_zero_cannot_establish_matching_validity():
 ds=dates(1)
 result=tracking_gate(EQUITIES,ds,[TrackingObservation(ds[0],D(0),D('.1'))])
 assert not result['tracking_valid'] and result['matched_fraction'] is None
 assert result['zero_reference_with_exposure_dates']==1


def test_options_aggregation_is_unfrozen_not_a_pass():
 ds=dates(1)
 row=TrackingObservation(ds[0],D('.2'),D('.2'),{'IWM':D(0),'QQQ':D(0),'SPY':D('.2')},{'IWM':D(0),'QQQ':D('.2'),'SPY':D(0)})
 result=tracking_gate(OPTIONS,ds,[row])
 assert result['matched_fraction']=='1' and result['reason']=='gate_aggregation_not_frozen'
 assert not result['tracking_valid'] and len(result['underlying_details'])==3
 assert next(r for r in result['underlying_details'] if r['underlying']=='SPY')['relative_error']=='1'


def test_tracking_duplicate_or_inconsistent_rows_rejected():
 ds=dates(1);row=TrackingObservation(ds[0],D('.1'),D('.1'))
 with pytest.raises(ValueError):tracking_gate(EQUITIES,ds,[row,row])
 bad=replace(row,reference_by_underlying={'IWM':D(0),'QQQ':D(0),'SPY':D('.2')},benchmark_by_underlying={'IWM':D(0),'QQQ':D(0),'SPY':D('.1')})
 with pytest.raises(ValueError,match='totals'):
  tracking_gate(OPTIONS,ds,[bad])


def test_options_startup_requires_planned_basis():
 data=DeltaSnapshot(D(100000),(delta_row(),),STAMP,True)
 with pytest.raises(ValueError,match='basis'):
  delta_target(data,BENCH,replace(TIMING,startup=True),expected_delta_model_sha256='b'*64)
 result=delta_target(replace(data,basis='planned'),BENCH,replace(TIMING,startup=True),expected_delta_model_sha256='b'*64)
 assert result.reference_gross==D('.25')


def test_missing_calendar_identity_rejected():
 with pytest.raises(ValueError,match='SHA256'):
  exposure_target(EQUITIES,ExposureSnapshot(D(100000),D(12000),STAMP),BENCH,replace(TIMING,calendar_sha256=''))


@pytest.mark.parametrize('strategy,sleeve',[(EQUITIES,D('.24')),(CRYPTO,D('.25')),(OPTIONS,D('.0075'))])
def test_passive_initial_sleeves_own_equity_and_no_rebalance(strategy,sleeve):
 result=passive_initial_target(strategy,BENCH,replace(TIMING,startup=True))
 assert result.target_gross==sleeve and sum(result.dollars.values())==BENCH.equity*sleeve
 assert result.control=='passive_initial' and result.cash_weight==1-sleeve
 assert not result.execution_authorized
 with pytest.raises(ValueError,match='initializes once'):
  passive_initial_target(strategy,BENCH,TIMING)
