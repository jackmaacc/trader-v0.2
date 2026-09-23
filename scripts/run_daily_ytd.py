"""Frozen YTD daily comparison, using a fingerprinted local dataset."""
from pathlib import Path
import argparse,hashlib,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pandas as pd,yaml
from trader_engine.research.relative_value import RelativeValueConfig,make_forecasts,validate_inputs,run_relative_value
from trader_engine.research.daily_relative_value import run_daily_relative_value

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default='configs/relative_value.yaml');p.add_argument('--data-dir',required=True);p.add_argument('--output-dir',required=True);p.add_argument('--start',default='2026-01-01');p.add_argument('--end',default='2026-09-22');args=p.parse_args()
    cfg=yaml.safe_load(Path(args.config).read_text());c=RelativeValueConfig(**cfg['model']);pairs=cfg['pairs'];source=Path(args.data_dir);out=Path(args.output_dir)
    if out.exists():raise FileExistsError('New output directory required')
    manifest=json.loads((source/'manifest.json').read_text());symbols=sorted({s for p in pairs for s in p})
    if manifest.get('adjustment')!='all' or manifest.get('timeframe')!='1Day':raise ValueError('Wrong data contract')
    for name in ['calendar.csv']+[s+'.parquet' for s in symbols]:
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=manifest['hashes'].get(name):raise ValueError('Data changed: '+name)
    frames={s:pd.read_parquet(source/(s+'.parquet')) for s in symbols};dates=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(source/'calendar.csv').session));validate_inputs(frames,dates,pairs)
    if pd.Timestamp(args.start)<dates[0] or pd.Timestamp(args.end)>dates[-1]:raise ValueError('Evaluation outside data')
    out.mkdir(parents=True)
    (out/'protocol.json').write_text(json.dumps(dict(start=args.start,end=args.end,config=cfg,overrides={'forecast_horizon':'next_session_open_to_close','hold_sessions':'unused for same-day exit'},variants=['same_day_cost_filtered','same_day_daily_strongest_diagnostic','overnight_cost_filtered'],cost_multipliers=[1,2],minimum_borrow_days=1,data_manifest=manifest,limitations=['adjusted research units, not executable fills','same-day two-leg open and close assumed','daily bars do not validate intraday stops or daily loss caps','drawdown halt checked at close; no overnight positions','daily strongest deliberately bypasses cost filter; no forced trading after drawdown halt','no parameter search or broker orders'],code_hashes={str(Path(sys.modules[m].__file__).name):hashlib.sha256(Path(sys.modules[m].__file__).read_bytes()).hexdigest() for m in [make_forecasts.__module__,run_daily_relative_value.__module__]}),indent=2))
    fc=make_forecasts(frames,pairs,c,same_day_exit=True);fc.to_csv(out/'forecasts.csv',index=False);summary=[]
    for multiple in [1,2]:
        model=RelativeValueConfig(**(c.model_dump()|dict(one_way_cost_bps=c.one_way_cost_bps*multiple,annual_borrow_rate=c.annual_borrow_rate*multiple)))
        for required in [False,True]:
            name=('daily_strongest_diagnostic' if required else 'cost_filtered')+f'_cost{multiple}'
            r=run_daily_relative_value(frames,dates,pairs,fc,model,args.start,args.end,require_daily_trade=required)
            folder=out/name;folder.mkdir();r.equity.to_csv(folder/'daily.csv');r.trades.to_csv(folder/'trades.csv',index=False);r.decisions.to_csv(folder/'decisions.csv',index=False)
            month=r.equity.groupby(r.equity.index.to_period('M')).agg(net_pnl=('net_pnl','sum'),trades=('trades','sum'),ending_equity=('equity','last'));month.to_csv(folder/'monthly.csv')
            summary.append(dict(scenario=name,**r.metrics));print(json.dumps(summary[-1]),flush=True)
    # Separate fixed next-open horizon, preserving the same cost/uncertainty gate.
    overnight_config=RelativeValueConfig(**(c.model_dump()|dict(hold_sessions=1)))
    overnight_fc=make_forecasts(frames,pairs,overnight_config)
    overnight_fc.to_csv(out/'overnight_forecasts.csv',index=False)
    for multiple in [1,2]:
        model=RelativeValueConfig(**(overnight_config.model_dump()|dict(one_way_cost_bps=c.one_way_cost_bps*multiple,annual_borrow_rate=c.annual_borrow_rate*multiple)))
        r=run_relative_value(frames,dates,pairs,overnight_fc,model,args.start,args.end)
        name=f'overnight_cost_filtered_cost{multiple}';folder=out/name;folder.mkdir()
        daily=r.equity.copy();daily['starting_equity']=daily.equity.shift(1,fill_value=c.initial_capital);daily['net_pnl']=daily.equity-daily.starting_equity;daily['daily_return']=daily.equity/daily.starting_equity-1
        daily['trades']=r.trades.groupby('entry_at').size().reindex(daily.index,fill_value=0)
        daily.to_csv(folder/'daily.csv');r.trades.to_csv(folder/'trades.csv',index=False);r.decisions.to_csv(folder/'decisions.csv',index=False)
        daily.groupby(daily.index.to_period('M')).agg(net_pnl=('net_pnl','sum'),trades=('trades','sum'),ending_equity=('equity','last')).to_csv(folder/'monthly.csv')
        summary.append(dict(scenario=name,initial_capital=c.initial_capital,final_equity=float(daily.equity.iloc[-1]),trading_sessions=len(daily),active_days=int((daily.trades>0).sum()),winning_days=int((daily.net_pnl>0).sum()),flat_days=int((daily.net_pnl==0).sum()),best_day=float(daily.daily_return.max()),worst_day=float(daily.daily_return.min()),gross_pnl=float(r.trades.gross_pnl.sum()),**r.metrics));print(json.dumps(summary[-1]),flush=True)
    pd.DataFrame(summary).to_csv(out/'summary.csv',index=False)
    rows=['# Year-to-date daily trading simulation','',f'{args.start} through {args.end}; $100,000 per scenario. Decisions after the previous session close; same-day variants open and close the next session; overnight variants exit at the following open. No orders submitted.','','| Scenario | Net P&L | Trades | Active days | Drawdown |','|---|---:|---:|---:|---:|']
    for r in summary:rows.append(f"| {r['scenario']} | ${r['net_pnl']:,.2f} | {r['trades']} | {r['active_days']} | {r['max_drawdown']:.2%} |")
    rows+=['','The daily-strongest diagnostic intentionally takes the highest-ranked available forecast even below the cost threshold. It is not a promoted strategy. The cost-filtered strategy may hold cash. Both use a prior-close forecast of next-session open-to-close hedged returns, retrained from matured labels; future returns are not inputs to selection.','','Scope: SPY/IVV, QQQ/QQQM and SOXX/SMH only. Gross allocation is at most 10% per pair, subject to the existing account cap. Base costs are 7 basis points per side and a minimum one-day short borrow charge at 3% annually; stress doubles both. No interest on cash.','','Adjusted historical prices and fractional quantities are research proxies. Quotes, locates, execution impact and corporate-action cashflows are not validated. Daily bars cannot verify intraday stops, maximum intraday drawdown or a 1% daily loss cap. All positions exit at the assumed close; the 5% account drawdown halt applies to later entries. No return target is guaranteed. The periods were already inspected in earlier research.','','Each scenario folder contains daily.csv, monthly.csv, trades.csv and decisions.csv. Zero-return days remain in the daily series.']
    rows+=['','Overnight comparison: separately trained one-session open-to-next-open forecast; same position caps and cost gate, actual calendar-day borrow charges. The existing overnight simulator starts generating orders after the first evaluation close and avoids opening on the final session. No historical winner is promoted automatically. Same-day minimum borrow is one day; overnight can include weekends and holidays.']
    (out/'REPORT.md').write_text('\n'.join(rows)+'\n')
if __name__=='__main__':main()
