"""Fixed four-hypothesis daily research; offline only, never broker-connected."""
from __future__ import annotations
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.research.strategy_spec import ETF_UNIVERSE

SCENARIOS=(('zero_cost_counterfactual',0.,0),('base',7.,0),('double_cost',14.,0),('one_session_delay',7.,1))
SOURCES=('src/trader_engine/backtest/engine.py','src/trader_engine/backtest/daily_etf.py',
         'src/trader_engine/research/daily_hypotheses.py','scripts/prepare_daily_hypothesis_inputs.py',
         'scripts/run_daily_pattern_research.py','docs/DAILY_PATTERN_PROTOCOL.md','docs/NEXT_HYPOTHESES.md')

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    if isinstance(value,(pd.Timestamp,datetime,date)):return value.isoformat()
    if isinstance(value,np.generic):return clean(value.item())
    if isinstance(value,float) and not np.isfinite(value):return None
    return value

def write_json(path,value):
    path.write_text(json.dumps(clean(value),indent=2,allow_nan=False)+'\n')

def summarize(result):
    ledger=result.equity_curve
    net=ledger.equity.to_numpy(float)
    gross=100000.+ledger.cumulative_gross_pnl.to_numpy(float)
    costs=ledger.cumulative_impact_cost.to_numpy(float)+ledger.cumulative_fees.to_numpy(float)
    if not np.allclose(net,ledger.cash+ledger.gross_exposure+ledger.dividend_receivable,atol=1e-6,rtol=0):
        raise AssertionError('Daily cash/positions/receivables identity failed')
    if not np.allclose(gross-net,costs,atol=1e-6,rtol=0):
        raise AssertionError('Daily fixed-fill gross minus costs identity failed')
    if (ledger.cash < -1e-6).any() or not np.isfinite(gross).all():raise AssertionError('Invalid cash/gross equity')
    if abs(result.symbol_contributions.net_pnl.sum()-(net[-1]-100000.))>1e-6:
        raise AssertionError('Symbol contribution identity failed')
    if abs(result.metrics['final_equity']-net[-1])>1e-6:raise AssertionError('Terminal equity mismatch')
    net_previous=np.r_[100000.,net[:-1]];gross_previous=np.r_[100000.,gross[:-1]]
    peaks=np.maximum.accumulate(np.r_[100000.,net])[1:]
    exposure=ledger.gross_exposure.to_numpy(float)/net
    transactions=result.decisions
    notional=0.
    if not transactions.empty:
        executed=transactions.loc[transactions.action.isin(['entry','add','exit','reduce'])]
        if not executed.empty:notional=float((executed.quantity*executed.raw_price).sum())
    trades=result.trades
    return {'net_return':net[-1]/100000.-1,'gross_return_same_fills':gross[-1]/100000.-1,
        'mean_daily_net_return':float(np.mean(net/net_previous-1)),
        'mean_daily_gross_return_same_fills':float(np.mean(gross/gross_previous-1)),
        'max_drawdown':float(np.max(1-net/peaks)),
        'average_exposure':float(np.mean(exposure)),'max_exposure':float(np.max(exposure)),
        'annualized_turnover':notional/float(np.mean(net))*252/len(net),
        'mean_closed_episode_net_pnl':None if trades.empty else float(trades.pnl.mean()),
        'mean_closed_episode_gross_pnl':None if trades.empty else float(trades.gross_pnl.mean()),
        'closed_episode_win_rate':None if trades.empty else float((trades.pnl>0).mean()),
        'median_completed_holding_sessions':None if trades.empty else float(trades.completed_sessions.median()),
        'identity_checks_passed':True}

