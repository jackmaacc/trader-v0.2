from dataclasses import asdict, replace
from datetime import date
import json
import numpy as np
import pandas as pd
import pytest
from trader_engine.research.net_edge import (EvidenceManifest,ConfirmationProtocol,canonical_hash,file_hash,
    assess_net_edge_confirmation,allocate_calendar_overhead,stationary_bootstrap_bounds,select_champion)


def fixture_bundle(tmp_path, *, monthly=0):
    sessions=tuple(pd.bdate_range('2026-01-02',periods=126).strftime('%Y-%m-%d'))
    # Synthetic explicit calendar is test data, not a claim about actual holidays.
    h='a'*64
    protocol=ConfirmationProtocol(h,h,canonical_hash(list(sessions)),'2026-01-01T00:00:00Z',sessions,monthly)
    overhead=allocate_calendar_overhead(sessions,monthly or 0)
    daily=pd.DataFrame(dict(session=sessions,gross_pnl=110.,execution_cost=10.,net_pnl=100.,
        operating_cost=overhead,business_pnl=100.-overhead,equity=100000+np.arange(1,127)*100,
        business_equity=100000+np.cumsum(100.-overhead),stressed_business_pnl=80.,adverse_business_pnl=70.,
        closed_episodes=1,reconciled=True,halt_compliant=True,valuation_valid=True))
    marks=pd.DataFrame([dict(timestamp=s+'T20:00:00Z',session=s,equity=100000+(i+1)*100,
        day_start_equity=100000+i*100,halt_compliant=True,entries_blocked=False) for i,s in enumerate(sessions)])
    loo={s:pd.Series(np.full(126,50.)) for s in ['SPY','QQQ','IWM','TLT','GLD']}
    data=dict(daily=daily,protocol=protocol,calendar_sessions=sessions,marks=marks,leave_one_out=loo,
        evidence_root=tmp_path,as_of=date.fromisoformat(sessions[-1]))
    return archive(data)


def archive(data):
    root=data['evidence_root'];p=data['protocol']
    files={'daily.json':json.loads(data['daily'].to_json(orient='records',double_precision=15)),
           'marks.json':json.loads(data['marks'].to_json(orient='records',double_precision=15)),
           'leave_one_out.json':{k:v.tolist() for k,v in data['leave_one_out'].items()},
           'protocol.json':asdict(p),'calendar.json':list(data['calendar_sessions'])}
    checks={'engineering.json':['signal_parity','halt_and_drawdown','partial_canceled_fills_included','no_fee_double_count','replay_shadow_parity','future_price_causality','future_missingness_causality','continuous_nonoverlap_portfolio','order_transition_races','duplicate_lost_ack_restart','overnight_split_dividend_calendar_dst','cash_position_fee_reconciliation','stale_data_unknown_ownership_blocking','emergency_seven_position_load','partial_fill_protection','overnight_restart_adoption'],
        'data_quality.json':['exchange_calendar','corporate_actions','open_position_valuation','complete_sessions'],
        'cost_calibration.json':['quotes_and_latency','double_cost_replay','adverse_delay_replay','leave_one_out_replays']}
    # Synthetic fixture evidence only: not a generated production pass report.
    files['engineering_support.json']={'fixture':'unit-test-only','actual_engineering_attestation':False}
    for name,names in checks.items():
        files[name]={'passed':True,'strategy_hash':p.strategy_hash,'checks':[{'name':n,'passed':True,'artifacts':['engineering_support.json']} | ({'measured_seconds':.5,'positions':7,'mode':'normal_simulation'} if n=='emergency_seven_position_load' else {}) for n in names],'artifacts':['daily.json','marks.json']}
    for name,value in files.items():(root/name).write_text(json.dumps(value))
    data['evidence']=EvidenceManifest(p.strategy_hash,p.code_hash,p.calendar_hash,'prospective_shadow',
        tuple((n,file_hash(root/n)) for n in files),'2026-09-23T00:00:00Z')
    return data


def test_positive_full_registered_record(tmp_path):
    result=assess_net_edge_confirmation(**fixture_bundle(tmp_path))
    assert result.status=='pass',result.reasons
    assert result.eligible_for_review and result.approved_for_trading is False
    assert len(result.metrics['lower_bounds_mean_daily_business_pnl'][5])==3


def test_incomplete_is_not_early_pass(tmp_path):
    data=fixture_bundle(tmp_path);data['daily']=data['daily'].iloc[:-1];archive(data)
    result=assess_net_edge_confirmation(**data)
    assert result.status=='inconclusive'
    assert 'confirmation_window_incomplete_or_changed' in result.reasons


def test_passed_table_is_bound_to_file(tmp_path):
    data=fixture_bundle(tmp_path);data['daily'].loc[0,'net_pnl']+=1
    assert 'unbound_daily_table' in assess_net_edge_confirmation(**data).reasons


