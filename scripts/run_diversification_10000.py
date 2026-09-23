"""Reproducible 5,000-rule / two-cost daily study. No trading endpoints."""
from pathlib import Path
import argparse,hashlib,itertools,json,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import numpy as np,pandas as pd
from trader_engine.research.relative_value import RelativeValueConfig,make_forecasts
from trader_engine.research.daily_relative_value import run_daily_relative_value
from trader_engine.research.diversification_trials import prepare_inputs,run_batch,correlation_to

PAIRS=[['QQQ','QQQM'],['SOXX','SMH'],['SPY','IVV']]

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True);parser.add_argument('--output-dir',required=True);args=parser.parse_args()
    source=Path(args.data_dir);out=Path(args.output_dir)
    if out.exists():raise FileExistsError('New output directory required')
    manifest=json.loads((source/'manifest.json').read_text());symbols=sorted({s for p in PAIRS for s in p})
    if manifest.get('adjustment')!='all' or manifest.get('timeframe')!='1Day':raise ValueError('Wrong data contract')
    for name in ['calendar.csv']+[s+'.parquet' for s in symbols]:
        if manifest['hashes'].get(name)!=digest(source/name):raise ValueError('Changed data: '+name)
    frames={s:pd.read_parquet(source/(s+'.parquet')) for s in symbols};dates=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(source/'calendar.csv').session))
    if dates[-1]<pd.Timestamp('2026-09-22'):raise ValueError('Incomplete YTD data')
    models=list(itertools.product([20,40,60,90,120],[120,180,252],[60,90,120]))
    choices=[]
    for mid in range(len(models)):
        for mask in range(1,8):
            for u,buffer,allocation,cap in itertools.product([0.,.5,1.,1.5,2.,2.5],[1.,1.25,1.5,2.],[.05,.1,.15,.2],range(1,mask.bit_count()+1)):
                choices.append((mid,mask,u,buffer,allocation,cap))
    rng=np.random.default_rng(20260923);selected=rng.choice(len(choices),5000,replace=False)
    candidates=pd.DataFrame([choices[i] for i in selected],columns=['forecast_id','pair_mask','uncertainty','cost_buffer','pair_gross','max_positions']);candidates.insert(0,'candidate_id',np.arange(5000))
    assert not candidates.drop(columns='candidate_id').duplicated().any()
    out.mkdir(parents=True);candidates.to_csv(out/'candidates.csv',index=False)
    protocol=dict(created_at=pd.Timestamp.now(tz='UTC').isoformat(),seed=20260923,unique_rule_sets=5000,ytd_runs=10000,cost_multipliers=[1,2],pairs=PAIRS,forecast_models=models,data=manifest,candidate_sha256=digest(out/'candidates.csv'),validation_periods=['2024','2025'],ytd_period=['2026-01-01','2026-09-22'],screen='Each of 2024 and 2025: positive net at doubled costs, >=40 trades, absolute SPY and QQQ daily return correlation <0.5, no drawdown halt. Rank survivors by worst-year stressed return, ties by candidate_id. Lock before YTD. No survivor means no candidate selected.',limitations=['retrospective dates already consumed by prior research','adjusted price proxy, unverified execution/borrow/short availability','daily close risk checks do not enforce intraday stops','45 related forecast models and shared data create dependent trials','low correlation alone is not a profitable edge','no personal holdings supplied; SPY and QQQ are proxies'],code_hashes={Path(sys.modules[f.__module__].__file__).name:digest(sys.modules[f.__module__].__file__) for f in [make_forecasts,run_daily_relative_value,run_batch]})
    protocol['runner_sha256']=digest(__file__);(out/'protocol.json').write_text(json.dumps(protocol,indent=2))
    forecasts=[];cache=out/'forecasts';cache.mkdir()
    for mid,(feature,training,minimum) in enumerate(models):
        c=RelativeValueConfig(feature_lookback=feature,training_lookback=training,minimum_labels=minimum)
        fc=make_forecasts(frames,PAIRS,c,same_day_exit=True);fc.to_parquet(cache/f'{mid:02d}.parquet',index=False);forecasts.append(fc)
        if (mid+1)%5==0:print(f'Forecast models prepared: {mid+1}/{len(models)}',flush=True)
    inputs=prepare_inputs(frames,dates,PAIRS,forecasts)
    # Full dataset/prefix equality independently checks one representative forecast cache.
    cut=pd.Timestamp('2025-12-31');feature,training,minimum=models[0]
    prefix=make_forecasts({s:f.loc[:cut] for s,f in frames.items()},PAIRS,RelativeValueConfig(feature_lookback=feature,training_lookback=training,minimum_labels=minimum),same_day_exit=True)
    cols=['pair','signal_at','feature','beta','prediction','mean_prediction_se','training_labels','last_training_label_at']
    pd.testing.assert_frame_equal(forecasts[0].loc[forecasts[0].signal_at<=cut,cols].reset_index(drop=True),prefix[cols].reset_index(drop=True))
    benchmarks={s:frames[s].close.pct_change() for s in ['SPY','QQQ']}
    validations=[];survive=np.ones(5000,dtype=bool);worst=np.full(5000,np.inf)
    for year in [2024,2025]:
        m,r,t,days=run_batch(inputs,candidates,f'{year}-01-01',f'{year}-12-31',cost_multiple=2)
        for s in benchmarks:m['correlation_'+s]=correlation_to(r,benchmarks[s].loc[days].values)
        valid=(m.net_pnl>0)&(m.trades>=40)&(~m.halted)&(m.correlation_SPY.abs()<.5)&(m.correlation_QQQ.abs()<.5)
        survive&=valid.to_numpy();worst=np.minimum(worst,m.total_return);m['period']=year;m['passes_period_screen']=valid;validations.append(m)
        print(f'Pre-YTD {year}: {int(valid.sum())} pass period screen; {int(survive.sum())} pass both so far',flush=True)
    pd.concat(validations).to_csv(out/'pre_ytd_screen.csv',index=False)
    survivors=np.flatnonzero(survive);chosen=int(sorted(survivors,key=lambda i:(-worst[i],i))[0]) if len(survivors) else None
    locked=dict(selected_candidate_id=chosen,eligible_candidates=len(survivors),locked_at=pd.Timestamp.now(tz='UTC').isoformat(),basis='2024 and 2025 only; no YTD ranking',approved_for_trading=False)
    (out/'selection_before_ytd.json').write_text(json.dumps(locked,indent=2));print('Selection locked: '+json.dumps(locked),flush=True)
    summaries=[];allreturns=[];alltrades=[];reference_checks=[]
    for multiple in [1,2]:
        m,r,t,days=run_batch(inputs,candidates,'2026-01-01','2026-09-22',cost_multiple=multiple)
        m['trial_id']=m.candidate_id.astype(str)+'_cost'+str(multiple)
        for s in benchmarks:m['correlation_'+s]=correlation_to(r,benchmarks[s].loc[days].values)
        spy=benchmarks['SPY'].loc[days].values
        m['blend_90pct_SPY_10pct_strategy_return']=np.prod(1+.9*spy[None,:]+.1*r,axis=1)-1
        m['blend_increment_vs_90pct_SPY_10pct_cash']=m.blend_90pct_SPY_10pct_strategy_return-(np.prod(1+.9*spy)-1)
        m['positive_months']=sum((np.prod(1+r[:,days.month==month],axis=1)-1)>1e-12 for month in sorted(set(days.month)))
        # Compare every daily return with the original event-based simulator for 12 configurations per cost.
        check_ids=list(range(6))+m.sort_values('trades',ascending=False).candidate_id.head(6).tolist()
        for cid in sorted(set(check_ids)):
            row=candidates.iloc[cid];pairs=[p for pi,p in enumerate(PAIRS) if int(row.pair_mask)&(1<<pi)];fc=forecasts[int(row.forecast_id)];fc=fc.loc[fc.pair.isin([a+'/'+b for a,b in pairs])]
            model=RelativeValueConfig(uncertainty_multiple=float(row.uncertainty),cost_buffer=float(row.cost_buffer),pair_gross=float(row.pair_gross),max_positions=int(row.max_positions),one_way_cost_bps=7*multiple,annual_borrow_rate=.03*multiple)
            ref=run_daily_relative_value(frames,dates,pairs,fc,model,'2026-01-01','2026-09-22')
            np.testing.assert_allclose(r[cid],ref.equity.daily_return,atol=1e-13,rtol=0)
            np.testing.assert_array_equal(t[cid],ref.equity.trades)
            assert abs(m.iloc[cid].net_pnl-ref.metrics['net_pnl'])<1e-6
            reference_checks.append(dict(candidate_id=cid,cost_multiple=multiple,matched=True))
        summaries.append(m);allreturns.append(r);alltrades.append(t)
        print(f'YTD completed {multiple*5000}/10000: positive={int((m.net_pnl>1e-7).sum())}, loss={int((m.net_pnl < -1e-7).sum())}, inactive={int((m.trades==0).sum())}',flush=True)
    summary=pd.concat(summaries,ignore_index=True);summary.to_csv(out/'ytd_10000.csv',index=False)
    returns=np.vstack(allreturns);trade_counts=np.vstack(alltrades)
    np.savez_compressed(out/'daily_paths.npz',returns=returns,trade_counts=trade_counts,dates=days.strftime('%Y-%m-%d').values.astype(str),trial_ids=summary.trial_id.values.astype(str))
    fingerprints=[hashlib.sha256(np.round(r,12).tobytes()+t.tobytes()).hexdigest() for r,t in zip(returns,trade_counts)]
    summary['outcome_fingerprint']=fingerprints;summary.to_csv(out/'ytd_10000.csv',index=False)
    top=summary.sort_values(['net_pnl','trial_id'],ascending=[False,True]).head(20);top.to_csv(out/'exploratory_top20_NOT_SELECTED.csv',index=False)
    stats=dict(ytd_runs=len(summary),unique_configurations=len(candidates)*2,unique_daily_outcomes=len(set(fingerprints)),positive=int((summary.net_pnl>1e-7).sum()),negative=int((summary.net_pnl< -1e-7).sum()),inactive=int((summary.trades==0).sum()),eligible_pre_ytd=int(len(survivors)),selected_candidate_id=chosen,reference_checks=len(reference_checks),causal_prefix_check=True,broker_orders_submitted=0,approved_for_trading=False)
    (out/'verification.json').write_text(json.dumps(dict(**stats,reference_comparisons=reference_checks),indent=2))
    lines=['# 10,000 YTD diversification simulations','', 'January 1–September 22, 2026; 181 sessions; $100,000 per run. Five thousand distinct rule sets, each at 7 and 14 basis points per side plus assumed short borrow. Each strategy may open pairs at the session open and closes them that day. No forced entries.','','## Screening decision','',f'Candidates passing both pre-2026 stressed screens: {len(survivors)} / 5,000. Candidate locked before YTD: {chosen}. No selection is made from the YTD leaderboard. Dates remain retrospective and previously used in research.','','## All results','',f"Of 10,000 runs: {stats['positive']} profitable, {stats['negative']} losing, {stats['inactive']} inactive. Distinct daily outcomes (returns rounded to 12 decimals plus trade counts): {stats['unique_daily_outcomes']}. Shared data and similar rules make these dependent trials, not independent confirmation.",'','| Costs | Profitable | Losing | Inactive | Best net P&L* | Worst net P&L |','|---|---:|---:|---:|---:|---:|']
    for multiple,m in zip([1,2],summaries):lines.append(f"| {multiple}x | {(m.net_pnl>1e-7).sum()} | {(m.net_pnl< -1e-7).sum()} | {(m.trades==0).sum()} | ${m.net_pnl.max():,.2f} | ${m.net_pnl.min():,.2f} |")
    lines+=['','*Best YTD outcomes are exploratory winners from thousands of attempts, not validated strategies. No statistical significance or return guarantee is claimed.','','## Diversification measurement','','Daily correlations with SPY and QQQ include cash days; constant-return strategies have undefined correlation, not zero. The hypothetical 90% SPY / 10% strategy blend is rebalanced daily and compared with 90% SPY / 10% cash to avoid crediting lower exposure as trading skill. It omits SPY-rebalancing costs and personal taxes. Your holdings are unknown; this is not measured correlation with your personal portfolio.','','## Scope and limitations','','Only QQQ/QQQM, SOXX/SMH and SPY/IVV are covered. Forecast lookbacks, training windows, label counts, uncertainty thresholds, cost buffers, pair subsets and position caps vary. Every parameter set and outcome is retained. There are 10,000 YTD runs plus 10,000 earlier-period screening runs. This is not a worldwide asset search.','','Adjusted prices and fractional holdings are research proxies, not executable fills. Two-leg fills, borrow availability and distributions are not independently verified. The 5% account drawdown halt is checked at close; intraday stops and daily loss limits are not simulated. No future labels are used for training. No broker orders are submitted.','','## Files','','- candidates.csv: 5,000 unique rule sets; protocol.json: frozen grids, seed and source fingerprints.','- pre_ytd_screen.csv and selection_before_ytd.json: chronological screening and selection.','- ytd_10000.csv: all YTD metrics and diversification diagnostics.','- daily_paths.npz: all daily returns and trade counts, aligned to trial IDs.','- exploratory_top20_NOT_SELECTED.csv: explicitly unvalidated historical leaderboard.','- verification.json: reference-engine comparisons, causality and counts.']
    if chosen is not None:
        selected_rows=summary.loc[summary.candidate_id==chosen];selected_rows.to_csv(out/'locked_candidate_ytd.csv',index=False)
        lines+=['','## Locked candidate YTD','',selected_rows[['trial_id','net_pnl','trades','max_drawdown','correlation_SPY','correlation_QQQ']].to_string(index=False),'','Passing a retrospective screen does not authorize deployment; fresh prospective evidence and executable-data validation remain required.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n');print(json.dumps(stats),flush=True)
if __name__=='__main__':main()