def run(inputs,output):
    inputs=Path(inputs).resolve();output=Path(output).resolve()
    if output.exists():raise FileExistsError('Preserve prior evidence; choose a new output directory')
    if shutil.disk_usage(output.parent).free<2*1024**3:raise RuntimeError('Require at least2GiB free for bounded research')
    manifest=json.loads((inputs/'manifest.json').read_text())
    expected=manifest.get('hashes',manifest.get('files',{}))
    if not expected:raise ValueError('Input manifest requires file hashes')
    for name,sha in expected.items():
        p=Path(name)
        if p.is_absolute() or '..' in p.parts:raise ValueError('Unsafe input path')
        if digest(inputs/p)!=sha:raise ValueError('Input integrity failure: '+name)
    frames={s:pd.read_parquet(inputs/'daily'/f'{s}.parquet') for s in ETF_UNIVERSE}
    schedule=pd.read_parquet(inputs/'schedule.parquet')
    actions=pd.read_parquet(inputs/'actions.parquet')
    dates=pd.to_datetime(schedule.market_open,utc=True).dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
    windows=(('development',dates.iloc[252],'2023-12-29'),('consumed_diagnostic','2024-01-02','2026-09-23'))
    source_hashes={name:digest(ROOT/name) for name in SOURCES}
    protocol={'created_at_utc':datetime.now(timezone.utc).isoformat(),'purpose':'development_only',
              'initial_capital':100000.,'hypotheses':['H1','H2','H3','H4'],'universe':ETF_UNIVERSE,
              'windows':windows,'scenarios':SCENARIOS,'candidate_replays':32,'benchmark_replays':16,
              'source_hashes':source_hashes,'input_manifest_sha256':digest(inputs/'manifest.json'),
              'input_hashes':expected,'data_qualification':manifest,
              'promotion_authorized':False,'business_overhead_known':False,
              'benchmark_timing':'first window open, including candidate-delay reference rows',
              'effective_configurations':44,'repeated_benchmark_references':4,
              'resource_budget':{'sequential_workers':1,'minimum_free_bytes':2*1024**3,
                                 'expected_output_max_bytes':256*1024**2}}
    output.mkdir(parents=True)
    write_json(output/'protocol.json',protocol)
    for name in SOURCES:
        target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    summaries=[];identities=[]
    for window,start,end in windows:
        expected_days=sum((dates>=start)&(dates<=end))
        for scenario,bps,delay in SCENARIOS:
            for candidate,weight in [(f'H{i}',None) for i in range(1,5)]+[('BH100',1.),('BH25',.25)]:
                if shutil.disk_usage(output).free<1024**3:raise RuntimeError('Research stopped: less than1GiB free')
                name=f'{window}_{candidate}_{scenario}';destination=output/name;destination.mkdir()
                result=BacktestEngine.run_daily_etf_replay(frames,candidate if weight is None else 'H1',
                    100000.,schedule=schedule,corporate_actions=actions,start=start,end=end,
                    one_way_bps=bps,delay_sessions=delay,benchmark_weight=weight)
                ledger=result.equity_curve
                if len(ledger)!=expected_days:raise AssertionError(f'{name}: missing evaluated sessions')
                if not np.isfinite(ledger['equity']).all():raise AssertionError(f'{name}: invalid equity')
                ledger.to_csv(destination/'daily.csv',index=True)
                result.trades.to_csv(destination/'trades.csv',index=False)
                result.decisions.to_csv(destination/'decisions.csv',index=False)
                result.symbol_contributions.to_csv(destination/'contributions.csv',index=False)
                write_json(destination/'positions.json',result.positions)
                statistics=summarize(result)
                write_json(destination/'metrics.json',{**result.metrics,**statistics})
                record={**result.metrics,**statistics,'window':window,'start':start,'end':end,'candidate':candidate,'scenario':scenario,
                        'one_way_bps':bps,'delay_sessions':delay,'sessions':len(ledger)}
                summaries.append(clean(record));identities.append(name)
                write_json(output/'progress.json',{'completed':len(summaries),'expected':48,'last':name})
                print(f'{len(summaries)}/48 {name}',flush=True)
    if len(set(identities))!=48:raise AssertionError('Scenario identity count mismatch')
    for name,sha in source_hashes.items():
        if digest(ROOT/name)!=sha:raise RuntimeError('Source changed during campaign: '+name)
    for name,sha in expected.items():
        if digest(inputs/name)!=sha:raise RuntimeError('Inputs changed during campaign')
    pd.DataFrame(summaries).to_csv(output/'summary.csv',index=False)
    write_json(output/'summary.json',summaries)
    files={str(p.relative_to(output)):digest(p) for p in output.rglob('*') if p.is_file()}
    write_json(output/'evidence_manifest.json',{'complete':True,'completed':48,'candidate_replays':32,
        'benchmark_replays':16,'unique_execution_identities':48,'effective_configurations':44,'repeated_benchmark_references':4,'files':files,
        'bytes_before_manifest':sum(p.stat().st_size for p in output.rglob('*') if p.is_file())})
    return summaries

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();run(args.inputs,args.output)
