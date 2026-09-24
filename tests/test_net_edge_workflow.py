import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from trader_engine.workflows.net_edge import freeze_registry,load_registry,load_dataset,run_research,analyze_snapshot,calendar_costs
from trader_engine.research.net_edge import file_hash
from trader_engine.research.strategy_spec import ETF_UNIVERSE


def dataset(root):
    (root/'minutes').mkdir(parents=True);(root/'daily').mkdir()
    index=pd.date_range('2026-01-02 14:30',periods=30,freq='min',tz='UTC')
    for s in ETF_UNIVERSE:
        pd.DataFrame(dict(open=100.,high=100.1,low=99.9,close=100.,volume=10000.),index=index).to_parquet(root/f'minutes/{s}.parquet')
        pd.DataFrame(dict(high=101.,low=99.,close=100.,total_return_close=100.,signal_scale=1.),index=pd.date_range('2025-09-01',periods=70,freq='B',tz='UTC')).to_parquet(root/f'daily/{s}.parquet')
    pd.DataFrame({'market_open':[index[0]],'market_close':[index[-1]+pd.Timedelta(minutes=1)]}).to_parquet(root/'schedule.parquet')
    hashes={str(p.relative_to(root)):file_hash(p) for p in root.rglob('*.parquet')}
    (root/'manifest.json').write_text(json.dumps({'hashes':hashes}))


def test_freeze_unknown_and_tamper(tmp_path):
    path=tmp_path/'registry.json';registry=freeze_registry(path)
    assert registry['operating_cost_monthly'] is None
    assert len(load_registry(path)[1])==4
    with pytest.raises(FileExistsError):freeze_registry(path)
    registry['operating_cost_monthly']=0;path.write_text(json.dumps(registry))
    with pytest.raises(ValueError,match='hash mismatch'):load_registry(path)


def test_data_integrity(tmp_path):
    dataset(tmp_path);load_dataset(tmp_path)
    (tmp_path/'minutes/SPY.parquet').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='integrity'):load_dataset(tmp_path)


def test_research_short_sample_cannot_qualify(tmp_path):
    data=tmp_path/'data';dataset(data);registry=tmp_path/'registry.json';freeze_registry(registry)
    result=run_research(data,registry,tmp_path/'research',diagnostic_last=1)
    assert result['mode']=='diagnostic' and result['champion'] is None
    assert len(result['candidates'])==4
    assert all('diagnostic_not_qualification' in c['reasons'] for c in result['candidates'])
    assert all('unknown_operating_overhead' in c['reasons'] for c in result['candidates'])
    assert (tmp_path/'research/benchmarks_7bps.parquet').exists()
    assert len(list((tmp_path/'research').glob('*/cost*/metrics.json')))==24
    assert not list((tmp_path/'research').glob('*/bootstrap.json'))


def test_analysis_dates_and_sources(tmp_path):
    raw={'as_of':'2026-09-23T16:00:00Z','strategy_hash':'a'*64,'symbols':['SPY'],'evidence':[]}
    path=tmp_path/'snapshot.json';path.write_text(json.dumps(raw))
    report=analyze_snapshot(path)
    assert len(report['reports'])==5 and report['order_authority'] is False
    raw['evidence']=[dict(source_id='one',source_url='https://example.com',kind='technical',observed_at='2026-09-24T16:00:00Z',published_at='2026-09-24T16:00:00Z',available_at='2026-09-24T16:00:00Z',data={'symbol':'SPY','closes':[100,101]})]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='Future'):analyze_snapshot(path)


def test_calendar_overhead_daily_includes_weekends():
    schedule=pd.DataFrame({'market_open':pd.to_datetime(['2026-01-30T14:30Z','2026-02-02T14:30Z'])})
    costs=calendar_costs(schedule,310)
    assert len(costs)==4 and sum(costs.values())==pytest.approx(20+620/28)


@pytest.mark.parametrize("bad_quote", [None, "malformed", {"raw_payload": {"t": "bad"}}])
def test_shadow_snapshot_runs_offline_and_preserves_risk(tmp_path, bad_quote):
    from trader_engine.workflows.net_edge import run_shadow_snapshot
    from trader_engine.research.strategy_spec import StrategySpec
    dataset(tmp_path)
    registry=tmp_path/'registry.json';freeze_registry(registry)
    state=dict(version=1,equity='99400',highwater='100000',daily_baseline='100000',cashflow_total='0',session='2026-01-02',daily_halt=False,drawdown_halt=False)
    risk=tmp_path/'risk.json';risk.write_text(json.dumps(state));prior=risk.read_bytes()
    raw=dict(context=dict(as_of='2026-01-02T15:00:00Z',strategy_hash=StrategySpec('MR30').spec_hash,symbols=list(ETF_UNIVERSE),evidence=[]),
        histories={s:f'minutes/{s}.parquet' for s in ETF_UNIVERSE},quotes={'SPY':bad_quote},risk_state='risk.json',positions=[],cash=99400,
        session_open='2026-01-02T14:30:00Z',session_close='2026-01-02T21:00:00Z')
    snapshot=tmp_path/'snapshot.json';snapshot.write_text(json.dumps(raw))
    result=run_shadow_snapshot(snapshot,registry,'MR30',tmp_path/'shadow')
    assert result['formal_forward_run'] is False and result['broker_orders_submitted']==0
    assert len(result['decisions'])==5
    spy=next(row for row in result['decisions'] if row['symbol']=='SPY')
    assert 'unexpected_feed' in spy['quote']['reasons']
    assert 'invalid_local_timestamp' in spy['quote']['reasons']
    assert not any(row['eligible'] for row in result['decisions'])
    assert risk.read_bytes()==prior
    saved=json.loads((tmp_path/'shadow/manifest.json').read_text())
    assert saved['account_risk']['reason']=='daily_halt'
    assert (tmp_path/'shadow/team_report.json').exists()
    raw['risk_state']='../outside.json';snapshot.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='stay inside'):run_shadow_snapshot(snapshot,registry,'MR30',tmp_path/'blocked')
