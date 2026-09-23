from pathlib import Path
import sys,argparse,json,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pandas as pd
import yaml
from trader_engine.intraday.engine import IntradayConfig,run_intraday,audit_coverage,intraday_benchmark
from trader_engine.data.alpaca_intraday import download_minutes


def main():
    parser=argparse.ArgumentParser(description='Intraday research only; never sends broker orders')
    parser.add_argument('--config',default='configs/intraday.yaml');parser.add_argument('--data-dir',required=True);parser.add_argument('--output-dir',required=True)
    parser.add_argument('--download',action='store_true');parser.add_argument('--start');parser.add_argument('--end')
    args=parser.parse_args();settings=yaml.safe_load(Path(args.config).read_text());symbols=settings['symbols'];c=IntradayConfig.model_validate(settings['strategy'])
    if args.download:
        if not args.start or not args.end:parser.error('--download requires --start and --end')
        download_minutes(symbols,args.start,args.end,args.data_dir,feed=settings.get('feed','sip'))
    source=Path(args.data_dir);manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('timeframe')!='1Min' or manifest.get('adjustment')!='raw' or manifest.get('feed')!=settings.get('feed','sip'):raise ValueError('Dataset contract differs from configuration')
    required=['calendar.csv']+[f'{s}.parquet' for s in symbols]
    for name in required:
        if manifest['hashes'].get(name)!=hashlib.sha256((source/name).read_bytes()).hexdigest():raise ValueError('Missing or changed dataset fingerprint: '+name)
    frames={s:pd.read_parquet(source/f'{s}.parquet') for s in symbols};calendar=pd.read_csv(source/'calendar.csv')
    out=Path(args.output_dir)
    if out.exists():raise FileExistsError('Choose a new output directory to preserve earlier experiments')
    out.mkdir(parents=True);issues=audit_coverage(frames,calendar);issues.to_csv(out/'coverage_issues.csv',index=False)
    (out/'frozen_run.json').write_text(json.dumps({'config':settings,'data_manifest':manifest,'config_sha256':hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),'code_sha256':hashlib.sha256(Path(sys.modules[run_intraday.__module__].__file__).read_bytes()).hexdigest(),'started_at':pd.Timestamp.now(tz='UTC').isoformat(),'broker_orders_submitted':0},indent=2))
    if not issues.empty:
        (out/'review_status.json').write_text(json.dumps({'historical_diagnostic_passed':False,'approved_for_trading':False,'reason':'missing_minute_coverage','broker_orders_submitted':0},indent=2))
        raise ValueError('Strict replay blocked by missing minute data. See coverage_issues.csv; no incomplete sessions were silently dropped.')
    summaries=[]
    for multiple in [1,2,4]:
        cfg=IntradayConfig.model_validate(c.model_dump()|{'one_way_cost_bps':c.one_way_cost_bps*multiple});result=run_intraday(frames,calendar,cfg);folder=out/f'cost_{multiple}x';folder.mkdir()
        result.equity.to_csv(folder/'equity.csv');result.daily.to_csv(folder/'daily.csv',index=False);result.trades.to_csv(folder/'trades.csv',index=False);result.decisions.to_csv(folder/'decisions.csv',index=False)
        benchmark=intraday_benchmark(frames,calendar,cfg);benchmark.to_csv(folder/'benchmark_daily.csv',index=False)
        result.metrics['benchmark_net_pnl']=float(benchmark.equity.iloc[-1]-c.initial_capital)
        result.metrics['benchmark_excess_dollars']=result.metrics['net_pnl']-result.metrics['benchmark_net_pnl']
        (folder/'metrics.json').write_text(json.dumps(result.metrics,indent=2));summaries.append(dict(cost_multiple=multiple,**result.metrics))
    pd.DataFrame(summaries).to_csv(out/'summary.csv',index=False)
    passed=all(r['net_pnl']>0 and r['benchmark_excess_dollars']>0 and r['intraday_close_drawdown']>=-.05 and r['trades']>=100 for r in summaries[:2])
    (out/'review_status.json').write_text(json.dumps({'historical_diagnostic_passed':passed,'approved_for_trading':False,'requires_new_prospective_evidence':True,'broker_orders_submitted':0},indent=2))
    print(json.dumps({'mode':'research_only','output':str(out),'results':summaries},indent=2))

if __name__=='__main__':main()
