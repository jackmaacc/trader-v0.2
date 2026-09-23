from pathlib import Path
import sys,argparse,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pandas as pd,numpy as np,yaml
from trader_engine.research.relative_value import RelativeValueConfig,make_forecasts,run_relative_value,validate_inputs
from trader_engine.data.relative_value_data import download_relative_data

def main():
    p=argparse.ArgumentParser(description='Run a fixed causal relative-value hypothesis; no broker orders')
    p.add_argument('--config',default='configs/relative_value.yaml');p.add_argument('--data-dir',required=True);p.add_argument('--output-dir',required=True);p.add_argument('--download',action='store_true');p.add_argument('--start',default='2022-01-01');p.add_argument('--end',default='2026-09-22');args=p.parse_args()
    cfg=yaml.safe_load(Path(args.config).read_text());c=RelativeValueConfig.model_validate(cfg['model']);pairs=cfg['pairs'];symbols=sorted({s for pair in pairs for s in pair});source=Path(args.data_dir);out=Path(args.output_dir)
    if out.exists():raise FileExistsError('Choose a new output directory')
    if args.download:download_relative_data(symbols,args.start,args.end,source)
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('adjustment')!='all' or manifest.get('timeframe')!='1Day':raise ValueError('Wrong data contract')
    for name in ['calendar.csv']+[s+'.parquet' for s in symbols]:
        if manifest['hashes'].get(name)!=hashlib.sha256((source/name).read_bytes()).hexdigest():raise ValueError('Dataset fingerprint mismatch: '+name)
    frames={s:pd.read_parquet(source/(s+'.parquet')) for s in symbols};dates=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(source/'calendar.csv').session));validate_inputs(frames,dates,pairs)
    for name,start,end in cfg['periods']:
        if pd.Timestamp(start)<dates[0] or pd.Timestamp(end)>dates[-1]:raise ValueError('Evaluation extends outside dataset')
    out.mkdir(parents=True)
    code=Path(sys.modules[make_forecasts.__module__].__file__)
    (out/'frozen_run.json').write_text(json.dumps(dict(config=cfg,data_manifest=manifest,code_sha256=hashlib.sha256(code.read_bytes()).hexdigest(),config_sha256=hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),created_at=pd.Timestamp.now(tz='UTC').isoformat()),indent=2))
    forecasts=make_forecasts(frames,pairs,c);forecasts.to_csv(out/'forecasts.csv',index=False);summary=[];cal=[]
    for name,start,end in cfg['periods']:
        measured=forecasts.loc[(forecasts.signal_at>=start)&(forecasts.signal_at<=end)&(forecasts.label_available_at<=pd.Timestamp(end))].dropna(subset=['label'])
        for pair,g in measured.groupby('pair'):
            mse=float(((g.prediction-g.label)**2).mean());zero=float((g.label**2).mean());cal.append(dict(period=name,pair=pair,forecasts=len(g),forecast_mse=mse,zero_mse=zero,skill_vs_zero=1-mse/zero if zero else None,mean_prediction=float(g.prediction.mean()),mean_label=float(g.label.mean())))
        for multiple in [1,2]:
            model=RelativeValueConfig.model_validate(c.model_dump()|{'one_way_cost_bps':c.one_way_cost_bps*multiple,'annual_borrow_rate':c.annual_borrow_rate*multiple})
            result=run_relative_value(frames,dates,pairs,forecasts,model,start,end);folder=out/f'{name}_cost{multiple}';folder.mkdir();result.equity.to_csv(folder/'equity.csv');result.trades.to_csv(folder/'trades.csv',index=False);result.decisions.to_csv(folder/'decisions.csv',index=False)
            # Diagnostic long-only benchmark; it is not a matched-risk hedged benchmark.
            d=dates[(dates>=start)&(dates<=end)];unit=c.initial_capital*.5/len(symbols);rate=model.one_way_cost_bps/10000
            benchmark=sum(unit/(frames[s].loc[d[0],'open']*(1+rate))*frames[s].loc[d[-1],'close']*(1-rate)-unit for s in symbols)
            summary.append(dict(period=name,cost_multiple=multiple,benchmark_long_only_pnl=float(benchmark),**result.metrics));print(json.dumps(summary[-1]),flush=True)
    pd.DataFrame(summary).to_csv(out/'summary.csv',index=False);pd.DataFrame(cal).to_csv(out/'forecast_calibration.csv',index=False)
    (out/'review_status.json').write_text(json.dumps(dict(approved_for_trading=False,broker_orders_submitted=0,reasons=['retrospective_market_dates','adjusted_prices_not_executable_fills','historical_borrow_and_short_distributions_unverified','no_fresh_prospective_evidence','dependent_forecasts_need_robust_uncertainty_review']),indent=2))
    rows=['# Relative-value research result','','Research only. All three relationships were specified before returns; none was selected for promotion. Adjusted units, assumed borrow, and retrospective dates prevent a deployment claim.','','| Period | Costs | Net P&L | Trades | Daily-close drawdown |','|---|---:|---:|---:|---:|']
    for r in summary:rows.append(f"| {r['period']} | {r['cost_multiple']}x | ${r['net_pnl']:,.2f} | {r['trades']} | {r['max_drawdown']:.2%} |")
    rows+=['','See forecast_calibration.csv for mature-label forecast error versus zero. The long-only benchmark is diagnostic and not risk matched. A smaller stressed loss may reflect changed entries, not better execution. No financial orders were submitted.']
    (out/'REPORT.md').write_text('\n'.join(rows)+'\n')
if __name__=='__main__':main()