def test_tampered_file_rejected(tmp_path):
    data=fixture_bundle(tmp_path);(tmp_path/'daily.json').write_text('[]')
    assert any('missing_or_changed' in r for r in assess_net_edge_confirmation(**data).reasons)


def test_unknown_overhead_blocks(tmp_path):
    result=assess_net_edge_confirmation(**fixture_bundle(tmp_path,monthly=None))
    assert result.status=='inconclusive' and 'unknown_operating_overhead' in result.reasons


def test_calendar_overhead_month_and_weekends():
    v=allocate_calendar_overhead(('2026-01-30','2026-02-02'),310.)
    assert v[0]==10 and v[1]==pytest.approx(10+2*310/28)


def test_negative_cost_rejected(tmp_path):
    data=fixture_bundle(tmp_path);data['daily'].loc[0,'execution_cost']=-1;archive(data)
    assert 'negative_cost' in assess_net_edge_confirmation(**data).reasons


def test_best_five_days_not_removed_from_requirements(tmp_path):
    data=fixture_bundle(tmp_path);d=data['daily'];d['gross_pnl']=9.;d.loc[:4,'gross_pnl']=1010.
    d['net_pnl']=d.gross_pnl-d.execution_cost;d['business_pnl']=d.net_pnl
    d['equity']=100000+d.net_pnl.cumsum();d['business_equity']=d.equity;archive(data)
    assert 'best_five_days_concentration' in assess_net_edge_confirmation(**data).reasons


def test_missing_mark_schema_blocks(tmp_path):
    data=fixture_bundle(tmp_path);data['marks']=data['marks'].drop(columns='entries_blocked');archive(data)
    assert 'missing_intraday_risk_evidence' in assess_net_edge_confirmation(**data).reasons


def test_daily_halt_is_latched(tmp_path):
    data=fixture_bundle(tmp_path);data['marks'].loc[0,'equity']=99499.;archive(data)
    assert 'daily_halt_not_latched' in assess_net_edge_confirmation(**data).reasons


def test_unsafe_path_and_symlink(tmp_path):
    data=fixture_bundle(tmp_path);m=data['evidence']
    with pytest.raises(ValueError):replace(m,files=(('../escape','a'*64),))
    (tmp_path/'link').symlink_to('/etc/passwd')
    modified=replace(m,files=(('link','a'*64),))
    assert modified.verify(tmp_path)==('unsafe_path:link',)


def test_calendar_holiday_not_weekday_heuristic(tmp_path):
    data=fixture_bundle(tmp_path);data['calendar_sessions']=tuple(s for s in data['calendar_sessions'] if s!='2026-01-19');archive(data)
    assert 'registered_sessions_do_not_match_exchange_calendar' in assess_net_edge_confirmation(**data).reasons


def test_stationary_joint_resampling_preserves_affine_relation():
    x=np.arange(30,dtype=float)
    a=stationary_bootstrap_bounds(np.column_stack([x,2*x+3]),resamples=1000,seed=22)
    b=stationary_bootstrap_bounds(np.column_stack([x,2*x+3]),resamples=1000,seed=22)
    assert a==b
    assert all(v[1]==pytest.approx(2*v[0]+3) for v in a.values())


def test_protocol_does_not_autoextend(tmp_path):
    data=fixture_bundle(tmp_path)
    with pytest.raises(ValueError):replace(data['protocol'],sessions=data['protocol'].sessions+('2027-01-01',))
    with pytest.raises(ValueError):replace(data['protocol'],block_lengths=(6,12))


def test_champion_conservative_deterministic():
    base=dict(development_passed=True,worst_lower_bound=1.,max_drawdown=.01,turnover=2.)
    assert select_champion([]) is None
    assert select_champion([base|{'candidate_id':'MR60'},base|{'candidate_id':'MR30'}])['candidate_id']=='MR30'
    assert select_champion([base|{'candidate_id':'MR30','worst_lower_bound':0}]) is None


@pytest.mark.parametrize('case',['partial_fill_protection','overnight_restart_adoption','unhashed_support','slow_load'])
def test_engineering_obligations_cannot_be_omitted(tmp_path,case):
    data=fixture_bundle(tmp_path);path=tmp_path/'engineering.json';report=json.loads(path.read_text())
    if case in ('partial_fill_protection','overnight_restart_adoption'):
        report['checks']=[c for c in report['checks'] if c['name']!=case]
    elif case=='unhashed_support':report['checks'][0]['artifacts']=['not-in-manifest.json']
    else:
        next(c for c in report['checks'] if c['name']=='emergency_seven_position_load')['measured_seconds']=1.01
    path.write_text(json.dumps(report))
    data['evidence']=replace(data['evidence'],files=tuple((name,file_hash(tmp_path/name)) for name,_ in data['evidence'].files))
    result=assess_net_edge_confirmation(**data)
    assert result.status=='fail'
    assert 'unverified_evidence:engineering.json' in result.reasons
