"""Freeze and run 100,000 current-strategy YTD execution sensitivities; no orders."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,shutil,sys,time
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from trader_engine.research import daily_sensitivity as h1,crypto_sensitivity as crypto
SEED=20260924
SOURCES=('scripts/run_current_ytd_100000.py','scripts/plot_ytd_sensitivity.py','src/trader_engine/research/daily_sensitivity.py','src/trader_engine/research/crypto_sensitivity.py',
 'src/trader_engine/backtest/daily_etf.py','src/trader_engine/backtest/engine.py','src/trader_engine/research/daily_hypotheses.py',
 'src/trader_engine/research/strategy_spec.py','src/trader_engine/risk/engine.py','src/trader_engine/core/config.py',
 'src/trader_engine/core/models.py','src/trader_engine/data/base.py','src/trader_engine/analytics/metrics.py','docs/YTD_100000_PROTOCOL.md')
MIN_FREE=5*1024**3

def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def write(p,x):Path(p).write_text(json.dumps(x,indent=2,default=lambda x:x.item() if isinstance(x,np.generic) else str(x),allow_nan=False)+'\n')

def guard(p):
    if shutil.disk_usage(p).free<MIN_FREE:raise RuntimeError('Research paused: reserve5GiB for services; preserve incomplete evidence')

def scenarios():
    rng=np.random.default_rng(SEED);records=[]
    for family,upper,anchors in [('H1',28.,(7.,14.)),('CRYPTO_SMA200',30.,(5.,15.))]:
        for delay in (0,1):
            costs=1+(np.arange(25000)+rng.random(25000))*(upper-1)/25000
            costs[0],costs[1],costs[-1]=anchors[0],anchors[1],upper
            for cost in costs:
                config=dict(family=family,impact_bps=float(cost),delay=delay,fee_bps=0. if family=='H1' else 25.)
                records.append(dict(trial_id=len(records),**config,scenario_hash=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()))
    table=pd.DataFrame(records)
    if len(table)!=100000 or table.scenario_hash.nunique()!=100000:raise AssertionError('Scenario uniqueness')
    return table

def freeze(out,etf,coins):
    if out.exists():raise FileExistsError('Use a new evidence directory')
    guard(out.parent);out.mkdir();(out/'inputs').mkdir();sources={}
    for source in SOURCES:
        dest=out/'source'/source;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/source,dest);sources[source]=digest(dest)
    for name,path in [('etf',etf),('crypto',coins)]:
        shutil.copytree(path,out/'inputs'/name)
    raw=coins.parent/'alpaca_crypto_daily.json'
    if not raw.is_file():raise ValueError('Original crypto raw response required')
    shutil.copy2(raw,out/'inputs/crypto/raw_alpaca.json')
    inputs={str(p.relative_to(out/'inputs')):digest(p) for p in (out/'inputs').rglob('*') if p.is_file()}
    grid=scenarios();grid.to_parquet(out/'scenarios.parquet',index=False);grid.to_csv(out/'scenarios.csv',index=False)
    protocol=dict(frozen_at=datetime.now(timezone.utc).isoformat(),start='2026-01-01',end='2026-09-23',initial_cash=100000.,count=100000,
        family_counts={'H1':50000,'CRYPTO_SMA200':50000},seed=SEED,grid_sha256=digest(out/'scenarios.parquet'),
        source_hashes=sources,input_hashes=inputs,rule_protocol='source/docs/YTD_100000_PROTOCOL.md',reference_checks_per_family=12,
        reference_costs={'H1':[0.,1.,7.,14.,27.999,28.],'CRYPTO_SMA200':[0.,1.,5.,15.,29.999,30.]},
        benchmark='Every candidate paired to samecostBH25; H1BH100 at7bps for exposurecontext. Controlsnotcounted.',
        allocation='Independent$100000 accountperscenario; familyresultsnotcombinedportfolio',
        engineering_preflight='SynthetictestsandrepresentativeYTDreferenceparities were inspected for implementation correctness before this machinefreeze; no signals,riskrules,costrange,datesorgridselectedfromreturns. Pythonbooleaninversion andemptytradeparityhelper corrected.',
        resource_budget=dict(workers=1,batch_size=500,minimum_free_bytes=MIN_FREE,expected_peak_output_bytes=1024**3,expected_peak_ram_bytes=512*1024**2),
        overhead_known=False,business_profit_qualified=False,broker_orders_submitted=0,promotion_authorized=False)
    write(out/'protocol.json',protocol);print('FROZEN '+str(out),flush=True)

def check_frozen(out):
    protocol=json.loads((out/'protocol.json').read_text())
    for name,h in protocol['source_hashes'].items():
        if digest(ROOT/name)!=h or digest(out/'source'/name)!=h:raise ValueError('Frozen source changed: '+name)
    for name,h in protocol['input_hashes'].items():
        if digest(out/'inputs'/name)!=h:raise ValueError('Frozen input changed: '+name)
    if digest(out/'scenarios.parquet')!=protocol['grid_sha256']:raise ValueError('Frozen scenarios changed')
    return protocol

def save_batch(folder,result,ids,dates,fields):
    values=result['daily']
    if values.shape!=(len(ids),len(dates),len(fields)) or not np.isfinite(values).all():raise AssertionError('Incomplete or nonfinite daily evidence')
    field={name:values[:,:,k] for k,name in enumerate(fields)}
    exposure=field['gross_exposure' if 'gross_exposure' in field else 'exposure']
    claim=field.get('dividend_receivable',0.)
    if (field['cash'] < -1e-6).any():raise AssertionError('Negative cash')
    np.testing.assert_allclose(field['equity'],field['cash']+exposure+claim,atol=1e-6,rtol=0)
    gross=field['cumulative_gross_pnl' if 'cumulative_gross_pnl' in field else 'gross_pnl']
    net=field['cumulative_net_pnl' if 'cumulative_net_pnl' in field else 'net_pnl']
    impact=field['cumulative_impact_cost' if 'cumulative_impact_cost' in field else 'cumulative_impact']
    np.testing.assert_allclose(net,field['equity']-100000.,atol=1e-6,rtol=0)
    np.testing.assert_allclose(gross-net,impact+field.get('cumulative_fees',0.),atol=1e-6,rtol=0)
    np.testing.assert_allclose(result['metrics'].final_equity,field['equity'][:,-1],atol=1e-6,rtol=0)
    folder.mkdir(parents=True)
    kwargs=dict(values=result['daily'],trial_ids=ids,dates=np.array([str(x) for x in dates]),fields=np.array(fields))
    for name in ('positions','pending_due','pending_target'):
        if name in result:kwargs[name]=result[name]
    np.savez_compressed(folder/'daily.npz',**kwargs)
    fills=result['fills'].copy()
    if not fills.empty:fills['trial_id']=ids[fills.trial_index.to_numpy(int)]
    fills.to_parquet(folder/'fills.parquet',index=False)
    metrics=result['metrics'].copy();metrics.insert(0,'trial_id',ids);metrics.to_parquet(folder/'metrics.parquet',index=False)
    return metrics

def run(out):
    protocol=check_frozen(out)
    if (out/'run_started.json').exists():raise FileExistsError('No implicit resume; preserve existing evidence')
    write(out/'run_started.json',dict(started_at=datetime.now(timezone.utc).isoformat()))
    grid=pd.read_parquet(out/'scenarios.parquet');etf=out/'inputs/etf';coins=out/'inputs/crypto'
    frames={s:pd.read_parquet(etf/'daily'/f'{s}.parquet') for s in h1.SYMBOLS};schedule=pd.read_parquet(etf/'schedule.parquet');actions=pd.read_parquet(etf/'actions.parquet')
    coinframes={s:pd.read_parquet(coins/(s+'.parquet')) for s in crypto.SYMBOLS}
    hp=h1.prepare(frames,schedule,actions,protocol['start'],protocol['end']);cp=crypto.prepare(coinframes,protocol['start'],protocol['end']);cb=crypto.prepare(coinframes,protocol['start'],protocol['end'],benchmark=True)
    if len(hp.dates)!=182 or len(cp['dates'])!=266:raise AssertionError('Unexpected session coverage')
    checks=[]
    for cost in protocol['reference_costs']['H1']:
        for delay in (0,1):checks.append(dict(family='H1',**h1.assert_reference_parity(hp,frames,schedule,actions,cost,delay)))
    for cost in protocol['reference_costs']['CRYPTO_SMA200']:
        for delay in (0,1):checks.append(dict(family='CRYPTO_SMA200',**crypto.assert_reference_parity(cp,cost,delay)))
    checks.append(dict(family='H1_BH25',**h1.assert_reference_parity(hp,frames,schedule,actions,7.,0,.25)))
    checks.append(dict(family='H1_BH100',**h1.assert_reference_parity(hp,frames,schedule,actions,7.,0,1.)))
    checks.append(dict(family='CRYPTO_BH25',**crypto.assert_reference_parity(cb,5.,0)))
    write(out/'reference_verification.json',dict(passed=True,checks=checks));print('REFERENCE_PARITY_PASSED '+str(len(checks)),flush=True)
    check_frozen(out);guard(out)
    allmetrics=[];began=time.perf_counter();batch_size=protocol['resource_budget']['batch_size']
    for base in range(0,len(grid),batch_size):
        guard(out);rows=grid.iloc[base:base+batch_size];family=rows.family.iloc[0];ids=rows.trial_id.to_numpy(int)
        costs=rows.impact_bps.to_numpy();delays=rows.delay.to_numpy(int)
        if family=='H1':
            result=h1.run_batch(hp,costs,delays);benchmark=h1.run_batch(hp,costs,delays,.25);dates=hp.dates;fields=h1.FIELDS
        else:
            result=crypto.run_batch(cp,costs,delays);benchmark=crypto.run_batch(cb,costs,delays);dates=cp['dates'];fields=crypto.DAILY_FIELDS
        folder=out/'batches'/f'{base:06d}'
        metrics=save_batch(folder/'candidate',result,ids,dates,fields);bm=save_batch(folder/'benchmark',benchmark,ids,dates,fields)
        metrics=rows.reset_index(drop=True).merge(metrics,on='trial_id',validate='one_to_one')
        metrics['benchmark_net_pnl']=bm.net_pnl.to_numpy();metrics['excess_net_pnl']=metrics.net_pnl-metrics.benchmark_net_pnl
        metrics['outcome_hash']=[hashlib.sha256(np.round(x,8).tobytes()).hexdigest() for x in result['daily']]
        allmetrics.append(metrics)
        write(folder/'manifest.json',dict(completed=len(ids),trial_start=int(ids[0]),trial_end=int(ids[-1]),family=family,sessions=len(dates),
            files={str(p.relative_to(folder)):digest(p) for p in folder.rglob('*') if p.is_file()}))
        progress=dict(completed=base+len(rows),expected=100000,elapsed_seconds=time.perf_counter()-began,free_bytes=shutil.disk_usage(out).free)
        write(out/'progress.tmp.json',progress);(out/'progress.tmp.json').replace(out/'progress.json')
        if base%2500==0:print(json.dumps(progress),flush=True)
    table=pd.concat(allmetrics,ignore_index=True)
    if len(table)!=100000 or table.trial_id.nunique()!=100000 or table.scenario_hash.nunique()!=100000:raise AssertionError('Incomplete scenario evidence')
    table.to_parquet(out/'all_100000_results.parquet',index=False);table.to_csv(out/'all_100000_results.csv',index=False)
    bh100=h1.run_batch(hp,[7.],[0],1.);save_batch(out/'controls/H1_BH100_base',bh100,np.array([-1]),hp.dates,h1.FIELDS)
    summary=table.groupby(['family','delay']).agg(trials=('trial_id','size'),profitable=('net_pnl',lambda x:int((x>0).sum())),net_min=('net_pnl','min'),net_median=('net_pnl','median'),net_max=('net_pnl','max'),benchmark_net_median=('benchmark_net_pnl','median'),excess_net_median=('excess_net_pnl','median'),distinct_outcomes=('outcome_hash','nunique')).reset_index()
    summary.to_csv(out/'summary.csv',index=False)
    check_frozen(out)
    write(out/'validation.json',dict(completed=100000,unique_configurations=table.scenario_hash.nunique(),unique_daily_outcomes=table.outcome_hash.nunique(),
        daily_candidate_rows=50000*(182+266),daily_benchmark_rows=50000*(182+266),reference_checks=len(checks),accounting_checks=True,
        source_and_input_hashes_unchanged=True,broker_orders_submitted=0,business_profit_qualified=False))
    files={str(p.relative_to(out)):digest(p) for p in out.rglob('*') if p.is_file()}
    write(out/'evidence_manifest.json',dict(complete=True,completed=100000,files=files,bytes_before_manifest=sum(p.stat().st_size for p in out.rglob('*') if p.is_file())))
    print(summary.to_string(index=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['freeze','run']);p.add_argument('--output',type=Path,required=True);p.add_argument('--etf-inputs',type=Path);p.add_argument('--crypto-inputs',type=Path);args=p.parse_args()
    if args.action=='freeze':freeze(args.output,args.etf_inputs,args.crypto_inputs)
    else:run(args.output)
