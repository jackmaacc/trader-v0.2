"""Frozen BTC/ETH baseline using the shared engine; offline replay, no orders."""
from __future__ import annotations
import argparse, hashlib, json, shutil, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trader_engine.backtest.engine import BacktestEngine
from trader_engine.core.config import BacktestConfig, FeatureConfig, RiskConfig
from trader_engine.core.models import AssetClass, UniverseMember
from trader_engine.data.base import validate_bars
from trader_engine.risk.engine import RiskManager
SYMBOLS=('BTC-USD','ETH-USD')
CASES=(('zero',0.,0.),('base',25.,5.),('stress',25.,15.))
SOURCES=('scripts/run_crypto_baseline.py','src/trader_engine/backtest/engine.py','src/trader_engine/risk/engine.py','src/trader_engine/core/config.py','src/trader_engine/analytics/metrics.py','src/trader_engine/data/base.py','docs/CRYPTO_BASELINE_PROTOCOL.md')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,x):Path(p).write_text(json.dumps(x,indent=2,default=str,allow_nan=False)+'\n')
def read_bars(path):
    f=pd.read_parquet(path)
    if isinstance(f.columns,pd.MultiIndex):f.columns=f.columns.get_level_values(0)
    f.columns=f.columns.str.lower()
    f=f[['open','high','low','close','volume']].copy()
    f.index=pd.to_datetime(f.index,utc=True)
    validate_bars(f)
    if not f.index.is_monotonic_increasing:raise ValueError('Unsorted dates')
    if not f.index.equals(pd.date_range(f.index[0],f.index[-1],freq='D')):raise ValueError('Missing daily bars')
    return f

def signal_frame(f,benchmark=False):
    out=f.copy()
    out['signal']=np.where(out.close>out.close.rolling(200,min_periods=200).mean(),'long','flat')
    if benchmark:out['signal']='long'
    out['state']='crypto_sma200';out['signal_score']=1.
    return out

def configs(fee,impact):
    return (BacktestConfig(hold_bars=100000,exit_on_state_change=False,exit_on_signal_flip=True,
        use_atr_stop=False,trailing_stop=False,commission_bps=fee,slippage_bps_crypto=impact,annualization_factor=365),
        RiskConfig(max_position_pct=.125,max_gross_exposure=.25,max_concurrent_positions=2,max_daily_loss_pct=.03))

def reconcile(result,frames,fee,impact):
    """Independent cash/holdings reconstruction from retained filled orders."""
    curve=result.equity_curve; orders=result.orders
    filled=orders.loc[orders.status=='filled'].copy() if not orders.empty else orders
    cash=100000.;positions={s:0. for s in frames};fees=0.;impact_cost=0.;records=[]
    for t,row in curve.iterrows():
        todays=filled.loc[filled.timestamp==t] if not filled.empty else filled
        for _,o in todays.iterrows():
            entry=o.notes=='Entry filled.'
            if o.notes not in ('Entry filled.','Exit filled.'):raise AssertionError('Unknown fill')
            raw=float(frames[o.symbol].loc[t,'close' if o.reason=='end_of_test' else 'open'])
            qty=float(o.quantity);price=float(o.price)
            np.testing.assert_allclose(price,raw*(1+(impact/10000 if entry else -impact/10000)),rtol=0,atol=1e-7)
            commission=qty*price*fee/10000
            cash+=(-qty*price if entry else qty*price)-commission
            positions[o.symbol]+=qty if entry else -qty
            fees+=commission;impact_cost+=qty*abs(price-raw)
        exposure=sum(q*frames[s].loc[t,'close'] for s,q in positions.items())
        np.testing.assert_allclose([cash,cash+exposure],[row.cash,row.equity],rtol=0,atol=1e-6)
        if min(positions.values()) < -1e-9 or cash < -1e-6:raise AssertionError('Short position or negative cash')
        records.append(dict(timestamp=t,cash=cash,exposure=exposure,equity=cash+exposure,fees=fees,impact=impact_cost,
            gross_same_fills=cash+exposure-100000+fees+impact_cost))
    np.testing.assert_allclose(sum(result.trades.pnl) if not result.trades.empty else 0.,curve.equity.iloc[-1]-100000,atol=1e-6,rtol=0)
    np.testing.assert_allclose(list(positions.values()),0.,atol=1e-9,rtol=0)
    return pd.DataFrame(records)

def freeze(inputs,output):
    inputs=Path(inputs).resolve();output=Path(output).resolve()
    if output.exists():raise FileExistsError(output)
    if shutil.disk_usage(output.parent).free<1024**3:raise RuntimeError('Need1GiB headroom')
    frames={s:read_bars(inputs/(s+'.parquet')) for s in SYMBOLS}
    if not frames[SYMBOLS[0]].index.equals(frames[SYMBOLS[1]].index):raise ValueError('Unequal calendars')
    end=frames[SYMBOLS[0]].index[-1].strftime('%Y-%m-%d')
    if end>'2026-09-22':raise ValueError('Incomplete or future bar')
    if frames[SYMBOLS[0]].index[0]!=pd.Timestamp('2023-01-01',tz='UTC'):raise ValueError('Expected warmup starts2023-01-01')
    output.mkdir();(output/'inputs').mkdir()
    for s in SYMBOLS:shutil.copy2(inputs/(s+'.parquet'),output/'inputs'/(s+'.parquet'))
    protocol=dict(frozen_at_utc=datetime.now(timezone.utc).isoformat(),initial_cash=100000,universe=SYMBOLS,
        candidate='CRYPTO_SMA200',windows=[['development','2023-07-20','2024-12-31'],['reserved_historical_diagnostic','2025-01-01',end]],
        cases=CASES,models=['CRYPTO_SMA200','BH25'],expected_replays=12,seed=None,
        provenance='Public Yahoo Finance daily OHLCV via yfinance; refreshed2026-09-23, no broker data or credentials. UTC labels assumed. Not execution-venue-specific.',
        qualification='Historical diagnostic only; earlier crypto data/model research existed, reserved split is not untouched confirmation.',
        input_hashes={s+'.parquet':sha(output/'inputs'/(s+'.parquet')) for s in SYMBOLS},
        source_hashes={p:sha(ROOT/p) for p in SOURCES},fee_source='https://docs.alpaca.markets/us/docs/crypto-trading',
        fee_caveat='Flat25bps taker fee conservatively ignores volume tiers; USD fee approximation instead of actual buy-side crypto deduction.',
        risk=configs(25,5)[1].model_dump(),execution=configs(25,5)[0].model_dump(),
        benchmark='Constant long signals; same initial cash, dates, engine sizing, terminal liquidation and costs. Allocation approximately12.5% per asset at entry; no rebalance. Exposure drift allowed.',
        business_overhead_known=False,promotion_authorized=False,annualization=365)
    write(output/'protocol.json',protocol)
    for p in SOURCES:
        dest=output/'source'/p;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/p,dest)
    print('FROZEN',output,flush=True)

def run(output):
    output=Path(output).resolve();protocol=json.loads((output/'protocol.json').read_text())
    if (output/'summary.csv').exists():raise FileExistsError('Preserve completed run')
    for p,h in protocol['source_hashes'].items():
        if sha(ROOT/p)!=h:raise ValueError('Frozen source changed:'+p)
    for p,h in protocol['input_hashes'].items():
        if sha(output/'inputs'/p)!=h:raise ValueError('Input hash mismatch')
    frames={s:read_bars(output/'inputs'/(s+'.parquet')) for s in SYMBOLS};rows=[]
    for window,start,end in protocol['windows']:
      for case,fee,impact in CASES:
       for model in protocol['models']:
        if shutil.disk_usage(output).free<512*1024**2:raise RuntimeError('Disk headroom below512MiB')
        prepared={s:signal_frame(f,model=='BH25').loc[start:end] for s,f in frames.items()}
        config,risk=configs(fee,impact)
        result=BacktestEngine(config,RiskManager(risk),FeatureConfig()).run(prepared,{s:UniverseMember(s,AssetClass.CRYPTO) for s in SYMBOLS},100000)
        ledger=reconcile(result,prepared,fee,impact)
        if len(ledger)!=len(pd.date_range(start,end,freq='D')):raise AssertionError('Missing sessions')
        dest=output/f'{window}_{model}_{case}';dest.mkdir()
        result.equity_curve.to_csv(dest/'daily.csv');result.orders.to_csv(dest/'orders.csv',index=False)
        result.trades.to_csv(dest/'trades.csv',index=False);ledger.to_csv(dest/'reconciliation.csv',index=False)
        equity=ledger.equity.to_numpy();peak=np.maximum.accumulate(np.r_[100000.,equity])[1:]
        row=dict(window=window,model=model,case=case,start=start,end=end,sessions=len(ledger),
            final_equity=equity[-1],net_pnl=equity[-1]-100000,net_return=equity[-1]/100000-1,
            gross_same_fills=float(ledger.gross_same_fills.iloc[-1]),fees=float(ledger.fees.iloc[-1]),
            modeled_impact=float(ledger.impact.iloc[-1]),max_drawdown=float(np.max(1-equity/peak)),
            average_exposure=float(np.mean(ledger.exposure/equity)),max_exposure=float(np.max(ledger.exposure/equity)),
            completed_trades=len(result.trades),filled_orders=int((result.orders.status=='filled').sum()) if not result.orders.empty else 0,
            identity_checks_passed=True)
        write(dest/'metrics.json',row);rows.append(row);print(len(rows),window,model,case,flush=True)
    for p,h in protocol['source_hashes'].items():
        if sha(ROOT/p)!=h:raise ValueError('Source changed during replay')
    pd.DataFrame(rows).to_csv(output/'summary.csv',index=False)
    hashes={str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file()}
    write(output/'evidence_manifest.json',dict(completed=len(rows),expected=12,files=hashes,bytes_before_manifest=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['freeze','run']);p.add_argument('--inputs',type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    if a.action=='freeze':freeze(a.inputs,a.output)
    else:run(a.output)
